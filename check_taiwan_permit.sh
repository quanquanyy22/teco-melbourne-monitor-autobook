#!/bin/bash
# Monitors availability on the TECO Melbourne (Taiwan Entry Permit) booking page:
#   https://tecomel-traveltotaiwan.youcanbook.me
# and sends an ntfy.sh push the moment a bookable slot opens (see Notification below).
#
# Flow (mirrors exactly what the booking page itself does on load):
#   1. GET the booking page -> extract a fresh ephemeral intent token (itt_...)
#   2. GET /v1/intents/{itt}/availabilitykey   -> { key: "avl_..." }
#   3. GET /v1/availabilities/{key}            -> { slots: [...] }
# A non-empty "slots" array means at least one bookable appointment is open.
# Appointment-type selection is disabled on this profile (APP_TYPES_DISABLED),
# so this default availability key is the whole calendar the public sees.
#
# Notification: ntfy.sh push (headless — works from launchd even when the Claude
# app is closed) with a tappable Click header to the booking page, PLUS it opens the
# booking page in the Mac's default browser for a running start. To avoid pinging on
# the same opening every run, it only alerts when the set of open slots CHANGES.
#
# RELEASE-TRIGGERED COLD START: disabled here.  Repeatedly flipping this flag
# used to launch Chromium only after a short-lived slot was detected.  As of
# the user's explicit 2026-08-06 instruction to pursue a verified automatic
# booking, launchd_runner.sh owns one PRE-WARMED teco_autobook.py process from
# 08:35. The single user-facing AUTOFILL_ENABLED switch lives in autobook.conf
# and is exported by launchd_runner.sh. Starting a second cold process from
# this monitor is intentionally disabled; the runner owns the one booking
# process.
#
# Output (stdout), for logs/debugging:
#   SLOTS_FOUND=<n>
#   <slot start times, one per line, if any>
#   NOTIFIED=1|0
#   AUTOFILL_LAUNCHED=0 (runner-managed; this script never starts booking)
# Exit code: 0 = ran OK, 2 = error reaching the service.

set -uo pipefail

# --- config ---
NTFY_TOPIC="${NTFY_TOPIC:-}"
SHARED_TOPIC="${SHARED_TOPIC:-}"
DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_FILE="$DIR/.last_slot_alert"           # dedupe state
LOG_FILE="$DIR/monitor.log"                  # per-run log for the daily summary
O="https://tecomel-traveltotaiwan.youcanbook.me"
API="https://api.youcanbook.me"
UA="Mozilla/5.0"
# Booking page with form answers as URL params (best-effort prefill; ignored harmlessly
# if this YCBM version doesn't support it). Anti-bot Q11/Q14 + declaration left to the human.
PREFILL_URL="$O"

# --- Cold-start-on-alert path — DISABLED. Prewarm is in launchd_runner.sh. ---
. "$DIR/autobook.conf"
AUTOFILL_SCRIPT="$DIR/teco_autobook.py"
AUTOFILL_PYTHON="python3"            # point at your venv's python3 if you used one
AUTOFILL_LOG="$DIR/autofill_launch.log"

# --- ALERT TIER -------------------------------------------------------------
# This tier only controls notification priority. It is deliberately NOT a
# booking eligibility rule: the live submit path accepts every valid slot date
# until a processing-time cutoff has been measured and explicitly approved.
SPRINT_BEFORE="2026-09-19"

send_ntfy() {
  # $1 = title, $2 = body. Both are personal-data-free (only slot count + dates).
  # Fires to TWO topics, two buzzes each (3s apart) so a missed first push gets a retry:
  #   • primary topic: Click = the public booking page.
  #   • optional shared topic: same public page; no personal data is included.
  # Same body/title on both (they carry no info); only the tap-target differs.
  # $3 = tier: "sprint" (near-term, winnable) or "fyi" (far batch, not worth waking for)
  local tier="${3:-sprint}"
  local prio tags private_ok=1
  if [ -z "$NTFY_TOPIC" ] && [ -z "$SHARED_TOPIC" ]; then
    return 0
  fi
  if [ "$tier" = "sprint" ]; then
    prio="max"; tags="rotating_light,alarm_clock"
  else
    prio="low"; tags="calendar"            # silent — no buzz, no lock-screen alarm
  fi

  # -- optional primary topic --
  curl -fsS --max-time 6 -X POST \
    -H "Title: $1" -H "Priority: $prio" -H "Tags: $tags" -H "Click: $PREFILL_URL" \
    -d "$2" "https://ntfy.sh/$NTFY_TOPIC" >/dev/null 2>&1 || private_ok=0
  # -- optional shared topic --
  curl -fsS --max-time 6 -X POST \
    -H "Title: $1" -H "Priority: $prio" -H "Tags: $tags" -H "Click: $O" \
    -d "$2" "https://ntfy.sh/$SHARED_TOPIC" >/dev/null 2>&1

  # Only the sprint tier gets the second buzz. A far batch waking you at 08:45
  # for a slot you can't use is exactly what makes you slow on the real one.
  if [ "$tier" = "sprint" ]; then
    curl -fsS --max-time 6 -X POST \
      -H "Title: $1" -H "Priority: max" -H "Tags: rotating_light" -H "Click: $PREFILL_URL" \
      -d "GO GO GO — $2" "https://ntfy.sh/$NTFY_TOPIC" >/dev/null 2>&1 && private_ok=1
    curl -fsS --max-time 6 -X POST \
      -H "Title: $1" -H "Priority: max" -H "Tags: rotating_light" -H "Click: $O" \
      -d "GO GO GO — $2" "https://ntfy.sh/$SHARED_TOPIC" >/dev/null 2>&1
  fi

  # Only the private delivery controls deduplication. If it failed, leave the
  # state unchanged so the next monitor pass retries instead of silently
  # suppressing the alert. The shared topic remains best-effort.
  [ "$private_ok" -eq 1 ]
}

