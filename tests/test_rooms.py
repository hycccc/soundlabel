"""Listening-room tests. Token minting is pure crypto — fully testable
offline; server operations are exercised only when a LiveKit server is
configured in the environment."""

import base64
import json
import os

import pytest

pytest.importorskip("livekit.api")

from soundlabel.rooms import RoomConfig, mint_token  # noqa: E402

CONFIG = RoomConfig(url="wss://example.test", api_key="testkey",
                    api_secret="testsecret-testsecret-testsecret")


def _payload(jwt: str) -> dict:
    body = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def test_host_token_grants_publish():
    claims = _payload(mint_token(CONFIG, "release-night", "yuchen", host=True))
    video = claims["video"]
    assert video["room"] == "release-night" and video["roomJoin"]
    assert video["canPublish"] is True
    assert claims["sub"] == "yuchen"


def test_listener_token_cannot_publish_audio():
    """Listeners score over the data channel but never publish audio —
    the role split is enforced in the token, not in client-side goodwill."""
    video = _payload(mint_token(CONFIG, "release-night", "listener-1"))["video"]
    assert video.get("canPublish") is not True
    assert video["canPublishData"] is True and video["canSubscribe"] is True


def test_config_from_env_requires_all_three(monkeypatch):
    for var in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError, match="LIVEKIT_URL"):
        RoomConfig.from_env()


@pytest.mark.skipif(not os.environ.get("LIVEKIT_URL"), reason="no LiveKit server")
def test_room_lifecycle_live():
    import asyncio

    from soundlabel.rooms import close_room, list_rooms, open_room

    config = RoomConfig.from_env()
    name = "soundlabel-ci-test"
    asyncio.run(open_room(config, name))
    assert any(r.name == name for r in asyncio.run(list_rooms(config)))
    asyncio.run(close_room(config, name))
