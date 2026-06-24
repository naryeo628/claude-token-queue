"""설정 — 전부 환경변수로 오버라이드 가능. 기본값은 bash CLI(~/.claude-queue)와 호환."""
from __future__ import annotations
import os
from pathlib import Path

# 큐 라인 구분자 (bash CLI와 동일 포맷: "cwd|||prompt")
DELIM = "|||"


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


# 상태 디렉토리 — bash CLI와 공유 → CLI/MCP 상호운용
QDIR: Path = _env_path("CTQ_DIR", "~/.claude-queue")
JOBS: Path = QDIR / "jobs.txt"
LOG: Path = QDIR / "runner.log"
LOCK: Path = QDIR / "lock.d"

# launchd 라벨/플리스트 — bash와 동일 라벨이라 어느 쪽에서든 취소 가능
LABEL: str = os.environ.get("CTQ_LABEL", "com.user.claudequeue")
PLIST: Path = _env_path("CTQ_PLIST", f"~/Library/LaunchAgents/{LABEL}.plist")

# claude 실행 파일 (PATH에 없거나 커스텀 경로면 오버라이드)
CLAUDE_BIN: str = os.environ.get("CTQ_CLAUDE_BIN", "claude")

# 한도 감지 정규식 (메시지 포맷 바뀌면 여기만 수정)
LIMIT_PATTERN: str = os.environ.get(
    "CTQ_LIMIT_PATTERN", r"usage limit|rate.?limit|resets|429|529|overloaded"
)

# 정각에도 아직 한도일 때 재시도 간격(분)
RETRY_DELAY_MIN: int = int(os.environ.get("CTQ_RETRY_DELAY_MIN", "10"))

# 스케줄러 백엔드 강제 지정 (launchd | 미지정 시 OS로 자동 판별)
SCHEDULER_BACKEND: str | None = os.environ.get("CTQ_SCHEDULER")


def ensure_dir() -> None:
    QDIR.mkdir(parents=True, exist_ok=True)
