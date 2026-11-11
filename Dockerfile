FROM otel/opentelemetry-collector-builder:0.165.0 AS builder

WORKDIR /build
COPY builder-config.yaml .
RUN /usr/local/bin/ocb --config=builder-config.yaml

FROM gcr.io/distroless/static-debian12:nonroot

COPY --from=builder /build/dist/otelcol-n8n-ntfy /otelcol
COPY otel-collector-config.yaml /etc/otelcol/config.yaml

EXPOSE 4318
ENTRYPOINT ["/otelcol", "--config=/etc/otelcol/config.yaml"]
