"""감지 데몬. launchd KeepAlive로 항상 떠서 ~/.claude/projects 트랜스크립트를 주기적으로 스캔.
새 토큰 한도 이벤트 발견 → 실패 프롬프트를 큐에 등록 → 가장 이른 리셋 시각에 재실행 예약.

과거 에러 일괄 실행 방지: 첫 실행 때 start_time을 기록하고 그 이후 발생분만 큐잉한다.
중복 방지: (session_id, prompt_id) 키를 처리 집합에 저장.
"""
from __future__ import annotations
import datetime
import json
import os
import time

from . import config, telegram
from .schedulers import get_scheduler
from .store import JobStore
from .transcript import find_limit_events, iter_session_files


def _log(msg: str) -> None:
    config.ensure_dir()
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with (config.QDIR / "watcher.log").open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")


def _load_state() -> dict:
    if config.WATCH_STATE.exists():
        try:
            return json.loads(config.WATCH_STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(st: dict) -> None:
    config.ensure_dir()
    tmp = config.WATCH_STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(config.WATCH_STATE)


def scan_once(state: dict, store: JobStore) -> tuple[int, tuple[int, int] | None]:
    """새 한도 이벤트를 큐에 등록. (신규 건수, 가장 이른 reset(h,m)) 반환."""
    processed = set(state.get("processed", []))
    start_iso = state.get("start_time")
    start = datetime.datetime.fromisoformat(start_iso) if start_iso else None
    if start and start.tzinfo is None:
        # 트랜스크립트 ts는 tz-aware(UTC) → 비교 위해 aware로
        start = start.astimezone()
    mtimes = state.get("mtimes", {})

    new = 0
    resets: list[tuple[int, int]] = []
    queued: list[dict] = []
    for p in iter_session_files():
        sp = str(p)
        try:
            mt = p.stat().st_mtime
        except FileNotFoundError:
            continue
        if mtimes.get(sp) == mt:
            continue  # 변경 없는 파일 스킵
        for ev in find_limit_events(p):
            if ev.key in processed:
                continue
            # 데몬 시작 이전(과거) 에러는 무시 — 처리됨으로만 기록
            if start and ev.ts and ev.ts < start:
                processed.add(ev.key)
                continue
            reset_str = f"{ev.reset[0]:02d}:{ev.reset[1]:02d}" if ev.reset else None
            rec = store.add(
                ev.prompt,
                ev.cwd or os.getcwd(),
                session_id=ev.session_id,
                prompt_id=ev.prompt_id,
                reset=reset_str,
                source="watcher",
                resume=config.RESUME,
                created_at=datetime.datetime.now().isoformat(timespec="seconds"),
                dedup=True,
            )
            processed.add(ev.key)
            if rec is not None:
                new += 1
                _log(f"감지: session={ev.session_id} reset={reset_str} | {ev.prompt[:60]}")
                queued.append({
                    "prompt": ev.prompt,
                    "session_id": ev.session_id or "",
                    "cwd": ev.cwd or "",
                    "reset": reset_str,
                })
                if ev.reset:
                    resets.append(ev.reset)
        mtimes[sp] = mt

    state["processed"] = sorted(processed)
    state["mtimes"] = mtimes
    return new, (min(resets) if resets else None), queued


def tick() -> int:
    """1회 스캔 + 필요 시 예약. 신규 큐잉 건수 반환."""
    state = _load_state()
    if "start_time" not in state:
        state["start_time"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        _log("워처 시작 — 이후 발생하는 한도만 감지")
    store = JobStore()
    new, min_reset, queued = scan_once(state, store)
    if new:
        # 어떤 요청이 큐에 들어갔는지 상세히 텔레그램 보고
        msg = [f"🕒 토큰 한도 감지 — 재실행 큐에 {new}건 등록"]
        for i, q in enumerate(queued, 1):
            prm = " ".join((q["prompt"] or "").split())
            if len(prm) > 200:
                prm = prm[:200] + "…"
            msg.append(
                f"\n{i}) {prm}\n   ↳ 세션 {q['session_id'][:8]} · {q['cwd']}"
                + (f" · 리셋 {q['reset']}" if q["reset"] else "")
            )
        if min_reset:
            h, m = min_reset
            get_scheduler().schedule(h, m)
            _log(f"{new}건 큐 등록 → 재실행 예약 {h:02d}:{m:02d}")
            msg.append(f"\n⏰ {h:02d}:{m:02d}에 자동 재실행 예정 (큐 총 {store.count()}건)")
        else:
            _log(f"{new}건 큐 등록 (리셋 시각 미파싱 → 수동 예약 필요)")
            msg.append("\n⚠️ 리셋 시각 자동 추출 실패 — 'ctq at HH:MM'로 수동 예약 필요")
        telegram.send("\n".join(msg))
    _save_state(state)
    return new


def main() -> None:
    _log(f"watcher 데몬 시작 (interval={config.WATCH_INTERVAL}s)")
    while True:
        try:
            tick()
        except Exception as e:  # 데몬은 죽지 않게
            _log(f"tick 오류: {e!r}")
        time.sleep(config.WATCH_INTERVAL)


if __name__ == "__main__":
    main()
