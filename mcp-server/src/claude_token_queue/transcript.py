"""클로드 앱 세션 트랜스크립트(~/.claude/projects/**/*.jsonl) 파싱.
토큰 한도(429/rate_limit) 이벤트를 찾아 실패한 사람 프롬프트·cwd·세션ID·리셋시각을 추출한다.

감지는 구조적 필드(isApiErrorMessage + apiErrorStatus==429)로 한다 — 자유 텍스트 regex 아님.
"""
from __future__ import annotations
import datetime
import json
from dataclasses import dataclass
from pathlib import Path

from . import config, util


@dataclass
class LimitEvent:
    session_id: str | None
    cwd: str | None
    git_branch: str | None
    prompt: str
    prompt_id: str | None
    reset_text: str
    reset: tuple[int, int] | None   # 로컬 (hour, minute)
    ts: datetime.datetime | None
    uuid: str | None

    @property
    def key(self) -> str:
        return f"{self.session_id}:{self.prompt_id}"


def _parse_ts(s) -> datetime.datetime | None:
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def iter_session_files() -> list[Path]:
    """메인 세션 파일만 (서브에이전트 agent-*.jsonl 제외 — 같은 세션/프롬프트 중복)."""
    if not config.PROJECTS_DIR.exists():
        return []
    return [p for p in config.PROJECTS_DIR.rglob("*.jsonl") if not p.name.startswith("agent-")]


def _is_human_prompt(o: dict) -> bool:
    """tool_result 등 합성 user 메시지 제외하고 실제 사람이 친 프롬프트만."""
    if o.get("type") != "user" or o.get("isMeta"):
        return False
    c = o.get("message", {}).get("content")
    return isinstance(c, str) and bool(o.get("promptId") or o.get("promptSource"))


def find_limit_events(path: Path) -> list[LimitEvent]:
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []
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
        if not o.get("isApiErrorMessage"):
            continue
        if str(o.get("apiErrorStatus")) != "429" and o.get("error") != "rate_limit":
            continue
        # 에러 직전의 실제 사람 프롬프트 역방향 탐색
        prompt = prompt_id = None
        for j in range(i - 1, -1, -1):
            if _is_human_prompt(lines[j]):
                prompt = lines[j]["message"]["content"]
                prompt_id = lines[j].get("promptId")
                break
        if not prompt:
            continue
        # 에러 '이후'에 사람 프롬프트가 또 있으면 = 한도 풀린 뒤 대화를 계속한 것(막힌 게 아님)
        # → 활성 대화 중 잠깐 뜬 429를 큐에 넣지 않음. 진짜로 그 에러가 마지막(세션이 멈춤)일 때만 큐잉.
        if any(_is_human_prompt(lines[j]) for j in range(i + 1, len(lines))):
            continue
        text = util.extract_text(o.get("message", {}).get("content"))
        events.append(LimitEvent(
            session_id=o.get("sessionId"),
            cwd=o.get("cwd"),
            git_branch=o.get("gitBranch"),
            prompt=prompt,
            prompt_id=prompt_id,
            reset_text=text,
            reset=util.parse_reset_message(text),
            ts=_parse_ts(o.get("timestamp")),
            uuid=o.get("uuid"),
        ))
    return events
