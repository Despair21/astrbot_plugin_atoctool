"""Static guards for the activity dashboard view assets."""

from __future__ import annotations

from pathlib import Path

VIEW_DIR = Path(__file__).resolve().parent.parent / "views" / "activity"
INDEX = VIEW_DIR / "index.html"
APP = VIEW_DIR / "app.js"
STYLE = VIEW_DIR / "style.css"


def test_icons_are_inline_not_an_external_sprite() -> None:
    index = INDEX.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    for symbol in ("i-refresh", "i-close", "i-alert", "i-copy"):
        assert f'<symbol id="{symbol}"' in index
    assert 'href="#i-refresh"' in index
    assert 'href="#i-close"' in index
    assert 'href="#i-alert"' in index
    assert "assets/icons.svg" not in index
    assert "assets/icons.svg" not in app
    assert 'setAttribute("href", "#" + name)' in app


def test_source_diagnostics_are_anchored_to_the_source_row() -> None:
    app = APP.read_text(encoding="utf-8")
    style = STYLE.read_text(encoding="utf-8")
    assert "data-diag-toggle" in app
    assert 'id: "diag-" + item.sourceId' in app
    assert '"aria-expanded"' in app
    assert "sourceDetails(" not in app
    assert ".c-row--diag[hidden]" in style
    assert ".c-sourcecell .c-table__mono" in style


def test_retry_is_only_offered_for_stale_or_error_sources() -> None:
    app = APP.read_text(encoding="utf-8")
    assert 'item.status !== "stale" && item.status !== "error"' in app
    assert 'item.status === "disabled"' in app
