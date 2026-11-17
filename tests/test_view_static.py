"""Static guards for the activity dashboard view assets."""

from __future__ import annotations

from pathlib import Path

VIEW_DIR = Path(__file__).resolve().parent.parent / "views" / "activity"
INDEX = VIEW_DIR / "index.html"
APP = VIEW_DIR / "app.js"
STYLE = VIEW_DIR / "style.css"
I18N_ZH = VIEW_DIR.parent.parent / ".astrbot-plugin" / "i18n" / "zh-CN.json"


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


def test_ambiguous_time_labels_are_renamed() -> None:
    app = APP.read_text(encoding="utf-8")
    zh = I18N_ZH.read_text(encoding="utf-8")
    for old in ("最近发现", "最近成功", "诊断详情"):
        assert old not in app
        assert old not in zh
    for label in ("同步时间", "详情"):
        assert label in app
        assert label in zh


def test_reminder_status_note_catch_up_and_toggle() -> None:
    app = APP.read_text(encoding="utf-8")
    zh = I18N_ZH.read_text(encoding="utf-8")
    assert "已提交" in app and "已提交" in zh
    assert "补发" in app and "补发" in zh
    assert "reminders.sentNote" in app
    assert "isCatchUp" in app
    assert ">= 60000" in app
    assert "data-reminder-toggle" in app
    assert "data-reminder-key" in app
    assert "reminder-details-" in app
    assert "onDetailsToggle" not in app
    assert 'addEventListener("toggle", onDetailsToggle' not in app
    assert ".c-row--details[hidden]" in STYLE.read_text(encoding="utf-8")


def test_reminder_details_collapse_by_default() -> None:
    app = APP.read_text(encoding="utf-8")
    assert 'if (!state.reminders.expanded.has(job.key)) row.hidden = true;' in app
    assert 'if (!state.reminders.expanded.has(job.key)) panel.hidden = true;' in app
    assert "onRemindersPanelClick" in app
    assert '"aria-controls": "reminder-details-" + index' in app


def test_time_zone_name_is_conditional() -> None:
    app = APP.read_text(encoding="utf-8")
    assert 'const key = state.locale + "|" + state.showTimezone;' in app
    assert "payload.show_timezone === true" in app
    assert 'minute: "2-digit", timeZoneName' not in app
    lines = [line.strip() for line in app.splitlines() if "timeZoneName" in line]
    assert lines == ['if (state.showTimezone) options.timeZoneName = "short";']
