#!/usr/bin/env python3
"""
TECO Melbourne Taiwan-permit auto-booking script (Playwright version).

WHAT THIS DOES AUTOMATICALLY (no human input needed for these steps):
  1. Loads the booking page in a REAL browser (not curl) — required because
     Google reCAPTCHA Enterprise needs actual JS execution to produce a valid
     token; curl/requests-based scripts cannot pass this, ever.
  2. Checks availability across multiple anchor dates (same approach as your
     check_taiwan_permit.sh — default + 30/60/90/120 days out).
  3. The moment a slot is found, selects the FIRST available slot.
  4. Fills every form field: FNAME, LNAME, EMAIL, Q3 (phone), Q10 (visa no.),
     Q9 (travel date).
  5. Solves and verifies the two rotating form questions from their live
     options. A visible reCAPTCHA challenge, if Google presents one, is left
     for a human; the script does not bypass it.
  6. Selects "否" for Q12 (accompanying family).
  7. Ticks the Q8 declaration checkbox.
  8. VERIFIES every field actually holds the right value and the checkbox is
     actually checked BEFORE clicking submit. If anything failed to fill,
     it stops and alerts you instead of submitting incomplete/wrong data.
  9. With AUTOFILL_ENABLED=0, preserves the filled page for verification;
     AUTOFILL_ENABLED=1 allows the audited form to submit.

WHAT THIS CANNOT GUARANTEE:
  - reCAPTCHA Enterprise scores the session invisibly. If Google decides the
    session looks suspicious (which is MORE likely for a fast, fully
    scripted flow than for a human clicking through), it may show a visible
    challenge (image grid) that only a human can solve. This script detects
    that case, pauses, and alerts you so you can solve it manually in the
    same browser window during the detached review hold. It cannot bypass or solve that step — nothing
    legitimately can.

ERROR HANDLING (this is the part that matters most on a visa form):
  - Every fill is verified by reading the field's actual value back out.
  - The declaration checkbox's `checked` state is verified, not assumed.
  - Both bot-check answers are verified as actually selected (not just
    "clicked and hoped").
  - If ANY required field is missing/wrong after filling, the script does
    NOT submit. It stops, prints exactly what's missing, sends an ntfy
    alert, preserves evidence, and holds the browser briefly for intervention.
  - After clicking submit, it explicitly checks for known error banners
    (invalid_answer, unavailable_slot, invalid_selections_intent, etc.)
    and distinguishes "confirmed" vs "rejected" vs "unclear" — it will
    NEVER report success unless it actually finds a confirmation page.
  - A full log of every step + timestamps is written to booking_run.log
    next to this script, so you can audit exactly what happened even if
    you weren't watching.

USAGE:
  1. pip install playwright && playwright install chromium
  2. Fill in the CONFIG block below with your real details.
  3. Run: python3 teco_autobook.py
     (leave it running; it polls in a loop until a slot is found or you Ctrl+C)
"""

import re
import os
import sys
import time
import datetime
import zoneinfo
import threading
import queue
import urllib.request
import fcntl
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ============================== CONFIG ==============================
BASE_URL = "https://tecomel-traveltotaiwan.youcanbook.me/"

ANSWERS = {
    # Configure these through the environment for a local run.  No applicant
    # data is stored in this public source tree.
    "FNAME": os.environ.get("TECO_FNAME", ""),
    "LNAME": os.environ.get("TECO_LNAME", ""),
    "EMAIL": os.environ.get("TECO_EMAIL", ""),
    "Q3": os.environ.get("TECO_PHONE", ""),
    "Q10": os.environ.get("TECO_VISA_GRANT_NO", ""),
    "Q9": os.environ.get("TECO_TRAVEL_DATE", ""),
    "Q12": os.environ.get("TECO_Q12", "否"),
}

POLL_INTERVAL_SECONDS = 2        # how often to re-check availability. Matches
                                  # the ~3s cadence you set in launchd_runner.sh
                                  # (2s sweep + sleep) — now that the 5 anchor
                                  # checks run in parallel in one call instead
                                  # of 5 sequential round-trips, each sweep is
                                  # much faster, so 2s keeps the same rough
                                  # real-world check-to-check timing without
                                  # hammering the API harder than the shell
                                  # script already does.
ANCHOR_DAYS_OUT = [0, 30, 60, 90, 120]

# The observed release window is concentrated around 08:45 Melbourne time
# (past batches at 08:45-08:57 and a further release at 09:19 on 2026-07-31).
# During this window +60d is the primary release range: all 11 historical
# Playwright hits were there. Query it every sweep, and include the default
# near-term cancellation range every fourth sweep. A 2026-08-07 live read-only
# benchmark measured warm +60d sweeps at 249–585 ms and [0,+60d] at 324–871 ms.
# This stagger supports a 0.5 s sleep without increasing average request load
# versus querying both anchors after every 1 s sleep.
# Outside the window, fall back to the full five-anchor sweep.
MELBOURNE_TZ = zoneinfo.ZoneInfo("Australia/Melbourne")
FAST_POLL_START = datetime.time(8, 45)
FAST_POLL_END = datetime.time(9, 30)   # includes the 09:19 release seen on 2026-07-31
FAST_POLL_INTERVAL_SECONDS = 0.5
FAST_ANCHOR_DAYS_OUT = [0, 60]
FAST_PRIMARY_ANCHOR_DAYS_OUT = [60]
FAST_NEAR_TERM_EVERY_N_POLLS = 4
AUTOMATION_WINDOW_END = datetime.time(9, 25)

# Back off when an ENTIRE sweep fails (every anchor errored, e.g. the 403
# block seen in the 2026-08-03 log) instead of continuing to hammer the API
# at full speed, which the live log showed just turns a short block into a
# long string of repeated failures. Backs off 5s -> 10s -> 20s -> 40s
# (capped), resets to 5s the moment a sweep succeeds again, and refreshes
# the polling page after 2 consecutive fully-failed sweeps in case the page
# itself is the thing that's stuck.
FETCH_BACKOFF_SECONDS = [5, 10, 20, 40]

HEARTBEAT_EVERY_N_POLLS = 15     # log a "still alive" line this often, so a
                                  # healthy-but-quiet script is distinguishable
                                  # from a hung one (find_available_slot logs
                                  # nothing on a clean no-slots cycle)

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
HEADLESS = False                 # False = you can watch/intervene if needed
DETACHED_BROWSER_HOLD_SECONDS = 15 * 60

# One external switch controls live submission. The derived name is retained
# for readable logs/tests, but is not a second user-facing setting.
AUTOFILL_ENABLED = os.environ.get("AUTOFILL_ENABLED", "0") == "1"
DRY_RUN_BEFORE_SUBMIT = not AUTOFILL_ENABLED

# No appointment-date cutoff is applied yet. The processing-time estimate has
# not been validated by a real successful appointment, so live mode must not
# silently skip otherwise bookable dates. Set this only after a measured,
# user-confirmed rule exists; None means accept every parseable slot date.
LATEST_ACCEPTABLE_APPOINTMENT_DATE = None

LOG_FILE = Path(__file__).with_name("booking_run.log")
SCREENSHOT_DIR = Path(__file__).with_name("confirmation_screenshots")
FORM_DUMP_DIR = Path(__file__).with_name("form_dumps")
RUN_LOCK_FILE = Path(__file__).with_name(".teco_autobook.lock")
SUCCESS_RECEIPT_FILE = Path(__file__).with_name(".teco_booking_confirmed")

# Known server-side error message fragments (from the app's own translation
# strings, extracted from the live bundle) — used to positively detect a
# REJECTED submission rather than assuming success.
KNOWN_ERROR_MARKERS = [
    "invalid_answer", "One of your answers seems to be invalid",
    "unavailable_slot", "is not available anymore",
    "invalid_selections_intent", "missing information",
    "misconfig", "something's not quite right",
    "There aren't enough units available for the selected time",
    "We weren't able to validate the automatic security check",
    "No start time for the booking selected",
    "booking page is misconfigured",
    "field_required", "is a required field",
    "invalid_email", "invalid_phone", "invalid_date",
    "Please enter a valid email address",
    "Please enter a valid telephone number",
    "Please enter a valid date",
    "錯誤選擇", "選擇錯誤", "預約無法繼續",  # bot-check wrong-answer messages (Chinese)
]
KNOWN_SUCCESS_MARKERS = [
    # NOTE: deliberately does NOT include "Confirm Booking" / "Request booking"
    # — those are the app's own toolbar_title / toolbar_title_request strings,
    # which are visible on the PRE-SUBMIT review screen too (confirmed via the
    # live translation bundle). Including them risked a false-positive
    # "submitted" read while the page was still mid-transition after the
    # click, before actually landing on a real confirmation. Only markers
    # that specifically only make sense AFTER a booking exists are kept.
    # The saved 2026-08-03 YCBM page bundle names the real post-booking
    # screen "Booking confirmed" and describes it as "The booking has been
    # confirmed...". The live Chinese confirmation page observed on
    # 2026-08-07 uses the explicit confirmation-page text below. Keep the
    # older English markers as compatibility fallbacks.
    "Booking confirmed", "The booking has been confirmed",
    "預約確認程序已變更如下",
    "預約確認電郵必須打印並於面交時提交",
]
POST_SUBMIT_OBSERVATION_TIMEOUT_MS = 20_000
POST_SUBMIT_UNCERTAIN_HOLD_MS = DETACHED_BROWSER_HOLD_SECONDS * 1000
SUBMIT_BUTTON_CLICK_TIMEOUT_MS = 5_000
# ======================================================================


def log(msg):
    line = f"{datetime.datetime.now().isoformat()} {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def log_latency(stage, attempt_started):
    """Log one monotonic end-to-end milestone without exposing form values."""
    if attempt_started is None:
        return
    elapsed_ms = round((time.monotonic() - attempt_started) * 1000, 1)
    log(f"LATENCY stage={stage} detected_to_stage_ms={elapsed_ms}")


def acquire_single_instance_lock():
    """Prevent two launch paths from racing the same applicant into duplicates.

    launchd, a manual terminal, and the shell monitor can otherwise all start the
    script at nearly the same time.  flock is held by the returned file object
    for the lifetime of main(); the caller must retain that object.
    """
    lock_handle = open(RUN_LOCK_FILE, "a+", encoding="utf-8")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_handle.close()
        return None
    lock_handle.seek(0)
    lock_handle.truncate()
    lock_handle.write(str(os.getpid()))
    lock_handle.flush()
    return lock_handle


def confirmed_receipt_exists():
    """A local hard stop after this automation has verified one real success."""
    return SUCCESS_RECEIPT_FILE.exists()


def live_submit_armed():
    """Return whether the single user-facing switch permits live submit."""
    return AUTOFILL_ENABLED


def write_confirmed_receipt():
    """Persist a minimal, non-PII success record so later runs cannot rebook."""
    SUCCESS_RECEIPT_FILE.write_text(
        datetime.datetime.now(MELBOURNE_TZ).isoformat() + "\n",
        encoding="utf-8",
    )


def save_confirmation_screenshot(page, tag="confirmation"):
    """Take a full-page screenshot of the CURRENT page and save it to
    SCREENSHOT_DIR (a 'confirmation_screenshots' folder next to this
    script). Called the moment a booking is confirmed, so there is always
    a saved visual record of what actually happened — not just log text.

    Filename includes a timestamp + tag so multiple screenshots never
    overwrite each other (e.g. if you want one for a real success AND one
    for a "status unclear, please check" moment).

    Returns the saved file path (str) on success, or None if it failed —
    logged either way. A screenshot failure is never allowed to block or
    fail the booking flow itself; this is purely a best-effort record.
    """
    try:
        SCREENSHOT_DIR.mkdir(exist_ok=True)
    except Exception as e:
        log(f"Could not create screenshot directory {SCREENSHOT_DIR}: {e}")
        return None

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename = f"{ts}_{tag}.png"
    filepath = SCREENSHOT_DIR / filename
    try:
        page.screenshot(path=str(filepath), full_page=True)
        log(f"📸 Screenshot saved: {filepath}")
        return str(filepath)
    except Exception as e:
        log(f"Screenshot capture failed: {e}")
        return None


def dump_form_html(page, tag="form"):
    """Save the CURRENT page's full HTML + a structured summary of every
    form control, to FORM_DUMP_DIR.

    WHY THIS EXISTS: on 2026-07-31 the script successfully detected slots
    and reached the booking form, but EVERY field fill failed with
    "label not found" — because the selectors were reconstructed from the
    API's JSON schema (`before` text) rather than from the real rendered
    HTML, which evidently labels its fields differently. There was no way
    to verify that without a live slot. This dump makes the next live slot
    self-diagnosing: even a completely failed attempt now captures exactly
    what the real form looks like, so the selectors can be fixed against
    reality instead of guesswork.

    Saves two files per call:
      <ts>_<tag>.html  — the complete rendered DOM
      <ts>_<tag>.txt   — a readable inventory of inputs/labels/buttons,
                          which is usually enough on its own to fix selectors
    Never allowed to raise — diagnostics must not break the booking flow.
    """
    try:
        FORM_DUMP_DIR.mkdir(exist_ok=True)
    except Exception as e:
        log(f"Could not create form dump directory {FORM_DUMP_DIR}: {e}")
        return None

    # Include microseconds because parallel attempts often dump within the
    # same second. Second-only names caused workers to overwrite each other.
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    html_path = FORM_DUMP_DIR / f"{ts}_{tag}.html"
    txt_path = FORM_DUMP_DIR / f"{ts}_{tag}.txt"

    try:
        html = page.content()
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        log(f"🔍 Form HTML dumped: {html_path}")
    except Exception as e:
        log(f"Form HTML dump failed: {e}")

    # A structured, human/AI-readable inventory of every form control —
    # usually far more directly useful than raw HTML for fixing selectors.
    try:
        summary = page.evaluate(
            """() => {
                const out = [];

                const describe = (el) => {
                    const bits = [];
                    bits.push('tag=' + el.tagName.toLowerCase());
                    if (el.type) bits.push('type=' + el.type);
                    if (el.id) bits.push('id=' + el.id);
                    if (el.name) bits.push('name=' + el.name);
                    if (el.placeholder) bits.push('placeholder=' + JSON.stringify(el.placeholder));
                    const aria = el.getAttribute && el.getAttribute('aria-label');
                    if (aria) bits.push('aria-label=' + JSON.stringify(aria));
                    if (el.className && typeof el.className === 'string') {
                        bits.push('class=' + JSON.stringify(el.className.slice(0, 120)));
                    }
                    // Any label element pointing at this control
                    let labelText = '';
                    if (el.id) {
                        const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                        if (lab) labelText = (lab.innerText || '').trim();
                    }
                    if (!labelText) {
                        const wrapLab = el.closest('label');
                        if (wrapLab) labelText = (wrapLab.innerText || '').trim();
                    }
                    if (labelText) bits.push('LABEL=' + JSON.stringify(labelText.slice(0, 200)));
                    // Nearby visible text, which is how this form seems to
                    // associate questions with their controls
                    const container = el.closest('div');
                    if (container) {
                        const t = (container.innerText || '').trim().replace(/\\s+/g, ' ');
                        if (t) bits.push('NEARBY=' + JSON.stringify(t.slice(0, 200)));
                    }
                    return bits.join('  ');
                };

                out.push('===== INPUTS / TEXTAREAS / SELECTS =====');
                document.querySelectorAll('input, textarea, select').forEach((el, i) => {
                    out.push('[' + i + '] ' + describe(el));
                });

                out.push('');
                out.push('===== BUTTONS =====');
                document.querySelectorAll('button').forEach((el, i) => {
                    const t = (el.innerText || '').trim().replace(/\\s+/g, ' ');
                    out.push('[' + i + '] text=' + JSON.stringify(t.slice(0,120)) + '  ' + describe(el));
                });

                out.push('');
                out.push('===== ALL VISIBLE TEXT (for locating question wording) =====');
                out.push((document.body ? document.body.innerText : '').slice(0, 8000));

                return out.join('\\n');
            }"""
        )
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(summary or "")
        log(f"🔍 Form control inventory dumped: {txt_path}")
        return str(txt_path)
    except Exception as e:
        log(f"Form inventory dump failed: {e}")
        return None


