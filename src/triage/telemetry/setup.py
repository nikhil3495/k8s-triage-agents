"""Install OpenTelemetry tracer/meter providers.

ADK's built-in spans use the global tracer provider, so installing ours here
routes ADK spans *and* our plugin spans to the same exporter:
- ``gcp``: Cloud Trace (``opentelemetry-exporter-gcp-trace``), plus Cloud Monitoring
  metrics when ``ENABLE_CLOUD_MONITORING=true``.
- ``otlp``: OTLP/HTTP (Jaeger in docker-compose).
- ``console``: stdout, for local debugging.
"""

from __future__ import annotations

import logging

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SpanExporter

from triage import __version__
from triage.config import Settings, TraceExporter

log = logging.getLogger(__name__)
_installed = False


def _span_exporter(settings: Settings) -> SpanExporter | None:
    match settings.trace_exporter:
        case TraceExporter.GCP:
            from opentelemetry.exporter.cloud_trace import (
                CloudTraceSpanExporter,
            )

            return CloudTraceSpanExporter(project_id=settings.gcp_project)  # type: ignore[no-untyped-call]
        case TraceExporter.OTLP:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            return OTLPSpanExporter(endpoint=f"{settings.otlp_endpoint.rstrip('/')}/v1/traces")
        case TraceExporter.CONSOLE:
            return ConsoleSpanExporter()
    return None


def setup_telemetry(
    settings: Settings, extra_processors: list[SpanProcessor] | None = None
) -> None:
    """Install global providers once per process."""
    global _installed
    if _installed:
        return
    resource = Resource.create(
        {"service.name": "k8s-triage-agents", "service.version": __version__}
    )
    tp = TracerProvider(resource=resource)
    exporter = _span_exporter(settings)
    if exporter is not None:
        tp.add_span_processor(BatchSpanProcessor(exporter))
    for p in extra_processors or []:
        tp.add_span_processor(p)
    trace.set_tracer_provider(tp)

    readers: list[MetricReader] = []
    if settings.enable_cloud_monitoring:
        from opentelemetry.exporter.cloud_monitoring import (
            CloudMonitoringMetricsExporter,
        )

        readers.append(
            PeriodicExportingMetricReader(
                CloudMonitoringMetricsExporter(project_id=settings.gcp_project),
                export_interval_millis=60_000,
            )
        )
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))
    _installed = True
    log.info(
        "telemetry installed",
        extra={"exporter": str(settings.trace_exporter), "metrics": bool(readers)},
    )
