import logging

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)


class TraceIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        ctx = trace.get_current_span().get_span_context()
        record.trace_id = format(ctx.trace_id, "032x") if ctx.is_valid else "-"
        return True


def init_telemetry(
    app: FastAPI | None,
    *,
    service_name: str,
    env: str,
    otlp_endpoint: str = "",
    glitchtip_dsn: str = "",
    log_level: str = "INFO",
) -> None:
    logging.basicConfig(
        level=log_level.upper(),
        format="%(asctime)s %(levelname)s [%(name)s] trace=%(trace_id)s %(message)s",
    )
    logging.getLogger().addFilter(TraceIdFilter())
    for handler in logging.getLogger().handlers:
        handler.addFilter(TraceIdFilter())

    resource = Resource.create({"service.name": service_name, "deployment.environment": env})
    provider = TracerProvider(resource=resource)
    if otlp_endpoint:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint)))
    trace.set_tracer_provider(provider)
    if app is not None:
        FastAPIInstrumentor.instrument_app(app)

    if glitchtip_dsn:
        try:
            import sentry_sdk

            sentry_sdk.init(dsn=glitchtip_dsn, environment=env, server_name=service_name)
        except Exception:
            logger.exception("GlitchTip init failed; continuing without error tracking")