# Booking ownership belongs exclusively to launchd_runner.sh. This monitor
# reports process state for diagnostics but never launches a second path.
report_autofill_status() {
  if pgrep -f "$AUTOFILL_SCRIPT" >/dev/null 2>&1; then
    echo "AUTOFILL_LAUNCHED=0 (runner-managed; already running)"
    return
  fi
  echo "AUTOFILL_LAUNCHED=0 (runner-managed; not running)"
}

# --- fetch availability ---
itt=$(curl -s --max-time 8 -A "$UA" "$O/" | grep -ioE "itt_[a-f0-9-]{36}" | head -1)
if [ -z "${itt:-}" ]; then
  echo "ERROR=could_not_load_booking_page"
  echo "SLOTS_FOUND=-1"
  echo "$(date '+%Y-%m-%dT%H:%M:%S%z') SLOTS_FOUND=-1 NOTIFIED=0 ERROR=could_not_load_booking_page" >> "$LOG_FILE"
  exit 2
fi

# Multi-anchor search (since 2026-07-08): the default availability key is anchored
# at TODAY and may only cover a limited forward window, silently hiding slots
# released for months ahead (the Sep/Oct batches were likely invisible to it). So
# query the default key PLUS keys anchored at +30/+60/+90/+120 days and merge.
anchors=""
for d in 30 60 90 120; do anchors="$anchors $(date -v+${d}d '+%Y-%m-%d')"; done

# All 5 anchors fetched IN PARALLEL (sweep ~3s instead of ~7s) — cadence matters:
# the 2026-07-15 batch (12 slots) sold out in ~30 seconds.
tmpd=$(mktemp -d "${TMPDIR:-/tmp}/tw_probe.XXXXXX")
for a in default $anchors; do
  (
    if [ "$a" = "default" ]; then
      ku="$API/v1/intents/$itt/availabilitykey"
    else
      ku="$API/v1/intents/$itt/availabilitykey?startSearchAt=$a"
    fi
    key=$(curl -s --max-time 8 -A "$UA" -H "Origin: $O" "$ku" \
          | python3 -c "import json,sys;print(json.load(sys.stdin).get('key',''))" 2>/dev/null)
    [ -n "${key:-}" ] && curl -s --max-time 8 -A "$UA" -H "Origin: $O" \
      "$API/v1/availabilities/$key" > "$tmpd/$a.json"
  ) &
