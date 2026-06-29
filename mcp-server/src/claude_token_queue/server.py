"""claude-token-queue MCP 서버 (stdio). Claude Code 등 MCP 클라이언트에서 도구로 노출.

자동 감지(워처):
  install_watcher   백그라운드 감지 데몬 설치 — 클로드 앱이 한도 치면 자동 큐+예약
  uninstall_watcher / watcher_status
  scan_now          지금 즉시 트랜스크립트 스캔
조회:
  get_plan          무엇을 언제 실행할지 (실행 순서 + 예정시각)
  get_status / list_tasks / get_logs
수동:
  run_task / enqueue_task / schedule_run / run_queue_now / remove_task / clear_tasks / cancel_schedule
"""
from __future__ import annotations
import os

from mcp.server.fastmcp import FastMCP

from . import config, telegram, util, watcher
from .daemon import WatcherAgent
from .runner import drain, hit_limit, run_claude
from .schedulers import get_scheduler
from .store import JobStore

mcp = FastMCP("claude-token-queue")
_store = JobStore()


def _resolve_cwd(cwd: str | None) -> str:
    return cwd or os.environ.get("CTQ_DEFAULT_CWD") or os.getcwd()


# ───────────────────────── 자동 감지 (워처) ─────────────────────────

@mcp.tool()
def install_watcher() -> dict:
    """백그라운드 감지 데몬을 설치(launchd KeepAlive)한다.
    설치되면 클로드 앱의 어떤 세션이든 토큰 한도(429)에 걸리는 순간,
    사용자가 아무것도 안 해도 실패한 프롬프트를 자동으로 큐에 담고 리셋 시각에 재실행 예약한다.
    과거 한도가 아니라 '설치 이후' 발생분만 감지한다.
    """
    return WatcherAgent().install()


@mcp.tool()
def uninstall_watcher() -> dict:
    """감지 데몬을 제거한다 (큐는 유지)."""
    return WatcherAgent().uninstall()


@mcp.tool()
def watcher_status() -> dict:
    """감지 데몬 동작 상태."""
    return WatcherAgent().status()


@mcp.tool()
def scan_now() -> dict:
    """예약 주기를 기다리지 않고 지금 즉시 트랜스크립트를 스캔해 새 한도 이벤트를 큐에 등록한다."""
    new = watcher.tick()
    return {"ok": True, "new_queued": new, "queue_count": _store.count()}


# ───────────────────────── 조회 ─────────────────────────

@mcp.tool()
def get_plan() -> dict:
    """'무엇을 언제 실행할지' 조회. 큐의 작업을 실행 순서대로, 다음 실행 예정 시각과 함께 반환.
    큐는 예약 시각에 1번부터 순차 실행된다(이전 작업이 끝나면 다음).
    """
    sched = get_scheduler()
    nxt = sched.next_run()
    jobs = _store.list()
    plan = [
        {
            "order": j.index,
            "prompt": j.prompt,
            "cwd": j.cwd,
            "session_id": j.session_id,
            "resume": j.resume,
            "reset_seen": j.reset,
            "source": j.source,
            "starts": "예약 시각" if j.index == 1 else "앞 작업 완료 후",
        }
        for j in jobs
    ]
    if not jobs:
        note = "큐 비어 있음."
    elif not nxt or not nxt.get("loaded"):
        note = "예약 없음 → schedule_run('HH:MM')으로 시각 지정 필요 (워처가 자동 예약하기도 함)."
    else:
        note = f"{nxt['scheduled_time']}({nxt['in_minutes']}분 뒤)에 {len(jobs)}건 순차 실행 예정."
    return {"count": len(jobs), "next_run": nxt, "plan": plan, "note": note}


@mcp.tool()
def get_status() -> dict:
    """큐 + 예약 + 다음 실행 예정 + 감지 데몬 상태 한눈에."""
    sched = get_scheduler()
    return {
        "queue_count": _store.count(),
        "tasks": [j.to_dict() for j in _store.list()],
        "schedule": sched.status(),
        "next_run": sched.next_run(),
        "watcher": WatcherAgent().status(),
    }


