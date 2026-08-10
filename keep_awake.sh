#!/bin/bash
# Keeps the Mac awake around the official booking window (weekdays 8:45-9:15).
# Guard window: 08:25-09:40 Mon-Fri. Called by launchd at 08:27 daily AND at
# load/login (RunAtLoad), so protection self-recovers if the Mac was asleep at
# 08:27 and you open it mid-window.
# caffeinate -i = block idle sleep; -s = block system sleep while on AC power.
# NOTE: nothing user-level can block lid-close sleep — keep the lid open.

dow=$(date +%u)
hm=$((10#$(date +%H) * 60 + 10#$(date +%M)))     # minutes since midnight
start=$((8*60+25)); end=$((9*60+40))
if [ "$dow" -le 5 ] && [ "$hm" -ge "$start" ] && [ "$hm" -lt "$end" ]; then
  secs=$(( (end - hm) * 60 ))
  echo "$(date '+%F %T') keeping awake for $secs s (until ~09:40)"
  exec /usr/bin/caffeinate -i -s -t "$secs"
fi
echo "$(date '+%F %T') outside window (dow=$dow hm=$hm), not caffeinating"
exit 0