def ntfy(title, body):
    if not NTFY_TOPIC:
        return
    try:
        # urllib encodes HTTP header values as latin-1. Keep the rich title
        # in the UTF-8 body, but use an ASCII-safe header so emoji such as
        # "✅" and "❓" cannot make Request construction fail locally.
        safe_title = title.encode("ascii", "ignore").decode("ascii").strip()
        if not safe_title:
            safe_title = "teco_autobook alert"
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode(),
            headers={"Title": safe_title, "Priority": "max", "Tags": "rotating_light"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=6)
    except Exception as e:
        log(f"ntfy send failed: {e}")


def ntfy_async(title, body):
    """Send a best-effort alert without putting network I/O on the race path."""
    if not NTFY_TOPIC:
        return
    threading.Thread(target=ntfy, args=(title, body), daemon=True).start()


def polling_profile(now=None, poll_count=0):
    """Return (anchors, sleep_seconds, profile_name) for the current time,
    in Melbourne local time regardless of what timezone this machine's
    system clock is set to (explicit zoneinfo lookup, not the bare
    datetime.now() the initial version used, which would silently pick the
    wrong window if this ever runs on a non-Melbourne-timezone machine).
    During the observed release window, poll +60d every cycle and add the
    near-term anchor every fourth cycle; otherwise use the full sweep.
    """
    current = (now or datetime.datetime.now(MELBOURNE_TZ)).time()
    if FAST_POLL_START <= current <= FAST_POLL_END:
        anchors = (
            FAST_ANCHOR_DAYS_OUT
            if poll_count % FAST_NEAR_TERM_EVERY_N_POLLS == 0
            else FAST_PRIMARY_ANCHOR_DAYS_OUT
        )
        return anchors, FAST_POLL_INTERVAL_SECONDS, "fast +60d; near-term every 4th sweep"
    return ANCHOR_DAYS_OUT, POLL_INTERVAL_SECONDS, "normal all-anchor"


def find_available_slot(page, anchor_days=None):
    """Check availability across all anchors IN PARALLEL in a single
    page.evaluate() call (Promise.all), mirroring how check_taiwan_permit.sh
    backgrounds its curl calls with `&` instead of looping one-at-a-time.
    This was previously 5 sequential round-trips (default + 4 anchors) —
    now it's 1 round-trip containing 5 concurrent fetches, which is the
    actual lever for speed here (not re-opening the browser — it was
    already being kept open across polls).

    anchor_days: which anchors to check this sweep (defaults to the full
    ANCHOR_DAYS_OUT). Lets polling_profile() request just [60] during the
    known fast-release window without touching the rest of this function.

    Returns (first_slot, days_out, total_slots_in_that_batch, all_failed).
    all_failed is True only when EVERY anchor in this sweep errored (e.g.
    a 403 block) — used by the caller to trigger a backoff instead of
    hammering the API again immediately at full speed."""
    anchors_to_check = anchor_days if anchor_days is not None else ANCHOR_DAYS_OUT
    try:
        results = page.evaluate(
            """async (args) => {
                const [anchorDays, baseUrl] = args;

                // Find the intent ID. Primary source: this page's own network
                // log. BUT after a page relaunch/recovery, a brand-new page has
                // an EMPTY performance log until the site's JS has actually
                // fired its API calls — and goto(domcontentloaded) returns
                // BEFORE that happens. That caused a real bug: after every
                // recovery, polling returned 'no_intent' forever and never
                // recovered, silently killing all slot detection.
                //
                // Fix: fall back to fetching the booking page HTML directly and
                // grepping the intent out of it (same approach as
                // check_taiwan_permit.sh, which is reliable), so a fresh page
                // with no network history still works immediately.
                let intent = null;
                const entries = performance.getEntriesByType('resource').map(e => e.name);
                const m = entries.join('\\n').match(/intents\\/(itt_[a-f0-9-]+)/);
                if (m) {
                    intent = m[1];
                } else {
                    try {
                        const r = await fetch(baseUrl, {cache: 'no-store'});
                        const html = await r.text();
                        const m2 = html.match(/itt_[a-f0-9-]{36}/);
                        if (m2) intent = m2[0];
                    } catch (e) {
                        return anchorDays.map(d => ({days: d, error: 'no_intent_fetch_failed: ' + String(e)}));
                    }
                }
                if (!intent) return anchorDays.map(d => ({days: d, error: 'no_intent'}));

                async function checkAnchor(days) {
                    try {
                        const d = new Date();
                        d.setDate(d.getDate() + days);
                        const anchor = d.toISOString().slice(0,10);
                        const url = days === 0
                            ? `https://api.youcanbook.me/v1/intents/${intent}/availabilitykey`
                            : `https://api.youcanbook.me/v1/intents/${intent}/availabilitykey?startSearchAt=${anchor}`;
                        const kr = await fetch(url, {cache: 'no-store'});
                        const kj = await kr.json();
                        if (!kj.key) return {days, error: 'no_key'};
                        const ar = await fetch(`https://api.youcanbook.me/v1/availabilities/${kj.key}?_=${Date.now()}`, {cache: 'no-store'});
                        const aj = await ar.json();
                        return {days, slots: aj.slots || []};
                    } catch (e) {
                        return {days, error: String(e)};
                    }
                }

                // All anchors fetched concurrently, not one after another.
                return await Promise.all(anchorDays.map(checkAnchor));
            }""",
            [anchors_to_check, BASE_URL],
        )
    except Exception as e:
        log(f"availability check error (all anchors, parallel call): {e}")
        return None, None, 0, True

    error_count = 0
    for r in results:
        days = r.get("days")
        if r.get("error"):
            error_count += 1
            log(f"availability check returned error (anchor +{days}d): {r['error']}")
            continue
        slots = r.get("slots") or []
        if slots:
            log(f"slot(s) found at anchor +{days}d: {slots[:3]}")
            return slots[0], days, len(slots), False
    all_failed = bool(results) and error_count == len(results)
    return None, None, 0, all_failed


def accept_cookies(page):
    """Auto-accept (not reject) the cookie consent banner the moment it
    appears. This matters for automation: the banner sits on top of the
    calendar/form and can visually and functionally block clicks on day
    buttons, time slots, and form fields underneath it. Accept is chosen
    over Reject because the site's own text says declining still only
    affects tracking cookies (essential/security cookies are kept either
    way), so Accept has no functional downside for booking and reliably
    dismisses the overlay.

    USES A SINGLE ATOMIC JS CLICK, deliberately — this is the fix for a
    real bug seen in testing:
        ElementHandle.click: Element is not attached to the DOM
        element is not stable / retrying click action ...
    The previous version did page.query_selector(...) to get an
    ElementHandle, then called .click() on it. That's two separate steps,
    and this site is a React app that re-renders the banner between them —
    so by click time the handle pointed at a DOM node React had already
    replaced. Playwright then burned its full retry budget fighting an
    element that no longer existed, spamming the log and slowing every
    health check (and causing pool top-up workers to fail their sanity
    check, degrading the pool to 2/3).

    Doing the find-and-click as ONE page.evaluate() call inside the browser
    removes the window entirely: there is no gap for a re-render to happen
    between locating the button and clicking it.
    """
    try:
        clicked = page.evaluate(
            """() => {
                // Find a visible "Accept" button and click it in the SAME tick —
                // no handle is ever held across a possible re-render.
                const buttons = Array.from(document.querySelectorAll('button'));
                const target = buttons.find(b => {
                    const t = (b.textContent || '').trim();
                    if (t !== 'Accept') return false;
                    // must actually be visible/clickable
                    const r = b.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                });
                if (!target) return false;
                target.click();
                return true;
            }"""
        )
        if clicked:
            log("Cookie consent banner accepted/dismissed")
            return True
        return False
    except Exception as e:
        log(f"Cookie banner accept attempt failed harmlessly: {e}")
        return False


def accept_cookies_with_quick_retries(page, attempts=3, gap_ms=200):
    """The banner can render slightly after page load (async), so a single
    instant check right after goto() might miss it. This does a FEW fast,
    cheap checks (default: 3 tries, 200ms apart = 600ms max) instead of one
    long blocking Playwright auto-wait — same idea as before, just spread
    across a couple of quick attempts rather than one slow one."""
    for _ in range(attempts):
        if accept_cookies(page):
            return True
        page.wait_for_timeout(gap_ms)
    return False


