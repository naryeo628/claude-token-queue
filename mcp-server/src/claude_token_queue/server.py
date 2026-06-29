"""MCP 서버 (server.py)
=======================
ctq의 모든 기능을 'MCP 도구(tool)'로 Claude에게 노출하는 서버.

[MCP(Model Context Protocol)란?]
  Claude 같은 AI가 외부 기능을 '도구(tool)'로 호출할 수 있게 하는 표준 프로토콜.
  쉽게 말해: Claude가 이 서버에 연결되면, Claude가 직접 ctq 명령을 실행할 수 있게 된다.
  사용자가 Claude에게 "큐 상태 알려줘"라고 하면 → Claude가 get_status() 도구 호출 → 결과 반환.

[서버 구동 방식]
  ctq-mcp 명령으로 실행 → Claude Code가 MCP 서버로 인식해 도구 목록을 가져온다.
  Claude Code 설정(mcp 섹션)에 등록해두면 항상 자동으로 연결된다.

[도구 카테고리]
  1. 자동 감지(워처): install_watcher / uninstall_watcher / watcher_status / scan_now
  2. 조회: get_plan / get_status / list_tasks / get_logs
  3. 수동 조작: run_task / enqueue_task / schedule_run / run_queue_now / remove_task / clear_tasks / cancel_schedule
  4. 텔레그램: send_telegram / telegram_status
"""
from __future__ import annotations
import os

from mcp.server.fastmcp import FastMCP

from . import config, telegram, util, watcher
from .daemon import WatcherAgent
from .runner import drain, hit_limit, run_claude
from .schedulers import get_scheduler
from .store import JobStore

# FastMCP: MCP 서버를 쉽게 만들 수 있는 프레임워크. 함수에 @mcp.tool()을 붙이면 도구가 됨.
mcp = FastMCP("claude-token-queue")
_store = JobStore()


def _resolve_cwd(cwd: str | None) -> str:
    """작업 디렉토리를 결정한다.
    우선순위: 명시적 cwd 인수 > CTQ_DEFAULT_CWD 환경변수 > 현재 디렉토리
    """
    return cwd or os.environ.get("CTQ_DEFAULT_CWD") or os.getcwd()


# ══════════════════════════════════════════════════════════════════════
# 1. 자동 감지(워처) 도구
# ══════════════════════════════════════════════════════════════════════

@mcp.tool()
def install_watcher() -> dict:
    """백그라운드 감지 데몬을 설치한다 (launchd KeepAlive).

    [설치 후 동작]
    - Mac이 켜져 있는 한 워처 프로세스가 항상 살아있다.
    - 30초마다 Claude 트랜스크립트를 스캔해 429 에러를 감지한다.
    - 감지 즉시 해당 요청을 큐에 등록하고 리셋 시각에 재실행을 예약한다.
    - 과거(설치 이전) 에러는 무시 → 오래된 에러가 갑자기 실행되는 사고 방지.
    """
    return WatcherAgent().install()


@mcp.tool()
def uninstall_watcher() -> dict:
    """감지 데몬을 제거한다. 큐에 있는 작업은 유지된다."""
    return WatcherAgent().uninstall()


@mcp.tool()
def watcher_status() -> dict:
    """감지 데몬의 현재 동작 상태를 반환한다.
    running=True면 정상 감시 중, False면 install_watcher로 재설치 필요.
    """
    return WatcherAgent().status()


@mcp.tool()
def scan_now() -> dict:
    """예약 주기(30초)를 기다리지 않고 지금 즉시 트랜스크립트를 스캔한다.
    새로 발견된 429 이벤트를 큐에 등록하고 신규 등록 건수를 반환한다.
    """
    new = watcher.tick()
    return {"ok": True, "new_queued": new, "queue_count": _store.count()}


