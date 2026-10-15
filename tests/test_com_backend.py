"""The COM backend's rules that can be checked without AutoCAD running.

A COM object belongs to the apartment of the thread that made it, and the MCP server
runs each tool call on whichever worker thread is free — so a backend built during one
call is used during the next from a different thread. win32com reports that as
``AttributeError: <unknown>.AddLine``, which is not a com_error, so the retry wrapper
never saw it and the tool layer reported a bare "failed". Measured before the fix: the
first call after a restart worked and every later one failed.

These tests pin the fix, the retry rule and the error messages, none of which needs
AutoCAD to be installed.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

pythoncom = pytest.importorskip("pythoncom")

from acadmcp.backends import com  # noqa: E402
from acadmcp.backends.base import BackendError  # noqa: E402


class Dead:
    """Every attribute access fails, the way a lost COM object behaves."""

    def __getattr__(self, name: str) -> object:
        raise RuntimeError(f"{name} is gone")


def _com_error(hr: int, text: str) -> Exception:
    return pythoncom.com_error(hr, text, None, None)


# ---------------------------------------------------------------------------
# one thread, one apartment
# ---------------------------------------------------------------------------


def test_every_com_call_runs_on_one_thread_that_is_not_the_caller():
    caller = threading.get_ident()

    threads = {com._call(lambda: threading.get_ident()) for _ in range(5)}

    assert len(threads) == 1, "COM work must not be spread over the caller's threads"
    assert caller not in threads


def test_com_calls_from_many_threads_land_on_the_same_com_thread():
    seen: list[int] = []
    lock = threading.Lock()

    def ask() -> None:
        with lock:
            seen.append(com._call(lambda: threading.get_ident()))

    threads = [threading.Thread(target=ask) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(set(seen)) == 1


def test_the_com_thread_initialises_its_apartment():
    com._call(lambda: None)

    # Without CoInitialize on that thread, GetActiveObject and Dispatch raise
    # "CoInitialize has not been called" and every call fails.
    assert com._com_thread().ready is True


def test_importing_the_backend_starts_no_thread():
    # The dxf path never touches COM, so importing this module must stay free of it.
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "import acadmcp.backends.com as c;"
        "print(c._owner is None)" % (ROOT / "src")
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True
    )

    assert out.stdout.strip() == "True"


# ---------------------------------------------------------------------------
# retrying only when AutoCAD says it is busy
# ---------------------------------------------------------------------------


def test_a_busy_autocad_is_retried(monkeypatch):
    monkeypatch.setattr(com, "CALL_RETRIES", 3)
    monkeypatch.setattr(com, "CALL_BACKOFF", 0.0)
    attempts: list[int] = []

    def busy(*_args: object) -> None:
        attempts.append(1)
        raise _com_error(com.RPC_E_CALL_REJECTED, "busy")

    with pytest.raises(Exception, match="busy"):
        com._call(busy)

    assert len(attempts) == 3


def test_any_other_com_failure_is_not_retried(monkeypatch):
    monkeypatch.setattr(com, "CALL_RETRIES", 3)
    attempts: list[int] = []

    def denied(*_args: object) -> None:
        attempts.append(1)
        raise _com_error(-2147024891, "access denied")

    with pytest.raises(Exception, match="access denied"):
        com._call(denied)

    # Re-sending a call AutoCAD refused for any other reason is how the same line gets
    # drawn twice.
    assert len(attempts) == 1


def test_a_call_that_never_comes_back_gives_up_with_advice(monkeypatch):
    monkeypatch.setattr(com, "CALL_WAIT", 0.2)

    with pytest.raises(BackendError, match="did not answer"):
        com._call(lambda: time.sleep(1.0))


# ---------------------------------------------------------------------------
# saying what went wrong
# ---------------------------------------------------------------------------


def test_a_stale_link_is_explained_rather_than_a_bare_attribute_error():
    def stale(*_args: object) -> None:
        raise AttributeError("<unknown>.AddLine")

    # win32com's dynamic dispatch raises exactly this off-apartment, and it is not a
    # com_error — so it used to escape both the retry and the tool layer's message.
    with pytest.raises(BackendError, match="stale"):
        com._call(stale)


def test_an_attribute_error_about_our_own_code_is_left_alone():
    def typo(*_args: object) -> None:
        raise AttributeError("'ComBackend' object has no attribute 'thing'")

    with pytest.raises(AttributeError, match="thing"):
        com._call(typo)


def test_info_reports_a_broken_link_instead_of_sentinels():
    be = com.ComBackend.__new__(com.ComBackend)
    be.doc = Dead()
    be.msp = Dead()
    be._ops = 4

    info = be.info()

    # `entities: -1` with `document: "<unnamed>"` reads as "an unnamed drawing with a
    # negative entity count" — a drawing problem — instead of "the link is gone".
    assert info["entities"] is None
    assert info["document"] is None
    assert info["ops"] == 4
    assert "problem" in info and "gone" in info["problem"]


def test_entities_refuses_to_look_like_an_empty_drawing():
    be = com.ComBackend.__new__(com.ComBackend)
    be.doc = Dead()
    be.msp = Dead()
    be._ops = 0

    # The tag numbering reads this list: an empty one means "no tags in the drawing",
    # which silently restarts numbering at 1.
    with pytest.raises(RuntimeError):
        be.entities()


# ---------------------------------------------------------------------------
# a hatch that fails must leave nothing behind
# ---------------------------------------------------------------------------


class FakeEntity:
    """A COM entity stand-in: remembers its layer, its deletion and its refusals."""

    def __init__(self, *, refuse: str = "") -> None:
        self.refuse = refuse
        self.deleted = False
        self.Layer = "0"

    def Delete(self) -> None:
        self.deleted = True

    def AppendOuterLoop(self, loop: object) -> None:
        if self.refuse == "loop":
            raise RuntimeError("AppendOuterLoop refused")

    def Evaluate(self) -> None:
        if self.refuse == "evaluate":
            raise RuntimeError("Evaluate refused")


class FakeSpace:
    def __init__(self, hatch: FakeEntity) -> None:
        self.hatch = hatch
        self.boundary = FakeEntity()
        self.pattern = ""

    def AddLightWeightPolyline(self, points: object) -> FakeEntity:
        return self.boundary

    def AddHatch(self, kind: int, pattern: str, associative: bool) -> FakeEntity:
        self.pattern = pattern
        return self.hatch


def _fake_backend(monkeypatch: pytest.MonkeyPatch, *, refuse: str = "") -> tuple:
    # VARIANT wants a real COM object, and the sequence under test does not.
    monkeypatch.setattr(com, "VARIANT", lambda vt, values: values[0])
    be = com.ComBackend.__new__(com.ComBackend)
    be._ops = 0
    hatch = FakeEntity(refuse=refuse)
    space = FakeSpace(hatch)
    be.msp = space
    return be, hatch, space


SQUARE = [(0, 0), (1000, 0), (1000, 240), (0, 240)]


def test_a_good_hatch_removes_its_temporary_boundary(monkeypatch):
    be, hatch, space = _fake_backend(monkeypatch)

    made = be.hatch(SQUARE, "ANSI31", 40, layer="A-WALL-PATT")

    assert made is hatch
    assert hatch.Layer == "A-WALL-PATT"
    assert space.pattern == "ANSI31"
    assert space.boundary.deleted is True


def test_a_hatch_that_fails_leaves_nothing_behind(monkeypatch):
    be, hatch, space = _fake_backend(monkeypatch, refuse="loop")

    with pytest.raises(RuntimeError, match="refused"):
        be.hatch(SQUARE, "ANSI31", 40, layer="A-WALL-PATT")

    # Both halves have to go. Measured on a real envelope: one of four wall segments
    # failed like this and left a hatch still at its default pattern scale — a solid
    # blob at 1:1 across a 240 mm wall — plus its naked boundary polyline, both on
    # layer 0 and both invisible, because a wall suppresses a hatch failure so that a
    # decorative fill cannot take the wall down with it.
    assert hatch.deleted is True
    assert space.boundary.deleted is True
    assert space.boundary.Layer == "A-WALL-PATT"
