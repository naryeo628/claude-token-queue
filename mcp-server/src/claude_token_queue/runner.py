"""큐 드레인 러너. launchd가 예약 시각에 `ctq-runner`로 호출.

무인 실행 + 실황 모니터링 + 텔레그램 보고:
- claude를 stream-json으로 실행(진실 소스). 사용자 없어도 동작.
- 진행을 라이브 로그파일에 사람 읽기 좋게 기록 → 리셋 때 Terminal.app 자동 오픈해 tail -f.
- 결과 results/*.md 저장 + macOS 알림 + (설정 시) 텔레그램 보고.
- resume(컨텍스트 복원, CLI 2.x)으로 원래 세션 이어감. 실패 시 새 세션 폴백.

안정화:
- RUN_TIMEOUT 초과 시 강제 종료 → 에러 처리(백그라운드 대기 hang 방지).
- 비한도 에러는 작업 보존하되 MAX_ATTEMPTS 초과하면 제거+수동필요 알림(무한 재시도 방지).
- 판정은 stream-json 최종 result의 is_error로.
"""
from __future__ import annotations
import datetime
import json
import re
import subprocess
import threading

from . import config, telegram, util
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
    """리셋 시각에 모니터링 터미널을 띄워 라이브 로그를 tail -f."""
    if not config.MONITOR:
        return
    try:
        script = (
            'tell application "Terminal"\n'
            f'  do script "echo claude-token-queue 재실행 실황; tail -n +1 -f \\"{logfile}\\""\n'
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
    """stream-json 이벤트를 모니터용 사람 읽기 형태로 (노이즈 제거: system/tool_result 숨김)."""
    try:
        ev = json.loads(line)
    except Exception:
        return None
    if ev.get("type") == "assistant":
        out = []
        for b in ev.get("message", {}).get("content", []) or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and (b.get("text") or "").strip():
                out.append("  🤖 " + b["text"].strip())
            elif b.get("type") == "tool_use":
                out.append(f"  🔧 {b.get('name')}")
        return "\n".join(out) if out else None
    if ev.get("type") == "result":
        mark = "❌ 에러" if ev.get("is_error") else "✅ 완료"
        return f"  {mark}: {str(ev.get('result', ''))[:300]}"
    return None


def _run_streaming(cmd: list[str], cwd: str, batchlog) -> tuple[bool, str, int]:
    """stream-json 실행 + 라이브 로그 + 타임아웃. (is_error, result_text, rc) 반환."""
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd or None, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
    except FileNotFoundError:
        with open(batchlog, "a", encoding="utf-8") as f:
            f.write(f"  ❌ claude 실행 불가: {config.CLAUDE_BIN}\n")
        return True, f"claude 없음: {config.CLAUDE_BIN}", 127

    killed = {"v": False}

    def _kill():
        killed["v"] = True
        try:
            proc.kill()
        except Exception:
            pass

    timer = threading.Timer(config.RUN_TIMEOUT, _kill)
    timer.start()
    is_error = True  # result 못 보면 실패로 간주
    result_text = ""
    try:
        with open(batchlog, "a", encoding="utf-8") as lf:
            for line in proc.stdout:
                human = _humanize(line.rstrip("\n"))
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
    finally:
        timer.cancel()
    rc = proc.wait()
    if killed["v"]:
        with open(batchlog, "a", encoding="utf-8") as f:
            f.write(f"  ⏱ 타임아웃 {config.RUN_TIMEOUT}s 초과 → 강제 종료\n")
        return True, f"timeout {config.RUN_TIMEOUT}s 초과", -9
    return is_error, result_text, rc


def _run_job(job: Job, idx: int, total: int, batchlog) -> tuple[str, str]:
    """반환: (상태, 결과텍스트). 상태 ∈ done|limited|error."""
    with open(batchlog, "a", encoding="utf-8") as f:
        tag = f"resume {job.session_id[:8]}" if (job.resume and job.session_id) else "새 세션"
        f.write(f"\n[{idx}/{total}] ({tag}) {job.prompt[:90]}\n  cwd={job.cwd}\n")
    _log(f"실행 [{job.cwd}] {job.prompt[:60]}")

    ie, res, rc = _run_streaming(_build_cmd(job, job.resume), job.cwd, batchlog)
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
    done = errors = dropped = 0
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
            telegram.send(f"🔔 claude-token-queue 재실행 시작 · {len(jobs)}건")

            remaining: list[Job] = []
            total = len(jobs)
            for idx, job in enumerate(jobs, 1):
                if stopped:
                    remaining.append(job)
                    continue
                status, res = _run_job(job, idx, total, batchlog)
                head = job.prompt[:100]
                if status == "limited":
                    _log("아직 한도 — 작업 보존, 중단")
                    remaining.append(job)
                    stopped = True
                    telegram.send(f"⏳ 아직 한도 — 이후 작업 보류\n{head}")
                elif status == "error":
                    job.attempts += 1
                    errors += 1
                    if job.attempts >= config.MAX_ATTEMPTS:
                        dropped += 1
                        _log(f"{job.attempts}회 연속 에러 → 큐에서 제거(수동 필요)")
                        telegram.send(f"⛔ {config.MAX_ATTEMPTS}회 실패 → 수동 처리 필요\n{head}\n{res[:150]}")
                    else:
                        remaining.append(job)
                        _log(f"에러({job.attempts}/{config.MAX_ATTEMPTS}) — 작업 보존")
                        telegram.send(f"❌ 에러({job.attempts}/{config.MAX_ATTEMPTS}) 재시도 예정\n{head}\n{res[:150]}")
                else:
                    done += 1
                    out_path = config.RESULTS_DIR / f"{_ts()}-{(job.session_id or 'job')[:8]}.md"
                    out_path.write_text(
                        f"# {job.prompt}\n\n- cwd: {job.cwd}\n- session: {job.session_id}\n\n---\n\n{res}\n",
                        encoding="utf-8",
                    )
                    _log("완료")
                    telegram.send(f"✅ 완료\n{head}")
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

    msg = f"완료 {done} · 에러 {errors} · 제거 {dropped} · 남음 {store.count()}"
    try:
        with open(batchlog, "a", encoding="utf-8") as f:
            f.write(f"\n=== 끝: {msg} ===\n")
    except Exception:
        pass
    _notify("claude-token-queue 재실행", msg)
    telegram.send(f"🏁 재실행 종료 · {msg}")
    _log(f"드레인 종료: {msg}")
    return {"drained": done, "errors": errors, "dropped": dropped,
            "remaining": store.count(), "log": str(batchlog)}


def run_claude(cwd: str, prompt: str) -> str:
    """헤드리스 1회 실행 (run_task용). 유효 모델 명시."""
    cmd = [config.CLAUDE_BIN, "-p", prompt, "--output-format", "json"]
    if config.CLAUDE_MODEL:
        cmd += ["--model", config.CLAUDE_MODEL]
    if config.SKIP_PERMISSIONS:
        cmd += ["--dangerously-skip-permissions"]
    try:
        r = subprocess.run(cmd, cwd=cwd or None, capture_output=True, text=True,
                           timeout=config.RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"timeout {config.RUN_TIMEOUT}s 초과"
    except FileNotFoundError:
        return f"claude 실행 불가: '{config.CLAUDE_BIN}' 없음"
    return (r.stdout or "") + (r.stderr or "")


def main() -> None:
    drain()


if __name__ == "__main__":
    main()
