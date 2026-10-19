import json
from io import BytesIO

import pytest

from al1s.infrastructure.direct_http import DirectHttpError, post_json_direct
from al1s.meme_workshop.onebot import OneBotClient


def install_response(monkeypatch, body):
    class Opener:
        def open(self, request, timeout):
            return BytesIO(body)

    monkeypatch.setattr("al1s.infrastructure.direct_http.build_opener", lambda *args: Opener())


def test_member_roster_larger_than_the_default_http_limit_is_complete(monkeypatch):
    members = [
        {"group_id": 300, "user_id": 1000 + index, "nickname": "n" * 100, "card": "c" * 100}
        for index in range(400)
    ]
    body = json.dumps({"status": "ok", "retcode": 0, "data": members}).encode()
    assert len(body) > 65_536
    install_response(monkeypatch, body)
    assert OneBotClient("http://gateway:3000", "fixture-token").group_members("300") == members


def test_existing_http_callers_keep_their_default_small_response_limit(monkeypatch):
    install_response(monkeypatch, b" " * 65_537)
    with pytest.raises(DirectHttpError, match="response_too_large"):
        post_json_direct("http://gateway:3000/get_status", None, {})


def test_custom_response_limit_remains_bounded_and_rejects_partial_rosters(monkeypatch):
    install_response(monkeypatch, b" " * (8 * 1024 * 1024 + 1))
    with pytest.raises(DirectHttpError, match="response_too_large"):
        post_json_direct(
            "http://gateway:3000/members", None, {}, max_response_bytes=8 * 1024 * 1024
        )
    with pytest.raises(ValueError, match="invalid_response_limit"):
        post_json_direct(
            "http://gateway:3000/members", None, {}, max_response_bytes=9 * 1024 * 1024
        )


@pytest.mark.parametrize(
    "data",
    [None, {}, ["invalid"], [{"group_id": 301, "user_id": 400}], [{"user_id": 400}], [{}] * 10_001],
)
def test_invalid_or_wrong_group_member_envelopes_are_rejected(monkeypatch, data):
    monkeypatch.setattr(
        "al1s.meme_workshop.onebot.post_json_direct",
        lambda *a, **kw: {"status": "ok", "retcode": 0, "data": data},
    )
    with pytest.raises(ValueError, match="members_invalid"):
        OneBotClient("http://gateway:3000", None).group_members("300")