@mcp.tool()
def list_tasks() -> dict:
    """현재 큐에 대기 중인 작업 목록."""
    return {"count": _store.count(), "tasks": [j.to_dict() for j in _store.list()]}


@mcp.tool()
def send_telegram(text: str) -> dict:
    """텔레그램으로 임의 메시지 발송. (CTQ_TELEGRAM_TOKEN/CHAT 설정돼 있어야 동작.)
    재실행 보고와 같은 봇/채팅으로 보낸다. 수동 알림·테스트용.
    """
    if not telegram.enabled():
        return {"ok": False, "note": "텔레그램 미설정 (CTQ_TELEGRAM_TOKEN/CHAT 필요)"}
    return {"ok": telegram.send(text)}


@mcp.tool()
def telegram_status() -> dict:
    """텔레그램 보고 설정 상태 (토큰은 노출 안 함)."""
    return {
        "enabled": telegram.enabled(),
        "chat": config.TELEGRAM_CHAT or None,
        "token_set": bool(config.TELEGRAM_TOKEN),
    }


@mcp.tool()
def get_logs(lines: int = 40, which: str = "runner") -> dict:
    """로그 마지막 N줄. which='runner'(재실행) 또는 'watcher'(감지)."""
    path = (config.QDIR / "watcher.log") if which == "watcher" else config.LOG
    if not path.exists():
        return {"log": "", "note": f"{which} 로그 없음"}
    return {"log": "\n".join(path.read_text(encoding="utf-8").splitlines()[-lines:])}


# ───────────────────────── 수동 조작 ─────────────────────────

@mcp.tool()
def run_task(prompt: str, cwd: str | None = None, auto_schedule: bool = True) -> dict:
    """claude로 작업을 지금 즉시 실행한다. 한도에 걸리면 자동 큐 등록 + 리셋 시각 예약.
    (워처를 깔았다면 보통 이걸 직접 쓸 필요 없음 — 자동 감지됨.)
    """
    cwd = _resolve_cwd(cwd)
    out = run_claude(cwd, prompt)
    if hit_limit(out):
        _store.add(prompt, cwd, source="run", resume=False)
        info: dict = {"ok": False, "limited": True, "queued": True, "cwd": cwd}
        when = util.parse_reset_message(out)
        if when and auto_schedule:
            h, m = when
            get_scheduler().schedule(h, m)
            info["scheduled"] = f"{h:02d}:{m:02d}"
        else:
            info["note"] = "리셋 시각 자동추출 실패 → schedule_run('HH:MM')으로 직접 예약"
        return info
    return {"ok": True, "limited": False, "result": out}


@mcp.tool()
def enqueue_task(prompt: str, cwd: str | None = None, session_id: str | None = None) -> dict:
    """작업을 큐에만 등록한다 (실행 안 함). session_id를 주면 그 세션을 resume해서 재실행한다."""
    cwd = _resolve_cwd(cwd)
    rec = _store.add(prompt, cwd, session_id=session_id,
                     resume=bool(session_id), source="manual")
    return {"ok": True, "queued": rec, "count": _store.count()}


@mcp.tool()
def schedule_run(at: str) -> dict:
    """재실행 시각 예약. at = 'HH:MM' / '+30m' / '+2h'. 큐 비면 자동 해제(1회성)."""
    h, m = util.parse_when(at)
    get_scheduler().schedule(h, m)
    return {"ok": True, "scheduled": f"{h:02d}:{m:02d}", "queue_count": _store.count()}


@mcp.tool()
def run_queue_now() -> dict:
    """예약 시각을 기다리지 않고 지금 즉시 큐를 실행한다."""
    return drain()


@mcp.tool()
def remove_task(index: int) -> dict:
    """큐에서 특정 작업 제거 (index는 get_plan/list_tasks의 1-기준 번호)."""
    removed = _store.remove(index)
    return {"ok": True, "removed": removed, "count": _store.count()}


@mcp.tool()
def clear_tasks() -> dict:
    """큐 전체 비우기."""
    return {"ok": True, "cleared": _store.clear()}


@mcp.tool()
def cancel_schedule() -> dict:
    """재실행 예약 해제 (큐는 유지)."""
    get_scheduler().cancel()
    return {"ok": True}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
