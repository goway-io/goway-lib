import uuid

import pytest

from goway_lib.errors import BadRequest
from goway_lib.pagination import decode_cursor, encode_cursor


def test_cursor_roundtrip():
    id_ = uuid.uuid7()
    assert decode_cursor(encode_cursor(id_)) == id_


def test_tampered_cursor_is_bad_request():
    with pytest.raises(BadRequest):
        decode_cursor("garbage!!")
    with pytest.raises(BadRequest):
        decode_cursor("aGVsbG8")
