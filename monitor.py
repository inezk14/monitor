"""
Utrecht student studio monitor — production script.

Runs on GitHub Actions. One invocation starts a loop that checks every
CHECK_INTERVAL_SECONDS for about RUN_DURATION_MINUTES, then exits. The workflow
starts a fresh run each hour.

Three outcomes per check:
    AVAILABLE      - the Single Studio panel no longer shows the "no offer" notice
    NOT_AVAILABLE  - the notice is present
    MonitorError   - we could not tell (layout change, timeout, page failure)

The third case never silently becomes "nothing available". After
MAX_CONSECUTIVE_FAILURES in a row, the script emails you and exits non-zero so
the workflow goes red.

Configuration comes from environment variables (GitHub Secrets):
    RESEND_API_KEY
    EMAIL_TO
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

TIMEZONE = ZoneInfo("Europe/Amsterdam")
WINDOW_START_HOUR = 8       # 08:00 local
WINDOW_END_HOUR = 18        # until 18:00 local (exclusive)
WEEKDAYS_ONLY = True

CHECK_INTERVAL_SECONDS = 120
RUN_DURATION_MINUTES = 50   # leaves headroom before the next hourly run
MAX_CONSECUTIVE_FAILURES = 3

# ---------------------------------------------------------------------------
# Site configuration — add further providers by copying this block
# ---------------------------------------------------------------------------

PROVIDER = "THE FIZZ Utrecht"
URL = "https://www.the-fizz.com/en/student-accommodation/utrecht/#apartment"
ROOM_TYPE_KEYWORD = "single"

PANEL_SELECTOR = ".pex-rooms-in-building .pex-room-type"
HEADING_SELECTOR = ".panel-heading"
NO_OFFER_SELECTOR = ".room-type-information-no-offer"
ROOMS_SELECTOR = ".room-type-rooms"

PAGE_LOAD_TIMEOUT_MS = 45_000
WIDGET_TIMEOUT_MS = 30_000

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

EMAIL_FROM = "onboarding@resend.dev"
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")

STATE_FILE = Path("state.json")


class MonitorError(Exception):
    """Could not determine availability. NOT the same as 'nothing available'."""


@dataclass
class CheckResult:
    provider: str
    room_type: str
    status: str
    details: str
    url: str

    def __str__(self) -> str:
        return f"[{self.status}] {self.provider} - {self.room_type}\n{self.details}"


def log(message: str) -> None:
    """Timestamped log line. Never log EMAIL_TO - these logs are public."""
    print(f"{datetime.now(TIMEZONE):%H:%M:%S}  {message}", flush=True)


# ---------------------------------------------------------------------------
# Email sending
# ---------------------------------------------------------------------------


def send_email(subject: str, body: str) -> None:
    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": EMAIL_FROM,
            "to": [EMAIL_TO],
            "subject": subject,
            "text": body,
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Email failed ({response.status_code})")
    log(f"EMAIL SENT: {subject}")


def build_availability_email(result: CheckResult) -> tuple[str, str]:
    subject = f"STUDIO AVAILABLE - {result.provider} ({result.room_type})"
    body = f"""A studio appears to be available. Act fast.

Provider:      {result.provider}
Accommodation: {result.room_type}
Status:        {result.status}
Detected at:   {datetime.now(TIMEZONE):%Y-%m-%d %H:%M:%S %Z}

Link:
{result.url}

What the page shows:
{result.details}

--
Utrecht monitor. Verify on the site before acting.
"""
    return subject, body


def build_failure_email(error_text: str, failures: int) -> tuple[str, str]:
    subject = f"MONITOR BROKEN - {PROVIDER}"
    body = f"""The monitor could not determine availability {failures} times in a row.

This does NOT mean nothing is available. It means the monitor has gone blind
and you should check the site manually until this is fixed.

Time:  {datetime.now(TIMEZONE):%Y-%m-%d %H:%M:%S %Z}
Page:  {URL}

Last error:
{error_text}

