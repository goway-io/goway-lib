import logging

import httpx
import sentry_sdk
from opentelemetry.sdk.trace import TracerProvider

from goway_lib.telemetry import FILTERED, RedactFilter, before_send, init_telemetry, scrub_event, scrub_text

TOKEN = "K7Q2M9XPLR4T8VWZ3HJN6CDF5BGY2SAE"


def http_request(url: str | httpx.URL) -> logging.LogRecord:
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


def test_the_access_line_keeps_the_shape_uvicorn_formats():
    args = ("84.54.82.215:0", "GET", f"/t/{TOKEN}", "1.1", 200)
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', args, None)
    RedactFilter().filter(record)
    assert record.args == ("84.54.82.215:0", "GET", "/t/***", "1.1", 200)


def test_a_call_to_loads_is_cut_the_same_way():
    record = http_request(httpx.URL(f"http://goway-loads:8000/v1/tracking/{TOKEN}?lang=uz"))
    RedactFilter().filter(record)
    assert record.getMessage() == 'HTTP Request: GET http://goway-loads:8000/v1/tracking/***?lang=uz "HTTP/1.1 200 OK"'


def test_other_paths_are_left_alone():
    url = httpx.URL("http://goway-loads:8000/v1/bookings/0199f3a2-7c1e-7d4b-9a10-3f2e5c6b7a81/eta")
    record = http_request(url)
    RedactFilter().filter(record)
    assert record.args[1] is url
    assert "/v1/bookings/0199f3a2-7c1e-7d4b-9a10-3f2e5c6b7a81/eta" in record.getMessage()


def test_an_error_report_loses_phone_numbers_wherever_they_sit():
    event = {
        "message": "no driver for +998 90 123-45-67",
        "request": {"url": "http://goway-auth:8000/v1/otp", "query_string": "phone=%2B998901234567&lang=uz"},
        "exception": {
            "values": [
                {
                    "value": "PhoneLogin(phone='+998901234567') rejected",
                    "stacktrace": {"frames": [{"vars": {"login": "PhoneLogin(phone='+998901234567')"}}]},
                }
            ]
        },
        "breadcrumbs": {"values": [{"message": "sms to +79161234567 failed"}]},
    }
    text = str(scrub_event(event))
    assert "998" not in text and "7916" not in text
    assert text.count(FILTERED) >= 5


def test_personal_fields_are_replaced_by_name_and_the_rest_kept():
    event = {
        "extra": {
            "phone": "+998901234567",
            "contact_phone": "+998901234567",
            "fromLat": 41.31,
            "to_lng": 69.24,
            "pickup_address": "Chilonzor 9",
            "plate_number": "01A123BC",
            "code": "4821",
            "status_code": 409,
            "load_code": "L-1042",
            "platform": "android",
            "empty_phone": None,
        }
    }
    extra = scrub_event(event)["extra"]
    for key in ("phone", "contact_phone", "fromLat", "to_lng", "pickup_address", "plate_number", "code"):
        assert extra[key] == FILTERED, key
    assert extra["status_code"] == 409
    assert extra["load_code"] == "L-1042"
    assert extra["platform"] == "android"
    assert extra["empty_phone"] is None


def test_coordinates_written_into_text_are_cut():
    message = scrub_text("Point(lat=41.311081, lng=69.240562) near 41.311081, 69.240562 for 3 tons")
    assert "41.31" not in message and "69.24" not in message
    assert "3 tons" in message


def test_a_tracking_token_in_a_reported_url_is_cut():
    assert scrub_text(f"GET /v1/tracking/{TOKEN}") == "GET /v1/tracking/***"


def test_ids_and_amounts_survive():
    text = "booking 0199f3a2-7c1e-7d4b-9a10-3f2e5c6b7a81 trace 4bf92f3577b34da6a3ce929d0e0e4736 amount 1500000"
    assert scrub_text(text) == text


def test_a_report_inside_a_request_carries_its_trace_id():
    provider = TracerProvider()
    with provider.get_tracer("test").start_as_current_span("request") as span:
        event = before_send({"message": "boom"}, {})
        trace_id = format(span.get_span_context().trace_id, "032x")
    assert event["tags"]["trace_id"] == trace_id
    assert "trace_id" not in before_send({"message": "boom"}, {})["tags"]


def test_error_tracking_never_ships_request_bodies_and_always_scrubs(monkeypatch):
    seen = {}
    monkeypatch.setattr(sentry_sdk, "init", lambda **options: seen.update(options))
    init_telemetry(None, service_name="test", env="test", glitchtip_dsn="http://key@glitchtip:8000/1")
    assert seen["max_request_body_size"] == "never"
    assert seen["send_default_pii"] is False
    assert seen["before_send"] is before_send
