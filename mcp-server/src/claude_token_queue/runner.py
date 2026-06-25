"""큐 드레인 러너. launchd가 예약 시각에 `ctq-runner`(또는 python -m ...runner)로 호출.

무인 실행 + 실황 모니터링:
- 백그라운드에서 claude를 stream-json으로 실행(진실 소스). 사용자가 없어도 동작.
- 진행 내용을 라이브 로그파일에 사람이 읽기 좋게 기록하고, 리셋 시각에 모니터링
  터미널(Terminal.app)을 띄워 그 파일을 tail -f → 사용자가 있으면 실황을 본다.
- 결과는 results/에 저장, 끝나면 macOS 알림.

판정: stream-json 최종 result 이벤트의 is_error로 성공/실패 구분(429 한도/그 외 에러/성공).
구 CLI 기본 모델이 죽어 404 → config.CLAUDE_MODEL 명시. resume replay는 400나기 쉬워
기본 off(새 세션). resume 시도 시 실패하면 새 세션으로 폴백."""
from __future__ import annotations
import datetime
import json
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


def _ts() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def hit_limit(out: str) -> bool:
    return re.search(config.LIMIT_PATTERN, out, re.I) is not None


def _open_monitor(logfile) -> None:
    """리셋 시각에 모니터링 터미널을 띄워 라이브 로그를 tail -f (있을 때 실황 보기)."""
    if not config.MONITOR:
        return
    try:
        script = (
            'tell application "Terminal"\n'
            f'  do script "echo ⏯  claude-token-queue 재실행 실황; tail -n +1 -f \\"{logfile}\\""\n'
            "  activate\n"
            "end tell"
        )
        subprocess.run(["osascript", "-e", script], capture_output=True)
    except Exception as e:
        _log(f"모니터 터미널 오픈 실패(무시): {e!r}")


def _notify(title: str, msg: str) -> None:
    try:
        body = msg.replace('"', "'")
        subprocess.run(
            ["osascript", "-e", f'display notification "{body}" with title "{title}"'],
            capture_output=True,
        )
    except Exception:
        pass


def _build_cmd(job: Job, resume: bool) -> list[str]:
    cmd = [config.CLAUDE_BIN]
    if resume and job.session_id:
        cmd += ["--resume", job.session_id]
    cmd += ["-p", job.prompt, "--output-format", "stream-json", "--verbose"]
    if config.CLAUDE_MODEL:
        cmd += ["--model", config.CLAUDE_MODEL]
    if config.SKIP_PERMISSIONS:
        cmd += ["--dangerously-skip-permissions"]
    return cmd


def _humanize(line: str) -> str | None:
    """stream-json 이벤트를 모니터 터미널용 사람 읽기 형태로."""
    try:
        ev = json.loads(line)
    except Exception:
        return None
    t = ev.get("type")
    if t == "system":
        return f"  · 세션 시작 (model={ev.get('model')})"
    if t == "assistant":
        txt = util.extract_text(ev.get("message", {}).get("content", []))
        return f"  🤖 {txt}" if txt.strip() else None
    if t == "user":  # tool_result 등
        return "  · (도구 결과)"
    if t == "result":
        mark = "❌ 에러" if ev.get("is_error") else "✅ 완료"
        return f"  {mark}: {str(ev.get('result', ''))[:300]}"
    return None


def _run_streaming(cmd: list[str], cwd: str, batchlog) -> tuple[bool, str, int]:
    """stream-json 실행. 라이브로 batchlog 기록. (is_error, result_text, rc) 반환."""
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd or None, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
    except FileNotFoundError:
        with open(batchlog, "a", encoding="utf-8") as f:
            f.write(f"  ❌ claude 실행 불가: {config.CLAUDE_BIN}\n")
        return True, f"claude 없음: {config.CLAUDE_BIN}", 127

    is_error = True  # result 이벤트 못 보면 실패로 간주
    result_text = ""
    with open(batchlog, "a", encoding="utf-8") as lf:
        for line in proc.stdout:
            line = line.rstrip("\n")
            human = _humanize(line)
            if human:
                lf.write(human + "\n")
                lf.flush()
            try:
                ev = json.loads(line)
                if ev.get("type") == "result":
                    is_error = bool(ev.get("is_error"))
                    result_text = str(ev.get("result", ""))
            except Exception:
                pass
    rc = proc.wait()
    return is_error, result_text, rc


