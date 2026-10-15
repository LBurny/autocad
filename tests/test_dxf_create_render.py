"""Regression tests from the live drawing exercise: drawing_new's create
semantics and theme-forced screenshot colours."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from acadmcp import server  # noqa: E402
from acadmcp.backends.base import BackendError  # noqa: E402


@pytest.fixture
def fresh_session():
    yield
    server._state.backend = None


def test_drawing_new_creates_a_missing_file(tmp_path, fresh_session):
    target = tmp_path / "fresh.dxf"
    out = server.drawing_new(backend="dxf", path=str(target))
    assert out["ok"] and out["entities"] == 0

    server.draw_line([0, 0], [1000, 0])
    server.drawing_save()
    assert target.is_file()


def test_drawing_new_at_an_existing_path_starts_empty(tmp_path, fresh_session):
    target = tmp_path / "occupied.dxf"
    server.drawing_new(backend="dxf", path=str(target))
    server.draw_line([0, 0], [1000, 0])
    server.drawing_save()

    server.drawing_new(backend="dxf", path=str(target))
    assert server.drawing_status()["entities"] == 0


def test_drawing_open_still_refuses_a_missing_file(tmp_path):
    with pytest.raises(BackendError, match="cannot open DXF"):
        server.drawing_open(str(tmp_path / "nope.dxf"))


def _dark_and_light_pixel_counts(png: Path) -> tuple[int, int]:
    import matplotlib.image as mpimg
    import numpy as np

    img = mpimg.imread(png)[..., :3]
    bright = int((np.all(img > 0.9, axis=-1)).sum())
    dark = int((np.all(img < 0.1, axis=-1)).sum())
    return dark, bright


def test_screenshot_theme_forces_a_legible_foreground(tmp_path):
    """ACI colours are a drafting convention: yellow on white or dark blue on
    dark both vanish, so each theme forces everything to its foreground."""
    be = server.make_backend("dxf")
    be.ensure_layer("Y", color=2)  # yellow: invisible on white without forcing
    be.line((0, 0), (5000, 0), layer="Y")
    be.line((0, 0), (0, 5000), layer="Y")

    light = be.screenshot(str(tmp_path / "light.png"), theme="light")
    dark = be.screenshot(str(tmp_path / "dark.png"), theme="dark")

    light_dark_px, _ = _dark_and_light_pixel_counts(Path(light))
    _, dark_bright_px = _dark_and_light_pixel_counts(Path(dark))
    assert light_dark_px > 500, "light theme left the yellow geometry white-on-white"
    assert dark_bright_px > 500, "dark theme failed to force the geometry white"


def _cjk_entities_seen_by_ezdxf(monkeypatch: pytest.MonkeyPatch, out: Path) -> list[str]:
    """Render a mixed drawing and report every text ezdxf was asked to draw."""
    from ezdxf.addons.drawing import Frontend

    seen: list[str] = []
    original = Frontend.draw_text_entity

    def spy(self, entity, properties):
        seen.append(entity.dxf.text)
        return original(self, entity, properties)

    monkeypatch.setattr(Frontend, "draw_text_entity", spy)

    be = server.make_backend("dxf")
    be.text((0, 0), "图", 350, layer="A-ANNO", align="center")
    be.text((0, 1000), "M0921", 350, layer="A-ANNO", align="center")
    be.screenshot(str(out / "deferral.png"), theme="light")
    return seen


def test_cjk_text_is_kept_off_ezdxf_glyph_pipeline(monkeypatch, tmp_path):
    """The fix for the 图 blob is that CJK text never reaches ezdxf's
    glyph-to-filled-path conversion — it is drawn by matplotlib instead. A
    pixel threshold could not pin this down: the blob's ink density varied
    (37-45%) with canvas scale and the two ranges overlapped. This asserts the
    mechanism, which is deterministic.

    The DXF must still point at SimSun — the deferral is a render-time choice,
    not a change to the file.
    """
    from acadmcp.backends import dxf as dxf_backend

    assert dxf_backend._has_cjk("图")
    assert dxf_backend._has_cjk("首层平面图 1:100")
    assert not dxf_backend._has_cjk("M0921")
    assert not dxf_backend._has_cjk("")

    seen = _cjk_entities_seen_by_ezdxf(monkeypatch, tmp_path)
    assert "M0921" in seen, "ASCII text should still take the ezdxf path"
    assert "图" not in seen, "CJK text must be deferred, not handed to ezdxf"


def test_a_deferred_title_is_still_inside_the_frame(tmp_path):
    """Deferring a text must not drop it from the framing: a drawing title sits
    below the plan, and if the deferred texts stop contributing to the data
    limits the full shot crops the title out."""
    import matplotlib.image as mpimg
    import numpy as np

    be = server.make_backend("dxf")
    be.polyline([(0, 0), (28000, 0), (28000, 16000), (0, 16000)], closed=True, layer="A-WALL")
    be.text((14000, -6500), "首层平面图", 700, layer="A-ANNO", align="center")
    png = be.screenshot(str(tmp_path / "framed.png"), theme="light")

    inked = np.any(mpimg.imread(png)[..., :3] < 0.5, axis=-1)
    height = inked.shape[0]
    assert inked[int(height * 0.9) :].any(), "the title under the plan was cropped out"
