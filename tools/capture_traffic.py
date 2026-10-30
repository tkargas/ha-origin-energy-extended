#!/usr/bin/env python3
"""
Manual-login network capture for reverse-engineering the Origin Energy
"My Account" web app.

Opens a real, visible browser window. You log in and click around
yourself -- your email/password are typed by you, directly into
Origin's own page, and are never seen by this script or anyone else.
All this script does is record the network traffic your browser
generates so we can see which endpoints carry usage data.

Usage:
    python3 -m venv .venv && source .venv/bin/activate
    pip install playwright
    playwright install chromium
    python3 capture_traffic.py
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).parent / "origin_traffic.har"
START_URL = "https://www.originenergy.com.au/"


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            record_har_path=str(OUT),
            record_har_content="embed",
        )
        page = context.new_page()
        page.goto(START_URL)

        print("\n" + "=" * 70)
        print("A browser window has opened.")
        print("1. Click 'Log in' and sign in to My Account as normal")
        print("   (handle any MFA/OTP prompt as you usually would).")
        print("2. Go to Usage / meter usage and view daily usage for the")
        print("   last 7-30 days if you can pick a date range.")
        print("3. If there's a CSV / interval-data / download button, use it.")
        print("4. Come back to this terminal and press Enter when done.")
        print("=" * 70 + "\n")
        input("Press Enter once you've finished browsing... ")

        context.close()
        browser.close()

    print(f"\nSaved raw traffic to {OUT}")
    print("Next: run sanitize_har.py on this file BEFORE sharing it with anyone.")


if __name__ == "__main__":
    main()