Likely causes: the site changed its layout, the booking widget is down, or the
page is slow to load.
"""
    return subject, body


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log("state.json unreadable - starting fresh")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def process_result(result: CheckResult, state: dict) -> bool:
    """Email only on a change into AVAILABLE. Returns True if an email was sent.

    Important ordering: for an availability alert we send FIRST and record the
    new status only if the send succeeded. Otherwise a failed send would leave
    the state saying AVAILABLE, the next check would report 'no change', and the
    alert would be lost forever.
    """
    key = f"{result.provider} | {result.room_type}"
    previous = state.get(key, {}).get("status")

    def remember() -> None:
        state[key] = {
            "status": result.status,
            "checked_at": datetime.now(TIMEZONE).isoformat(timespec="seconds"),
            "details": result.details,
        }

    if previous is None:
        remember()
        log(f"Baseline recorded: {result.status} (no email on first run)")
        return False

    if previous == result.status:
        state.setdefault(key, {})["checked_at"] = datetime.now(TIMEZONE).isoformat(
            timespec="seconds"
        )
        return False

    log(f"CHANGE: {previous} -> {result.status}")

    if result.status == "AVAILABLE":
        subject, body = build_availability_email(result)
        send_email(subject, body)   # if this raises, state is NOT updated
        remember()
        return True

    remember()
    return False


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def _norm(text: str | None) -> str:
    return " ".join((text or "").split())


async def _panel_status(panel) -> tuple[str | None, str]:
    """The decisive signal is the presence of the 'no offer' notice, which FIZZ
    renders only when nothing is available. The rooms container is NOT a signal:
    when nothing is available it still holds the waiting-list buttons."""
    no_offer_el = await panel.query_selector(NO_OFFER_SELECTOR)
    rooms_el = await panel.query_selector(ROOMS_SELECTOR)
    rooms_text = _norm(await rooms_el.text_content()) if rooms_el else ""

    if no_offer_el:
        return "NOT_AVAILABLE", _norm(await no_offer_el.text_content())

    if rooms_el is not None:
        return "AVAILABLE", rooms_text or "(no-offer notice gone; no details captured)"

    return None, ""


async def check_once(page) -> CheckResult:
    """Reuses an already-open page. Raises MonitorError if the answer is unclear."""
    try:
        await page.goto(URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
    except PlaywrightTimeout as exc:
        raise MonitorError("Page did not load in time") from exc

    try:
        await page.wait_for_selector(PANEL_SELECTOR, timeout=WIDGET_TIMEOUT_MS)
    except PlaywrightTimeout as exc:
        raise MonitorError(
            f"Booking widget never appeared ({PANEL_SELECTOR}). "
            "Layout may have changed, or the booking API may be down."
        ) from exc

    panels = await page.query_selector_all(PANEL_SELECTOR)
    if not panels:
        raise MonitorError(f"No room-type panels found ({PANEL_SELECTOR})")

    matches, headings_seen = [], []
    for panel in panels:
        heading_el = await panel.query_selector(HEADING_SELECTOR)
        heading = _norm(await heading_el.text_content()) if heading_el else ""
        headings_seen.append(heading or "(no heading)")
        if ROOM_TYPE_KEYWORD in heading.lower():
            matches.append((heading, panel))

    if not matches:
        raise MonitorError(
            f"No panel matching '{ROOM_TYPE_KEYWORD}'. Headings: {headings_seen}"
        )

    # The widget is rendered once per popup, so the same room type can appear
    # several times. Read every copy and require them to agree.
    readings = []
    for heading, panel in matches:
        status, details = await _panel_status(panel)
        if status is None:
            raise MonitorError(
                f"A '{heading}' panel had neither an availability notice nor room listings."
            )
        readings.append((heading, status, details))

    distinct = {status for _, status, _ in readings}
    if len(distinct) > 1:
        raise MonitorError(
            f"Copies of the panel disagree: {[s for _, s, _ in readings]}"
        )

    heading, status, details = readings[0]
    if len(readings) > 1:
        details = f"{details}\n\n(confirmed across {len(readings)} copies on the page)"
    return CheckResult(PROVIDER, heading, status, details, URL)


# ---------------------------------------------------------------------------
# Schedule helpers
# ---------------------------------------------------------------------------


def within_window(now: datetime | None = None) -> bool:
    now = now or datetime.now(TIMEZONE)
    if WEEKDAYS_ONLY and now.weekday() >= 5:
        return False
    return WINDOW_START_HOUR <= now.hour < WINDOW_END_HOUR


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def monitor_loop() -> int:
    state = load_state()
    deadline = time.monotonic() + RUN_DURATION_MINUTES * 60
    consecutive_failures = 0
    checks = 0
    last_error = ""

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            user_agent=USER_AGENT, viewport={"width": 1440, "height": 900}
        )
        page.set_default_timeout(PAGE_LOAD_TIMEOUT_MS)

        try:
            while time.monotonic() < deadline:
                if not within_window():
                    log("Left the monitoring window - stopping.")
                    break

                checks += 1
                try:
                    result = await check_once(page)
                    consecutive_failures = 0
                    log(f"check {checks}: {result.status}")
                    process_result(result, state)
                    save_state(state)

                except MonitorError as exc:
                    consecutive_failures += 1
                    last_error = str(exc)
                    log(f"check {checks}: FAILED ({consecutive_failures}) - {exc}")

                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        log("Too many consecutive failures - alerting and exiting.")
                        try:
                            subject, body = build_failure_email(
                                last_error, consecutive_failures
                            )
                            send_email(subject, body)
                        except Exception as mail_exc:   # noqa: BLE001
                            log(f"Could not send failure email: {mail_exc}")
                        save_state(state)
                        return 1

                except Exception as exc:   # noqa: BLE001
                    # e.g. the availability email failed to send
                    consecutive_failures += 1
                    last_error = f"{type(exc).__name__}: {exc}"
                    log(f"check {checks}: UNEXPECTED ERROR - {last_error}")

                if time.monotonic() + CHECK_INTERVAL_SECONDS >= deadline:
                    break
                await asyncio.sleep(CHECK_INTERVAL_SECONDS)

        finally:
            await browser.close()

    save_state(state)
    log(f"Run complete: {checks} checks.")
    return 0


def main() -> int:
    if not RESEND_API_KEY or not EMAIL_TO:
        log("ERROR: RESEND_API_KEY and EMAIL_TO must be set as secrets.")
        return 1

    now = datetime.now(TIMEZONE)
    log(f"Starting. Local time {now:%Y-%m-%d %H:%M:%S %Z} ({now:%A}).")

    if not within_window(now):
        log(
            f"Outside the window (Mon-Fri {WINDOW_START_HOUR:02d}:00-"
            f"{WINDOW_END_HOUR:02d}:00 local). Exiting cleanly."
        )
        return 0

    log(
        f"Window open. Checking every {CHECK_INTERVAL_SECONDS}s "
        f"for up to {RUN_DURATION_MINUTES} minutes."
    )
    return asyncio.run(monitor_loop())


if __name__ == "__main__":
    sys.exit(main())
