import logging
import re

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

#: A shared haul's link is its own key (`/t/{token}` on edge, `/v1/tracking/{token}` on loads): whoever reads it can
#: follow the haul, so the token is cut out of every log line, the access log and outgoing calls included.
SECRET_IN_PATH = re.compile(r"(/t/|/tracking/)[^/?#\s\"]+")


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = SECRET_IN_PATH.sub(r"\1***", message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


class TraceIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        ctx = trace.get_current_span().get_span_context()
        record.trace_id = format(ctx.trace_id, "032x") if ctx.is_valid else "-"
        return True


def redact(target: logging.Logger | logging.Handler) -> None:
    """uvicorn's access logger writes through its own handler, never the root's, so it gets the filter too."""
    if not any(isinstance(f, RedactFilter) for f in target.filters):
        target.addFilter(RedactFilter())


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
        redact(handler)
    redact(logging.getLogger("uvicorn.access"))

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