def goto_and_dismiss_cookies_fast(page, url, max_attempts=3):
    """Navigate to `url` and dismiss the cookie banner as early as possible.

    Previously every call site did page.goto(url, wait_until="networkidle")
    FIRST, and only THEN tried to dismiss the banner. But the banner renders
    early — well before all network activity settles — so the banner was
    sitting on screen, dismissable, the whole time the code was still just
    waiting for goto() to return. That's exactly the delay observed: banner
    appears immediately, but nothing tries to close it until full load.

    Fix: use a much lighter wait_until ("domcontentloaded" — fires once the
    HTML/DOM is parsed, long before all network requests finish) so control
    returns to us fast, then immediately start polling for the Accept button
    in a tight loop. This races the cookie-dismiss against the rest of the
    page still loading, instead of queuing behind it.

    RETRY + VERIFY added after testing showed concurrent tab-pool warm-up
    (3 threads all calling browser.new_page()+goto() at once, right as the
    browser process itself is still stabilising) can leave some pages stuck
    on about:blank — the goto() call apparently not actually landing,
    without raising an exception we were catching. Fix: after attempting
    goto(), explicitly check page.url actually matches the target (not
    about:blank or empty), and retry the whole goto() up to max_attempts
    times if it didn't land. Also broadened the except clause from just
    PWTimeout to Exception, since a dropped concurrent navigation may not
    raise a timeout specifically.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
        except Exception as e:
            log(f"goto() attempt {attempt}/{max_attempts} raised {e} — will verify/retry")

        # VERIFY the navigation actually landed on the real page, not stuck
        # on about:blank (or some other unrelated page) despite goto() not
        # having raised an exception.
        try:
            current_url = page.url
        except Exception:
            current_url = ""

        if current_url and "about:blank" not in current_url and "youcanbook" in current_url:
            break  # landed correctly

        log(f"goto() attempt {attempt}/{max_attempts}: page.url is '{current_url}' "
            f"(expected it to contain the booking domain) — retrying" if attempt < max_attempts
            else f"goto() FAILED to land after {max_attempts} attempts — page.url is '{current_url}'. "
                 f"Proceeding anyway; this tab may be unusable.")
        if attempt < max_attempts:
            page.wait_for_timeout(300)  # brief pause before retrying, in case the browser process was just momentarily busy

    # Poll aggressively right from DOM-ready — banner is usually clickable
    # within the first 1-2 checks at this point, well before networkidle.
    accept_cookies_with_quick_retries(page, attempts=10, gap_ms=100)


def reset_booking_page(page, reason):
    """Discard stale form/intent state and return to a fresh booking page."""
    log(f"RESET_BOOKING_PAGE reason={reason}")
    try:
        goto_and_dismiss_cookies_fast(page, BASE_URL)
        page.wait_for_timeout(250)
        log("RESET_BOOKING_PAGE complete")
        return True
    except Exception as e:
        log(f"RESET_BOOKING_PAGE failed: {e}")
        return False


def slot_dom_targets(slot):
    """Return the live bundle's exact day/slot testids for one API slot.

    The current YCBM bundle renders `day_YYYY-MM-DD` in the booking-page
    timezone and `slot_<ISO UTC timestamp>`.  API v1 returns startsAt as epoch
    milliseconds, while newer responses may already contain ISO text.
    """
    if not isinstance(slot, dict) or slot.get("startsAt") is None:
        return None, None
    raw = str(slot["startsAt"])
    try:
        if raw.isdigit():
            instant = datetime.datetime.fromtimestamp(
                int(raw) / 1000, datetime.timezone.utc)
        else:
            instant = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=datetime.timezone.utc)
        utc = instant.astimezone(datetime.timezone.utc)
        iso = utc.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        day = instant.astimezone(MELBOURNE_TZ).date().isoformat()
        return day, iso
    except (ValueError, OverflowError, OSError):
        return None, None


def slot_is_eligible_for_live_submit(slot):
    """Accept valid slot dates; apply a cutoff only if explicitly configured."""
    day, _ = slot_dom_targets(slot)
    if day is None or LATEST_ACCEPTABLE_APPOINTMENT_DATE is None:
        return day is not None
    return datetime.date.fromisoformat(day) <= LATEST_ACCEPTABLE_APPOINTMENT_DATE


def patch_intent_slot_selection(page, slot, attempt_started=None):
    """Apply the same v1 startsAt PATCH used by the current live bundle.

    Evidence from the 2026-08-06 bundle:
      _onSelectSlot -> tF({startsAt}) -> cM -> kre
      -> PATCH /v1/intents/{id}/selections
    and VC() converts an ISO startsAt to epoch milliseconds for v1.

    This changes only the ephemeral booking intent; it does not confirm or
    create a booking.  The caller must still render, fill, audit and explicitly
    click the final confirm button.  Any non-2xx response fails closed.
    """
    if not isinstance(slot, dict) or slot.get("startsAt") is None:
        return False, None, "missing startsAt"
    try:
        starts_at_ms = int(slot["startsAt"])
    except (TypeError, ValueError):
        try:
            parsed = datetime.datetime.fromisoformat(
                str(slot["startsAt"]).replace("Z", "+00:00"))
            starts_at_ms = int(parsed.timestamp() * 1000)
        except (TypeError, ValueError, OverflowError):
            return False, None, "invalid startsAt"

    started = time.monotonic()
    try:
        result = page.evaluate(
            r"""async (startsAt) => {
                let intent = window.uiStore && window.uiStore.currentIntentId;
                if (!intent) {
                    const resources = performance.getEntriesByType('resource')
                        .map(e => e.name).join('\n');
                    const match = resources.match(/intents\/(itt_[a-f0-9-]+)/);
                    if (match) intent = match[1];
                }
                if (!intent) return {ok: false, why: 'intent id unavailable'};
                try {
                    const response = await fetch(
                        `https://api.youcanbook.me/v1/intents/${intent}/selections`,
                        {
                            method: 'PATCH',
                            cache: 'no-store',
                            credentials: 'include',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({startsAt}),
                        }
                    );
                    const text = await response.text();
                    let responseCode = null;
                    try {
                        const parsed = JSON.parse(text);
                        responseCode = parsed.code || parsed.errorCode || parsed.error?.code || null;
                    } catch (_) {}
                    return {
                        ok: response.ok,
                        status: response.status,
                        intent,
                        responseCode,
                    };
                } catch (error) {
                    return {ok: false, intent, why: String(error)};
                }
            }""",
            starts_at_ms,
        )
    except Exception as e:
        return False, None, str(e)

    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    if not result or not result.get("ok"):
        status = result.get("status") if result else None
        why = result.get("why") if result else "empty result"
        code = result.get("responseCode") if result else None
        log(f"DIRECT_INTENT_SELECTION failed status={status} code={code} elapsed_ms={elapsed_ms} why={why}")
        return False, result.get("intent") if result else None, why or code or f"HTTP {status}"

    log(f"DIRECT_INTENT_SELECTION accepted status={result.get('status')} elapsed_ms={elapsed_ms}")
    log_latency("intent_slot_patched", attempt_started)
    return True, result.get("intent"), None


def navigate_to_intent_form(page, intent_id, attempt_started=None):
    """Open the current bundle's verified `/form?i=<intent>` route.

    Try an in-app history transition first so the pre-warmed React runtime and
    reCAPTCHA context remain intact.  If this deployment does not react to a
    synthetic popstate within 750 ms, fall back to a normal navigation.
    """
    if not intent_id or not re.fullmatch(r"itt_[a-f0-9-]+", intent_id):
        return False
    try:
        spa_started = time.monotonic()
        page.evaluate(
            """(intent) => {
                const url = `/form?i=${encodeURIComponent(intent)}`;
                history.pushState({}, '', url);
                window.dispatchEvent(new PopStateEvent('popstate'));
            }""",
            intent_id,
        )
        deadline = time.monotonic() + 0.75
        while time.monotonic() < deadline:
            route_started = page.evaluate(
                """() => location.pathname === '/form' && !!document.querySelector(
                    '[data-testid="loadingForm"], [data-testid="formContent"]')"""
            )
            if route_started:
                elapsed_ms = round((time.monotonic() - spa_started) * 1000, 1)
                log(f"FORM_ROUTE navigation=in_app elapsed_ms={elapsed_ms}")
                log_latency("form_route_loaded", attempt_started)
                return True
            page.wait_for_timeout(25)

        log("In-app form route did not activate within 750ms; using full navigation fallback.")
        goto_and_dismiss_cookies_fast(page, f"{BASE_URL}form?i={intent_id}")
        elapsed_ms = round((time.monotonic() - spa_started) * 1000, 1)
        log(f"FORM_ROUTE navigation=full elapsed_ms={elapsed_ms}")
        log_latency("form_route_loaded", attempt_started)
        return True
    except Exception as e:
        log(f"Direct form-route navigation failed: {e}")
        return False


def select_detected_slot_fast(page, days_out, slot, attempt_started=None):
    """Select an API-detected slot via the verified direct contract.

    The direct path is the current bundle's own PATCH + /form route and avoids
    waiting for the calendar DOM to catch up with the availability API. Exact
    day/slot DOM selection remains a fail-safe if PATCH or routing fails.
    """
    accepted, intent_id, direct_reason = patch_intent_slot_selection(
        page, slot, attempt_started=attempt_started)
    if accepted and navigate_to_intent_form(
            page, intent_id, attempt_started=attempt_started):
        return True

    log(
        "DIRECT_INTENT_SELECTION route unavailable "
        f"(reason={direct_reason}); falling back to exact calendar UI."
    )
    return select_slot_in_ui(
        page, days_out, slot=slot, attempt_started=attempt_started)


def select_slot_in_ui(page, days_out, slot=None, slot_index=0, attempt_started=None):
    """Navigate the real calendar UI to select an open slot.
    `slot` is the exact availability object returned by the live API.  The
    current site bundle exposes stable `day_...` and `slot_...` testids, so
    that exact slot is preferred over a visual/text guess.  slot_index is only
    a fallback when the exact slot was taken while the UI was updating.

    slot_index picks WHICH time slot on the found day to click — used by the
    concurrent racing logic (race_multiple_slots) so that parallel attempts
    each go for a DIFFERENT time slot in the same batch instead of piling
    onto the same one. Defaults to 0 (first available) for normal single-
    attempt use, unchanged from before.
    Returns True/False, and logs exactly what it did or why it failed.

    SPEED FIX 2026-07-31: this used to unconditionally call
    goto_and_dismiss_cookies_fast() first — a full page reload — EVERY
    time, even though pool workers are already sitting on the calendar
    page. That reload was the actual cause of the observed ~2-3s delay
    between "slot found" and "day selected" (confirmed by timestamps in a
    live run). The browser being open doesn't avoid this cost by itself —
    only skipping the unnecessary reload does.

    Now it only reloads if the page is NOT already a live, responsive
    calendar page (e.g. this is a freshly opened worker, or the previous
    attempt left the page in some other state). If the page is already
    fine, it goes straight to finding an enabled day — no network
    round-trip, no full re-render.
    """
    target_day, target_iso = slot_dom_targets(slot)

    try:
        already_on_calendar = page.evaluate(
            """() => !!document.querySelector('button.avl_dayButton') ||
                     (document.body && document.body.innerText.includes('No Availability'))"""
        )
    except Exception:
        already_on_calendar = False

    if not already_on_calendar:
        log("Page not already on a live calendar — reloading (this is the slow path).")
        goto_and_dismiss_cookies_fast(page, BASE_URL)
    # else: skip the reload entirely — this is the fast path pool workers
    # should normally take, since they're pre-warmed and sitting ready.

    def _poll_until(check_fn, timeout_ms, interval_ms=50):
        """Poll check_fn() every interval_ms until it returns truthy or
        timeout_ms elapses. Returns whatever check_fn() returned (or None).

        This does NOT shorten how long we're willing to wait — timeout_ms
        is the same ceiling the old flat sleep used. It only avoids waiting
        the FULL ceiling when the DOM update already happened faster than
        that: if the real render takes the full 800ms, this waits the full
        800ms too, same as before. It never returns early because time is
        short; it only returns early because the condition is already true."""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            result = check_fn()
            if result:
                return result
            page.wait_for_timeout(interval_ms)
        return None

    for step in range(6):
        if target_day:
            exact_day = page.locator(f'[data-testid="day_{target_day}"]')
            if exact_day.count():
                if not exact_day.first.is_disabled():
                    exact_day.first.click()
                    log(f"selected exact detected day {target_day} (after {step} month-forward steps)")
                    log_latency("day_clicked", attempt_started)
                    break
                # The precise slot disappeared, but another day in this live
                # batch may still be winnable.  Fall through to the first
                # enabled day instead of abandoning the whole attempt.
                log(f"Exact detected day {target_day} is no longer enabled; trying another live day in this batch.")

        enabled_days = page.query_selector_all("button.avl_dayButton:not([disabled])")
        if enabled_days:
            enabled_days[0].click()
            log(f"selected first available day (after {step} month-forward steps)")
            log_latency("day_clicked_fallback", attempt_started)
            break
        next_btn = page.query_selector('button[aria-label="Next Month"]')
        if not next_btn or next_btn.is_disabled():
            log("ERROR: ran out of months to page forward through, no enabled day found")
            return False
        next_btn.click()
        # Poll instead of a blind 800ms sleep — waits the SAME 800ms
        # ceiling if genuinely needed, just doesn't idle past a render
        # that already finished.
        _poll_until(
            lambda: page.query_selector_all("button.avl_dayButton:not([disabled])"),
            timeout_ms=800, interval_ms=50,
        )
    else:
        log("ERROR: exhausted 6 month-forward attempts without finding an enabled day")
        return False

    # Poll for real time-slot buttons to appear after the day click, instead
    # of a blind 800ms sleep. Ceiling raised to 1200ms (higher than the old
    # 800ms) precisely so a genuinely slower render still gets enough time
    # instead of being cut off early — this only saves time when the
    # buttons appear faster than the ceiling, never forces a shorter wait.
    _poll_until(
        lambda: page.evaluate(
            """() => Array.from(document.querySelectorAll('button')).some(b => {
                const t = (b.innerText || '').trim();
                return !b.disabled && /\\b\\d{1,2}:\\d{2}\\s*(AM|PM)?\\b/i.test(t);
            })"""
        ),
        timeout_ms=1200, interval_ms=50,
    )

    # Prefer the exact API slot using the testid proven in the current live
    # bundle (`slot_${startsAt ISO}`).  This prevents the old duration-button
    # false match and avoids selecting a different time merely because React
    # rendered it first.
    if target_iso:
        exact_slot = page.locator(f'[data-testid="slot_{target_iso}"]')
        try:
            exact_slot.first.wait_for(state="visible", timeout=250)
        except Exception:
            pass
        if exact_slot.count() and exact_slot.first.get_attribute("aria-disabled") != "true":
            chosen_text = exact_slot.first.inner_text().strip()
            exact_slot.first.click()
            log(f"selected exact detected time slot: {chosen_text} ({target_iso})")
            log_latency("slot_clicked", attempt_started)
            return True

        log(f"Exact detected time {target_iso} was not clickable; falling back to another live time in the batch.")

    # Find real TIME SLOT buttons only.
    #
    # SPEED NOTE: the 800ms wait above is a genuine dependency (waiting for
    # React to render the time-slot buttons after the day click), kept as
    # a simple flat wait rather than converted to a poll — the two together
    # (reload-skip + this) already address the bulk of the 2-3s observed
    # delay; the render wait itself is small relative to the reload that
    # was removed.
    #
    # BUG FIXED 2026-07-31: the previous selector was
    #   '[data-testid="time-slot-button"], button[aria-label*=":"]'
    # and the aria-label part matched the wrong element — the booking
    # DURATION button has aria-label "Booking duration: 20 minutes", which
    # contains a colon, so the script clicked "20 minutes" instead of an
    # actual time. The live log showed exactly this:
    #   selected time slot at index 0: 20 minutes
    # Now we require the button's own visible text to look like a clock
    # time (e.g. "9:30 AM", "11:15", "13:30"), which the duration button
    # never does.
    time_buttons = page.evaluate_handle(
        """() => {
            const looksLikeTime = (s) => /\\b\\d{1,2}:\\d{2}\\s*(AM|PM)?\\b/i.test((s || '').trim());
            const all = Array.from(document.querySelectorAll('button'));
            // Prefer an explicit testid if the site ever provides one.
            const byTestId = all.filter(b => b.getAttribute('data-testid') === 'time-slot-button');
            const candidates = byTestId.length ? byTestId
                : all.filter(b => looksLikeTime(b.innerText) && !b.disabled);
            return candidates;
        }"""
    )
    try:
        count = page.evaluate("(els) => els.length", time_buttons)
    except Exception:
        count = 0

    if not count:
        log("ERROR: day selected but no real time-slot buttons found (none whose text looks like a clock time)")
        return False

    idx = slot_index if slot_index < count else 0
    if idx != slot_index:
        log(f"Requested slot_index {slot_index} out of range ({count} available) — falling back to index 0")

    chosen_text = page.evaluate(
        "([els, i]) => { const el = els[i]; const t = (el.innerText||'').trim(); el.click(); return t; }",
        [time_buttons, idx],
    )
    log(f"selected time slot at index {idx}: {chosen_text}")
    log_latency("slot_clicked_fallback", attempt_started)
    return True


def fill_by_label(page, label_substr, value, alt_labels=None):
    """Find a form field associated with `label_substr` and fill it.

    REWRITTEN 2026-07-31 after a live run where EVERY field failed with
    "label not found". The old version required Playwright's
    `text=<exact substring>` locator to match, then walked up to the
    nearest div/label ancestor. That's brittle: it depends on the label
    text appearing as its own text node in a predictable ancestor
    relationship to the input. The real form evidently doesn't lay out
    that way.

    This version does the search INSIDE the browser in one pass, trying
    several independent strategies per field, in order:
      1. <label for=...> whose text contains the target
      2. an ancestor <label> wrapping the input
      3. aria-label / aria-labelledby / placeholder / name / id containing it
      4. nearest preceding text in the same container block
      5. any container whose text contains it, then the first input inside
    It also accepts `alt_labels` — alternative wordings to try, since the
    API's schema text and the rendered label may differ.

    Returns True only if the value actually landed in the field.
    """
    candidates = [label_substr] + list(alt_labels or [])
    try:
        ok = page.evaluate(
            """([labels, value]) => {
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const matches = (text) => {
                    const t = norm(text);
                    if (!t) return false;
                    return labels.some(l => t.includes(l));
                };

                const fillable = (el) => {
                    if (!el || (el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA')) return false;
                    if (el.disabled || el.readOnly || ['hidden', 'checkbox', 'radio', 'submit', 'button']
                            .includes(el.type)) return false;
                    if ((el.name || '').toLowerCase() === 'g-recaptcha-response' ||
                        (el.id || '').toLowerCase().includes('g-recaptcha-response')) return false;
                    const style = getComputedStyle(el);
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                           el.getClientRects().length > 0;
                };

                const setValue = (el) => {
                    // Use the native setter so React/Vue register the change.
                    const proto = el.tagName === 'TEXTAREA'
                        ? window.HTMLTextAreaElement.prototype
                        : window.HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                    setter.call(el, value);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                    return norm(el.value) === norm(value);
                };

                // 1. <label for="...">
                for (const lab of document.querySelectorAll('label')) {
                    if (!matches(lab.innerText)) continue;
                    const forId = lab.getAttribute('for');
                    if (forId) {
                        const el = document.getElementById(forId);
                        if (fillable(el)) return setValue(el);
                    }
                    // 2. label wrapping the input
                    const inner = lab.querySelector('input, textarea');
                    if (fillable(inner)) return setValue(inner);
                }

                // 3. attributes on the input itself
                for (const el of document.querySelectorAll('input, textarea')) {
                    if (!fillable(el)) continue;
                    const attrs = [
                        el.getAttribute('aria-label'),
                        el.getAttribute('placeholder'),
                        el.getAttribute('name'),
                        el.getAttribute('id'),
                        el.getAttribute('title'),
                    ];
                    const labelledBy = el.getAttribute('aria-labelledby');
                    if (labelledBy) {
                        labelledBy.split(/\\s+/).forEach(id => {
                            const n = document.getElementById(id);
                            if (n) attrs.push(n.innerText);
                        });
                    }
                    if (attrs.some(a => matches(a))) return setValue(el);
                }

                // 4/5. container-based: find the smallest block containing the
                // label text, then the first fillable input within it.
                const blocks = Array.from(document.querySelectorAll('div, section, fieldset, li, p'));
                // smallest-first so we pick the tightest wrapper, not <body>
                blocks.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
                for (const b of blocks) {
                    if (!matches(b.innerText)) continue;
                    const el = b.querySelector('input, textarea');
                    if (fillable(el)) return setValue(el);
                }

                return false;
            }""",
            [candidates, value],
        )
    except Exception as e:
        log(f"FILL FAILED (exception) for '{label_substr}': {e}")
        return False

    if not ok:
        log(f"FILL FAILED (no matching field found) for '{label_substr}' (also tried: {alt_labels or []})")
        return False

    # RE-VERIFY after a short delay: the fill above was confirmed at the
    # instant it was set, but per observed behaviour on React/Vue-driven
    # forms, the framework can finish an async init/re-render shortly after
    # and silently wipe out a value that was written before it was ready —
    # so "it matched right when I set it" does NOT guarantee it still holds
    # a moment later. This catches that case instead of reporting a false
    # success. If the value was wiped, retry the fill once more (the
    # re-render has likely settled by now) before giving up.
    page.wait_for_timeout(150)
    try:
        still_ok = page.evaluate(
            """([labels, value]) => {
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const matches = (text) => {
                    const t = norm(text);
                    return !!t && labels.some(l => t.includes(l));
                };
                for (const lab of document.querySelectorAll('label')) {
                    if (!matches(lab.innerText)) continue;
                    const forId = lab.getAttribute('for');
                    const el = forId ? document.getElementById(forId) : lab.querySelector('input, textarea');
                    if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) {
                        return norm(el.value) === norm(value);
                    }
                }
                for (const el of document.querySelectorAll('input, textarea')) {
                    const attrs = [el.getAttribute('aria-label'), el.getAttribute('placeholder'),
                                   el.getAttribute('name'), el.getAttribute('id'), el.getAttribute('title')];
                    if (attrs.some(a => matches(a))) return norm(el.value) === norm(value);
                }
                return null;  // couldn't relocate the field at all — don't claim failure, caller keeps prior ok
            }""",
            [candidates, value],
        )
    except Exception:
        still_ok = None

    if still_ok is False:
        log(f"FILL LOST after re-render for '{label_substr}' — value was wiped post-fill; retrying once")
        try:
            ok2 = page.evaluate(
                """([labels, value]) => {
                    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const matches = (text) => { const t = norm(text); return !!t && labels.some(l => t.includes(l)); };
                    const setValue = (el) => {
                        const proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
                        Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        el.dispatchEvent(new Event('blur', { bubbles: true }));
                        return norm(el.value) === norm(value);
                    };
                    for (const lab of document.querySelectorAll('label')) {
                        if (!matches(lab.innerText)) continue;
                        const forId = lab.getAttribute('for');
                        const el = forId ? document.getElementById(forId) : lab.querySelector('input, textarea');
                        if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) return setValue(el);
                    }
                    return false;
                }""",
                [candidates, value],
            )
        except Exception:
            ok2 = False
        if not ok2:
            log(f"FILL FAILED (re-render wiped it, retry also failed) for '{label_substr}'")
            return False
        log(f"FILL recovered after re-render for '{label_substr}'")
    return True


def fill_required_text_fields_batch(page):
    """Fill the six simple required answers in one browser round-trip.

    The current live bundle wraps every question in `data-testid=<code>`.
    Writing all six controlled inputs atomically and doing one shared 150 ms
    stability check removes five redundant wait periods from the success path.
    Any field that is absent or wiped is returned as False and the caller uses
    the slower, label-based retry for that field only.
    """
    values = {code: ANSWERS[code] for code in
              ("FNAME", "LNAME", "EMAIL", "Q3", "Q10", "Q9")}
    try:
        immediate = page.evaluate(
            r"""(values) => {
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
                const dateKey = (s) => {
                    const v = norm(s);
                    let m = v.match(/^(\d{2})\/(\d{2})\/(\d{4})$/);
                    if (m) return `${m[3]}-${m[2]}-${m[1]}`;
                    m = v.match(/^(\d{4})-(\d{2})-(\d{2})$/);
                    return m ? `${m[1]}-${m[2]}-${m[3]}` : v;
                };
                const matches = (code, actual, expected) => {
                    if (code === 'Q3') return String(actual || '').replace(/\D/g, '') === String(expected || '').replace(/\D/g, '');
                    if (code === 'Q9') return dateKey(actual) === dateKey(expected);
                    return norm(actual) === norm(expected);
                };
                const result = {};
                for (const [code, value] of Object.entries(values)) {
                    const root = document.querySelector(`[data-testid="${CSS.escape(code)}"]`);
                    const selector = 'input:not([type="hidden"]), textarea';
                    const el = root && (root.matches(selector) ? root : root.querySelector(selector));
                    if (!el || el.disabled || el.readOnly) {
                        result[code] = false;
                        continue;
                    }
                    const proto = el.tagName === 'TEXTAREA'
                        ? window.HTMLTextAreaElement.prototype
                        : window.HTMLInputElement.prototype;
                    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.dispatchEvent(new Event('blur', {bubbles: true}));
                    result[code] = matches(code, el.value, value);
                }
                return result;
            }""",
            values,
        )
        page.wait_for_timeout(150)
        stable = page.evaluate(
            r"""(values) => {
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
                const dateKey = (s) => {
                    const v = norm(s);
                    let m = v.match(/^(\d{2})\/(\d{2})\/(\d{4})$/);
                    if (m) return `${m[3]}-${m[2]}-${m[1]}`;
                    m = v.match(/^(\d{4})-(\d{2})-(\d{2})$/);
                    return m ? `${m[1]}-${m[2]}-${m[3]}` : v;
                };
                const matches = (code, actual, expected) => {
                    if (code === 'Q3') return String(actual || '').replace(/\D/g, '') === String(expected || '').replace(/\D/g, '');
                    if (code === 'Q9') return dateKey(actual) === dateKey(expected);
                    return norm(actual) === norm(expected);
                };
                const result = {};
                for (const [code, value] of Object.entries(values)) {
                    const root = document.querySelector(`[data-testid="${CSS.escape(code)}"]`);
                    const selector = 'input:not([type="hidden"]), textarea';
                    const el = root && (root.matches(selector) ? root : root.querySelector(selector));
                    let ok = !!el && matches(code, el.value, value);
                    // The live Q9 DATE control consistently mounts after the
                    // other nine controls, but within this shared 150 ms
                    // stability window. Fill it here if it was absent during
                    // the first pass; the later ten-field audit still verifies
                    // that React retained the value before submission.
                    if (!ok && code === 'Q9' && el && !el.disabled && !el.readOnly) {
                        const proto = el.tagName === 'TEXTAREA'
                            ? window.HTMLTextAreaElement.prototype
                            : window.HTMLInputElement.prototype;
                        Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        el.dispatchEvent(new Event('blur', {bubbles: true}));
                        ok = matches(code, el.value, value);
                    }
                    result[code] = ok;
                }
                return result;
            }""",
            values,
        )
    except Exception as e:
        log(f"BATCH_TEXT_FILL failed as a whole; using per-field fallbacks: {e}")
        return {code: False for code in values}

    status = {
        code: bool(stable.get(code) and (immediate.get(code) or code == "Q9"))
        for code in values
    }
    log("BATCH_TEXT_FILL " + " ".join(f"{code}={int(ok)}" for code, ok in status.items()))
    return status


def fill_phone_field(page, national_number):
    """Fill Q3 (澳洲手機號 / phone).

    The form's country-code selector defaults to +61, so the text input
    itself takes ONLY the national number, exactly as given —
    "0461475750", with the leading 0. Deliberately does NOT paste
    "+61461475750", which would double the country code and fail the app's
    own phone parser (cT() / typeIsMobile in the bundle).

    REWRITTEN 2026-07-31 to route through the corrected fill_by_label(),
    which resolves <label for=...> -> input id (the structure the app
    actually uses). The previous ancestor-walking approach failed on every
    field in the live run.
    """
    ok = fill_by_label(
        page, "手機號", national_number,
        alt_labels=["澳洲手機號", "Phone number", "phone", "手機", "電話"],
    )
    if not ok:
        log("PHONE FILL FAILED: could not locate/fill the phone field")
        return False

    # Check for the app's own inline validation error near the phone field.
    try:
        has_error = page.evaluate(
            """() => {
                const t = (document.body ? document.body.innerText : '');
                return t.includes('valid telephone') || t.includes('invalid_phone');
            }"""
        )
    except Exception:
        has_error = False

    if has_error:
        log("PHONE FILL FAILED: field filled but the page shows a phone validation error")
        return False

    log(f"Phone field filled successfully with '{national_number}'")
    return True


def fill_date_field(page, date_str):
    """Fill Q9 (預計入台旅遊日期 / planned travel date).

    CONFIRMED 2026-07-31 from a real filled form (a competing bot's
    screenshot showed "31/12/2026" sitting in this field with an inline
    clear (x) button): this behaves as a TEXT-style input that accepts a
    typed DD/MM/YYYY value directly. So a plain fill is the right primary
    strategy, and the elaborate calendar-clicking fallback that was here
    before is unnecessary complexity.

    Uses the corrected fill_by_label() (label[for] -> input id), which is
    the structure the app actually renders.

    The caller's final DOM audit treats this as required, matching the saved
    live YCBM schema and its declaration that booking details cannot be
    changed after completion.
    """
    ok = fill_by_label(
        page, "入台旅遊日期", date_str,
        alt_labels=["預計入台旅遊日期", "旅遊日期", "入台日期", "travel date"],
    )
    if ok:
        log(f"Date field filled with '{date_str}'")
        return True
    log(f"DATE FILL FAILED: could not locate/fill the travel-date field with '{date_str}'")
    return False



def select_dropdown_option(page, label_substr, value, alt_labels=None):
    """Set a MULTI_DROPDOWN question to `value`.

    REWRITTEN 2026-07-31, same root cause as fill_by_label: the old version
    used a `text=<label>` locator then `following::select[1]`, which failed
    in the live run ("DROPDOWN FAILED (label not found)").

    Confirmed from the app bundle: these ARE real <select> elements —
        s.jsx("select", {id: l, ref: this.selectRef, className: V9.hideSelect, ...})
    — visually hidden/restyled, but genuine <select>s carrying an `id` that
    a sibling <label for=...> points at. So the reliable approach is the
    same as for text inputs: resolve label -> for/id -> element, then set
    the value natively and fire input/change so React picks it up.

    Returns True only if the select's value actually ended up as `value`.
    """
    candidates = [label_substr] + list(alt_labels or [])
    try:
        result = page.evaluate(
            """([labels, value]) => {
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const matches = (text) => {
                    const t = norm(text);
                    return !!t && labels.some(l => t.includes(l));
                };

                const setSelect = (sel) => {
                    // find the option whose text (or value) equals/contains the target
                    let opt = Array.from(sel.options).find(o => norm(o.textContent) === norm(value));
                    if (!opt) opt = Array.from(sel.options).find(o => norm(o.value) === norm(value));
                    if (!opt) opt = Array.from(sel.options).find(o => norm(o.textContent).includes(norm(value)));
                    if (!opt) return {ok: false, why: 'option not found', options: Array.from(sel.options).map(o => norm(o.textContent))};

                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLSelectElement.prototype, 'value').set;
                    setter.call(sel, opt.value);
                    sel.dispatchEvent(new Event('input', { bubbles: true }));
                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                    const ok = norm(sel.options[sel.selectedIndex]?.textContent) === norm(opt.textContent);
                    return {ok, why: ok ? 'set' : 'value did not stick'};
                };

                // 1. <label for> -> select
                for (const lab of document.querySelectorAll('label')) {
                    if (!matches(lab.innerText)) continue;
                    const forId = lab.getAttribute('for');
                    if (forId) {
                        const el = document.getElementById(forId);
                        if (el && el.tagName === 'SELECT') return setSelect(el);
                    }
                    const inner = lab.querySelector('select');
                    if (inner) return setSelect(inner);
                }

                // 2. smallest container mentioning the label, then its select
                const blocks = Array.from(document.querySelectorAll('div, section, fieldset, li, p'))
                    .filter(el => matches(el.innerText))
                    .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
                for (const blk of blocks) {
                    const sel = blk.querySelector('select');
                    if (sel) return setSelect(sel);
                }

                // 3. any select that actually offers this exact option
                for (const sel of document.querySelectorAll('select')) {
                    if (Array.from(sel.options).some(o => norm(o.textContent) === norm(value))) {
                        return setSelect(sel);
                    }
                }

                return {ok: false, why: 'no matching select found'};
            }""",
            [candidates, value],
        )
    except Exception as e:
        log(f"DROPDOWN FAILED (exception) for '{label_substr}': {e}")
        return False

    if result and result.get("ok"):
        return True
    log(f"DROPDOWN FAILED for '{label_substr}' (wanted '{value}'): {result.get('why') if result else 'unknown'}"
        + (f" — available options: {result.get('options')}" if result and result.get("options") else ""))
    return False


def solve_bot_question(question_text, options):
    """Best-effort programmatic solver. Returns None (never guesses) if unsure."""
    m = re.search(r"(\d+)\s*(減|加|乘|除)\s*(\d+)", question_text)
    if m:
        a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
        if op == "除" and b == 0:
            return None
        result = {"減": a - b, "加": a + b, "乘": a * b, "除": a // b if b else None}[op]
        result_str = str(result)
        if result_str in options:
            return result_str
        log(f"BOT-Q computed answer '{result_str}' not among offered options {options} — refusing to guess")
        return None

    AU_CITIES = {"雪梨", "墨爾本", "布里斯本", "伯斯", "阿德萊德", "坎培拉", "達爾文", "荷伯特"}
    matches = [o for o in options if o in AU_CITIES]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        log(f"BOT-Q ambiguous: multiple AU-city matches {matches} — refusing to guess")
        return None

    log(f"BOT-Q unrecognised question type: '{question_text[:80]}' options={options} — refusing to guess")
    return None


def answer_bot_questions(page):
    """Find every bot-check question, read its real options, solve it, and
    set the answer. Returns list of {question, answer, ok} results.

    REWRITTEN from the 2026-08-03 live evidence. The previous implementation
    fell back from a select's direct label to ANY ancestor containing the bot
    marker. Because the whole form contains Q11/Q14, that classified Q12's
    ordinary 是/否 select (and sometimes the whole 申請前須知 block) as a bot
    question. The live log proved it with:
        BOT-Q computed answer '57' not among offered options ['是', '否']

    A candidate is now accepted only when ITS OWN directly-associated label
    contains the marker. The saved live YCBM schema contains exactly two such
    controls (Q11 arithmetic and Q14 Australian city), so any count other than
    exactly two is a blocking discovery failure rather than a silent pass.

    The solving itself still happens in Python (solve_bot_question), which
    deliberately refuses to guess when unsure — that behaviour is unchanged
    and is what prevents a wrong answer hard-failing the booking.
    """
    BOT_MARKER = "為了證明您不是機器人"

    # Step 1: pull every candidate question + its options straight from the DOM.
    try:
        found = page.evaluate(
            """(marker) => {
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const out = [];
                document.querySelectorAll('select').forEach((sel, idx) => {
                    // Only the select's OWN label is authoritative. Never walk
                    // up to the form container: it also contains Q11/Q14 and
                    // caused Q12 to be misclassified in the live run.
                    let qtext = '';
                    if (sel.id) {
                        const lab = document.querySelector('label[for="' + CSS.escape(sel.id) + '"]');
                        if (lab) qtext = norm(lab.innerText);
                    }
                    if (!qtext) {
                        const wrappingLabel = sel.closest('label');
                        if (wrappingLabel) qtext = norm(wrappingLabel.innerText);
                    }
                    if (!qtext.includes(marker)) return;  // not a bot question
                    out.push({
                        index: idx,
                        selectId: sel.id || null,
                        question: qtext,
                        options: Array.from(sel.options)
                                      .map(o => norm(o.textContent))
                                      .filter(t => t.length > 0),
                    });
                });
                return out;
            }""",
            BOT_MARKER,
        )
    except Exception as e:
        log(f"BOT-Q discovery failed: {e}")
        return []

    if len(found) != 2:
        log(f"BOT-Q discovery expected exactly 2 directly-labelled questions, found {len(found)}")
        return [{
            "question": f"expected exactly 2 directly-labelled bot questions, found {len(found)}",
            "answer": None,
            "ok": False,
            "select_id": None,
        }]

    results = []
    for q in found:
        question_text = q.get("question", "")
        options = q.get("options", [])
        answer = solve_bot_question(question_text, options)

        ok = False
        if answer:
            try:
                set_result = page.evaluate(
                    """([selectId, idx, value]) => {
                        const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                        const sel = selectId
                            ? document.getElementById(selectId)
                            : document.querySelectorAll('select')[idx];
                        if (!sel) return {ok: false, why: 'select disappeared'};
                        const opt = Array.from(sel.options)
                            .find(o => norm(o.textContent) === norm(value));
                        if (!opt) return {ok: false, why: 'option not found'};
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLSelectElement.prototype, 'value').set;
                        setter.call(sel, opt.value);
                        sel.dispatchEvent(new Event('input', { bubbles: true }));
                        sel.dispatchEvent(new Event('change', { bubbles: true }));
                        const now = norm(sel.options[sel.selectedIndex]?.textContent);
                        return {ok: now === norm(value), now};
                    }""",
                    [q.get("selectId"), q["index"], answer],
                )
                ok = bool(set_result and set_result.get("ok"))
                if not ok:
                    log(f"BOT-Q verification failed: wanted '{answer}', control shows "
                        f"'{set_result.get('now') if set_result else '?'}' ({set_result.get('why') if set_result else ''})")
                else:
                    log(f"BOT-Q answered: '{question_text[:60]}' -> '{answer}'")
            except Exception as e:
                log(f"BOT-Q selection exception: {e}")

        results.append({
            "question": question_text[:80],
            "answer": answer,
            "ok": ok,
            "select_id": q.get("selectId"),
        })
    return results


def tick_declaration(page):
    """Tick the required declaration checkbox (Q8) and VERIFY it's checked.

    REWRITTEN 2026-07-31: the previous version required a Playwright
    `text=聲明` locator to match, then walked to an ancestor div. In the
    live run this failed every time ("'聲明' label not found"). This
    version works inside the browser and tries several strategies, and —
    importantly — falls back to ticking the *required* checkbox even if the
    declaration wording can't be located, since this form has exactly one
    required checkbox (Q8) and possibly one optional SMS opt-in.
    """
    try:
        result = page.evaluate(
            """() => {
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const boxes = Array.from(document.querySelectorAll('input[type="checkbox"]'))
                                   .filter(b => !b.disabled);
                if (!boxes.length) return {ok: false, why: 'no checkboxes on page'};

                const tick = (b) => {
                    if (!b.checked) {
                        b.click();
                        if (!b.checked) {
                            // Fallback for controlled components that ignore .click()
                            const setter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'checked').set;
                            setter.call(b, true);
                            b.dispatchEvent(new Event('click', { bubbles: true }));
                            b.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    }
                    return b.checked;
                };

                const DECL_WORDS = ['聲明', '我聲明', '本人聲明', 'declaration', 'declare'];
                const looksDecl = (t) => {
                    const s = norm(t).toLowerCase();
                    return DECL_WORDS.some(w => s.includes(w.toLowerCase()));
                };

                // 1. checkbox whose own label / wrapper mentions the declaration
                for (const b of boxes) {
                    const wrapLab = b.closest('label');
                    if (wrapLab && looksDecl(wrapLab.innerText)) {
                        if (tick(b)) return {ok: true, why: 'matched wrapping label'};
                    }
                    if (b.id) {
                        const lab = document.querySelector('label[for="' + CSS.escape(b.id) + '"]');
                        if (lab && looksDecl(lab.innerText)) {
                            if (tick(b)) return {ok: true, why: 'matched label[for]'};
                        }
                    }
                    const aria = b.getAttribute('aria-label');
                    if (looksDecl(aria)) {
                        if (tick(b)) return {ok: true, why: 'matched aria-label'};
                    }
                }

                // 2. checkbox inside the smallest container mentioning it
                const blocks = Array.from(document.querySelectorAll('div, section, fieldset, li, p'))
                    .filter(el => looksDecl(el.innerText))
                    .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
                for (const blk of blocks) {
                    const b = blk.querySelector('input[type="checkbox"]:not([disabled])');
                    if (b && tick(b)) return {ok: true, why: 'matched containing block'};
                }

                // 3. Fallback: a REQUIRED checkbox (this form has exactly one —
                // Q8 — plus possibly an optional SMS opt-in, so "required"
                // uniquely identifies the declaration).
                const required = boxes.filter(b => b.required || b.getAttribute('aria-required') === 'true');
                if (required.length === 1 && tick(required[0])) {
                    return {ok: true, why: 'ticked the single required checkbox (declaration by elimination)'};
                }

                // 4. Last resort: if there's exactly ONE checkbox total, it must be it.
                if (boxes.length === 1 && tick(boxes[0])) {
                    return {ok: true, why: 'only one checkbox on the page'};
                }

                return {ok: false, why: 'found ' + boxes.length + ' checkbox(es) but none identifiable as the declaration'};
            }"""
        )
    except Exception as e:
        log(f"DECLARATION FAILED (exception): {e}")
        return False

    if result and result.get("ok"):
        log(f"Declaration checkbox verified checked ({result.get('why')})")
        return True
    log(f"DECLARATION FAILED: {result.get('why') if result else 'unknown'}")
    return False


def check_page_for_errors(page):
    """After clicking submit, look for known error text. Returns error string or None."""
    try:
        body_text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        return "could_not_read_page"
    for marker in KNOWN_ERROR_MARKERS:
        if marker in body_text:
            return marker
    return None


def check_page_for_success(page):
    try:
        body_text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        return False
    body_text = body_text.casefold()
    return any(marker.casefold() in body_text for marker in KNOWN_SUCCESS_MARKERS)


def visible_captcha_challenge(page):
    """Return True only for a visible reCAPTCHA challenge iframe.

    The always-present invisible badge uses an `anchor` iframe. Google's
    interactive challenge uses `bframe`, so checking the visible iframe itself
    avoids treating the normal badge as a human-blocking CAPTCHA.
    """
    try:
        frames = page.locator('iframe[src*="recaptcha"][src*="bframe"]')
        return any(frames.nth(i).is_visible() for i in range(frames.count()))
    except Exception:
        return False


def wait_for_post_submit_outcome(page, timeout_ms, return_on_captcha=True,
                                 poll_interval_ms=100):
    """Poll the live DOM after the irreversible final click.

    Returns `(outcome, detail)` where outcome is `submitted`, `rejected`,
    `blocked_captcha`, or `unclear_after_submit`. A temporarily unreadable page
    during navigation is not classified as rejection. This function never
    clicks anything and therefore remains safe during a slow server response.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if check_page_for_success(page):
            return "submitted", None

        error_marker = check_page_for_errors(page)
        if error_marker and error_marker != "could_not_read_page":
            return "rejected", error_marker

        if return_on_captcha and visible_captcha_challenge(page):
            return "blocked_captcha", "visible recaptcha bframe"

        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        if remaining_ms:
            page.wait_for_timeout(min(poll_interval_ms, remaining_ms))
    return "unclear_after_submit", None


