#!/bin/bash
# claude-token-queue 제거기
QDIR="$HOME/.claude-queue"
PLIST="$HOME/Library/LaunchAgents/com.user.claudequeue.plist"

launchctl unload "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
rm -f "$QDIR/ctq" "$QDIR/add.sh" "$QDIR/runner.sh" "$QDIR/schedule.sh" "$QDIR/cq.sh"

echo "✅ 스크립트·예약(plist) 제거됨."
echo "   큐/로그는 보존: $QDIR/jobs.txt, $QDIR/runner.log"
echo "   완전 삭제: rm -rf $QDIR"
echo "   ~/.zshrc, ~/.bashrc 에서 'claude-token-queue alias' 블록은 직접 삭제하세요."
