import logging

from goway_lib.telemetry import RedactFilter, init_telemetry

TOKEN = "K7Q2M9XPLR4T8VWZ3HJN6CDF5BGY2SAE"


def http_request(url: str) -> logging.LogRecord:
    args = ("GET", url, "HTTP/1.1 200 OK")
    return logging.LogRecord("httpx", logging.INFO, __file__, 1, 'HTTP Request: %s %s "%s"', args, None)


def test_the_access_log_never_holds_a_tracking_token(caplog):
    init_telemetry(None, service_name="test", env="test")
    with caplog.at_level(logging.INFO):
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d', "84.54.82.215:0", "GET", f"/t/{TOKEN}", "1.1", 200
        )
    assert TOKEN not in caplog.text
    assert '"GET /t/*** HTTP/1.1" 200' in caplog.text


def test_a_call_to_loads_is_cut_the_same_way():
    record = http_request(f"http://goway-loads:8000/v1/tracking/{TOKEN}?lang=uz")
    RedactFilter().filter(record)
    assert record.getMessage() == 'HTTP Request: GET http://goway-loads:8000/v1/tracking/***?lang=uz "HTTP/1.1 200 OK"'


def test_other_paths_are_left_alone():
    record = http_request("http://goway-loads:8000/v1/bookings/0199f3a2-7c1e-7d4b-9a10-3f2e5c6b7a81/eta")
    RedactFilter().filter(record)
    assert record.args is not None
    assert "/v1/bookings/0199f3a2-7c1e-7d4b-9a10-3f2e5c6b7a81/eta" in record.getMessage()
