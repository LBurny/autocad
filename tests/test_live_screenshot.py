"""How the live-AutoCAD screenshot behaves when the window is not available.

Driving AutoCAD from the background is the normal case — the user is working in
another application while the AI draws — so a screenshot must never restore or
raise AutoCAD's window to get its picture. These are the two decisions that
guarantee it, both taken from a measured failure: the capture used to call
ShowWindow(SW_RESTORE), which pulled AutoCAD in front of whatever the user had
moved on to, and a minimised window's rect is a 160x28 off-screen stub, so the
picture it produced was meaningless as well.
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import capture  # noqa: E402


def _fake_win32(monkeypatch: pytest.MonkeyPatch, *, iconic: bool, calls: list) -> None:
    fake = types.ModuleType("win32gui")
    fake.IsIconic = lambda hwnd: iconic
    fake.ShowWindow = lambda *a: calls.append(("ShowWindow", a))
    fake.GetWindowRect = lambda hwnd: (0, 0, 800, 600)
    monkeypatch.setitem(sys.modules, "win32gui", fake)
    monkeypatch.setitem(sys.modules, "win32ui", types.ModuleType("win32ui"))


def test_a_minimised_window_is_reported_not_restored(monkeypatch, tmp_path):
    calls: list = []
    _fake_win32(monkeypatch, iconic=True, calls=calls)

    with pytest.raises(capture.WindowUnavailable, match="minimised"):
        capture.capture_window(4242, str(tmp_path / "shot.png"))
    assert not calls, "the capture restored the window — it must not touch the user's screen"


def test_a_zero_area_window_is_reported_not_captured(monkeypatch, tmp_path):
    """The other shape of the same problem: no drawable rect, so say so rather
    than write a picture of nothing."""
    calls: list = []
    fake = types.ModuleType("win32gui")
    fake.IsIconic = lambda hwnd: False
    fake.ShowWindow = lambda *a: calls.append(a)
    fake.GetWindowRect = lambda hwnd: (0, 0, 0, 0)
    monkeypatch.setitem(sys.modules, "win32gui", fake)
    monkeypatch.setitem(sys.modules, "win32ui", types.ModuleType("win32ui"))

    with pytest.raises(capture.WindowUnavailable, match="drawable area"):
        capture.capture_window(4242, str(tmp_path / "shot.png"))
    assert not calls


def test_com_writes_a_dxf_copy_without_saving_the_drawing(tmp_path, monkeypatch):
    """`drawing_save('x.dxf')` promises a non-destructive copy. It used SaveAs,
    which ignored the extension (writing `x.dxf.dwg`, a DWG with a DXF's name)
    and renamed the open drawing to the copy's name. It goes through
    Document.Export now."""
    from acadmcp.backends.com import ComBackend

    backend = object.__new__(ComBackend)  # no AutoCAD session needed
    asked: dict = {}

    def fake_export(stem: str, kind: str):
        asked["kind"] = kind
        written = Path(f"{stem}.{kind}")
        written.write_text("0\nSECTION\n")
        return written

    monkeypatch.setattr(backend, "_export_to", fake_export)

    out = ComBackend.save(backend, str(tmp_path / "copy.dxf"))
    assert asked["kind"] == "DXF", "a .dxf target must not go through SaveAs"
    assert Path(out) == tmp_path / "copy.dxf"
    assert Path(out).read_text().startswith("0\nSECTION")


def test_com_reports_a_refused_export_rather_than_a_wrong_file(tmp_path, monkeypatch):
    from acadmcp.backends.base import BackendError
    from acadmcp.backends.com import ComBackend

    backend = object.__new__(ComBackend)
    monkeypatch.setattr(backend, "_export_to", lambda stem, kind: None)

    with pytest.raises(BackendError, match="would not export a DXF copy"):
        ComBackend.save(backend, str(tmp_path / "copy.dxf"))
