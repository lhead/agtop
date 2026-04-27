import asyncio
from pathlib import Path

from agtop.app import AgtopApp
from agtop.history import HistorySession


def _visible_bindings(app: AgtopApp) -> dict[str, str]:
    return {
        key: active.binding.description
        for key, active in app.screen.active_bindings.items()
        if active.binding.show
    }


def test_toggle_history_updates_h_binding_label(monkeypatch) -> None:
    monkeypatch.setattr(AgtopApp, "_scan", lambda self: [])
    monkeypatch.setattr("agtop.app.scan_history", lambda days=7: [])

    async def run() -> None:
        app = AgtopApp()
        async with app.run_test() as pilot:
            assert _visible_bindings(app) == {
                "q": "Quit",
                "r": "Refresh",
                "h": "History",
            }

            await pilot.press("h")
            await pilot.pause()

            assert _visible_bindings(app) == {
                "q": "Quit",
                "r": "Resume",
                "c": "Copy Resume",
                "h": "Live",
            }

            await pilot.press("h")
            await pilot.pause()

            assert _visible_bindings(app) == {
                "q": "Quit",
                "r": "Refresh",
                "h": "History",
            }
            assert len(app._bindings.key_to_bindings["h"]) == 1

    asyncio.run(run())


def test_footer_state_tracks_mode_and_selection() -> None:
    app = AgtopApp()
    app.sessions = [{"session_id": "s1", "alive": True}]
    app.sel_id = "s1"

    app._sync_footer_bindings()

    assert app._bindings.key_to_bindings["a"][0].description == "Subscribe"
    assert app._bindings.key_to_bindings["r"][0].description == "Refresh"
    assert app._bindings.key_to_bindings["c"][0].description == "Copy"
    assert app.check_action("jump", ()) is True
    assert app.check_action("subscribe", ()) is True
    assert app.check_action("copy_output", ()) is True

    app._subscribed.add("s1")
    app._sync_footer_bindings()

    assert app._bindings.key_to_bindings["a"][0].description == "Unsubscribe"

    app._history_mode = True
    app._sel_history = HistorySession(
        session_id="h1",
        path=Path("/tmp/h1.jsonl"),
        source="claude",
        project="demo",
        cwd="~/demo",
        actual_cwd="/tmp/demo",
        mtime=0,
        birthtime=0,
        first_user_msg="demo",
        stop_ts=None,
    )
    app._sync_footer_bindings()

    assert app._bindings.key_to_bindings["r"][0].description == "Resume"
    assert app._bindings.key_to_bindings["c"][0].description == "Copy Resume"
    assert app._bindings.key_to_bindings["h"][0].description == "Live"
    assert app.check_action("jump", ()) is False
    assert app.check_action("subscribe", ()) is False
    assert app.check_action("refresh_or_resume", ()) is True
    assert app.check_action("copy_output", ()) is True

    app._sel_history = None
    app._sync_footer_bindings()

    assert app.check_action("refresh_or_resume", ()) is None
    assert app.check_action("copy_output", ()) is None
