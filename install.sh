#!/bin/bash
# claude-token-queue 설치기
#   curl -fsSL https://raw.githubusercontent.com/naryeo628/claude-token-queue/main/install.sh | bash
set -e

REPO="naryeo628/claude-token-queue"
BRANCH="main"
RAW="https://raw.githubusercontent.com/$REPO/$BRANCH/bin"
QDIR="$HOME/.claude-queue"

echo "claude-token-queue 설치 중..."
case "$(uname)" in
  Darwin) ;;
  *) echo "⚠️ launchd 기반이라 macOS 전용. (Linux는 cron/systemd 포팅 필요)"; ;;
esac

mkdir -p "$QDIR"
for f in ctq add.sh runner.sh schedule.sh cq.sh; do
  curl -fsSL "$RAW/$f" -o "$QDIR/$f"
  chmod +x "$QDIR/$f"
  echo "  받음: $f"
done

add_alias() {
  local rc="$1"
  [ -f "$rc" ] || return 0
  grep -q "claude-token-queue alias" "$rc" 2>/dev/null && { echo "  alias 이미 있음: $rc"; return 0; }
  {
    echo ""
    echo "# claude-token-queue alias"
    echo "alias ctq='$QDIR/ctq'"
    echo "alias cq='$QDIR/cq.sh'"
  } >> "$rc"
  echo "  alias 추가: $rc"
}
add_alias "$HOME/.zshrc"
add_alias "$HOME/.bashrc"

echo ""
echo "✅ 설치 완료."
echo "   새 터미널을 열거나 → source ~/.zshrc"
echo "   사용법:  ctq"
