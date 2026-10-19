"""The event pipeline's own measures, on the global meter `init_telemetry` sets; until it runs they go nowhere.

Outbox: what was published and how (`path`: the commit's fast path or the relay), what failed and will be retried, what
gave up (`dead`), and how many rows wait (`goway.outbox.backlog`, read by the relay every few ticks, never per scrape).
Consumer: each delivery's outcome (`ok`, `retry`, `dead_lettered`), how long the handler took, how long the event sat
in the stream before it (`goway.consumer.lag`), and how many messages each durable has not taken yet
(`goway.consumer.pending`, from JetStream). SMS: each send's outcome per provider.
"""

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

meter = metrics.get_meter("goway.events")

#: Last counts the relay read, by status; the backlog gauge reports them as they are.
OUTBOX_BACKLOG: dict[str, int] = {}
#: Messages each durable consumer has not been delivered yet, as JetStream last reported.
CONSUMER_PENDING: dict[str, int] = {}


def _backlog(options: CallbackOptions) -> list[Observation]:
    return [Observation(count, {"status": status}) for status, count in OUTBOX_BACKLOG.items()]


def _pending(options: CallbackOptions) -> list[Observation]:
    return [Observation(count, {"durable": durable}) for durable, count in CONSUMER_PENDING.items()]


outbox_published = meter.create_counter("goway.outbox.published", unit="{event}", description="Events published")
outbox_failed = meter.create_counter(
    "goway.outbox.failed", unit="{event}", description="Publish attempts that failed and will be retried"
)
outbox_dead = meter.create_counter(
    "goway.outbox.dead", unit="{event}", description="Events the relay gave up on after every attempt"
)
meter.create_observable_gauge(
    "goway.outbox.backlog", callbacks=[_backlog], unit="{event}", description="Outbox rows by status, pending and dead"
)
consumer_processed = meter.create_counter(
    "goway.consumer.processed", unit="{message}", description="Deliveries handled, by outcome"
)
consumer_duration = meter.create_histogram(
    "goway.consumer.duration", unit="s", description="Time a handler took for one delivery"
)
consumer_lag = meter.create_histogram(
    "goway.consumer.lag", unit="s", description="Time an event waited in the stream before this delivery"
)
meter.create_observable_gauge(
    "goway.consumer.pending",
    callbacks=[_pending],
    unit="{message}",
    description="Messages a durable consumer has not been delivered yet",
)
sms_sent = meter.create_counter("goway.sms.sent", unit="{message}", description="SMS sends, by provider and outcome")
