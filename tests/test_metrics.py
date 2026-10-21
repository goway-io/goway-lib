"""Metrics that mean something: histograms in seconds, and one series per process."""

import json
import subprocess
import sys

PROBE = """
import json

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from goway_lib import metrics as goway

reader = InMemoryMetricReader()
metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
goway.consumer_lag.record(0.04, {"durable": "probe"})
goway.consumer_duration.record(0.3, {"durable": "probe"})
found = {}
for resource in reader.get_metrics_data().resource_metrics:
    for scope in resource.scope_metrics:
        for metric in scope.metrics:
            point = metric.data.data_points[0]
            found[metric.name] = {"bounds": list(point.explicit_bounds), "counts": list(point.bucket_counts)}
print(json.dumps(found))
"""


def measured() -> dict:
    """In a fresh process: the instruments are made before any meter provider is set, as at a service's import."""
    run = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, check=True)
    return json.loads(run.stdout)


def test_a_delivery_under_a_second_lands_in_its_own_bucket_not_the_first_five_seconds():
    found = measured()
    assert set(found) == {"goway.consumer.lag", "goway.consumer.duration"}
    for histogram in found.values():
        assert histogram["bounds"][:4] == [0.005, 0.01, 0.025, 0.05]
        assert histogram["bounds"][-1] == 120
    lag = found["goway.consumer.lag"]
    assert lag["counts"].index(1) == 3, "0.04 s falls between 0.025 and 0.05"
    duration = found["goway.consumer.duration"]
    assert duration["counts"].index(1) == 6, "0.3 s falls between 0.25 and 0.5"


INSTANCE = """
import json, os, socket

from opentelemetry import trace

from goway_lib.telemetry import init_telemetry

init_telemetry(service_name="probe", env="test")
attributes = dict(trace.get_tracer_provider().resource.attributes)
print(json.dumps({"id": attributes.get("service.instance.id"), "expected": f"{socket.gethostname()}-{os.getpid()}"}))
"""


def test_each_worker_process_reports_as_its_own_instance():
    """Two uvicorn workers of one service must not write one series: the instance names the process."""
    first, second = (
        json.loads(subprocess.run([sys.executable, "-c", INSTANCE], capture_output=True, text=True, check=True).stdout)
        for _ in range(2)
    )
    assert first["id"] == first["expected"] and second["id"] == second["expected"]
    assert first["id"] != second["id"]
