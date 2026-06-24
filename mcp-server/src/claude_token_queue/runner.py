"""큐 드레인 러너. launchd가 예약 시각에 `python -m claude_token_queue.runner` 로 호출.
재실행: resume=True면 원래 세션을 이어서(claude --resume <sessionId> -p ...), 아니면 헤드리스(claude -p).
한도 미해제면 작업 보존 + N분 뒤 재예약."""
from __future__ import annotations
import datetime
import re
import subprocess

from . import config, util
from .schedulers import get_scheduler
from .store import Job, JobStore


def _log(msg: str) -> None:
    config.ensure_dir()
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with config.LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")


def _exec(cmd: list[str], cwd: str | None) -> tuple[str, int]:
    try:
        r = subprocess.run(cmd, cwd=cwd or None, capture_output=True, text=True)
    except FileNotFoundError:
        return (f"claude 실행 불가: '{config.CLAUDE_BIN}' 없음 (CTQ_CLAUDE_BIN 확인)", 127)
    return ((r.stdout or "") + (r.stderr or ""), r.returncode)


def hit_limit(out: str) -> bool:
    return re.search(config.LIMIT_PATTERN, out, re.I) is not None


def run_claude(cwd: str, prompt: str) -> str:
    """헤드리스 1회 실행 (run_task용)."""
    out, _ = _exec([config.CLAUDE_BIN, "-p", prompt, "--output-format", "json"], cwd)
    return out


def run_job(job: Job) -> str:
    """resume 우선, 실패 시 헤드리스 폴백. 출력 텍스트 반환."""
    base = [config.CLAUDE_BIN, "-p", job.prompt, "--output-format", "json"]
    if job.resume and job.session_id:
        out, rc = _exec(
            [config.CLAUDE_BIN, "--resume", job.session_id, "-p", job.prompt,
             "--output-format", "json"],
            job.cwd,
        )
        # 한도 아니고 실패면(세션 못 찾음 등) 헤드리스로 폴백
        if rc != 0 and not hit_limit(out):
            _log(f"resume 실패(rc={rc}) → 헤드리스 폴백: {job.session_id}")
            out, _ = _exec(base, job.cwd)
        return out
    out, _ = _exec(base, job.cwd)
    return out


def drain() -> dict:
    store = JobStore()
    sched = get_scheduler()
    done = 0
    stopped = False

    try:
        with util.queue_lock(timeout=5):
            jobs = store.list()
            if not jobs:
                sched.cancel()
                return {"drained": 0, "remaining": 0, "stopped": False}
            remaining: list[Job] = []
            for job in jobs:
                if stopped:
                    remaining.append(job)
                    continue
                tag = f"resume {job.session_id}" if (job.resume and job.session_id) else "headless"
                _log(f"실행({tag}) [{job.cwd}] {job.prompt[:60]}")
                out = run_job(job)
                if hit_limit(out):
                    _log("아직 한도 — 작업 보존, 중단")
                    remaining.append(job)
                    stopped = True
                else:
                    _log("완료")
                    done += 1
            store._write_records([j.to_record() for j in remaining])
            store._clear_legacy()  # 레거시 jobs.txt 항목은 queue.jsonl로 이관됨
    except TimeoutError:
        _log("락 획득 실패 (다른 드레인/스캔 진행 중) → 스킵")
        return {"drained": 0, "remaining": store.count(), "stopped": False, "skipped": True}

    if stopped:
        t = datetime.datetime.now() + datetime.timedelta(minutes=config.RETRY_DELAY_MIN)
        sched.schedule(t.hour, t.minute)
        _log(f"한도 미해제 → {config.RETRY_DELAY_MIN}분 뒤({t.hour:02d}:{t.minute:02d}) 재예약")
    elif store.count() == 0:
        sched.cancel()
        _log("큐 전부 완료 → 예약 해제")

    return {"drained": done, "remaining": store.count(), "stopped": stopped}


def main() -> None:
    drain()


if __name__ == "__main__":
    main()