def _run_job(job: Job, idx: int, total: int, batchlog) -> tuple[str, str]:
    """반환: (상태, 결과텍스트). 상태 ∈ done|limited|error."""
    with open(batchlog, "a", encoding="utf-8") as f:
        tag = f"resume {job.session_id[:8]}" if (job.resume and job.session_id) else "새 세션"
        f.write(f"\n[{idx}/{total}] ({tag}) {job.prompt[:90]}\n  cwd={job.cwd}\n")
    _log(f"실행 [{job.cwd}] {job.prompt[:60]}")

    ie, res, rc = _run_streaming(_build_cmd(job, job.resume), job.cwd, batchlog)
    # resume 실패(비한도 에러)면 새 세션으로 폴백
    if job.resume and ie and not hit_limit(res):
        with open(batchlog, "a", encoding="utf-8") as f:
            f.write("  ↻ resume 실패 → 새 세션으로 재실행\n")
        _log("resume 실패 → 새 세션 폴백")
        ie, res, rc = _run_streaming(_build_cmd(job, False), job.cwd, batchlog)

    if ie and hit_limit(res):
        return "limited", res
    if ie:
        return "error", res
    return "done", res


def drain() -> dict:
    store = JobStore()
    sched = get_scheduler()
    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    batchlog = config.RESULTS_DIR / f"run-{_ts()}.log"
    done = errors = 0
    stopped = False

    try:
        with util.queue_lock(timeout=5):
            jobs = store.list()
            if not jobs:
                sched.cancel()
                return {"drained": 0, "remaining": 0, "stopped": False}

            with open(batchlog, "w", encoding="utf-8") as f:
                f.write(f"=== claude-token-queue 재실행 "
                        f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} · {len(jobs)}건 ===\n")
            _open_monitor(batchlog)

            remaining: list[Job] = []
            total = len(jobs)
            for idx, job in enumerate(jobs, 1):
                if stopped:
                    remaining.append(job)
                    continue
                status, res = _run_job(job, idx, total, batchlog)
                if status == "limited":
                    _log("아직 한도 — 작업 보존, 중단")
                    remaining.append(job)
                    stopped = True
                elif status == "error":
                    _log(f"비한도 에러 — 작업 보존: {res[:80]}")
                    errors += 1
                    remaining.append(job)  # 조용히 버리지 않음
                else:
                    _log("완료")
                    done += 1
                    out_path = config.RESULTS_DIR / f"{_ts()}-{(job.session_id or 'job')[:8]}.md"
                    out_path.write_text(
                        f"# {job.prompt}\n\n- cwd: {job.cwd}\n- session: {job.session_id}\n\n---\n\n{res}\n",
                        encoding="utf-8",
                    )
            store._write_records([j.to_record() for j in remaining])
            store._clear_legacy()
    except TimeoutError:
        _log("락 획득 실패 (다른 드레인/스캔 중) → 스킵")
        return {"skipped": True, "remaining": store.count()}

    if stopped:
        t = datetime.datetime.now() + datetime.timedelta(minutes=config.RETRY_DELAY_MIN)
        sched.schedule(t.hour, t.minute)
        _log(f"한도 미해제 → {config.RETRY_DELAY_MIN}분 뒤({t:%H:%M}) 재예약")
    elif store.count() == 0:
        sched.cancel()
        _log("큐 전부 처리 → 예약 해제")

    msg = f"완료 {done} · 에러 {errors} · 남음 {store.count()}"
    try:
        with open(batchlog, "a", encoding="utf-8") as f:
            f.write(f"\n=== 끝: {msg} ===\n")
    except Exception:
        pass
    _notify("claude-token-queue 재실행", msg)
    _log(f"드레인 종료: {msg}")
    return {"drained": done, "errors": errors, "remaining": store.count(), "log": str(batchlog)}


def run_claude(cwd: str, prompt: str) -> str:
    """헤드리스 1회 실행 (run_task용). 유효 모델 명시."""
    cmd = [config.CLAUDE_BIN, "-p", prompt, "--output-format", "json"]
    if config.CLAUDE_MODEL:
        cmd += ["--model", config.CLAUDE_MODEL]
    if config.SKIP_PERMISSIONS:
        cmd += ["--dangerously-skip-permissions"]
    try:
        r = subprocess.run(cmd, cwd=cwd or None, capture_output=True, text=True)
    except FileNotFoundError:
        return f"claude 실행 불가: '{config.CLAUDE_BIN}' 없음"
    return (r.stdout or "") + (r.stderr or "")


def main() -> None:
    drain()


if __name__ == "__main__":
    main()
