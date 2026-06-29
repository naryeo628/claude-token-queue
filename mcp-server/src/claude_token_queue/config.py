"""설정 (config.py)
==================
ctq 전체에서 공유하는 설정값들을 한 곳에 모아둔 파일.

[핵심 원칙]
  - 모든 설정은 환경변수(CTQ_*)로 오버라이드 가능하다.
  - 기본값은 bash CLI(~/.claude-queue)와 호환되도록 맞춰져 있다.
  - 민감한 값(텔레그램 토큰 등)은 절대 이 파일에 하드코딩하지 말 것.

[환경변수로 설정하는 법]
  launchd plist의 EnvironmentVariables 섹션에 추가하거나
  ~/.zshrc / ~/.bash_profile에 'export CTQ_xxx=값' 형태로 추가한다.
"""
from __future__ import annotations
import os
from pathlib import Path

# 큐 레코드 구분자. bash CLI의 레거시 jobs.txt 포맷: "cwd|||prompt"
DELIM = "|||"


def _env_path(name: str, default: str) -> Path:
    """환경변수 name에서 경로를 읽어 Path 객체로 반환. 없으면 default 사용.
    '~'(홈 디렉토리 축약)도 자동 展開한다.
    """
    return Path(os.environ.get(name, default)).expanduser()


# ──────────────────────────────────────────────────────────────────────
# 디렉토리 / 파일 경로
# ──────────────────────────────────────────────────────────────────────

# 큐 데이터가 저장되는 디렉토리. bash CLI와 같은 곳을 써야 상호운용됨.
QDIR: Path = _env_path("CTQ_DIR", "~/.claude-queue")

# 레거시 큐 파일. bash CLI(ctq run/drain)가 쓰는 포맷.
# 현재는 queue.jsonl(QUEUE)이 주 큐이고, 이건 하위호환용.
JOBS: Path = QDIR / "jobs.txt"

# 재실행 로그 파일 (ctq log 명령으로 확인 가능)
LOG: Path = QDIR / "runner.log"

# 큐 락 디렉토리. 동시에 여러 드레인이 실행되지 않도록 하는 잠금 장치.
# Python의 mkdir은 원자적(atomic) 연산이라 Mac에서 flock 대신 이걸 사용.
LOCK: Path = QDIR / "lock.d"

# ──────────────────────────────────────────────────────────────────────
# launchd 스케줄러 설정
# ──────────────────────────────────────────────────────────────────────

# launchd 에이전트 라벨. 같은 라벨이라 bash/MCP 어느 쪽에서든 예약·취소 가능.
LABEL: str = os.environ.get("CTQ_LABEL", "com.user.claudequeue")

# launchd plist 파일 경로. 예약 시 생성, 취소 시 삭제.
PLIST: Path = _env_path("CTQ_PLIST", f"~/Library/LaunchAgents/{LABEL}.plist")

# ──────────────────────────────────────────────────────────────────────
# Claude CLI 실행 설정
# ──────────────────────────────────────────────────────────────────────

# claude 실행 파일 경로. PATH에 있으면 "claude"만으로 충분.
# 커스텀 경로면 CTQ_CLAUDE_BIN=/절대/경로/claude 로 오버라이드.
CLAUDE_BIN: str = os.environ.get("CTQ_CLAUDE_BIN", "claude")

# ──────────────────────────────────────────────────────────────────────
# 한도 감지 정규식
# ──────────────────────────────────────────────────────────────────────

# 토큰 한도 에러 메시지를 인식하는 정규식 패턴.
# Claude API/CLI의 에러 포맷이 바뀌면 여기만 수정하면 된다.
LIMIT_PATTERN: str = os.environ.get(
    "CTQ_LIMIT_PATTERN", r"usage limit|rate.?limit|resets|429|529|overloaded"
)

# ──────────────────────────────────────────────────────────────────────
# 재시도 / 안정화 설정
# ──────────────────────────────────────────────────────────────────────

# 예약 시각에도 아직 한도일 때 재시도 간격(분).
# 기본 10분: 10분 뒤에 다시 실행을 시도한다.
RETRY_DELAY_MIN: int = int(os.environ.get("CTQ_RETRY_DELAY_MIN", "10"))

# 스케줄러 백엔드 강제 지정. 기본은 OS 자동 판별(macOS → launchd).
# 리눅스 환경이면 "cron" 등으로 지정 가능.
SCHEDULER_BACKEND: str | None = os.environ.get("CTQ_SCHEDULER")

# ──────────────────────────────────────────────────────────────────────
# 트랜스크립트 감시(워처) 설정
# ──────────────────────────────────────────────────────────────────────

