"""Tests for the plugin client: framing, error mapping and the retry rule.

No AutoCAD is needed. A throwaway loopback server stands in for the plugin, which
is enough to pin down the parts that are easy to get wrong — how a reply is
matched to its request, what a blocked call looks like, and above all *which*
methods may be sent a second time.
"""

from __future__ import annotations

import json
import socket
import threading
from typing import Any

import pytest

from acadmcp.acad_plugin_client import (
    PluginBlocked,
    PluginClient,
    PluginError,
    PluginUnavailable,
    configured_port,
)

DROP = "drop"


class Raw:
    """A behaviour that writes bytes and waits, i.e. sends no complete reply."""

    def __init__(self, data: bytes) -> None:
        self.data = data


class FakePlugin:
    """A stand-in for the plugin's socket that follows a script.

    Each behaviour is consumed by one received request: a dict is merged into the
    reply sent back, and :data:`DROP` closes the connection without replying, which
    is how a dead AutoCAD or a reloaded plugin looks from the client.
    """

    def __init__(self, *behaviours: Any) -> None:
        self.behaviours = list(behaviours)
        self.requests: list[dict] = []
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(4)
        self.port = self._server.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                connection, _ = self._server.accept()
            except OSError:
                return
            self._serve(connection)

    def _serve(self, connection: socket.socket) -> None:
        with connection, connection.makefile("rb") as stream:
            for raw in stream:
                request = json.loads(raw)
                self.requests.append(request)
                behaviour = (
                    self.behaviours.pop(0) if self.behaviours else {"ok": True, "result": {}}
                )
                if behaviour == DROP:
                    return
                if isinstance(behaviour, Raw):
                    # Bytes a real plugin would never send: no newline, so no reply.
                    connection.sendall(behaviour.data)
                    continue
                reply = {"id": request.get("id")} | behaviour
                connection.sendall(json.dumps(reply).encode("utf-8") + b"\n")

    def stop(self) -> None:
        self._server.close()


@pytest.fixture
def fake():
    made: list[FakePlugin] = []

    def build(*behaviours: Any) -> FakePlugin:
        server = FakePlugin(*behaviours)
        made.append(server)
        return server

    yield build
    for server in made:
        server.stop()


def test_a_call_returns_the_result(fake):
    plugin = fake({"ok": True, "result": {"count": 3}})
    with PluginClient(port=plugin.port, timeout=2.0) as client:
        assert client.call("status") == {"count": 3}


def test_the_request_carries_its_method_and_id(fake):
    plugin = fake({"ok": True, "result": {}})
    with PluginClient(port=plugin.port, timeout=2.0) as client:
        client.call("run_to", index=3)
    assert plugin.requests[0]["method"] == "run_to"
    assert plugin.requests[0]["params"] == {"index": 3}
    assert isinstance(plugin.requests[0]["id"], int)


def test_an_error_reply_becomes_an_error(fake):
    plugin = fake({"ok": False, "error": "no step 9"})
    with (
        PluginClient(port=plugin.port, timeout=2.0) as client,
        pytest.raises(PluginError, match="no step 9"),
    ):
        client.call("rollback_to", index=9)


def test_a_blocked_reply_says_what_was_in_the_way(fake):
    plugin = fake({"ok": False, "blocked": True, "error": "AutoCAD is in the LINE command"})
    with (
        PluginClient(port=plugin.port, timeout=2.0) as client,
        pytest.raises(PluginBlocked) as caught,
    ):
        client.call("run_all")
    assert caught.value.method == "run_all"
    assert "LINE" in str(caught.value)


def test_a_read_is_sent_again_after_the_connection_dies(fake):
    # The plugin closes the connection instead of answering, then answers the
    # second attempt on a fresh connection.
    plugin = fake(DROP, {"ok": True, "result": {"count": 3}})
    with PluginClient(port=plugin.port, timeout=2.0) as client:
        assert client.call("status") == {"count": 3}
    assert len(plugin.requests) == 2


def test_a_drawing_call_is_never_sent_twice(fake):
    # This is the one that matters. The request may have been carried out before
    # the socket broke, so a re-send would draw the same step into the drawing
    # twice. Better to report the failure and let the caller re-read the journal.
    plugin = fake(DROP, {"ok": True, "result": {"ok": True}})
    with PluginClient(port=plugin.port, timeout=2.0) as client, pytest.raises(PluginUnavailable):
        client.call("apply", step={"op": "draw_wall"})
    assert len(plugin.requests) == 1


def test_an_unterminated_reply_is_not_parsed(fake):
    # Bytes with no newline are not a reply. The client must not parse a partial
    # line, and since this is a read it retries once before giving up.
    plugin = fake(Raw(b'{"id": 1, "ok": true, "result": '), Raw(b'{"id": 2, "ok": true, '))
    with (
        PluginClient(port=plugin.port, timeout=0.5) as client,
        pytest.raises(PluginUnavailable, match="timed out"),
    ):
        client.call("status")


def test_nothing_listening_is_reported_as_unavailable():
    # Port 1 on loopback: nothing can be listening there without privileges.
    with pytest.raises(PluginUnavailable, match="nothing listening"):
        PluginClient(port=1, connect_timeout=1.0).connect()


def test_available_is_false_rather_than_raising():
    assert PluginClient(port=1, connect_timeout=1.0).available() is False


def test_available_is_true_when_the_plugin_answers(fake):
    plugin = fake({"ok": True, "result": {"pong": True}})
    assert PluginClient(port=plugin.port, timeout=2.0).available() is True


def test_the_port_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("ACADMCP_PORT", "9999")
    assert PluginClient().port == 9999
    monkeypatch.setenv("ACADMCP_PORT", "not a number")
    assert configured_port() == 9880
    monkeypatch.delenv("ACADMCP_PORT")
    assert configured_port() == 9880


def test_closing_twice_is_harmless(fake):
    plugin = fake({"ok": True, "result": {}})
    client = PluginClient(port=plugin.port, timeout=2.0)
    client.connect()
    client.close()
    client.close()