def pre_submit_audit(page, bot_results):
    """Re-read every required answer from the rendered DOM in ONE browser call.

    This is deliberately independent of the earlier fill return values: a React
    re-render can wipe or move a value after a successful fill. The saved live
    YCBM schema identifies ten required answers (FNAME, LNAME, EMAIL, Q3, Q10,
    Q9, Q12, Q8, Q11, Q14). All ten must match before submission.

    The single page.evaluate keeps the success-path latency to one Playwright
    round-trip. Exceptions and missing controls fail closed. No personal values
    are written to the log; only boolean field results and elapsed time are.
    """
    started = time.monotonic()
    expected_bots = {}
    for result in bot_results:
        if not result.get("ok") or not result.get("answer"):
            continue
        question = result.get("question", "")
        if re.search(r"\d+\s*(?:減|加|乘|除)\s*\d+", question):
            expected_bots["Q11"] = result["answer"]
        elif "澳洲城市" in question:
            expected_bots["Q14"] = result["answer"]

    try:
        observed = page.evaluate(
            r"""([answers, expectedBots, marker]) => {
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
                const digits = (s) => (s || '').replace(/\D/g, '');
                const labels = Array.from(document.querySelectorAll('label'));

                const controlForQuestion = (code, fragments, selector) => {
                    // Current live bundle: every question owns a stable
                    // data-testid equal to its schema code.  Labels remain a
                    // compatibility fallback for saved/older DOMs.
                    const root = document.querySelector(`[data-testid="${CSS.escape(code)}"]`);
                    const byCode = root && (root.matches(selector) ? root : root.querySelector(selector));
                    if (byCode) return byCode;
                    for (const lab of labels) {
                        const text = norm(lab.innerText);
                        if (!fragments.some(f => text.includes(f))) continue;
                        const forId = lab.getAttribute('for');
                        const byFor = forId ? document.getElementById(forId) : null;
                        if (byFor && byFor.matches(selector)) return byFor;
                        const wrapped = lab.querySelector(selector);
                        if (wrapped) return wrapped;
                    }
                    return null;
                };
                const valueMatches = (code, fragments, expected) => {
                    const el = controlForQuestion(code, fragments, 'input, textarea');
                    return !!el && norm(el.value) === norm(expected);
                };
                const selectedText = (sel) => {
                    if (!sel || sel.tagName !== 'SELECT' || sel.selectedIndex < 0) return '';
                    return norm(sel.options[sel.selectedIndex]?.textContent);
                };
                const dateKey = (s) => {
                    const v = norm(s);
                    let m = v.match(/^(\d{2})\/(\d{2})\/(\d{4})$/);
                    if (m) return `${m[3]}-${m[2]}-${m[1]}`;
                    m = v.match(/^(\d{4})-(\d{2})-(\d{2})$/);
                    return m ? `${m[1]}-${m[2]}-${m[3]}` : v;
                };

                const phone = controlForQuestion('Q3', ['澳洲手機號 Phone number', '澳洲手機號'], 'input, textarea');
                const date = controlForQuestion('Q9', ['預計入台旅遊日期'], 'input, textarea');
                const family = controlForQuestion('Q12', ['是否有同行親屬申請人員'], 'select');
                const declaration = controlForQuestion('Q8', ['聲明'], 'input[type="checkbox"]');

                const botControls = [];
                for (const lab of labels) {
                    const text = norm(lab.innerText);
                    if (!text.includes(marker)) continue;
                    const forId = lab.getAttribute('for');
                    const sel = forId ? document.getElementById(forId) : lab.querySelector('select');
                    if (sel && sel.tagName === 'SELECT') botControls.push({text, sel});
                }
                const botValue = (code) => {
                    const item = botControls.find(({text}) =>
                        code === 'Q11' ? /\d+\s*(減|加|乘|除)\s*\d+/.test(text)
                                       : text.includes('澳洲城市'));
                    return item ? selectedText(item.sel) : '';
                };

                return {
                    FNAME: valueMatches('FNAME', ['申請人護照中文姓名'], answers.FNAME),
                    LNAME: valueMatches('LNAME', ['申請人護照英文全名'], answers.LNAME),
                    EMAIL: valueMatches('EMAIL', ['有效 Email'], answers.EMAIL),
                    Q3: !!phone && digits(phone.value) === digits(answers.Q3),
                    Q10: valueMatches('Q10', ['澳洲簽證號碼 Visa Grant No.', '澳洲簽證號碼'], answers.Q10),
                    Q9: !!date && dateKey(date.value) === dateKey(answers.Q9),
                    Q12: selectedText(family) === norm(answers.Q12),
                    Q8: !!declaration && declaration.checked === true,
                    Q11: botControls.length === 2 && !!expectedBots.Q11 &&
                         botValue('Q11') === norm(expectedBots.Q11),
                    Q14: botControls.length === 2 && !!expectedBots.Q14 &&
                         botValue('Q14') === norm(expectedBots.Q14),
                    bot_count: botControls.length,
                };
            }""",
            [ANSWERS, expected_bots, "為了證明您不是機器人"],
        )
    except Exception as e:
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        log(f"PRE_SUBMIT_AUDIT elapsed_ms={elapsed_ms} result=BLOCKED error={e}")
        return False, {key: False for key in
                       ("FNAME", "LNAME", "EMAIL", "Q3", "Q10", "Q9", "Q12", "Q8", "Q11", "Q14")}

    fields = ("FNAME", "LNAME", "EMAIL", "Q3", "Q10", "Q9", "Q12", "Q8", "Q11", "Q14")
    status = {key: bool(observed and observed.get(key)) for key in fields}
    passed = all(status.values())
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    bits = " ".join(f"{key}={int(status[key])}" for key in fields)
    bot_count = observed.get("bot_count", 0) if observed else 0
    log(f"PRE_SUBMIT_AUDIT elapsed_ms={elapsed_ms} {bits} bot_count={bot_count} "
        f"result={'PASS' if passed else 'BLOCKED'}")
    return passed, status


