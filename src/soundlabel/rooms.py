"""The listening room (M2) — LiveKit orchestration for synced group listening.

A listening room is where humans finally enter the loop: the label plays a
candidate batch to everyone at once, playback stays in sync, and scores are
collected live. This module is the orchestration layer:

- **Access tokens** are minted locally (pure crypto, no server round-trip) —
  hosts can publish and control playback, listeners subscribe and score.
- **Room lifecycle** (open / list / close) talks to any LiveKit server via
  the standard API; bring your own server, same as generation backends.
- **The sync protocol** is a handful of JSON data-messages over LiveKit's
  data channel, documented in :data:`PROTOCOL` and implemented by the
  reference client in ``room/listen.html``.

Optional dependency: ``pip install "soundlabel[rooms]"`` (livekit-api).
Configuration is env: ``LIVEKIT_URL``, ``LIVEKIT_API_KEY``,
``LIVEKIT_API_SECRET``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# The wire protocol for synced playback + live scoring. Messages are JSON on
# the LiveKit data channel (topic "soundlabel"). Hosts send play/pause/seek;
# every client applies them against the shared track list; listeners send
# score messages which the host (or a recorder bot) persists via
# Catalog/scoring. Kept deliberately small — the room is a transport, the
# scoring stack stays the source of truth.
PROTOCOL = {
    "play":  {"type": "play", "track_id": "trk_...", "position_s": 0.0, "at_unix": 0.0},
    "pause": {"type": "pause", "at_unix": 0.0},
    "seek":  {"type": "seek", "position_s": 0.0, "at_unix": 0.0},
    "score": {"type": "score", "track_id": "trk_...", "identity": "listener-1",
              "score": 7, "comment": ""},
}


@dataclass
class RoomConfig:
    url: str
    api_key: str
    api_secret: str

    @classmethod
    def from_env(cls) -> "RoomConfig":
        try:
            return cls(os.environ["LIVEKIT_URL"],
                       os.environ["LIVEKIT_API_KEY"],
                       os.environ["LIVEKIT_API_SECRET"])
        except KeyError as exc:
            raise RuntimeError(
                "listening rooms need LIVEKIT_URL / LIVEKIT_API_KEY / "
                "LIVEKIT_API_SECRET in the environment") from exc


def _api():
    try:
        from livekit import api
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "listening rooms need the LiveKit SDK: pip install 'soundlabel[rooms]'"
        ) from exc
    return api


def mint_token(config: RoomConfig, room: str, identity: str,
               host: bool = False, ttl_s: int = 6 * 3600) -> str:
    """Mint a join token locally. Hosts can publish audio and drive playback;
    listeners can only subscribe and send data messages (their scores)."""
    import datetime

    api = _api()
    grants = api.VideoGrants(
        room_join=True, room=room,
        can_publish=host, can_publish_data=True, can_subscribe=True,
    )
    return (api.AccessToken(config.api_key, config.api_secret)
            .with_identity(identity)
            .with_grants(grants)
            .with_ttl(datetime.timedelta(seconds=ttl_s))
            .to_jwt())


async def open_room(config: RoomConfig, room: str, max_listeners: int = 50):
    """Create the room on the server (idempotent on most servers)."""
    api = _api()
    async with api.LiveKitAPI(config.url, config.api_key, config.api_secret) as lk:
        return await lk.room.create_room(
            api.CreateRoomRequest(name=room, max_participants=max_listeners + 1))


async def list_rooms(config: RoomConfig) -> list:
    api = _api()
    async with api.LiveKitAPI(config.url, config.api_key, config.api_secret) as lk:
        result = await lk.room.list_rooms(api.ListRoomsRequest())
        return list(result.rooms)


async def close_room(config: RoomConfig, room: str) -> None:
    api = _api()
    async with api.LiveKitAPI(config.url, config.api_key, config.api_secret) as lk:
        await lk.room.delete_room(api.DeleteRoomRequest(room=room))