# ══════════════════════════════════════════════════════════════════════
# 2. 조회 도구
# ══════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_plan() -> dict:
    """'무엇을 언제 실행할지' 전체 계획을 반환한다.

    [반환 내용]
    - 큐의 작업 목록 (실행 순서, 내용, resume 여부 등)
    - 다음 실행 예정 시각
    - 참고 메모(note): 예약 상태나 주의사항
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
    """큐 전체 상태를 한눈에 반환한다.

    [반환 내용]
    - queue_count: 현재 큐에 있는 작업 수
    - tasks: 작업 목록 상세
    - schedule: launchd 예약 상태
    - next_run: 다음 실행 예정 시각 및 남은 시간(분)
    - watcher: 감지 데몬 동작 상태
    """
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
    """현재 큐에 대기 중인 작업 목록만 반환한다. get_status의 tasks 부분만 빠르게 보는 용도."""
    return {"count": _store.count(), "tasks": [j.to_dict() for j in _store.list()]}


@mcp.tool()
def get_logs(lines: int = 40, which: str = "runner") -> dict:
    """로그 파일의 마지막 N줄을 반환한다.

    [인수]
    which: 어떤 로그를 볼지
      - "runner" (기본): 재실행 로그 (~/.claude-queue/runner.log)
      - "watcher": 감지 데몬 로그 (~/.claude-queue/watcher.log)
    lines: 가져올 줄 수 (기본 40줄)
    """
    path = (config.QDIR / "watcher.log") if which == "watcher" else config.LOG
    if not path.exists():
        return {"log": "", "note": f"{which} 로그 없음"}
    return {"log": "\n".join(path.read_text(encoding="utf-8").splitlines()[-lines:])}


# ══════════════════════════════════════════════════════════════════════
# 3. 수동 조작 도구
# ══════════════════════════════════════════════════════════════════════

@mcp.tool()
def run_task(prompt: str, cwd: str | None = None, auto_schedule: bool = True) -> dict:
    """claude를 지금 즉시 실행한다. 토큰 한도에 걸리면 자동으로 큐 등록 + 리셋 예약.

    [워처가 설치된 경우]
    보통 이 도구를 직접 쓸 필요 없다. 워처가 자동으로 감지해서 큐에 넣는다.
    워처 없이 수동으로 "지금 실행해봐, 안 되면 큐에 넣어줘"라는 식으로 쓸 때 유용.

    [인수]
    prompt: Claude에게 보낼 요청
    cwd: 실행 디렉토리 (없으면 현재 디렉토리)
    auto_schedule: 한도 시 자동 예약 여부 (기본 True)
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
    """작업을 큐에만 등록한다 (즉시 실행 안 함).

    [session_id를 주면]
    그 세션을 resume해서 재실행한다 (대화 맥락 유지).
    [session_id 없으면]
    새 세션으로 실행한다 (대화 맥락 없이 prompt만).
    """
    cwd = _resolve_cwd(cwd)
    rec = _store.add(prompt, cwd, session_id=session_id,
                     resume=bool(session_id), source="manual")
    return {"ok": True, "queued": rec, "count": _store.count()}


@mcp.tool()
def schedule_run(at: str) -> dict:
    """재실행 시각을 예약한다.

    [at 인수 형식]
    - "HH:MM": 절대 시각 (예: "20:10")
    - "+30m": 지금부터 30분 뒤
    - "+2h": 지금부터 2시간 뒤

    큐가 비어있으면 예약 시각에 실행해도 아무것도 안 하고 자동 해제된다(1회성).
    """
    h, m = util.parse_when(at)
    get_scheduler().schedule(h, m)
    return {"ok": True, "scheduled": f"{h:02d}:{m:02d}", "queue_count": _store.count()}


@mcp.tool()
def run_queue_now() -> dict:
    """예약 시각을 기다리지 않고 지금 즉시 큐를 실행한다.
    토큰이 충분한지 확인하고 싶을 때 수동으로 트리거할 때 사용.
    """
    return drain()


@mcp.tool()
def remove_task(index: int) -> dict:
    """큐에서 특정 작업을 제거한다.
    index는 get_plan/list_tasks에서 보이는 1-기준 번호.
    """
    removed = _store.remove(index)
    return {"ok": True, "removed": removed, "count": _store.count()}


@mcp.tool()
def clear_tasks() -> dict:
    """큐 전체를 비운다. 예약도 함께 해제된다."""
    return {"ok": True, "cleared": _store.clear()}


@mcp.tool()
def cancel_schedule() -> dict:
    """재실행 예약만 해제한다. 큐에 있는 작업은 유지된다.
    예약 시각이 잘못됐거나 수동으로 취소하고 싶을 때 사용.
    """
    get_scheduler().cancel()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════
# 4. 텔레그램 도구
# ══════════════════════════════════════════════════════════════════════

@mcp.tool()
def send_telegram(text: str) -> dict:
    """텔레그램으로 임의 메시지를 발송한다.
    재실행 보고와 같은 봇/채팅으로 보낸다. 수동 알림이나 테스트용.
    CTQ_TELEGRAM_TOKEN / CTQ_TELEGRAM_CHAT 설정이 되어 있어야 동작한다.
    """
    if not telegram.enabled():
        return {"ok": False, "note": "텔레그램 미설정 (CTQ_TELEGRAM_TOKEN/CHAT 필요)"}
    return {"ok": telegram.send(text)}


@mcp.tool()
def telegram_status() -> dict:
    """텔레그램 보고 설정 상태를 반환한다.
    토큰 값은 보안상 노출하지 않고 설정 여부만 표시한다.
    """
    return {
        "enabled": telegram.enabled(),
        "chat": config.TELEGRAM_CHAT or None,
        "token_set": bool(config.TELEGRAM_TOKEN),
    }


def main() -> None:
    """ctq-mcp 진입점. Claude Code가 이 서버를 MCP 서버로 인식해 연결한다."""
    mcp.run()


if __name__ == "__main__":
    main()
