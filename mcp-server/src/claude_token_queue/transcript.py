"""트랜스크립트 파서 (transcript.py)
=====================================
Claude 앱이 저장하는 대화 기록 파일(.jsonl)을 읽어서
'토큰 한도 초과(429)' 이벤트를 찾아내는 파서.

[Claude 트랜스크립트 파일이란?]
  Claude 데스크톱 앱/CLI는 모든 대화를 ~/.claude/projects/ 아래에 JSONL 형식으로 저장한다.
  각 줄은 JSON 객체 하나 = 대화의 한 이벤트(사람 메시지, Claude 응답, 에러 등).

[감지 방식]
  자유 텍스트 정규식이 아닌 구조적 필드로 감지:
    - isApiErrorMessage=true (API 에러 메시지임을 표시)
    - apiErrorStatus=429 또는 error="rate_limit" (한도 초과 코드)
  → 오탐(false positive) 없이 정확하게 감지 가능.

[활성 대화 보호]
  한도 에러 이후에 또 다른 사람 프롬프트가 있으면 → 한도가 풀리고 대화가 계속된 것
  → 큐에 넣지 않음. 실제로 막혀서 멈춘 요청만 큐잉.
"""
from __future__ import annotations
import datetime
import json
from dataclasses import dataclass
from pathlib import Path

from . import config, util


@dataclass
class LimitEvent:
    """한도 초과 이벤트 하나를 표현하는 데이터 클래스.

    [필드 설명]
      session_id : 어떤 Claude 세션에서 발생했는지 (UUID)
      cwd        : 해당 세션의 작업 디렉토리 경로
      git_branch : 어떤 git 브랜치에서 작업 중이었는지 (있으면)
      prompt     : 한도에 막힌 사람의 요청 문자열
      prompt_id  : 요청 고유 ID (중복 방지용)
      reset_text : Claude가 알려준 리셋 관련 에러 메시지 원문
      reset      : 추출된 리셋 시각 (hour, minute) 튜플. 파싱 실패 시 None.
      ts         : 에러 발생 시각 (UTC timezone-aware datetime)
      uuid       : 이벤트 자체의 고유 ID
    """
    session_id: str | None
    cwd: str | None
    git_branch: str | None
    prompt: str
    prompt_id: str | None
    reset_text: str
    reset: tuple[int, int] | None   # 로컬 시각 (hour, minute)
    ts: datetime.datetime | None
    uuid: str | None

    @property
    def key(self) -> str:
        """중복 방지용 고유 키. "session_id:prompt_id" 형태."""
        return f"{self.session_id}:{self.prompt_id}"


def _parse_ts(s) -> datetime.datetime | None:
    """ISO8601 타임스탬프 문자열을 datetime 객체로 변환한다.
    Claude 트랜스크립트는 UTC 'Z' 접미사를 쓰므로 '+00:00'으로 변환 후 파싱.
    실패하면 None 반환.
    """
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def iter_session_files() -> list[Path]:
    """~/.claude/projects/ 아래의 메인 세션 파일 목록을 반환한다.

    [필터링 이유]
    서브에이전트 파일(agent-*.jsonl)을 제외하는 이유:
      하나의 사용자 요청이 서브에이전트를 여러 개 생성할 수 있다.
      서브에이전트 파일에도 같은 에러가 기록되면 같은 요청이 여러 번 큐잉될 수 있다.
      → 메인 세션 파일만 읽어서 중복 방지.
    """
    if not config.PROJECTS_DIR.exists():
        return []
    return [p for p in config.PROJECTS_DIR.rglob("*.jsonl") if not p.name.startswith("agent-")]


def _is_human_prompt(o: dict) -> bool:
    """이 JSON 이벤트가 실제로 사람이 입력한 프롬프트인지 판별한다.

    [제외 대상]
      - type != "user": 사람 메시지가 아님
      - isMeta=true: 내부 메타 메시지 (UI 상태 등)
      - content가 문자열이 아님: tool_result 등 합성 user 메시지
      - promptId도 promptSource도 없음: 자동 생성된 내부 메시지
    """
    if o.get("type") != "user" or o.get("isMeta"):
        return False
    c = o.get("message", {}).get("content")
    return isinstance(c, str) and bool(o.get("promptId") or o.get("promptSource"))


def find_limit_events(path: Path) -> list[LimitEvent]:
    """하나의 트랜스크립트 파일에서 429(한도 초과) 이벤트를 모두 찾아 반환한다.

    [처리 흐름]
    1. 파일을 줄 단위로 읽어 JSON으로 파싱한다.
    2. 각 줄에서 isApiErrorMessage=true, apiErrorStatus=429 인 이벤트를 찾는다.
    3. 그 에러 직전에 있는 사람 프롬프트를 역방향 탐색으로 찾는다.
    4. 에러 이후에 사람 프롬프트가 또 있으면 활성 대화 → 제외한다.
    5. 해당 에러 메시지에서 리셋 시각을 파싱한다.
    6. LimitEvent를 생성해 목록에 추가한다.

    [반환값]
      LimitEvent 목록. 파일 읽기 실패 시 빈 목록.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []

    # 각 줄을 JSON으로 파싱. 실패한 줄은 조용히 무시.
    lines: list[dict] = []
    for ln in raw:
        ln = ln.strip()
        if not ln:
            continue
        try:
            lines.append(json.loads(ln))
        except json.JSONDecodeError:
            continue

    events: list[LimitEvent] = []
    for i, o in enumerate(lines):
        # 429 에러 이벤트인지 확인
        if not o.get("isApiErrorMessage"):
            continue
        if str(o.get("apiErrorStatus")) != "429" and o.get("error") != "rate_limit":
            continue

        # 에러 직전의 사람 프롬프트를 역방향으로 탐색
        prompt = prompt_id = None
        for j in range(i - 1, -1, -1):
            if _is_human_prompt(lines[j]):
                prompt = lines[j]["message"]["content"]
                prompt_id = lines[j].get("promptId")
                break

        if not prompt:
            continue  # 대응하는 사람 프롬프트를 못 찾음 → 무시

        # 에러 이후에 사람 프롬프트가 또 있으면 → 한도가 풀리고 대화를 계속한 것
        # → 이 에러로 세션이 멈춘 게 아님 → 큐에 넣지 않음
        if any(_is_human_prompt(lines[j]) for j in range(i + 1, len(lines))):
            continue

        # 에러 메시지에서 리셋 시각 문자열 추출 (util.parse_reset_message로 파싱)
        text = util.extract_text(o.get("message", {}).get("content"))

        events.append(LimitEvent(
            session_id=o.get("sessionId"),
            cwd=o.get("cwd"),
            git_branch=o.get("gitBranch"),
            prompt=prompt,
            prompt_id=prompt_id,
            reset_text=text,
            reset=util.parse_reset_message(text),    # (hour, minute) 또는 None
            ts=_parse_ts(o.get("timestamp")),         # UTC datetime
            uuid=o.get("uuid"),
        ))
    return events