# Claude 앱이 대화 기록을 저장하는 디렉토리.
# 이 안의 .jsonl 파일들을 30초마다 훑어봐서 429 에러를 찾는다.
PROJECTS_DIR: Path = _env_path("CTQ_PROJECTS_DIR", "~/.claude/projects")

# 정식 큐 파일 (JSONL 형식).
# 각 줄이 하나의 작업 JSON → 세션ID, 리셋시각, resume 여부 등 풍부한 정보를 담을 수 있다.
QUEUE: Path = QDIR / "queue.jsonl"

# 워처 상태 파일. 다음 내용을 저장:
#   - processed: 이미 처리한 에러 키 목록 (중복 등록 방지)
#   - start_time: 워처가 시작된 시각 (과거 에러 무시 기준)
#   - mtimes: 파일별 마지막 수정 시각 (변경 없는 파일 스킵용)
WATCH_STATE: Path = QDIR / "watch-state.json"

# 항상 떠 있는 감지 데몬의 launchd 라벨/plist.
# KeepAlive=true → 죽어도 자동 재시작.
WATCHER_LABEL: str = os.environ.get("CTQ_WATCHER_LABEL", "com.claude-token-queue.watcher")
WATCHER_PLIST: Path = _env_path(
    "CTQ_WATCHER_PLIST", f"~/Library/LaunchAgents/{WATCHER_LABEL}.plist"
)

# 워처 스캔 주기(초). 기본 30초마다 트랜스크립트를 검사한다.
WATCH_INTERVAL: int = int(os.environ.get("CTQ_WATCH_INTERVAL", "30"))

# resume 여부. True면 '--resume 세션ID'로 원래 대화를 이어서 실행.
# resume 시 '--model'은 전달하지 않음 (원 세션 모델과 불일치 시 rc=1 실패 방지).
RESUME: bool = os.environ.get("CTQ_RESUME", "1") not in ("0", "false", "False", "")

# 재실행에 쓸 Claude 모델. 새 세션(resume=False) 시에만 적용됨.
# resume 시에는 원 세션 모델을 유지하므로 이 설정이 무시됨.
# sonnet-4-6 기본: opus-4-8 대비 약 5배 저렴하면서 대부분 작업에 충분.
CLAUDE_MODEL: str = os.environ.get("CTQ_CLAUDE_MODEL", "claude-sonnet-4-6")

# 재실행 결과 및 라이브 로그 파일이 저장되는 디렉토리.
RESULTS_DIR: Path = QDIR / "results"

# 재실행 시 Terminal.app에 라이브 로그 창을 자동으로 띄울지 여부.
# 사용자가 Mac 앞에 있을 때 실황을 볼 수 있어 편리하다.
MONITOR: bool = os.environ.get("CTQ_MONITOR", "1") not in ("0", "false", "False", "")

# 무인 실행이 실제 파일 편집·명령 실행 등을 할 수 있도록 권한 자동 승인.
# 보안 주의: 신뢰할 수 없는 환경에서는 0으로 설정하길 권장.
SKIP_PERMISSIONS: bool = os.environ.get("CTQ_SKIP_PERMISSIONS", "1") not in ("0", "false", "False", "")

# 작업당 최대 실행 시간(초). 초과 시 강제 종료.
# 기본 1200초(20분): 대부분의 코딩 작업은 20분 안에 끝난다.
RUN_TIMEOUT: int = int(os.environ.get("CTQ_RUN_TIMEOUT", "1200"))

# 같은 작업의 연속 에러 허용 횟수. 초과 시 큐에서 제거하고 "수동 필요" 알림.
# 기본 3회: 3번 실패하면 자동 재시도를 포기하고 사람이 직접 확인하도록 한다.
MAX_ATTEMPTS: int = int(os.environ.get("CTQ_MAX_ATTEMPTS", "3"))

# ──────────────────────────────────────────────────────────────────────
# 텔레그램 보고 설정
# ──────────────────────────────────────────────────────────────────────

# 텔레그램 봇 토큰. BotFather에서 발급. 절대 이 파일에 직접 쓰지 말 것.
# 설정 방법: launchd plist EnvironmentVariables에 추가하거나 ~/.zshrc에 export로 설정.
TELEGRAM_TOKEN: str = os.environ.get("CTQ_TELEGRAM_TOKEN", "")

# 메시지를 받을 텔레그램 채팅 ID (개인 채팅 또는 그룹 ID).
TELEGRAM_CHAT: str = os.environ.get("CTQ_TELEGRAM_CHAT", "")


def ensure_dir() -> None:
    """큐 디렉토리(QDIR)가 없으면 생성한다. 모든 파일 읽기/쓰기 전에 호출."""
    QDIR.mkdir(parents=True, exist_ok=True)
