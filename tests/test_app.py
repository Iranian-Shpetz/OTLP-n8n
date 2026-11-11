import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from intermediate.app import Settings, create_app, extract_workflow_events

AUTH_TOKEN = "test-token-" + ("a" * 32)
SETTINGS = Settings(
    auth_token=AUTH_TOKEN,
    ntfy_topic="test-topic",
)


def otlp_payload() -> dict:
    return {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "name": "workflow.execute",
                                "attributes": [
                                    {
                                        "key": "n8n.workflow.name",
                                        "value": {"stringValue": "nightly backup"},
                                    },
                                    {
                                        "key": "n8n.execution.status",
                                        "value": {"stringValue": "error"},
                                    },
                                    {
                                        "key": "n8n.execution.error_type",
                                        "value": {"stringValue": "WorkflowCrashedError"},
                                    },
                                ],
                            },
                            {"name": "node.execute", "attributes": []},
                        ]
                    }
                ]
            }
        ]
    }


class SettingsTests(unittest.TestCase):
    def test_rejects_short_auth_token(self) -> None:
        with self.assertRaises(ValueError):
            Settings(auth_token="short", ntfy_topic="valid-topic")

    def test_rejects_invalid_ntfy_topic(self) -> None:
        with self.assertRaises(ValueError):
            Settings(auth_token=AUTH_TOKEN, ntfy_topic="../unsafe")


class TraceHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(create_app(SETTINGS))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_healthcheck(self) -> None:
        response = self.client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    @patch("intermediate.app.httpx.AsyncClient.post", new_callable=AsyncMock)
    def test_authenticates_and_notifies_only_workflow_spans(self, post: AsyncMock) -> None:
        post.return_value = httpx.Response(
            200,
            request=httpx.Request("POST", "https://ntfy.sh/test-topic"),
        )

        response = self.client.post(
            "/v1/traces",
            json=otlp_payload(),
            headers={"Authorization": f"Bearer {AUTH_TOKEN}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {})
        post.assert_awaited_once()
        self.assertEqual(
            post.await_args.kwargs["content"],
            b"Workflow: nightly backup\nStatus: error\nError type: WorkflowCrashedError",
        )

    @patch("intermediate.app.httpx.AsyncClient.post", new_callable=AsyncMock)
    def test_rejects_invalid_token_without_forwarding(self, post: AsyncMock) -> None:
        response = self.client.post(
            "/v1/traces",
            json=otlp_payload(),
            headers={"Authorization": "Bearer wrong"},
        )

        self.assertEqual(response.status_code, 401)
        post.assert_not_awaited()

    @patch("intermediate.app.httpx.AsyncClient.post", new_callable=AsyncMock)
    def test_returns_otlp_success_for_batches_without_workflow_spans(
        self, post: AsyncMock
    ) -> None:
        response = self.client.post(
            "/v1/traces",
            json={"resourceSpans": []},
            headers={"Authorization": f"Bearer {AUTH_TOKEN}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {})
        post.assert_not_awaited()

    def test_rejects_malformed_otlp_payload(self) -> None:
        response = self.client.post(
            "/v1/traces",
            json={"resourceSpans": "invalid"},
            headers={"Authorization": f"Bearer {AUTH_TOKEN}"},
        )

        self.assertEqual(response.status_code, 400)

    def test_rejects_non_json_content_type(self) -> None:
        response = self.client.post(
            "/v1/traces",
            content=b"{}",
            headers={
                "Authorization": f"Bearer {AUTH_TOKEN}",
                "Content-Type": "application/x-protobuf",
            },
        )

        self.assertEqual(response.status_code, 415)


if __name__ == "__main__":
    unittest.main()
