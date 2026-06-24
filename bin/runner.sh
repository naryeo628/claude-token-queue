#!/bin/bash
# 큐 비우기 (launchd가 예약 시각에 호출). 정각이라 한도 풀려 있음.
# 안전망: 아직 한도면 작업 보존 + 10분 뒤 자동 재시도.
QDIR="$HOME/.claude-queue"
JOBS="$QDIR/jobs.txt"; LOG="$QDIR/runner.log"; LOCK="$QDIR/lock.d"
PLIST="$HOME/Library/LaunchAgents/com.user.claudequeue.plist"
mkdir -p "$QDIR"

# 동시 실행 방지 (mac엔 flock 없음 → mkdir 원자 락)
if ! mkdir "$LOCK" 2>/dev/null; then exit 0; fi
trap 'rmdir "$LOCK"' EXIT

[ -s "$JOBS" ] || { launchctl unload "$PLIST" 2>/dev/null; exit 0; }

tmp="$JOBS.tmp"; : > "$tmp"; stopped=0

while IFS= read -r line; do
  [ -z "$line" ] && continue
  if [ "$stopped" = 1 ]; then echo "$line" >> "$tmp"; continue; fi
  dir="${line%%|||*}"; prompt="${line#*|||}"
  echo "[$(date)] 실행 [$dir] $prompt" >> "$LOG"
  out=$(cd "$dir" && claude -p "$prompt" --output-format json 2>&1)
  if printf '%s' "$out" | grep -qiE "usage limit|rate.?limit|resets|429|529|overloaded"; then
    echo "[$(date)] 아직 한도 — 작업 보존, 중단" >> "$LOG"
    echo "$line" >> "$tmp"; stopped=1
  else
    echo "[$(date)] 완료" >> "$LOG"
    printf '%s\n' "$out" >> "$LOG"
  fi
done < "$JOBS"
mv "$tmp" "$JOBS"

if [ "$stopped" = 1 ]; then
  echo "[$(date)] 한도 미해제 → 10분 뒤 재시도 예약" >> "$LOG"
  "$QDIR/schedule.sh" "+10m" >> "$LOG" 2>&1
elif [ ! -s "$JOBS" ]; then
  echo "[$(date)] 큐 전부 완료 → 트리거 해제" >> "$LOG"
  launchctl unload "$PLIST" 2>/dev/null
fi