done
wait
all_avail="["
sep=""
got_key=0
for f in "$tmpd"/*.json; do
  [ -s "$f" ] || continue
  got_key=1
  all_avail="$all_avail$sep$(cat "$f")"
  sep=","
done
all_avail="$all_avail]"
rm -rf "$tmpd"
if [ "$got_key" = "0" ]; then
  echo "ERROR=no_availability_key"
  echo "SLOTS_FOUND=-1"
  echo "$(date '+%Y-%m-%dT%H:%M:%S%z') SLOTS_FOUND=-1 NOTIFIED=0 ERROR=no_availability_key" >> "$LOG_FILE"
  exit 2
fi

# --- parse merged responses: SLOTS_FOUND=n and slot start times (deduped) ---
result=$(echo "$all_avail" | python3 -c "
import json,sys
try:
    docs=json.load(sys.stdin)
except Exception:
    print('SLOTS_FOUND=-1'); sys.exit(0)
slots=[]
for d in docs:
    if isinstance(d,dict): slots.extend(d.get('slots') or [])
# Known schema (captured live 2026-07-09): [{'freeUnits': 1, 'startsAt': '1790126100000'}]
# startsAt = epoch milliseconds as a string. Render as Melbourne local date/time.
import datetime, zoneinfo
mel=zoneinfo.ZoneInfo('Australia/Melbourne')
t=set()
def render(v):
    s=str(v)
    if s.isdigit() and len(s)>=12:
        return datetime.datetime.fromtimestamp(int(s)/1000, mel).strftime('%a %d %b %H:%M')
    return s if ('T' in s and ':' in s) else None
def find_times(o):
    if isinstance(o,dict):
        for k,v in o.items():
            if k.lower() in ('start','startsat','starttime','time','from','begin','datetime','slotstart') and not isinstance(v,(dict,list)):
                r=render(v)
                if r: t.add(r)
            else: find_times(v)
    elif isinstance(o,list):
        for v in o: find_times(v)
find_times(slots)
ts=sorted(t)
print('SLOTS_FOUND=%d' % (len(ts) if ts else len(slots)))
for x in ts: print(x)
# NEAREST= the earliest slot as a sortable ISO date, so the shell can decide
# whether this is a SPRINT-worthy near-term slot or a far batch (see ALERT TIER).
iso=set()
def find_iso(o):
    if isinstance(o,dict):
        for k,v in o.items():
            if k.lower() in ('start','startsat','starttime','time','from','begin','datetime','slotstart') and not isinstance(v,(dict,list)):
                s=str(v)
                if s.isdigit() and len(s)>=12:
                    iso.add(datetime.datetime.fromtimestamp(int(s)/1000, mel).strftime('%Y-%m-%d'))
            else: find_iso(v)
    elif isinstance(o,list):
        for v in o: find_iso(v)
find_iso(slots)
if iso: print('NEAREST='+min(iso))
if slots and not ts: print('RAW='+json.dumps(slots)[:500])
")

echo "$result"

n=$(printf '%s\n' "$result" | sed -n 's/^SLOTS_FOUND=//p')
times=$(printf '%s\n' "$result" | grep -vE '^(SLOTS_FOUND=|NEAREST=|RAW=)' || true)

# --- decide whether to notify + (optionally) auto-fill ---
notified=0
if [ "${n:-0}" -gt 0 ] 2>/dev/null; then
  sig="count=$n
$times"
  last=$(cat "$STATE_FILE" 2>/dev/null || true)
  if [ "$sig" != "$last" ]; then
    first=$(printf '%s\n' "$times" | grep -v '^$' | head -3 | paste -sd '; ' - 2>/dev/null)
    [ -z "$first" ] && first="see booking page"

    # Tier the alert on the EARLIEST slot date (see ALERT TIER note above).
    nearest=$(printf '%s\n' "$result" | sed -n 's/^NEAREST=//p')
    if [[ -n "$nearest" && "$nearest" > "$SPRINT_BEFORE" ]]; then
      tier="fyi"
      title="🇹🇼 遠期批次 $n 個（低優先）"
      body="TECO — $first. 最早 $nearest，日期較遠，低優先提醒；自動預約不受此分類限制。"
    else
      tier="sprint"
      title="🚨 近期位子 $n 個 — 快！"
      body="TECO — $first. 這個趕得上，衝。"
    fi
    echo "TIER=$tier NEAREST=${nearest:-?}"

    # Only pop the browser for slots actually worth sprinting for.
    if [ "$tier" = "sprint" ]; then
      open "$PREFILL_URL" >/dev/null 2>&1
    fi
    # Only does anything if you've set AUTOFILL_ENABLED=1 above. Notification
    # tier does not decide whether a slot may be auto-booked.
    report_autofill_status
    if send_ntfy "$title" "$body" "$tier"; then
      printf '%s' "$sig" > "$STATE_FILE"
      notified=1
      echo "NOTIFIED=1"
    else
      echo "NOTIFIED=0 (private push failed; will retry next run)"
    fi
  else
    echo "NOTIFIED=0 (unchanged since last alert; suppressed duplicate)"
  fi
else
  # clean zero -> reset state so the next reopening alerts again.
  # error (n=-1) -> leave state untouched, send nothing.
  if [ "${n:-}" = "0" ]; then : > "$STATE_FILE"; fi
  echo "NOTIFIED=0"
fi

# append one line per run for the end-of-day summary task to read
echo "$(date '+%Y-%m-%dT%H:%M:%S%z') SLOTS_FOUND=${n:--1} NOTIFIED=$notified" >> "$LOG_FILE"

# dead-man's-switch heartbeat: tell healthchecks.io this run happened. If these
# pings stop for >5 min during the window (Mac asleep, lid closed), healthchecks
# notices the silence and pushes an alarm to the ntfy topic -> user's phone.
HC_PING_URL="$(cat "$DIR/.hc_ping_url" 2>/dev/null)"
if [ -n "$HC_PING_URL" ]; then
  curl -s --max-time 10 "$HC_PING_URL" >/dev/null 2>&1
fi

exit 0
