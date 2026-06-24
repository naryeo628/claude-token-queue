#!/bin/bash
# 큐에 작업 추가 (실행 시점의 현재 디렉토리를 같이 저장 → 나중에 그 디렉토리에서 실행)
QDIR="$HOME/.claude-queue"; mkdir -p "$QDIR"
if [ -z "$1" ]; then echo "사용법: ctq add \"할 일\""; exit 1; fi
printf '%s|||%s\n' "$PWD" "$*" >> "$QDIR/jobs.txt"
echo "큐 등록: [$PWD] $*"
echo "현재 대기 작업 $(grep -c . "$QDIR/jobs.txt")건"
