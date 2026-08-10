#!/bin/bash
# Sends a once-a-day ntfy push summarizing the day's slot-monitor activity.
# Reads monitor.log (written by check_taiwan_permit.sh, one line per run) and
# reports today's check count, time span, errors, and whether any slot opened.
# Headless (pure HTTP) so it runs from launchd even with the Claude app closed.
# Runs once daily at 09:32 Melbourne, just after the 08:35–09:25 monitoring window.

set -uo pipefail

NTFY_TOPIC="${NTFY_TOPIC:-}"
DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$DIR/monitor.log"
O="https://tecomel-traveltotaiwan.youcanbook.me"
TODAY=$(date '+%Y-%m-%d')
HUMAN=$(date '+%a %-d %b')

send_ntfy() {
  [ -z "$NTFY_TOPIC" ] && return 0
  # $1 = body text. Retries up to 6 times over ~3 min (network can be down right
  # after a wake) and logs the real outcome instead of pretending success.
  local i
  for i in 1 2 3 4 5 6; do
    if curl -s --max-time 15 -X POST \
      -H "Title: 📋 Taiwan permit — $HUMAN" -H "Tags: clipboard" -H "Click: $O" \
      -d "$1" "https://ntfy.sh/$NTFY_TOPIC" >/dev/null 2>&1; then
      echo "$(date '+%F %T') ntfy publish OK (attempt $i)"
      return 0
    fi
    sleep 30
  done
  echo "$(date '+%F %T') ntfy publish FAILED after 6 attempts"
  return 1
}

# Weekends: the booking window (weekdays 8:45-9:15) doesn't exist — stay silent.
if [ "$(date +%u)" -gt 5 ]; then
  echo "summary: weekend, no booking window today"
  exit 0
fi

lines=$(grep "^$TODAY" "$LOG_FILE" 2>/dev/null || true)

total=$(printf '%s\n' "$lines" | grep -c 'SLOTS_FOUND=' || true)
[ -z "$total" ] && total=0

if [ "$total" -eq 0 ]; then
  send_ntfy "⚠️ No checks ran during today's 8:45–9:15 booking window — the Mac was likely off or asleep. Slots may have been released unseen."
  echo "summary: no runs today"
  exit 0
fi

errors=$(printf '%s\n' "$lines" | grep -c 'SLOTS_FOUND=-1' || true);  [ -z "$errors" ] && errors=0
withslots=$(printf '%s\n' "$lines" | grep -cE 'SLOTS_FOUND=[1-9]' || true); [ -z "$withslots" ] && withslots=0
alerts=$(printf '%s\n' "$lines" | grep -c 'NOTIFIED=1' || true); [ -z "$alerts" ] && alerts=0
ok=$(( total - errors ))
first=$(printf '%s\n' "$lines" | head -1 | cut -dT -f2 | cut -d: -f1,2)
lastt=$(printf '%s\n' "$lines" | tail -1 | cut -dT -f2 | cut -d: -f1,2)

if [ "$withslots" -gt 0 ]; then
  send_ntfy "✅ A SLOT OPENED today! $ok checks ($first–$lastt), $alerts alert(s) sent, errors: $errors."
else
  send_ntfy "No slots opened today. $ok checks ($first–$lastt), errors: $errors. Still watching."
fi

echo "summary sent: total=$total ok=$ok errors=$errors withslots=$withslots alerts=$alerts span=$first-$lastt"
exit 0
