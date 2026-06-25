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

# --- 트랜스크립트 감시(워처) ---
# 클로드 앱 세션 기록 디렉토리
PROJECTS_DIR: Path = _env_path("CTQ_PROJECTS_DIR", "~/.claude/projects")
# 정식 큐 (JSONL, 세션ID·리셋시각 등 풍부한 필드). bash CLI의 jobs.txt는 레거시로 함께 읽음.
QUEUE: Path = QDIR / "queue.jsonl"
# 워처 상태 (처리한 에러 키, 시작시각, 파일 mtime)
WATCH_STATE: Path = QDIR / "watch-state.json"
# 항상 떠 있는 감지 데몬(launchd KeepAlive) 라벨/플리스트
WATCHER_LABEL: str = os.environ.get("CTQ_WATCHER_LABEL", "com.claude-token-queue.watcher")
WATCHER_PLIST: Path = _env_path(
    "CTQ_WATCHER_PLIST", f"~/Library/LaunchAgents/{WATCHER_LABEL}.plist"
)
# 워처 스캔 주기(초)
WATCH_INTERVAL: int = int(os.environ.get("CTQ_WATCH_INTERVAL", "30"))
# 재실행 시 원래 세션 resume 여부. claude CLI 2.x는 헤드리스 resume로 컨텍스트 복원됨 → 기본 on.
# (구버전 1.x는 resume replay가 400나니, 그 경우 CTQ_RESUME=0으로 두고 새 세션 실행.)
RESUME: bool = os.environ.get("CTQ_RESUME", "1") not in ("0", "false", "False", "")
# 재실행에 쓸 모델 (구 CLI 기본모델이 죽어 404 → 유효 모델 명시 필수)
CLAUDE_MODEL: str = os.environ.get("CTQ_CLAUDE_MODEL", "claude-opus-4-8")
# 재실행 결과·라이브 로그 저장 위치
RESULTS_DIR: Path = QDIR / "results"
# 재실행 시 모니터링 터미널 자동 오픈 (있을 때 실황 보기)
MONITOR: bool = os.environ.get("CTQ_MONITOR", "1") not in ("0", "false", "False", "")
# 무인 실행이 실제 작업(편집·명령)을 하도록 도구 권한 자동승인. 보안 주의 — 끄려면 0.
SKIP_PERMISSIONS: bool = os.environ.get("CTQ_SKIP_PERMISSIONS", "1") not in ("0", "false", "False", "")


def ensure_dir() -> None:
    QDIR.mkdir(parents=True, exist_ok=True)
