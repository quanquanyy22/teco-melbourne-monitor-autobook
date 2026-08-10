#!/bin/bash
# Watches TECO Melbourne's Taiwan-entry-permit instructions page for ANY text change
# (online-system reopening, schedule/quota changes, new announcements) and pushes an
# ntfy alert when the content differs from the stored snapshot.
# Runs daily via launchd. First run just stores the snapshot silently.

set -uo pipefail
NTFY_TOPIC="${NTFY_TOPIC:-}"
DIR="$(cd "$(dirname "$0")" && pwd)"
SNAP="$DIR/.teco_page_snapshot"
URL="https://www.roc-taiwan.org/aumel/post/11018.html"

body=$(curl -s --max-time 20 -A "Mozilla/5.0" "$URL" | python3 -c "
import re,sys,html
raw=sys.stdin.read()
t=html.unescape(re.sub(r'<script.*?</script>|<style.*?</style>','',raw,flags=re.S))
t=re.sub(r'<[^>]+>',' ',t)
t=re.sub(r'\s+','',t)          # normalize ALL whitespace (page uses spaced chars)
print(t)")

if [ -z "$body" ]; then
  echo "$(date '+%F %T') fetch failed, skipping compare"
  exit 0
fi

new_hash=$(printf '%s' "$body" | /usr/bin/shasum -a 256 | cut -d' ' -f1)
old_hash=$(cut -d' ' -f1 "$SNAP" 2>/dev/null || true)

if [ -z "$old_hash" ]; then
  printf '%s\n' "$new_hash" > "$SNAP"
  printf '%s' "$body" > "$SNAP.text"
  echo "$(date '+%F %T') snapshot initialized"
elif [ "$new_hash" != "$old_hash" ]; then
  if [ -n "$NTFY_TOPIC" ]; then
    curl -s --max-time 10 -X POST \
      -H "Title: 📢 TECO permit page CHANGED" -H "Priority: high" -H "Tags: loudspeaker" \
      -H "Click: $URL" \
      -d "The TECO Melbourne permit page was updated — possibly the online system reopening, new quotas, or schedule changes. Tap to read it." \
      "https://ntfy.sh/$NTFY_TOPIC" >/dev/null 2>&1
  fi
  printf '%s\n' "$new_hash" > "$SNAP"
  printf '%s' "$body" > "$SNAP.text"
  echo "$(date '+%F %T') CHANGE detected + alerted"
else
  echo "$(date '+%F %T') no change"
fi
exit 0
