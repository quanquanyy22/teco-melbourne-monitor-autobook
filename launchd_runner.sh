#!/bin/bash
# Invoked by launchd every 30s. On weekdays, the first invocation at/after 08:35
# starts one Playwright auto-booker and supervises it until 09:25. The browser
# process is the only booking path; the curl monitor is notification/evidence
# only and never starts a second auto-booker.
#
# The browser remains on its own fast polling loop (+60 every sweep, near-term
# every fourth; measured warm medians about 0.54–0.55s plus 0.5s sleep). The
# shell monitor runs independently every 30s for notification/evidence and is
# not used as a booking-speed fallback.
#
# Official booking window: weekdays 8:45-9:15 AM Melbourne
# (https://www.roc-taiwan.org/aumel/post/11018.html).
# Outside the window: exit instantly.

DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="${AUTOBOOK_CONFIG:-$DIR/autobook.conf}"
if [ ! -f "$CONFIG_FILE" ]; then
  echo "Missing $CONFIG_FILE; copy autobook.conf.example and configure it first." >&2
  exit 1
fi
. "$CONFIG_FILE"
export NTFY_TOPIC SHARED_TOPIC
export AUTOFILL_ENABLED
AUTOBOOK_SCRIPT="$DIR/teco_autobook.py"
AUTOBOOK_LOG="$DIR/autobook_prewarm.log"
AUTOBOOK_PYTHON="${AUTOBOOK_PYTHON:-python3}"
SUPERVISOR_INTERVAL_SECONDS=2
MONITOR_INTERVAL_SECONDS=30
CONFIRMED_RECEIPT="$DIR/.teco_booking_confirmed"

END=$((9*60+25))                                  # loop until 09:25
dow=$(date +%u)                                   # 1=Mon .. 7=Sun
hm=$((10#$(date +%H) * 60 + 10#$(date +%M)))
if [ "$dow" -le 5 ] && [ "$hm" -ge $((8*60+35)) ] && [ "$hm" -lt "$END" ]; then
  # teco_autobook.py still owns the cross-process lock, ten-field audit, and
  # date/duplicate checks. This runner owns recovery: if the one managed
  # process exits unexpectedly, start that same single process again.
  start_autobook() {
    if [ -f "$CONFIRMED_RECEIPT" ]; then
      return 0
    fi
    if pgrep -f "$AUTOBOOK_SCRIPT" >/dev/null 2>&1; then
      return 0
    fi
    nohup env AUTOFILL_ENABLED="$AUTOFILL_ENABLED" "$AUTOBOOK_PYTHON" "$AUTOBOOK_SCRIPT" >> "$AUTOBOOK_LOG" 2>&1 &
    echo "$(date '+%Y-%m-%dT%H:%M:%S%z') runner started autobook pid=$!" >> "$AUTOBOOK_LOG"
  }

  monitor_pid=""
  next_monitor_at=0
  cleanup_runner() {
    if [ -n "$monitor_pid" ] && kill -0 "$monitor_pid" 2>/dev/null; then
      kill "$monitor_pid" 2>/dev/null || true
    fi
  }
  trap cleanup_runner EXIT INT TERM

  while :; do
    start_autobook

    # Run the notification/evidence monitor independently, at a deliberately
    # non-racing cadence. It must never become the booking recovery mechanism.
    now=$(date +%s)
    if [ "$now" -ge "$next_monitor_at" ] && {
      [ -z "$monitor_pid" ] || ! kill -0 "$monitor_pid" 2>/dev/null
    }; then
      bash "$DIR/check_taiwan_permit.sh" &
      monitor_pid=$!
      next_monitor_at=$((now + MONITOR_INTERVAL_SECONDS))
    fi

    hm=$((10#$(date +%H) * 60 + 10#$(date +%M)))
    [ "$hm" -ge "$END" ] && break
    # The short supervisor tick is for crash recovery only. It does not alter
    # the browser's own polling interval and does not launch a second instance.
    sleep "$SUPERVISOR_INTERVAL_SECONDS"
  done
fi
exit 0
