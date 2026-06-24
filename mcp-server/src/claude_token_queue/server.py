"""claude-token-queue MCP 서버 (stdio). Claude Code 등 MCP 클라이언트에서 도구로 노출.

도구 요약:
  run_task        지금 실행. 한도면 자동 큐+예약
  enqueue_task    큐에만 등록
  schedule_run    재실행 시각 예약 (HH:MM | +30m | +2h)
  run_queue_now   지금 즉시 큐 실행
  list_tasks / remove_task / clear_tasks
  cancel_schedule / get_status / get_logs
"""
from __future__ import annotations
import os

from mcp.server.fastmcp import FastMCP

from . import config, util
from .runner import drain, hit_limit, run_claude
from .schedulers import get_scheduler
from .store import JobStore

mcp = FastMCP("claude-token-queue")
_store = JobStore()


def _resolve_cwd(cwd: str | None) -> str:
    return cwd or os.environ.get("CTQ_DEFAULT_CWD") or os.getcwd()


@mcp.tool()
def run_task(prompt: str, cwd: str | None = None, auto_schedule: bool = True) -> dict:
    """claude -p로 작업을 지금 즉시 실행한다.
    토큰 한도에 걸리면 자동으로 큐에 등록하고, 에러 메시지에서 리셋 시각을 추출해 예약한다.
    추출 실패 시 schedule_run으로 직접 예약하면 된다.

    prompt: claude에 보낼 작업 (세션 컨텍스트 없는 1회성 실행 → 독립적으로 작성).
    cwd: 실행 디렉토리 (생략 시 서버 cwd 또는 CTQ_DEFAULT_CWD).
    auto_schedule: 한도 시 리셋 시각 자동 예약 여부.
    """
    cwd = _resolve_cwd(cwd)
    out = run_claude(cwd, prompt)
    if hit_limit(out):
        _store.add(prompt, cwd)
        info: dict = {"ok": False, "limited": True, "queued": True, "cwd": cwd}
        when = util.parse_reset(out)
        if when and auto_schedule:
            h, m = when
            get_scheduler().schedule(h, m)
            info["scheduled"] = f"{h:02d}:{m:02d}"
        else:
            info["note"] = "리셋 시각 자동추출 실패 → schedule_run('HH:MM')으로 직접 예약"
        return info
    return {"ok": True, "limited": False, "result": out}


@mcp.tool()
def enqueue_task(prompt: str, cwd: str | None = None) -> dict:
    """작업을 큐에만 등록한다 (실행하지 않음). 대화형에서 막힌 작업을 옮길 때 사용.
    이후 schedule_run으로 시각을 예약하면 그 시각에 자동 실행된다.
    """
    cwd = _resolve_cwd(cwd)
    job = _store.add(prompt, cwd)
    return {"ok": True, "queued": job.to_dict(), "count": _store.count()}


@mcp.tool()
def schedule_run(at: str) -> dict:
    """재실행 시각을 예약한다. 큐가 비면 예약은 자동 해제된다(1회성).

    at: 'HH:MM' 절대시각 또는 '+30m' / '+2h' 상대시각.
    """
    h, m = util.parse_when(at)
    get_scheduler().schedule(h, m)
    return {"ok": True, "scheduled": f"{h:02d}:{m:02d}", "queue_count": _store.count()}


@mcp.tool()
def run_queue_now() -> dict:
    """예약 시각을 기다리지 않고 지금 즉시 큐를 실행한다 (디버그/수동 트리거용)."""
    return drain()


@mcp.tool()
def list_tasks() -> dict:
    """현재 큐에 대기 중인 작업 목록."""
    return {"count": _store.count(), "tasks": [j.to_dict() for j in _store.list()]}


@mcp.tool()
def remove_task(index: int) -> dict:
    """큐에서 특정 작업 제거 (index는 list_tasks의 1-기준 번호)."""
    job = _store.remove(index)
    return {"ok": True, "removed": job.to_dict(), "count": _store.count()}


@mcp.tool()
def clear_tasks() -> dict:
    """큐 전체 비우기."""
    n = _store.clear()
    return {"ok": True, "cleared": n}


@mcp.tool()
def cancel_schedule() -> dict:
    """예약 해제 (큐는 유지)."""
    get_scheduler().cancel()
    return {"ok": True}


@mcp.tool()
def get_status() -> dict:
    """큐 + 예약 상태 한눈에."""
    return {
        "queue_count": _store.count(),
        "tasks": [j.to_dict() for j in _store.list()],
        "schedule": get_scheduler().status(),
    }


@mcp.tool()
def get_logs(lines: int = 40) -> dict:
    """러너 실행 로그 마지막 N줄."""
    if not config.LOG.exists():
        return {"log": "", "note": "로그 없음"}
    tail = config.LOG.read_text(encoding="utf-8").splitlines()[-lines:]
    return {"log": "\n".join(tail)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
