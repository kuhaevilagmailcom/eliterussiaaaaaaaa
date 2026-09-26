import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from aiohttp import web

from miniapp import validate_init_data


def signed_init_data(token: str, auth_date: int) -> str:
    fields = {
        "auth_date": str(auth_date),
        "query_id": "test",
        "user": json.dumps({"id": 42, "first_name": "Test"}, separators=(",", ":")),
    }
    data_check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_init_data_signature_and_ttl():
    token = "123456:TEST"
    parsed = validate_init_data(signed_init_data(token, int(time.time())), token, 3600)
    assert parsed["id"] == 42
    with pytest.raises(web.HTTPUnauthorized):
        validate_init_data(signed_init_data(token, int(time.time()) - 7200), token, 3600)
