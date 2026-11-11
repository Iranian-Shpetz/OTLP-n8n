import hmac
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request, Response

logger = logging.getLogger("ntfy_adapter")
MAX_BODY_BYTES = 4 * 1024 * 1024
TOPIC_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class Settings:
    auth_token: str
    ntfy_topic: str
    ntfy_url: str = "https://ntfy.sh"
    ntfy_token: str | None = None

    def __post_init__(self) -> None:
        if len(self.auth_token) < 32 or any(char.isspace() for char in self.auth_token):
            raise ValueError("OTLP_AUTH_TOKEN must be at least 32 non-whitespace characters")
        if not TOPIC_PATTERN.fullmatch(self.ntfy_topic):
            raise ValueError("NTFY_TOPIC must contain 1-64 letters, digits, underscores, or hyphens")

        parsed_url = urlsplit(self.ntfy_url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.netloc
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError("NTFY_URL must be an http(s) URL without credentials, query, or fragment")

        if self.ntfy_token is not None and any(char in self.ntfy_token for char in "\r\n"):
            raise ValueError("NTFY_TOKEN must not contain line breaks")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            auth_token=os.environ.get("OTLP_AUTH_TOKEN", ""),
            ntfy_topic=os.environ.get("NTFY_TOPIC", ""),
            ntfy_url=os.environ.get("NTFY_URL", "https://ntfy.sh"),
            ntfy_token=os.environ.get("NTFY_TOKEN") or None,
        )


@dataclass(frozen=True)
class WorkflowEvent:
    name: str
    status: str
    error_type: str | None


def _attribute_value(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None

    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            return str(value[key]).lower() if isinstance(value[key], bool) else str(value[key])
    return None


def _span_attributes(span: dict[str, Any]) -> dict[str, str]:
    attributes = span.get("attributes", [])
    if not isinstance(attributes, list):
        raise ValueError("span attributes must be a list")

    result: dict[str, str] = {}
    for attribute in attributes:
        if not isinstance(attribute, dict) or not isinstance(attribute.get("key"), str):
            raise ValueError("span attribute must have a string key")
        value = _attribute_value(attribute.get("value"))
        if value is not None:
            result[attribute["key"]] = value
    return result


def extract_workflow_events(payload: Any) -> list[WorkflowEvent]:
    if not isinstance(payload, dict):
        raise ValueError("OTLP request must be a JSON object")

    resource_spans = payload.get("resourceSpans", [])
    if not isinstance(resource_spans, list):
        raise ValueError("resourceSpans must be a list")

    events: list[WorkflowEvent] = []
    for resource_span in resource_spans:
        if not isinstance(resource_span, dict):
            raise ValueError("resourceSpans entries must be objects")
        scope_spans = resource_span.get("scopeSpans", [])
        if not isinstance(scope_spans, list):
            raise ValueError("scopeSpans must be a list")

        for scope_span in scope_spans:
            if not isinstance(scope_span, dict):
                raise ValueError("scopeSpans entries must be objects")
            spans = scope_span.get("spans", [])
            if not isinstance(spans, list):
                raise ValueError("spans must be a list")

            for span in spans:
                if not isinstance(span, dict):
                    raise ValueError("span entries must be objects")
                if span.get("name") != "workflow.execute":
                    continue

                attributes = _span_attributes(span)
                name = attributes.get("n8n.workflow.name", "unnamed workflow")
                status = attributes.get("n8n.execution.status", "unknown")
                error_type = attributes.get("n8n.execution.error_type")
                events.append(
                    WorkflowEvent(
                        name=" ".join(name.split())[:120],
                        status=" ".join(status.split())[:40],
                        error_type=" ".join(error_type.split())[:120] if error_type else None,
                    )
                )
    return events


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings if settings is not None else Settings.from_env()
        yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/traces")
    async def receive_traces(request: Request) -> Response:
        current_settings: Settings = request.app.state.settings
        authorization = request.headers.get("authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if (
            not separator
            or scheme.lower() != "bearer"
            or not hmac.compare_digest(token, current_settings.auth_token)
        ):
            raise HTTPException(status_code=401, detail="Invalid bearer token")

        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise HTTPException(status_code=415, detail="Expected application/json")

        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_BODY_BYTES:
                    raise HTTPException(status_code=413, detail="Request body is too large")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc

        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Request body is too large")

        try:
            payload = json.loads(body)
            events = extract_workflow_events(payload)
        except UnicodeDecodeError as exc:
            logger.warning("Rejected OTLP trace payload with invalid UTF-8 at byte %d", exc.start)
            raise HTTPException(status_code=400, detail="Invalid OTLP trace payload") from exc
        except json.JSONDecodeError as exc:
            logger.warning(
                "Rejected invalid OTLP JSON at line %d, column %d: %s",
                exc.lineno,
                exc.colno,
                exc.msg,
            )
            raise HTTPException(status_code=400, detail="Invalid OTLP trace payload") from exc
        except ValueError as exc:
            logger.warning("Rejected invalid OTLP trace structure: %s", exc)
            raise HTTPException(status_code=400, detail="Invalid OTLP trace payload") from exc

        if not events:
            return Response(content="{}", media_type="application/json")

        headers = {"Title": "n8n workflow", "Tags": "opentelemetry"}
        if current_settings.ntfy_token:
            headers["Authorization"] = f"Bearer {current_settings.ntfy_token}"
        ntfy_endpoint = f"{current_settings.ntfy_url.rstrip('/')}/{current_settings.ntfy_topic}"

        async with httpx.AsyncClient(timeout=10.0) as client:
            for event in events:
                lines = [f"Workflow: {event.name}", f"Status: {event.status}"]
                if event.error_type:
                    lines.append(f"Error type: {event.error_type}")
                try:
                    response = await client.post(
                        ntfy_endpoint,
                        content="\n".join(lines).encode("utf-8"),
                        headers=headers,
                    )
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    logger.error("ntfy delivery failed (%s)", type(exc).__name__)
                    raise HTTPException(
                        status_code=502,
                        detail="Failed to deliver notification",
                    ) from exc

        return Response(content="{}", media_type="application/json")

    return app


app = create_app()
