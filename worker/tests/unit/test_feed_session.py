"""Two reconnect-path rules from the 2026-10 audit.

A reconnect used to open a fresh socket and send nothing: the live token set
lives in ``_subscribed``, not ``_pending``, so the feed sat CONNECTED and blind.
And a bare "pong" used to count as a feed frame, which let a heartbeat-only
session outrun the staleness watchdog forever. Both are pinned here.
"""

from __future__ import annotations

import json

from worker.config import AngelSettings
from worker.marketdata.smartapi_ws import SmartApiFeed


class FakeSocket:
    def __init__(self, frames=()):
        self.sent: list[str] = []
        self._frames = list(frames)

    async def send(self, data: str) -> None:
        self.sent.append(data)

    def __aiter__(self):
        async def stream():
            for frame in self._frames:
                yield frame

        return stream()


class _OpenSocket:
    def __init__(self, socket: FakeSocket) -> None:
        self._socket = socket

    async def __aenter__(self) -> FakeSocket:
        return self._socket

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Auth:
    async def websocket_headers(self) -> dict[str, str]:
        return {}


def make_feed(sockets: list[FakeSocket], clock: list[float]) -> SmartApiFeed:
    def connect(url: str, **kwargs: object) -> _OpenSocket:
        return _OpenSocket(sockets.pop(0))

    return SmartApiFeed(
        settings=AngelSettings(),
        auth=_Auth(),
        resolve=lambda *args: None,
        on_tick=lambda *args: None,
        connect=connect,
        monotonic=lambda: clock[0],
    )


def subscribed_tokens(socket: FakeSocket) -> set[str]:
    out: set[str] = set()
    for raw in socket.sent:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue  # the heartbeat sends a bare "ping"
        if payload.get("action") == 1:
            for entry in payload["params"]["tokenList"]:
                out.update(str(token) for token in entry["tokens"])
    return out


async def test_a_reconnect_resends_every_token_the_feed_already_held() -> None:
    clock = [1.0]
    first, second = FakeSocket(), FakeSocket()
    feed = make_feed([first, second], clock)

    await feed.subscribe(["100", "101"])
    await feed._session()
    assert subscribed_tokens(first) == {"100", "101"}

    await feed._session()  # reconnect: _pending is empty, _subscribed holds the set
    assert subscribed_tokens(second) == {"100", "101"}, "a fresh socket must not open blind"


async def test_pongs_alone_never_look_like_a_flowing_feed() -> None:
    clock = [7.0]
    socket = FakeSocket(frames=["pong", "pong"])
    feed = make_feed([socket], clock)

    await feed._receive(socket)

    assert feed.stats.pongs_received == 2
    assert feed.stats.seconds_since_frame(now=99.0) == float("inf"), (
        "only real frames reset the staleness clock"
    )


async def test_a_real_text_frame_does_reset_the_staleness_clock() -> None:
    clock = [7.0]
    socket = FakeSocket(frames=[json.dumps({"messageType": 12, "errorCode": "0"})])
    feed = make_feed([socket], clock)

    await feed._receive(socket)

    assert feed.stats.seconds_since_frame(now=10.0) == 3.0
