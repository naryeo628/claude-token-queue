"""큐 드레인 러너. launchd가 예약 시각에 `python -m claude_token_queue.runner` 로 호출.
bash runner.sh와 동일 로직: 한도 미해제면 작업 보존 + N분 뒤 재예약."""
from __future__ import annotations
import datetime
import re
import subprocess

from . import config, util
from .schedulers import get_scheduler
from .store import JobStore


def _log(msg: str) -> None:
    config.ensure_dir()
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with config.LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")


def run_claude(cwd: str, prompt: str) -> str:
    """claude -p 헤드리스 실행. stdout+stderr 합쳐서 반환."""
    try:
        r = subprocess.run(
            [config.CLAUDE_BIN, "-p", prompt, "--output-format", "json"],
            cwd=cwd or None,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return f"claude 실행 불가: '{config.CLAUDE_BIN}' 없음 (CTQ_CLAUDE_BIN 확인)"
    return (r.stdout or "") + (r.stderr or "")


def hit_limit(out: str) -> bool:
    return re.search(config.LIMIT_PATTERN, out, re.I) is not None


def drain() -> dict:
    """큐를 순서대로 실행. 결과 요약 dict 반환."""
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
            remaining: list[str] = []
            for job in jobs:
                if stopped:
                    remaining.append(f"{job.cwd}{config.DELIM}{job.prompt}")
                    continue
                _log(f"실행 [{job.cwd}] {job.prompt}")
                out = run_claude(job.cwd, job.prompt)
                if hit_limit(out):
                    _log("아직 한도 — 작업 보존, 중단")
                    remaining.append(f"{job.cwd}{config.DELIM}{job.prompt}")
                    stopped = True
                else:
                    _log("완료")
                    done += 1
            store._write(remaining)
    except TimeoutError:
        _log("락 획득 실패 (다른 드레인 진행 중) → 스킵")
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