def wait_for_booking_form(page, timeout_ms=15000, attempt_started=None):
    """Wait for the React booking form, not merely the confirmation route.

    YouCanBookMe updates the title and booking summary before mounting the
    question controls. The previous fixed 500 ms delay routinely observed
    only the duration button and hidden reCAPTCHA textarea, then exhausted
    every fill retry against that incomplete DOM.

    IMPORTANT: this must NOT be shortened to chase a latency target. The
    morning failures were caused by filling fields BEFORE the form had
    actually rendered (every field selector failed because the inputs
    weren't in the DOM yet) — the fix for that is waiting exactly as long
    as it genuinely takes, not less. This function already does the right
    thing: it's a POLL that returns the moment >=5 real controls exist
    (normally within a few hundred ms), so the 15s ceiling is only a safety
    cap for a truly broken page — it is not the expected/typical wait, and
    shortening it would reintroduce the exact bug it was written to fix.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    last_state = None
    while time.monotonic() < deadline:
        try:
            state = page.evaluate(
                """() => {
                    const controls = Array.from(document.querySelectorAll('input, textarea, select'))
                        .filter(el => {
                            if (el.disabled || el.type === 'hidden') return false;
                            if ((el.name || '').toLowerCase() === 'g-recaptcha-response' ||
                                (el.id || '').toLowerCase().includes('g-recaptcha-response')) return false;
                            return true;
                        });
                    const text = document.body ? document.body.innerText : '';
                    const error = ["time is not available", "something's not quite right",
                                   "booking page is misconfigured", "403 ERROR"]
                                  .find(marker => text.includes(marker)) || null;
                    const loadingForm = !!document.querySelector('[data-testid="loadingForm"]');
                    const formContent = !!document.querySelector('[data-testid="formContent"]');
                    return {count: controls.length, loadingForm, formContent, error,
                            readyState: document.readyState};
                }"""
            )
            last_state = state
            # `formContent` is the current live bundle's own boundary between
            # loadingForm and the fully rendered question form.  Keep the
            # control-count fallback for saved fixtures/older bundle versions,
            # but make it strict enough to represent this profile's ten
            # required controls rather than the old, unsafe >=5 heuristic.
            bundle_ready = state.get("formContent") and not state.get("loadingForm")
            legacy_ready = state.get("count", 0) >= 10 and not state.get("loadingForm")
            if bundle_ready or legacy_ready:
                log(f"Booking form ready with {state['count']} usable control(s).")
                log_latency("form_ready", attempt_started)
                return True
            if state.get("error"):
                log(f"Booking form stopped on an error state: {state['error']}")
                return False
        except Exception as e:
            last_state = {"exception": str(e)}
        page.wait_for_timeout(200)

    log(f"Booking form did not render within {timeout_ms} ms; last state={last_state}")
    dump_form_html(page, tag="form_render_timeout")
    return False


def fill_form_and_submit(page, abort_check=None, attempt_started=None,
                         capture_diagnostic=False):
    """Fill and verify the booking form, then hand off human verification.

    abort_check: optional callable, checked IMMEDIATELY BEFORE the actual
    submit click. If it returns True, this attempt aborts WITHOUT clicking
    submit — used by race_multiple_slots() so that once ANY parallel
    attempt wins, every other concurrent attempt bails out right before
    their own submit, instead of also submitting and creating a duplicate
    booking under the same identity. This is the one hard safety property
    that makes concurrent racing acceptable: at most one attempt is ever
    allowed to actually click Confirm.
    The saved live YCBM schema marks all applicant fields, Q11/Q14, Q12 and
    Q8 as required, and Q8 states that personal information cannot be changed
    after completion. Consequently there are no "soft" applicant fields and
    no positional/non-empty fallback: the final single-call DOM audit must
    confirm all ten answers exactly before the submit button can be clicked.

    Returns one of: 'submitted', 'dry_run_ready', 'blocked_incomplete',
    'blocked_captcha', 'rejected', 'unclear_after_submit', 'error'.
    """
    # Always give the form the FULL render-wait budget — never shorten it
    # to chase a submit-latency target. Filling before the form has
    # actually rendered is exactly what caused every field to fail on the
    # morning run (fields simply weren't in the DOM yet). This wait is
    # already a poll that returns as soon as the form is ready (usually a
    # few hundred ms), so it costs nothing extra in the common case and
    # only takes longer when the page genuinely needs longer.
    if not wait_for_booking_form(page, attempt_started=attempt_started):
        ntfy("⚠️ Booking form did not load",
             "A slot was selected, but YouCanBookMe never rendered the form controls. Browser left open for review.")
        return "error"

    # A full DOM/control dump costs about 120 ms on the real page. Keep it for
    # explicit diagnostics, but do not put disk I/O ahead of a live submit.
    # Every failure path below still captures the page before moving on.
    if capture_diagnostic:
        dump_form_html(page, tag="booking_form")

    def fill_with_retries(fill_fn, label, attempts=2):
        """Try a fill up to `attempts` times. Returns True if it verified.

        Retry gap kept short (50ms) since wait_for_booking_form() has
        already confirmed the form's controls exist before this runs — a
        fill failure here is a selector/label mismatch, not a rendering
        race, so there's no reason for a long backoff. Deliberately does
        NOT shed retries based on any latency deadline: giving up early on
        a field that just needs one more retry is the same class of bug as
        filling before the form rendered — trading correctness for speed
        where it isn't actually necessary.
        """
        for i in range(attempts):
            if fill_fn():
                return True
            log(f"fill retry {i+1}/{attempts} for '{label}'")
            page.wait_for_timeout(50)
        return False

    # Fast path proven against the current bundle's per-question data-testid
    # wrappers: six simple fields, one DOM call, one shared stability wait.
    # Each False entry falls back to the older label-based retry independently.
    batch = fill_required_text_fields_batch(page)

    email_ok = batch["EMAIL"] or fill_with_retries(
        lambda: fill_by_label(page, "Email", ANSWERS["EMAIL"],
                              alt_labels=["有效 Email", "有效Email", "email", "E-mail", "電子郵件", "郵箱"]),
        "Email", attempts=3)
    fname_ok = batch["FNAME"] or fill_with_retries(
        lambda: fill_by_label(page, "護照中文姓名", ANSWERS["FNAME"],
                              alt_labels=["申請人護照中文姓名", "中文姓名", "繁體中文", "姓名"]),
        "FNAME")
    lname_ok = batch["LNAME"] or fill_with_retries(
        lambda: fill_by_label(page, "護照英文全名", ANSWERS["LNAME"],
                              alt_labels=["申請人護照英文全名", "英文全名", "英文姓名", "護照英文"]),
        "LNAME")
    phone_ok = batch["Q3"] or fill_with_retries(
        lambda: fill_phone_field(page, ANSWERS["Q3"]), "Q3 phone")
    visa_ok = batch["Q10"] or fill_with_retries(
        lambda: fill_by_label(page, "簽證號碼", ANSWERS["Q10"],
                              alt_labels=["澳洲簽證號碼", "Visa Grant", "Visa Grant No", "簽證"]),
        "Q10 visa")
    date_ok = batch["Q9"] or fill_with_retries(
        lambda: fill_date_field(page, ANSWERS["Q9"]), "Q9 date")
    family_ok = fill_with_retries(lambda: select_dropdown_option(page, "同行親屬", ANSWERS["Q12"]), "Q12 family")

    # Anti-bot questions: the 2026-08-03 live log proves the answer solver
    # itself works for Q11 and Q14. Discovery is now restricted to each
    # select's direct label so Q12's 是/否 control cannot be swept in via the
    # common form ancestor.
    bot_results = answer_bot_questions(page)

    # Declaration checkbox, retried and then independently re-read by the
    # all-field audit below.
    decl_ok = fill_with_retries(lambda: tick_declaration(page), "Q8 declaration", attempts=3)

    initial_fill_status = {
        "FNAME": fname_ok, "LNAME": lname_ok, "EMAIL": email_ok,
        "Q3": phone_ok, "Q10": visa_ok, "Q9": date_ok,
        "Q12": family_ok, "Q8": decl_ok,
    }
    log("INITIAL_FILL " + " ".join(
        f"{key}={int(bool(ok))}" for key, ok in initial_fill_status.items()))

    # Fail closed on the ACTUAL rendered state, not on the fill functions'
    # earlier return values. The site's saved Q8 declaration explicitly says
    # personal information cannot be changed after booking, so a value in the
    # wrong field is not an acceptable fallback.
    audit_passed, audit_status = pre_submit_audit(page, bot_results)
    if not audit_passed:
        missing = [key for key, ok in audit_status.items() if not ok]
        log("SUBMIT BLOCKED — pre-submit DOM audit failed: " + ", ".join(missing))
        dump_form_html(page, tag="pre_submit_audit_failed")
        save_confirmation_screenshot(page, tag="pre_submit_audit_failed")
        ntfy("⚠️ Booking blocked — needs you NOW",
             "Slot selected, but the final DOM audit failed for: " + ", ".join(missing))
        return "blocked_incomplete"

    log_latency("form_filled_and_audited", attempt_started)

    if not live_submit_armed():
        reason = "AUTOFILL_ENABLED=0"
        log(f"DRY RUN PASS — all ten DOM checks passed; stopping before final submit ({reason}).")
        save_confirmation_screenshot(page, tag="dry_run_audit_passed")
        ntfy("🧪 Booking dry-run passed",
             "All ten form checks passed on the real page. Final submit was intentionally not clicked; browser left open.")
        return "dry_run_ready"

    log("Critical checks passed. Submitting immediately.")
    # The current live bundle gives the actual form action a stable testid.
    # Role/text remains a compatibility fallback for an older deployment.
    submit_btn = page.locator('[data-testid="confirm_button"]')
    if submit_btn.count() == 0:
        submit_btn = page.get_by_role("button", name=re.compile("Request Booking|Confirm Booking|Confirm"))
    if submit_btn.count() == 0:
        log("SUBMIT FAILED: no submit button found")
        ntfy("⚠️ Could not find submit button", "Form filled but no submit button was found. Please finish manually.")
        return "error"

    # --- CRITICAL SAFETY GATE for concurrent racing ---
    # Checked as late as possible (right before the actual click) to
    # minimise the race window. If another concurrent attempt has already
    # won, abort here WITHOUT clicking submit — never let two attempts
    # both reach the click.
    if abort_check is not None and abort_check():
        log("ABORTING before submit: another concurrent attempt already won this race.")
        return "aborted_lost_race"

    if confirmed_receipt_exists():
        log("ABORTING before submit: a prior confirmed-booking receipt exists.")
        return "aborted_duplicate_guard"

    try:
        submit_btn.first.click(timeout=SUBMIT_BUTTON_CLICK_TIMEOUT_MS)
    except PWTimeout as e:
        log(f"SUBMIT CLICK TIMEOUT after {SUBMIT_BUTTON_CLICK_TIMEOUT_MS}ms: {e}")
        dump_form_html(page, tag="submit_button_timeout")
        save_confirmation_screenshot(page, tag="submit_button_timeout")
        return "error"
    except Exception as e:
        log(f"SUBMIT CLICK FAILED before click: {e}")
        dump_form_html(page, tag="submit_button_error")
        save_confirmation_screenshot(page, tag="submit_button_error")
        return "error"
    log_latency("final_submit_clicked", attempt_started)
    outcome, detail = wait_for_post_submit_outcome(
        page, POST_SUBMIT_OBSERVATION_TIMEOUT_MS)

    if outcome == "submitted":
        log("SUBMIT CONFIRMED: success marker found on page")
        return "submitted"

    if outcome == "rejected":
        log(f"SUBMIT REJECTED by server: matched error marker '{detail}'")
        ntfy("❌ Booking rejected", f"Server returned an error ('{detail}'). Browser is open — please review and fix manually.")
        return "rejected"

    # Once the final click has happened, CAPTCHA and unclear states are not
    # safe to retry: the first booking may still complete asynchronously.
    # Alert immediately, keep observing the SAME page, and never start another
    # submission unless a positive rejection is seen.
    if outcome == "blocked_captcha":
        log("Visible CAPTCHA challenge appeared after submit click; suspending all new attempts.")
        save_confirmation_screenshot(page, tag="captcha_waiting")
        ntfy("🔴 CAPTCHA needs you",
             "A visible challenge appeared. The browser is being held and no further booking attempt will be made.")
    else:
        log("SUBMIT UNCLEAR after initial observation; suspending all new attempts.")
        save_confirmation_screenshot(page, tag="unclear_status")
        ntfy("❓ Booking status unclear",
             "Final submit was clicked but no confirmation or rejection is visible. No further attempt will be made; check the browser/email.")

    later_outcome, later_detail = wait_for_post_submit_outcome(
        page,
        POST_SUBMIT_UNCERTAIN_HOLD_MS,
        return_on_captcha=False,
        poll_interval_ms=500,
    )
    if later_outcome == "submitted":
        log("SUBMIT CONFIRMED during extended post-click observation.")
        return "submitted"
    if later_outcome == "rejected":
        log(f"SUBMIT REJECTED during extended observation: '{later_detail}'")
        return "rejected"
    return outcome


def _wait_for_human(prompt):
    """input() blocks forever with no visible prompt if this process was
    launched detached (nohup/launchd, no attached terminal) — which is
    exactly how launchd launches this script. In that case, keep the visible
    browser open for a bounded review period, then return so the process cannot
    block future runs forever. Only block on input() for an interactive terminal.
    """
    if sys.stdin is not None and sys.stdin.isatty():
        input(prompt)
    else:
        log("(no interactive terminal attached — browser held open for "
            f"{DETACHED_BROWSER_HOLD_SECONDS}s; ntfy alert sent)")
        # Keep the browser available briefly for intervention/review, then
        # close it so a detached launchd process cannot block all future days.
        # Screenshots and logs remain after this window.
        try:
            deadline = time.monotonic() + DETACHED_BROWSER_HOLD_SECONDS
            while time.monotonic() < deadline:
                time.sleep(min(5, deadline - time.monotonic()))
        except KeyboardInterrupt:
            pass


def _is_page_alive(page):
    """Check whether the page/browser is still usable. Detects the exact
    failure mode seen in testing: the browser window gets closed (by the
    user, or a crash) while the script is mid-poll, and page.evaluate() then
    fails with 'Target page, context or browser has been closed' — forever,
    on every single poll, without the script noticing anything is wrong.
    """
    try:
        page.evaluate("1")
        return True
    except Exception:
        return False


def _relaunch_fast(browser, context_holder, p):
    """Recover from a closed page as cheaply as possible.

    IMPORTANT DISTINCTION this function relies on: closing a PAGE/TAB (what
    happens when you click the little red window-close button on the
    Chromium window) is NOT the same as the underlying BROWSER PROCESS
    dying. Playwright can keep a browser process alive with zero open pages.
    The previous version always did browser.close() + chromium.launch()
    on ANY page-closed detection — a full OS process cold-start every time,
    which is where most of the observed ~7s came from (process launch alone
    is typically 1.5-3s, on top of close() teardown + a real network
    round-trip for goto()).

    Fast path (the common case — you just closed the window): if the
    browser PROCESS is still alive (browser.is_connected()), just open a
    NEW PAGE in it — no process cold-start at all. This should cut the
    dominant cost out entirely for the exact scenario you tested (closing
    the Chromium window while the script runs).

    Slow path (rare — the whole browser process crashed/was killed, not
    just the window): only then does it fall back to a full relaunch.
    """
    if browser.is_connected():
        try:
            page = browser.new_page()
            goto_and_dismiss_cookies_fast(page, BASE_URL)
            log("Fast recovery: reused existing browser process, opened a new page (no process cold-start).")
            return browser, page
        except Exception as e:
            log(f"Fast recovery (new page in existing browser) failed: {e} — falling back to full relaunch.")

    # Slow path: the browser process itself is gone, must cold-start.
    try:
        browser.close()
    except Exception:
        pass
    browser = p.chromium.launch(headless=HEADLESS)
    page = browser.new_page()
    goto_and_dismiss_cookies_fast(page, BASE_URL)
    log("Full relaunch: browser process itself was gone, started a new one (this is the slow path).")
    return browser, page


# ===================== CONCURRENT RACING (multiple slots, same batch) =====================
#
# Purpose: when a batch releases SEVERAL slots at once (observed: 4-8 per
# batch), attempt MULTIPLE of them in parallel — each on its own browser
# page/context — so a competitor grabbing "your" specific slot doesn't cost
# you the whole batch. The moment ANY attempt reaches a real, confirmed
# submission, every other concurrent attempt is signalled to abort BEFORE
# clicking submit.
#
# HARD SAFETY RULE (why this is not "book multiple slots"): only ONE
# attempt is ever allowed past the final submit click. This matters because
# the declaration explicitly states duplicate applications get ALL bookings
# cancelled ("我沒有其他入台證申請預約，如重複申請將被取消所有預約") — so
# racing must produce at most one real submission, never more. The
# abort_check hook in fill_form_and_submit() is what enforces this: each worker
# must atomically claim the single allowed submit BEFORE the irreversible
# click. Setting an event only after confirmation was too late — several
# workers could already have clicked while the first server response was in
# flight.
#
# This is intentionally simple (Python threads + Playwright's sync API,
# each thread owns its own page) rather than asyncio, since the rest of
# this script already uses the sync Playwright API throughout.

_race_winner_lock = threading.Lock()


def claim_single_race_submit(submit_claimed_event):
    """Atomically grant exactly one race worker permission to click submit."""
    with _race_winner_lock:
        if submit_claimed_event.is_set():
            return False
        submit_claimed_event.set()
        return True


def _attempt_one_slot(days_out, slot_index, winner_event, results, idx, pool=None, worker=None):
    """Runs the ENTIRE attempt (select slot, fill, submit) as ONE callable
    submitted to a TabWorker via worker.run() — so all Playwright calls for
    this attempt happen on that worker's own dedicated thread, never
    crossing threads. If no pooled worker was available, creates a fresh
    dedicated TabWorker just for this attempt (slower path: pays its own
    browser-launch cost, but still safe — never shares a browser across
    threads, which was the actual bug found via testing).
    """
    used_pooled_worker = worker is not None
    if worker is None:
        log(f"[race #{idx}] no pooled worker available — starting a fresh dedicated one (slower path).")
        worker = TabWorker(worker_id=f"race{idx}")
        worker.start()
        if worker._page is None:
            log(f"[race #{idx}] could not start a fresh worker/browser.")
            results[idx] = "error"
            return

    def _do_attempt(page):
        if not select_slot_in_ui(page, days_out, slot_index=slot_index):
            log(f"[race #{idx}] could not select slot_index {slot_index} — dropping out of the race")
            return "error"

        def abort_check():
            # fill_form_and_submit calls this once, immediately before click.
            # True means another worker already owns the one allowed submit.
            return not claim_single_race_submit(winner_event)

        outcome = fill_form_and_submit(page, abort_check=abort_check)

        if outcome == "submitted":
            log(f"[race #{idx}] WON the one submit claim — booking confirmed.")
            save_confirmation_screenshot(page, tag=f"race_winner_{idx}")
        return outcome

    outcome = worker.run(_do_attempt, timeout=45)
    if outcome is None:
        outcome = "error"
    results[idx] = outcome

    if not used_pooled_worker:
        # Fresh dedicated worker for this attempt only — shut it down
        # unless it won (leave the winning browser open for the human).
        if outcome != "submitted":
            worker.shutdown()
    elif pool is not None:
        pool.return_worker(worker, won=(outcome == "submitted"))


def race_multiple_slots(days_out, num_slots_available, max_parallel=3, pool=None):
    """Attempt up to `max_parallel` DIFFERENT slots from the same batch
    concurrently. Returns the winning outcome string if any thread
    succeeded, else the most informative non-success outcome.

    num_slots_available caps how many distinct slot_index values actually
    exist to race for (no point racing 3 threads if the batch only has 1
    slot) — race_count = min(max_parallel, num_slots_available).

    If `pool` (a TabPool) is provided, pre-warmed tabs are checked out and
    used instead of opening fresh ones — this is the whole point of the
    pool: no page-load latency in the critical path of the race.
    """
    race_count = max(1, min(max_parallel, num_slots_available))
    log(f"Racing {race_count} concurrent attempt(s) across this batch of {num_slots_available} slot(s).")

    pooled_workers = pool.checkout(race_count) if pool is not None else []
    if pool is not None:
        log(f"Tab pool: checked out {len(pooled_workers)}/{race_count} pre-warmed worker(s) for this race.")

    winner_event = threading.Event()
    results = [None] * race_count
    threads = []
    for i in range(race_count):
        pooled_worker = pooled_workers[i] if i < len(pooled_workers) else None
        t = threading.Thread(target=_attempt_one_slot,
                              args=(days_out, i, winner_event, results, i),
                              kwargs={"pool": pool, "worker": pooled_worker})
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=50)  # safety timeout so a hung thread can't block forever
                             # (worker.run() itself already has a 45s internal
                             # timeout, so this outer one is just a backstop)

    if pool is not None:
        pool.top_up()  # replace any workers that got shut down rather than returned

    if winner_event.is_set():
        for r in results:
            if r == "submitted":
                return "submitted"
        # A worker did click but did not positively confirm success. Never let
        # another worker submit in this batch; propagate a fail-closed state.
        log("A race worker claimed/clicked submit but no confirmation was proven; no second click allowed.")
        for preferred in ("blocked_captcha", "unclear_after_submit", "rejected", "error"):
            if preferred in results:
                return preferred
        return "unclear_after_submit"

    # No thread won. Report the most useful outcome for the caller to log/alert on.
    for preferred in ("blocked_captcha", "unclear_after_submit", "rejected",
                      "blocked_incomplete", "error", "aborted_lost_race"):
        if preferred in results:
            return preferred
    return "error"


# ===================== PRE-WARMED TAB POOL =====================
#
# Purpose: race_multiple_slots() used to open brand-new tabs (browser.new_page
# + goto + cookie-dismiss) ONLY at the moment a multi-slot batch was
# detected — putting a real network round-trip and page-load right in the
# critical path of a race that can be won or lost in seconds. This pool
# opens N tabs UP FRONT at script startup (well before 8:45am, thanks to
# launchd_runner.sh's pre-warm), navigates each one, dismisses cookies, and
# pre-fills the STATIC fields (name/visa/date/family — the ones that don't
# change between attempts) — so that when a batch is actually detected,
# each pooled tab only needs to: select the slot, answer the (fresh,
# per-intent) bot questions, tick the declaration, and submit. No page-load
# latency left in the critical path at all.
#
# What is deliberately NOT pre-filled/cached, and why:
#   - Bot-check questions (Q11/Q14): rotate PER INTENT. A fresh goto() gets
#     a fresh itt_... intent, and the question text/options are tied to
#     that specific intent — reusing an old answer risks submitting a WRONG
#     answer, which hard-fails per the form's own blockingConditions. These
#     are always re-read and re-solved fresh, every single attempt.
#   - The declaration checkbox and submit click: always fresh per attempt,
#     never assumed carried over from a previous use of the tab.
#
# After a pooled tab is used in a failed/lost-race attempt, it is refreshed
# (fresh goto + cookie-dismiss + static-field re-fill) and returned to the
# pool for the NEXT batch, rather than being discarded — so the pool stays
# ready across multiple batches during the window, not just the first one.

class TabWorker:
    """Owns ONE Playwright instance + ONE browser + ONE page, all created
    and used EXCLUSIVELY from a single dedicated background thread.

    THIS EXISTS TO FIX A REAL BUG found via live testing: Playwright's sync
    API is NOT thread-safe for sharing one Browser object across multiple
    Python threads — doing so raises "Cannot switch to a different thread"
    (a greenlet/event-loop error), which is exactly what caused all 3
    pooled tabs to fail and get stuck on about:blank during testing. The
    previous TabPool design launched ONE shared browser and had multiple
    THREADS call browser.new_page() concurrently — that's the broken
    pattern. Playwright's own documented guidance for multi-threaded sync
    usage is: each thread must have its OWN sync_playwright() + browser,
    never share one across threads.

    Design: a worker thread runs a simple loop, pulling callables off an
    input queue and executing them (with its own page as the argument),
    putting the result on an output queue. All Playwright calls for this
    worker's page happen ON the worker's own thread, never from outside —
    which is what makes this safe.
    """

    def __init__(self, worker_id):
        self.worker_id = worker_id
        self._task_q = queue.Queue()
        self._result_q = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._ready = threading.Event()
        self._page = None  # only ever touched from inside self._run's thread

    def start(self):
        self._thread.start()
        self._ready.wait(timeout=30)

    def _run(self):
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=HEADLESS)
                page = browser.new_page()
                self._page = page
                self._ready.set()
            except Exception as e:
                log(f"[worker {self.worker_id}] failed to launch its own browser: {e}")
                self._ready.set()
                return

            while True:
                fn = self._task_q.get()
                if fn is None:  # shutdown signal
                    try:
                        browser.close()
                    except Exception:
                        pass
                    return
                try:
                    result = fn(page)
                except Exception as e:
                    result = ("__worker_exception__", str(e))
                self._result_q.put(result)

    def run(self, fn, timeout=60):
        """Submit a callable (taking `page` as its only argument) to run ON
        this worker's own thread/page, and block for its result. This is
        the ONLY way outside code should interact with this worker's page —
        never touch self._page directly from another thread."""
        self._task_q.put(fn)
        try:
            result = self._result_q.get(timeout=timeout)
        except queue.Empty:
            log(f"[worker {self.worker_id}] task timed out after {timeout}s")
            return None
        if isinstance(result, tuple) and len(result) == 2 and result[0] == "__worker_exception__":
            log(f"[worker {self.worker_id}] task raised: {result[1]}")
            return None
        return result

    def shutdown(self):
        self._task_q.put(None)


class TabPool:
    """Manages a pool of `size` TabWorker instances — each with its own
    independent Playwright/browser/page, avoiding the cross-thread sharing
    bug described in TabWorker's docstring. Pre-warms them (navigate +
    dismiss cookies) concurrently at startup, checks them out for races,
    and returns/refreshes them for reuse across batches."""

    def __init__(self, size=3):
        self.size = size
        self.pool = []          # list of ready TabWorker objects
        self.lock = threading.Lock()

    def _prepare_worker(self, worker):
        """Navigate + dismiss cookies, executed ON the worker's own thread
        via worker.run(), never touching its page from outside."""
        worker.run(lambda page: goto_and_dismiss_cookies_fast(page, BASE_URL))

    def _is_worker_clean(self, worker):
        """Verify a worker's page is genuinely ready for reuse.

        Uses a SINGLE page.evaluate() doing all checks inside the browser,
        rather than several separate query_selector() round-trips. Same
        reasoning as accept_cookies(): this site re-renders (React), and
        holding element handles across separate calls caused real
        "Element is not attached to the DOM" failures — which in turn made
        pool top-up workers fail their sanity check and left the pool
        degraded (observed live: "2/3 worker(s) ready").
        """
        def _check(page):
            if not _is_page_alive(page):
                return {"ok": False, "why": "page/browser not alive"}
            try:
                # Dismiss any leftover banner first (atomic JS click).
                accept_cookies(page)
                detail = page.evaluate(
                    """() => {
                        const challenge = document.querySelector('iframe[src*="recaptcha"][src*="bframe"]');
                        if (challenge) {
                            const r = challenge.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) {
                                return {ok: false, why: 'leftover visible CAPTCHA challenge iframe'};
                            }
                        }
                        const hasCalendar = !!document.querySelector('button.avl_dayButton');
                        const bodyText = document.body ? document.body.innerText : '';
                        const hasNoAvail = bodyText.indexOf('No Availability') !== -1;
                        if (hasCalendar || hasNoAvail) return {ok: true, why: 'calendar or No Availability present'};
                        return {
                            ok: false,
                            why: 'neither calendar nor No Availability found',
                            url: location.href,
                            bodySnippet: bodyText.slice(0, 200),
                            readyState: document.readyState,
                        };
                    }"""
                )
                return detail
            except Exception as e:
                return {"ok": False, "why": f"exception during check: {e}"}
        result = worker.run(_check)
        if isinstance(result, dict) and not result.get("ok"):
            log(f"Tab pool sanity check FAILED: {result.get('why')}"
                + (f" | url={result.get('url')}" if result.get('url') else "")
                + (f" | body='{result.get('bodySnippet')}'" if result.get('bodySnippet') else "")
                + (f" | readyState={result.get('readyState')}" if result.get('readyState') else ""))
        return bool(result and result.get("ok"))

    def warm_up(self):
        """Start `size` independent workers CONCURRENTLY (each is already
        its own thread by construction — starting them all and letting each
        do its own launch+navigate+verify is safe now, unlike the old
        shared-browser design, because there is no cross-thread Playwright
        access anywhere in this version)."""
        log(f"Warming up a pool of {self.size} independent tab worker(s) ahead of the booking window...")

        def _warm_one(i):
            worker = TabWorker(worker_id=i)
            worker.start()
            if worker._page is None:
                log(f"Tab pool: worker #{i} failed to launch its own browser at all.")
                return
            self._prepare_worker(worker)
            if self._is_worker_clean(worker):
                with self.lock:
                    self.pool.append(worker)
                log(f"Tab pool: worker #{i} ready and VERIFIED clean (own independent browser).")
            else:
                log(f"Tab pool: worker #{i} failed sanity check after warm-up — retrying once.")
                self._prepare_worker(worker)
                if self._is_worker_clean(worker):
                    with self.lock:
                        self.pool.append(worker)
                    log(f"Tab pool: worker #{i} ready on retry.")
                else:
                    log(f"Tab pool: worker #{i} failed sanity check AGAIN — giving up on this slot, pool runs under-size.")
                    worker.shutdown()

        threads = [threading.Thread(target=_warm_one, args=(i,)) for i in range(self.size)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=40)

        log(f"Tab pool warm-up complete: {len(self.pool)}/{self.size} worker(s) ready and verified.")

    def checkout(self, count):
        with self.lock:
            taken = self.pool[:count]
            self.pool = self.pool[count:]
        return taken

    def return_worker(self, worker, won):
        """Refresh and return a used worker to the pool, UNLESS it won (in
        which case it's left alone so the confirmation page stays visible)."""
        if won:
            log("Tab pool: winning worker left running/open (not returned) so the confirmation stays visible.")
            return
        self._prepare_worker(worker)
        if self._is_worker_clean(worker):
            with self.lock:
                self.pool.append(worker)
            log("Tab pool: a used worker was refreshed, VERIFIED clean, and returned to the pool.")
        else:
            log("Tab pool: a used worker failed its post-refresh sanity check — shutting it down. top_up() will replace it.")
            worker.shutdown()

    def top_up(self):
        missing = self.size - len(self.pool)
        if missing <= 0:
            return

        def _replace_one():
            worker = TabWorker(worker_id="topup")
            worker.start()
            if worker._page is None:
                # BUG FIXED 2026-08: this used to return silently here, with
                # NO log line at all — meaning if browser launches started
                # failing repeatedly (e.g. after the user closed several
                # Chromium windows to test recovery), the pool could sit at
                # 0/3 for many poll cycles with zero explanation in the log.
                log("Tab pool top-up: a replacement worker failed to launch its own browser at all — will retry on the next health check.")
                return
            self._prepare_worker(worker)
            if self._is_worker_clean(worker):
                with self.lock:
                    self.pool.append(worker)
            else:
                log("Tab pool top-up: a replacement worker launched but failed its post-launch sanity check — discarding, will retry.")
                worker.shutdown()

        threads = [threading.Thread(target=_replace_one) for _ in range(missing)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=40)

    def health_check(self):
        """Check every pooled worker is still alive, drop any that died, and
        top up to full size.

        WHY THIS EXISTS: previously the main loop only health-checked the
        MAIN polling page — the pooled workers were never checked at all.
        So if you closed one of the pool browsers, nothing noticed, and
        top_up() only ran AFTER a race (which never happens if no slot is
        found). Net effect: the pool could silently shrink to zero over
        time and nobody would know until a batch actually arrived and there
        was nothing ready to race with. Now this runs on every polling
        cycle, so a closed/crashed pool browser is detected and replaced
        promptly.
        """
        with self.lock:
            current = list(self.pool)

        alive = []
        dead_count = 0
        for w in current:
            # Ask the worker itself (on its own thread) whether its page is
            # still usable — never touch w._page from this thread.
            ok = w.run(lambda page: _is_page_alive(page), timeout=8)
            if ok:
                alive.append(w)
            else:
                dead_count += 1
                try:
                    w.shutdown()
                except Exception:
                    pass

        if dead_count:
            with self.lock:
                self.pool = alive
            log(f"Tab pool health check: {dead_count} worker(s) found dead/closed and removed; topping up.")
            self.top_up()
            log(f"Tab pool health check complete: {len(self.pool)}/{self.size} worker(s) ready.")


def run_live_readonly_self_check():
    """One real page load + five availability samples, no selection or submit.

    This is intentionally a command-line diagnostic rather than a unit test:
    it measures the current network/site/browser path and exits even if a slot
    is visible.  It never opens a form and never mutates an appointment.
    """
    total_started = time.monotonic()
    with sync_playwright() as p:
        launch_started = time.monotonic()
        browser = p.chromium.launch(headless=True)
        launch_ms = round((time.monotonic() - launch_started) * 1000, 1)
        page = browser.new_page()
        page_started = time.monotonic()
        goto_and_dismiss_cookies_fast(page, BASE_URL)
        page_ms = round((time.monotonic() - page_started) * 1000, 1)
        sweep_samples = []
        slot = days_out = None
        batch_count = 0
        all_failed = False
        for _ in range(5):
            sweep_started = time.monotonic()
            slot, days_out, batch_count, all_failed = find_available_slot(
                page, anchor_days=FAST_ANCHOR_DAYS_OUT)
            sweep_samples.append(round((time.monotonic() - sweep_started) * 1000, 1))
            if all_failed or slot:
                break
            page.wait_for_timeout(100)
        ordered = sorted(sweep_samples)
        sweep_median_ms = ordered[len(ordered) // 2]
        total_ms = round((time.monotonic() - total_started) * 1000, 1)
        log(
            "LIVE_READONLY_SELF_CHECK "
            f"browser_launch_ms={launch_ms} page_ready_ms={page_ms} "
            f"availability_sweep_ms={sweep_samples} "
            f"sweep_min_ms={min(sweep_samples)} sweep_median_ms={sweep_median_ms} "
            f"sweep_max_ms={max(sweep_samples)} total_ms={total_ms} "
            f"anchors={FAST_ANCHOR_DAYS_OUT} all_failed={int(all_failed)} "
            f"slot_visible={int(bool(slot))} batch_count={batch_count} "
            f"anchor_hit={days_out}"
        )
        browser.close()
    return not all_failed


def run_live_poll_benchmark():
    """Compare one-anchor and two-anchor warm sweeps on the current site.

    Read-only: it requests availability exactly as the public calendar does,
    but never selects a time, creates a form selection, or submits anything.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        goto_and_dismiss_cookies_fast(page, BASE_URL)
        failed = False
        for anchors in ([60], [0, 60]):
            samples = []
            for _ in range(5):
                started = time.monotonic()
                slot, days_out, count, all_failed = find_available_slot(
                    page, anchor_days=anchors)
                samples.append(round((time.monotonic() - started) * 1000, 1))
                failed = failed or all_failed
                if slot:
                    log(
                        "LIVE_POLL_BENCHMARK observed an open slot but did not "
                        f"select it: anchors={anchors} hit={days_out} count={count}"
                    )
                page.wait_for_timeout(200)
            ordered = sorted(samples)
            log(
                "LIVE_POLL_BENCHMARK "
                f"anchors={anchors} samples_ms={samples} "
                f"min_ms={min(samples)} median_ms={ordered[len(ordered)//2]} "
                f"max_ms={max(samples)}"
            )
        browser.close()
    return not failed


def run_live_intent_contract_check(fill_and_audit=False):
    """Probe the live startsAt selection contract without confirming a booking.

    Uses the current profile's fixed-start date and first displayed office time.
    At night this should normally be rejected as unavailable; either a structured
    unavailable response or a transient intent/form proves the request contract.
    With fill_and_audit=False the browser closes without filling.  With True,
    it runs the normal ten-field dry-run against the real rendered form; the
    normal submit gates still make a final click impossible.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        goto_and_dismiss_cookies_fast(page, BASE_URL)
        candidate = page.evaluate(
            """() => {
                const entries = Object.values(window.requestCache || {});
                const context = entries.map(e => e && e.response && e.response.data)
                    .find(data => data && data.configuration && data.configuration.times);
                if (!context) return null;
                const times = context.configuration.times;
                const date = times.fixedStart;
                const display = context.configuration.display?.displayTimes?.[0]?.[0] || '09:30';
                if (!date) return null;
                const [year, month, day] = date.split('-').map(Number);
                const [hour, minute] = display.split(':').map(Number);
                return {
                    date,
                    display,
                    startsAt: new Date(year, month - 1, day, hour, minute, 0, 0).getTime(),
                };
            }"""
        )
        if not candidate:
            log("LIVE_INTENT_CONTRACT_CHECK failed: current profile timing config unavailable")
            browser.close()
            return False

        accepted, intent_id, reason = patch_intent_slot_selection(
            page, {"startsAt": candidate["startsAt"]})
        form_rendered = False
        fill_outcome = None
        if accepted and navigate_to_intent_form(page, intent_id):
            form_rendered = wait_for_booking_form(page, timeout_ms=5000)
        if form_rendered and fill_and_audit:
            # Avoid sending a user alert for an explicitly invoked diagnostic.
            # The normal scheduled run still uses the configured topic.
            saved_topic = globals().get("NTFY_TOPIC", "")
            globals()["NTFY_TOPIC"] = ""
            try:
                attempt_started = time.monotonic()
                fill_outcome = fill_form_and_submit(
                    page, attempt_started=attempt_started,
                    capture_diagnostic=True)
            finally:
                globals()["NTFY_TOPIC"] = saved_topic
        log(
            "LIVE_INTENT_CONTRACT_CHECK "
            f"candidate_date={candidate['date']} candidate_time={candidate['display']} "
            f"patch_accepted={int(accepted)} form_rendered={int(form_rendered)} "
            f"fill_outcome={fill_outcome} reason={reason} final_click=0"
        )
        browser.close()
    if fill_and_audit:
        return fill_outcome == "dry_run_ready"
    # A structured server rejection still proves the endpoint/body contract;
    # returning True means the diagnostic executed, not that a slot was valid.
    return accepted or bool(reason)


def main():
    log("=== script starting ===")
    run_lock = acquire_single_instance_lock()
    if run_lock is None:
        log("Another teco_autobook process already holds the run lock; exiting to prevent duplicate attempts.")
        return
    if confirmed_receipt_exists():
        log(f"Confirmed-booking receipt already exists at {SUCCESS_RECEIPT_FILE}; exiting without polling.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_page()
        goto_and_dismiss_cookies_fast(page, BASE_URL)

        log("Watching for availability... (Ctrl+C to stop)")
        consecutive_dead_checks = 0
        consecutive_failed_sweeps = 0
        poll_count = 0
        booking_secured = False
        window_closed = False
        last_profile_name = None
        while not booking_secured:
            now_melbourne = datetime.datetime.now(MELBOURNE_TZ)
            if now_melbourne.time() >= AUTOMATION_WINDOW_END:
                log(f"Automation window ended at {AUTOMATION_WINDOW_END.strftime('%H:%M')}; stopping cleanly.")
                window_closed = True
                break

            active_anchors, active_interval, profile_name = polling_profile(
                poll_count=poll_count)
            if profile_name != last_profile_name:
                log(
                    f"Polling profile: {profile_name}; "
                    f"anchors_this_sweep={active_anchors}; sleep={active_interval}s"
                )
                last_profile_name = profile_name

            if not _is_page_alive(page):
                consecutive_dead_checks += 1
                log(f"Browser/page appears closed (check #{consecutive_dead_checks}) — recovering...")
                try:
                    browser, page = _relaunch_fast(browser, None, p)
                    log("Recovery successful — resuming polling.")
                    consecutive_dead_checks = 0
                except Exception as e:
                    log(f"Relaunch attempt failed: {e}")
                    if consecutive_dead_checks >= 5:
                        log("Browser relaunch has failed 5 times in a row — giving up and exiting "
                            "instead of looping forever. Check your Mac/Chromium install and restart this script manually.")
                        ntfy("🔴 Autobook script stopped", "Browser kept closing/failing to relaunch. Script exited — please check and restart it manually.")
                        return
                    time.sleep(active_interval)
                    continue

            slot, days_out, batch_count, all_failed = find_available_slot(page, anchor_days=active_anchors)

            if all_failed:
                # EVERY anchor in this sweep errored (e.g. a 403 block) —
                # back off instead of immediately hammering the API again,
                # since the live log showed that turns a short block into a
                # long run of repeated failures. Resets to the shortest
                # backoff the moment a sweep succeeds again.
                consecutive_failed_sweeps += 1
                delay = FETCH_BACKOFF_SECONDS[min(consecutive_failed_sweeps - 1, len(FETCH_BACKOFF_SECONDS) - 1)]
                log(f"All availability requests failed this sweep (streak={consecutive_failed_sweeps}) — backing off {delay}s")
                if consecutive_failed_sweeps == 2:
                    try:
                        goto_and_dismiss_cookies_fast(page, BASE_URL)
                        log("Polling page refreshed after repeated all-request failure.")
                    except Exception as e:
                        log(f"Polling-page refresh after failures did not succeed: {e}")
                time.sleep(delay)
                continue
            consecutive_failed_sweeps = 0

            # Heartbeat: find_available_slot() logs NOTHING on a normal
            # "no slots, no errors" cycle, which makes a healthy running
            # script look identical to a hung one in the log. Emit a
            # periodic line so silence is distinguishable from stuck.
            poll_count += 1
            if poll_count % HEARTBEAT_EVERY_N_POLLS == 0:
                log(f"…still watching (poll #{poll_count}, no slots yet)")

            if slot:
                t0 = time.monotonic()
                log(f"SLOT FOUND: {slot} (anchor +{days_out}d, {batch_count} slot(s) in this batch) — attempting booking now")
                ntfy_async("🇹🇼 Slot found — auto-booking now", f"{batch_count} slot(s): {slot}")

                if live_submit_armed() and not slot_is_eligible_for_live_submit(slot):
                    slot_day, _ = slot_dom_targets(slot)
                    log(
                        "LIVE SUBMIT SKIPPED — detected appointment date "
                        f"{slot_day or '?'} is invalid or outside configured policy."
                    )
                    time.sleep(active_interval)
                    continue

                # The current site's own bundle PATCHes startsAt on the
                # ephemeral intent, then routes to /form?i=<intent>. This exact
                # contract returned HTTP 200 against the live site on
                # 2026-08-06 and reached formContent in about 1.0–1.3 s. Use it
                # first to avoid waiting for a stale calendar render; preserve
                # exact day/slot DOM clicking as the fail-safe fallback.
                selected = select_detected_slot_fast(
                    page, days_out, slot, attempt_started=t0)

                if not selected:
                    log("Could not select the detected slot by direct intent or exact UI — will keep watching for the next batch.")
                    ntfy("⚠️ Slot selection failed", "Couldn't select this slot. Still watching for further batches.")
                    time.sleep(active_interval)
                    continue

                # Timing is logged (not enforced) — correctness comes first.
                # A hard latency ceiling here is what caused the morning
                # failures (fields filled before the form had rendered), so
                # there's no deadline forcing fill_form_and_submit to cut
                # corners; this just measures how long a normal attempt
                # actually takes.
                outcome = fill_form_and_submit(page, attempt_started=t0)
                log(f"slot-found -> submit-attempt elapsed: {time.monotonic() - t0:.2f}s")

                if outcome in ("blocked_captcha", "unclear_after_submit"):
                    # The irreversible click already happened. Retrying could
                    # create a duplicate if the first request completes late or
                    # after the user solves the challenge, so stop all polling.
                    log(f"POST-SUBMIT state={outcome}; stopping without another attempt to prevent duplicates.")
                    save_confirmation_screenshot(page, tag=f"single_attempt_{outcome}")
                    _wait_for_human("Submission needs review — press Enter to close the browser...")
                    browser.close()
                    log(f"=== script ended ({outcome}; duplicate-safe stop) ===")
                    return

                elif outcome == "submitted":
                    log("✅ Booking submitted and CONFIRMED by page content. Slot secured — stopping.")
                    save_confirmation_screenshot(page, tag="single_attempt_success")
                    write_confirmed_receipt()
                    ntfy("✅ Booking confirmed", "Submission succeeded — page shows a confirmation. Please double check your email too.")
                    booking_secured = True
                    break

                elif outcome == "dry_run_ready":
                    log("Dry-run validation completed successfully. Browser left on the fully audited form; stopping polling.")
                    _wait_for_human("Dry-run passed — review the real form and press Enter to close the browser...")
                    browser.close()
                    log("=== script ended (dry-run passed; no submission clicked) ===")
                    return

                elif outcome == "blocked_incomplete":
                    log("Stopped before submit on this attempt (email/declaration/bot-question issue) — "
                        "will keep watching for the next batch rather than giving up entirely.")
                    ntfy("⚠️ This attempt blocked", "A critical field failed on this slot. Still watching for further batches.")
                    reset_booking_page(page, "pre_submit_audit_blocked")
                    time.sleep(active_interval)
                    continue

                elif outcome == "rejected":
                    log("Server positively rejected this submission; safe to watch for the next batch.")
                    reset_booking_page(page, "server_rejected")
                    time.sleep(active_interval)
                    continue

                else:  # 'error'
                    # `error` is reserved for failures before the irreversible
                    # click (form load/selector/etc.), so retrying cannot create
                    # a duplicate booking.
                    log("This attempt failed before final submission — continuing to watch.")
                    ntfy("⚠️ Attempt error", "This slot attempt failed before final submission. Still watching for further batches.")
                    reset_booking_page(page, "pre_submit_error")
                    time.sleep(active_interval)
                    continue

            time.sleep(active_interval)

        if window_closed:
            browser.close()
            log("=== script ended (window closed; no confirmed booking) ===")
            return

        _wait_for_human("Booking secured — press Enter to close the browser...")
        browser.close()
        log("=== script ended (booking secured) ===")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        raise SystemExit(0 if run_live_readonly_self_check() else 2)
    if "--poll-benchmark" in sys.argv:
        raise SystemExit(0 if run_live_poll_benchmark() else 2)
    if "--contract-check" in sys.argv:
        raise SystemExit(0 if run_live_intent_contract_check() else 2)
    if "--live-form-dry-run" in sys.argv:
        raise SystemExit(0 if run_live_intent_contract_check(fill_and_audit=True) else 2)
    main()
