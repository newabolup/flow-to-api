#!/usr/bin/env python3
"""Dry-run health check for the Flow UI layer. Costs zero credits.

It opens the browser with the saved profile, walks every read-only step the
bridge uses (probe -> login -> project -> composer -> agent state) and, with
`--models`, opens the model menus and prints what the account can use. It
never types a prompt, never clicks "Start generation" and never downloads.

Use it after every change, before spending credits on a real job:

    ~/flowbridge/venv/bin/python tools/check_ui.py
    ~/flowbridge/venv/bin/python tools/check_ui.py --models --screenshot

Exit code 0 = the UI layer is healthy; 1 = something the bridge depends on
moved and needs a selector update (stderr says which step).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowbridge.api import configure_logging  # noqa: E402
from flowbridge.config import Settings  # noqa: E402
from flowbridge.discovery import ModelDiscovery  # noqa: E402
from flowbridge.runtime import BrowserRuntime  # noqa: E402
from flowbridge.state import StateStore  # noqa: E402
from flowbridge.ui import COMPOSER, NotLoggedIn  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", action="store_true",
                        help="also read the model menus (still no generation)")
    parser.add_argument("--screenshot", action="store_true",
                        help="save a screenshot of the final state")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    settings = Settings.from_env()
    configure_logging("INFO")
    settings.prepare_dirs()
    state = StateStore(settings.state_file)
    runtime = BrowserRuntime(settings, state)

    report: dict = {}
    ok = True
    try:
        ui = runtime.ui()  # launches Chromium with the saved profile
        probe = ui.probe()
        report["url"] = probe.get("url")
        report["composer_visible"] = bool((probe.get("composer") or {}).get("visible"))
        report["agent_on"] = probe.get("agent")
        report["panel_open"] = probe.get("panel_open")
        report["cards_on_page"] = len(probe.get("cards") or [])
        report["download_button"] = probe.get("download")
        report["buttons"] = [b.get("t") or b.get("al") for b in (probe.get("buttons") or [])][:20]

        try:
            logged_in = ui.is_logged_in(navigate=True)
            report["logged_in"] = logged_in
            if not logged_in:
                raise NotLoggedIn("not_logged_in: run login.sh again")
            ui.ensure_ready()
            report["project_url"] = ui.d.url
            report["composer_after_navigation"] = bool(
                (ui.probe().get("composer") or {}).get("visible"))
            report["prompt_box_found"] = ui.d.count(COMPOSER) > 0
        except Exception as exc:
            ok = False
            report["error"] = str(exc)

        if args.models and ok:
            discovery = ModelDiscovery(settings, state)
            report["models"] = discovery.get(ui, refresh=True)

        if args.screenshot:
            shot = settings.debug_dir / "check-ui.png"
            ok = ui.d.screenshot(str(shot)) and ok
            report["screenshot"] = str(shot)
    except Exception as exc:  # browser could not even start
        ok = False
        report["fatal"] = str(exc)
    finally:
        try:
            runtime.shutdown()
        except Exception:
            pass

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print("Flow UI dry run")
        for key, value in report.items():
            print(f"  {key:24} {value}")
        print("RESULT:", "OK" if ok else "PROBLEM (see above)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
