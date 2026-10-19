import logging
import re
from collections.abc import MutableMapping
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider, LogRecordProcessor, ReadWriteLogRecord
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

#: A shared haul's link is its own key (`/t/{token}` on edge, `/v1/tracking/{token}` on loads): whoever reads it can
#: follow the haul, so the token is cut out of every log line, the access log and outgoing calls included.
SECRET_IN_PATH = re.compile(r"(/t/|/tracking/)[^/?#\s\"]+")


FILTERED = "[Filtered]"

#: Paths a load balancer or a health check hits every few seconds: neither a span nor a duration sample is worth it.
QUIET_PATHS = frozenset({"/health", "/healthz", "/ready"})
#: How often metrics leave for the collector; an alert looks at a few minutes, so half a minute is fine-grained enough.
METRIC_EXPORT_MILLIS = 30_000

#: Phone numbers in any text an error report carries: messages, stack-frame locals (reprs), URLs (`+` sent as %2B).
PHONE = re.compile(
    r"(?<![\w+%])(?:\+|%2[Bb])?998[\s-]?\(?\d{2}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}(?!\d)|(?<![\w+])\+\d{10,14}(?!\d)"
)
#: A location written into text: a precise "41.311081, 69.240562" pair, or a `lat=41.31` style field in a repr.
COORDINATE_PAIR = re.compile(r"-?\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}")
COORDINATE_FIELD = re.compile(r"(?i)\b((?:\w+_)?(?:lat|lng|lon|latitude|longitude)['\"]?\s*[:=]\s*)-?\d+(?:\.\d+)?")

#: Field names holding personal data, matched per word of the name (`contact_phone`, `fromLat`, `pickup_address`).
PII_KEY_WORDS = frozenset(
    {
        "phone",
        "otp",
        "lat",
        "lng",
        "lon",
        "latitude",
        "longitude",
        "location",
        "coordinates",
        "address",
        "plate",
        "plates",
        "passport",
        "waypoint",
    }
)
#: Names too generic to match per word, so only the whole name counts: `code` is the sign-in code, `status_code` is not.
PII_KEYS = frozenset({"code", "point"})
_KEY_WORD = re.compile(r"[-_.\s]+|(?<=[a-z0-9])(?=[A-Z])")


def scrub(value: object) -> object:
    if value is None or isinstance(value, (int, float)):
        return value
    text = str(value)
    redacted = SECRET_IN_PATH.sub(r"\1***", text)
    return value if redacted == text else redacted


def scrub_text(text: str) -> str:
    text = SECRET_IN_PATH.sub(r"\1***", text)
    text = PHONE.sub(FILTERED, text)
    text = COORDINATE_PAIR.sub(FILTERED, text)
    return COORDINATE_FIELD.sub(rf"\1{FILTERED}", text)


def is_pii_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    if key.lower() in PII_KEYS:
        return True
    return any(word.lower() in PII_KEY_WORDS for word in _KEY_WORD.split(key) if word)


def scrub_event(value: Any, depth: int = 0) -> Any:
    if depth > 32:
        return value
    if isinstance(value, dict):
        return {
            key: FILTERED if is_pii_key(key) and item not in (None, "") else scrub_event(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub_event(item, depth + 1) for item in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


def before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any]:
    ctx = trace.get_current_span().get_span_context()
    tags = event.setdefault("tags", {})
    if ctx.is_valid and isinstance(tags, dict):
        tags["trace_id"] = format(ctx.trace_id, "032x")
    return scrub_event(event)


class ScrubSpanProcessor(SpanProcessor):
    """FastAPI puts the raw path and query on the request span; a tracking token, a phone number or a coordinate in
    them is cut out as the span starts, before any exporter sees it."""

    def on_start(self, span: Span, parent_context: Any = None) -> None:
        for key in ("url.path", "url.query", "url.full"):
            value = (span.attributes or {}).get(key)
            if isinstance(value, str) and (clean := scrub_text(value)) != value:
                span.set_attribute(key, clean)


class ScrubLogProcessor(LogRecordProcessor):
    """Every log record bound for the collector, the stdlib's and FastAPI's own exception records alike: the same
    scrubbing an error report gets, on the body and the attributes."""

    def on_emit(self, log_record: ReadWriteLogRecord) -> None:
        record = log_record.log_record
        if isinstance(record.body, str):
            record.body = scrub_text(record.body)
        attributes = record.attributes
        if not attributes:
            return
        for key in list(attributes):
            value = attributes[key]
            if is_pii_key(key) and value not in (None, ""):
                attributes[key] = FILTERED
            elif isinstance(value, str) and (clean := scrub_text(value)) != value:
                attributes[key] = clean

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


class LocalOnlyFilter(logging.Filter):
    """Records that stay in the container's log: the exporters' own failures (sending those through the exporter
    that failed would loop) and anything marked `local_only`, such as the console SMS text with its code."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith("opentelemetry") and not getattr(record, "local_only", False)


def fastapi_telemetry() -> dict[str, Any]:
    """`FastAPI(telemetry=...)` for every service. FastAPI (≥0.143) records request spans named by route template,
    `http.server.request.duration`, `http.server.active_requests` and unhandled exceptions on the global providers
    `init_telemetry` sets; it adds no exporter of its own, so nothing is sent twice."""
    return {"auto_configure": False, "exclude": quiet}


def quiet(scope: MutableMapping[str, Any]) -> bool:
    return scope.get("path") in QUIET_PATHS


class RedactFilter(logging.Filter):
    """Scrubs the message and each argument in place: uvicorn's access formatter unpacks `args` by position, so
    they stay a tuple of the same length; httpx passes the URL as an object, so an argument is read as text."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(scrub(arg) for arg in record.args)
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

    resource = Resource.create({"service.name": service_name, "deployment.environment.name": env})
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(ScrubSpanProcessor())
    readers = []
    if otlp_endpoint:
        base = otlp_endpoint.rstrip("/")
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base}/v1/traces")))
        readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{base}/v1/metrics"), export_interval_millis=METRIC_EXPORT_MILLIS
            )
        )
        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(ScrubLogProcessor())
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{base}/v1/logs")))
        set_logger_provider(logger_provider)
        root = logging.getLogger()
        for old in [h for h in root.handlers if isinstance(h, LoggingHandler)]:
            root.removeHandler(old)
        handler = LoggingHandler(level=logging.getLevelName(log_level.upper()), logger_provider=logger_provider)
        handler.addFilter(LocalOnlyFilter())
        handler.addFilter(RedactFilter())
        root.addHandler(handler)
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))

    if glitchtip_dsn:
        try:
            import sentry_sdk

            sentry_sdk.init(
                dsn=glitchtip_dsn,
                environment=env,
                server_name=service_name,
                send_default_pii=False,
                max_request_body_size="never",
                before_send=before_send,
            )
        except Exception:
            logger.exception("GlitchTip init failed; continuing without error tracking")
