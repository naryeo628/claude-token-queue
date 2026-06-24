#!/bin/bash
# 재실행 시각 지정 (그때그때 입력)
#   ctq at 14:30     절대 시각
#   ctq at +30m      30분 뒤
#   ctq at +2h       2시간 뒤
QDIR="$HOME/.claude-queue"
PLIST="$HOME/Library/LaunchAgents/com.user.claudequeue.plist"
LABEL="com.user.claudequeue"

arg="$1"
if [ -z "$arg" ]; then echo "사용법: ctq at HH:MM | +30m | +2h"; exit 1; fi

if [[ "$arg" == +* ]]; then
  unit="${arg: -1}"; num="${arg:1:${#arg}-2}"
  case "$unit" in
    m) when=$(date -v+${num}M +%H:%M) ;;
    h) when=$(date -v+${num}H +%H:%M) ;;
    *) echo "상대 단위는 m 또는 h (예 +30m, +2h)"; exit 1 ;;
  esac
else
  when="$arg"
fi

HH="${when%%:*}"; MM="${when##*:}"
HH=$((10#$HH)); MM=$((10#$MM))   # 08/09 8진수 파싱 오류 방지
if [ "$HH" -gt 23 ] || [ "$MM" -gt 59 ]; then echo "시각 범위 오류: $when"; exit 1; fi

mkdir -p "$(dirname "$PLIST")"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$QDIR/runner.sh</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>$HH</integer><key>Minute</key><integer>$MM</integer></dict>
  <key>StandardErrorPath</key><string>$QDIR/err.log</string>
</dict></plist>
EOF

launchctl unload "$PLIST" 2>/dev/null
launchctl load "$PLIST"
printf '예약 완료: %02d:%02d 발사 (큐 다 비우면 자동 해제)\n' "$HH" "$MM"
