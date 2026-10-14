"""Client for the AcadMcp plugin's RPC port.

The plugin (see ``plugin/`` in this repository) listens on loopback and speaks one
JSON object per line. This module is the only thing in the Python package that
talks to it: :mod:`acadmcp.backends.plugin` records primitives and hands whole
steps over through here, and the step tools use it to drive the journal.

Wire format::

    {"id": 3, "method": "rollback_to", "params": {"index": 2, "force": false}}
    {"id": 3, "ok": true, "result": {...}}
    {"id": 3, "ok": false, "error": "no step 9"}
    {"id": 3, "ok": false, "blocked": true, "method": "run_all", "error": "AutoCAD is in the LINE command ..."}

The plugin's own ``ping`` is answered without touching AutoCAD, so it keeps
working while AutoCAD is busy — which is the only moment it is needed.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
from typing import Any

#: Must match the plugin's ``RpcServer.DefaultPort``.
DEFAULT_PORT = 9880

#: Deliberately longer than the plugin's own request timeout (30s by default), so
#: that a call which cannot run returns the plugin's *explanation* — "AutoCAD is
#: in the LINE command" — instead of the client giving up first and reporting a
#: bare timeout. Set ``ACADMCP_RPC_TIMEOUT_MS`` in the plugin to change that side.
DEFAULT_TIMEOUT = 40.0
CONNECT_TIMEOUT = 3.0

#: A reply longer than this is treated as a broken stream rather than trusted.
MAX_REPLY_BYTES = 64 * 1024 * 1024

#: Methods that may be sent a second time after a connection failure.
#:
#: Reads only. A drawing call must never be re-sent when the connection dies: the
#: request may have been carried out before the socket broke, so sending it again
#: would draw the step twice into the user's drawing. It is better to fail and let
#: the caller re-read the journal, which knows what actually happened.
#:
#: Idempotent writes (``ensure_layer``, ``set_units_mm``) are deliberately not in
#: here either. They are cheap to make safe, but a rule with exceptions is a rule
#: somebody will get wrong; the reconnect is rare and re-reading is cheap.
RETRY_SAFE = frozenset({"ping", "status", "info", "get_steps", "reload", "blocks"})


class PluginError(RuntimeError):
    """The plugin answered with an error, or the exchange could not be completed."""


class PluginUnavailable(PluginError):
    """Nothing answered: AutoCAD is not running, or the plugin is not loaded."""


class PluginBlocked(PluginError):
    """The plugin is there, but AutoCAD could not run the call in time.

    The plugin refuses rather than half-doing something, and it never runs an
    abandoned request later, so a blocked call changed nothing.
    """

    def __init__(self, message: str, method: str = "") -> None:
        super().__init__(message)
        self.method = method


def configured_port() -> int:
    """The port to use: ``ACADMCP_PORT``, else :data:`DEFAULT_PORT`."""
    raw = os.environ.get("ACADMCP_PORT")
    try:
        port = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_PORT
    return port if 0 < port < 65536 else DEFAULT_PORT


class PluginClient:
    """A connection to one running plugin.

    Not thread-safe: it keeps one socket and one request counter. Give each thread
    its own client, or serialise the calls.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int | None = None,
        timeout: float | None = None,
        connect_timeout: float = CONNECT_TIMEOUT,
    ) -> None:
        self.host = host
        self.port = configured_port() if port is None else int(port)
        self.timeout = _configured_timeout() if timeout is None else float(timeout)
        self.connect_timeout = float(connect_timeout)
        self._socket: socket.socket | None = None
        self._stream: Any = None
        self._next_id = 0

    # --- connection --------------------------------------------------------

    def connect(self) -> PluginClient:
        if self._socket is not None:
            return self
        try:
            sock = socket.create_connection((self.host, self.port), self.connect_timeout)
        except OSError as exc:
            raise PluginUnavailable(
                f"nothing listening on {self.host}:{self.port} — is AutoCAD running "
                f"with the AcadMcp plugin loaded? ({exc})"
            ) from exc
        sock.settimeout(self.timeout)
        self._socket = sock
        self._stream = sock.makefile("rb")
        return self

    def close(self) -> None:
        if self._stream is not None:
            _suppress_close(self._stream)
            self._stream = None
        if self._socket is not None:
            _suppress_close(self._socket)
            self._socket = None

    def __enter__(self) -> PluginClient:
        return self.connect()

    def __exit__(self, *_: object) -> None:
        self.close()

    # --- calls -------------------------------------------------------------

    def call(self, method: str, **params: Any) -> Any:
        """Run one method and return its result.

        Raises :class:`PluginUnavailable`, :class:`PluginBlocked` or
        :class:`PluginError`. A connection failure is retried only for the
        read-only methods in :data:`RETRY_SAFE`.
        """
        attempts = 2 if method in RETRY_SAFE else 1
        failure: PluginError | None = None
        for _ in range(attempts):
            try:
                return self._exchange(method, params)
            except PluginUnavailable as exc:
                # The plugin died mid-call: a restarted AutoCAD, or the plugin
                # reloaded. Reconnecting is the caller's next request, not this one.
                self.close()
                failure = exc
        assert failure is not None
        raise failure

    def ping(self) -> dict[str, Any]:
        """Ask whether the plugin is alive. Answered without touching AutoCAD."""
        return self.call("ping")

    def available(self) -> bool:
        """True when the plugin answers. Never raises."""
        try:
            self.ping()
        except PluginError:
            return False
        except OSError:
            return False
        return True

    def _exchange(self, method: str, params: dict[str, Any]) -> Any:
        sock = self.connect()._socket
        assert sock is not None

        self._next_id += 1
        request: dict[str, Any] = {"id": self._next_id, "method": method}
        if params:
            request["params"] = params

        try:
            sock.sendall(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
        except OSError as exc:
            raise PluginUnavailable(f"sending {method} failed: {exc}") from exc

        reply = self._read_reply(method)

        if reply.get("ok"):
            return reply.get("result")
        if reply.get("blocked"):
            raise PluginBlocked(reply.get("error") or "blocked", method=method)
        raise PluginError(f"{method}: {reply.get('error') or 'no error given'}")

    def _read_reply(self, method: str) -> dict[str, Any]:
        assert self._stream is not None
        try:
            raw = self._stream.readline(MAX_REPLY_BYTES)
        except TimeoutError as exc:
            raise PluginUnavailable(f"{method} timed out after {self.timeout:g}s") from exc
        except OSError as exc:
            raise PluginUnavailable(f"reading the {method} reply failed: {exc}") from exc

        if not raw:
            raise PluginUnavailable(f"the plugin closed the connection during {method}")
        if not raw.endswith(b"\n"):
            raise PluginError(f"the {method} reply was not terminated by a newline")

        try:
            reply = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise PluginError(f"the {method} reply was not JSON: {raw[:200]!r}") from exc
        if not isinstance(reply, dict):
            raise PluginError(f"the {method} reply was not a JSON object")
        return reply


def _configured_timeout() -> float:
    raw = os.environ.get("ACADMCP_RPC_TIMEOUT")
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


def _suppress_close(closer: Any) -> None:
    # Already gone is fine: closing is best-effort, and a shutdown must not raise
    # over a socket the other end has already dropped.
    with contextlib.suppress(OSError):
        closer.close()


def connect(
    host: str = "127.0.0.1", port: int | None = None, timeout: float | None = None
) -> PluginClient:
    """Connect to the plugin, or raise :class:`PluginUnavailable`."""
    return PluginClient(host=host, port=port, timeout=timeout).connect()
