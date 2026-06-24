#!/bin/bash
# claude 헤드리스 실행. 토큰 한도면 자동 큐 등록 + 리셋 시각 자동 예약 시도.
# "실패 자동 감지"의 핵심: 래퍼가 에러 출력을 직접 보기 때문에 알 수 있음.
QDIR="$HOME/.claude-queue"; mkdir -p "$QDIR"
prompt="$*"
if [ -z "$prompt" ]; then echo "사용법: ctq run \"할 일\""; exit 1; fi

out=$(claude -p "$prompt" --output-format json 2>&1)

if printf '%s' "$out" | grep -qiE "usage limit|rate.?limit|resets|429|529|overloaded"; then
  printf '%s|||%s\n' "$PWD" "$prompt" >> "$QDIR/jobs.txt"
  echo "⛔ 토큰 한도 감지 → 큐 자동 등록: $prompt"

  # 에러 메시지에서 리셋 시각 자동 추출 시도 (포맷 보장 안 됨 → 실패 시 안내)
  reset=$(printf '%s' "$out" | grep -oiE "[0-9]{1,2}:[0-9]{2} ?[ap]?m?|[0-9]{1,2} ?[ap]m" | head -1)
  if [ -n "$reset" ]; then
    HH=$(printf '%s' "$reset" | grep -oE "^[0-9]{1,2}")
    MM=$(printf '%s' "$reset" | grep -oE ":[0-9]{2}" | tr -d ':'); MM=${MM:-00}
    if printf '%s' "$reset" | grep -qi "p"; then [ "$HH" -lt 12 ] && HH=$((HH+12)); fi
    "$QDIR/schedule.sh" "$(printf '%02d:%02d' "$HH" "$MM")"
  else
    echo "⚠️ 리셋 시각 자동추출 실패. 직접 예약: ctq at HH:MM"
  fi
else
  printf '%s\n' "$out"
fi
