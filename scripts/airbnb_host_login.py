"""One-time setup: opens a real (visible) browser for you to log into Airbnb
manually — handles 2FA/OTP fine since a human is doing it — then saves the
logged-in session to data/.airbnb_auth_state.json so agents/booking_pace_agent.py
can reuse it on every future run without logging in again.

Run this again whenever the saved session expires (HostSession will raise a
clear error telling you to).
"""

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent.parent))
from scrapers.airbnb_host import AUTH_STATE_PATH, BASE_URL, HostSession  # noqa: E402

POLL_SECONDS = 10
TIMEOUT_SECONDS = 15 * 60


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()

        # This tab is yours — the polling loop below never touches it.
        login_page = context.new_page()
        login_page.goto(f"{BASE_URL}/login")

        print("Log into your Airbnb host account in the opened browser window.", flush=True)
        print("Complete any 2FA/OTP step if prompted.", flush=True)
        print(f"Waiting for login (checking every {POLL_SECONDS}s, up to {TIMEOUT_SECONDS // 60} min)...", flush=True)

        # Separate tab, only used to poll login state — keeps the login tab undisturbed.
        check_page = context.new_page()

        session = HostSession()
        waited = 0
        logged_in = False
        while waited < TIMEOUT_SECONDS:
            time.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
            try:
                is_logged_in = session._is_logged_in(check_page)
            except Exception as e:
                # A transient failure here (slow load, cookie banner, etc.)
                # must not crash the script — that would tear down the whole
                # browser, including the user's still-open login tab.
                print(f"...check failed ({waited}s elapsed): {e!r}", flush=True)
                continue
            if is_logged_in:
                logged_in = True
                break
            print(f"...not logged in yet ({waited}s elapsed)", flush=True)

        if not logged_in:
            print("Timed out waiting for login. Nothing saved — rerun this script to try again.", flush=True)
            browser.close()
            return

        AUTH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(AUTH_STATE_PATH))
        print(f"Logged in — saved session to {AUTH_STATE_PATH}", flush=True)
        browser.close()


if __name__ == "__main__":
    main()
