"""큐 드레인 러너 (runner.py)
============================
이 파일은 큐에 쌓인 작업들을 실제로 실행하는 '실행기' 역할을 한다.

[역할]
  - launchd가 예약 시각(토큰 리셋 시각)에 `ctq-runner` 명령으로 이 파일을 호출한다.
  - 큐에 있는 작업들을 1번부터 순서대로 claude CLI로 실행한다.
  - 실행 결과를 라이브 로그 파일에 기록하고 텔레그램으로 보고한다.
  - 아직 한도면 작업을 보존하고 일정 시간 뒤 다시 예약한다.

[핵심 개념]
  - stream-json 모드: claude CLI에 '--output-format stream-json' 옵션을 줘서
    실행 중 Claude가 하는 일(도구 호출, 텍스트 출력 등)을 JSON 이벤트 스트림으로 받는다.
    이 방식이 '진실 소스(source of truth)' — 완료 여부, 에러 여부를 정확하게 알 수 있다.
  - resume: '--resume 세션ID' 옵션으로 원래 대화 맥락을 이어서 실행한다.
    resume 시에는 '--model'을 주지 않는다 (원 세션 모델과 충돌 방지).

[안정화 장치]
  - RUN_TIMEOUT 초과 시 강제 종료 → hang 방지
  - 비한도 에러는 MAX_ATTEMPTS 번까지만 재시도 후 제거 → 무한 재시도 방지
  - 한도 에러면 작업 보존 + 재예약
"""
from __future__ import annotations
import datetime
import json
import os
import re
import subprocess
import threading

from . import config, telegram, util
from .schedulers import get_scheduler
from .store import Job, JobStore


def _log(msg: str) -> None:
    """runner.log에 타임스탬프와 함께 한 줄 기록."""
    config.ensure_dir()
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with config.LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")


def _ts() -> str:
    """결과 파일 이름에 쓸 타임스탬프 문자열 (예: 20260629-153000)."""
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _fmt_prompt(prompt: str, maxlen: int = 300) -> str:
    """프롬프트를 텔레그램 표시용으로 정리.
    - 줄바꿈/다중 공백 정규화
    - maxlen 초과 시 말줄임표로 자름
    """
    p = " ".join((prompt or "").split())
    return (p[:maxlen] + "…") if len(p) > maxlen else p


def _fmt_folder(cwd: str) -> str:
    """전체 경로에서 마지막 폴더 이름만 꺼냄. 텔레그램 메시지를 짧게 유지하기 위해."""
    return os.path.basename(cwd) if cwd else "?"


def _job_header(job: Job, idx: int, total: int) -> str:
    """각 작업의 텔레그램 메시지 공통 헤더 블록을 만든다.

    표시 예:
        ━━━━━━━━━━━━━━━━━━━━
        [2/3] 두 번째 작업

        💬 요청 내용:
        "OrderController@encodedGet을 호출할 수 있는 커맨드 만들어줘..."

        📁 작업 폴더: logispot
        🆔 세션 ID: c941d019
        🔄 방식: 이전 세션 이어서 (resume)
    """
    prm = _fmt_prompt(job.prompt)
    folder = _fmt_folder(job.cwd)
    sid = (job.session_id or "")[:8] or "새 세션"
    mode = "이전 세션 이어서 (resume)" if (job.resume and job.session_id) else "새 세션으로 실행"

    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        f"[{idx}/{total}] {idx}번째 작업\n",
        f'💬 요청 내용:\n"{prm}"\n',
        f"📁 작업 폴더: {folder}",
        f"🆔 세션 ID: {sid}",
        f"🔄 실행 방식: {mode}",
    ]
    return "\n".join(lines)


def hit_limit(out: str) -> bool:
    """출력 문자열에 토큰 한도 관련 메시지가 있는지 확인한다.
    패턴은 config.LIMIT_PATTERN에 정의되어 있어 환경변수로 커스터마이즈 가능.
    """
    return re.search(config.LIMIT_PATTERN, out, re.I) is not None


def _open_monitor(logfile) -> None:
    """재실행 시작 시 Terminal.app을 자동으로 열어 라이브 로그를 tail -f로 보여준다.
    CTQ_MONITOR=0 이면 비활성화. 터미널 오픈 실패해도 실행에는 영향 없음.
    """
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
    """macOS 알림 센터에 팝업 알림을 표시한다. 실패해도 무시."""
    try:
        body = msg.replace('"', "'")
        subprocess.run(
            ["osascript", "-e", f'display notification "{body}" with title "{title}"'],
            capture_output=True,
        )
    except Exception:
        pass


def _build_cmd(job: Job, resume: bool) -> list[str]:
    """claude CLI 실행 명령어 리스트를 만든다.

    [resume=True 일 때]
        claude --resume <session_id> -p <prompt> --output-format stream-json --verbose
        ※ --model 생략: 원 세션 모델과 불일치하면 rc=1 실패 → 이중실행 낭비 발생

    [resume=False 일 때 (새 세션)]
        claude --model <model> -p <prompt> --output-format stream-json --verbose
        --dangerously-skip-permissions (CTQ_SKIP_PERMISSIONS=1 이면 추가)
    """
    cmd = [config.CLAUDE_BIN]
    if resume and job.session_id:
        cmd += ["--resume", job.session_id]
        # resume 시 --model 생략: 원 세션 모델과 불일치하면 rc=1 실패 → 이중실행 낭비
    else:
        if config.CLAUDE_MODEL:
            cmd += ["--model", config.CLAUDE_MODEL]
    cmd += ["-p", job.prompt, "--output-format", "stream-json", "--verbose"]
    if config.SKIP_PERMISSIONS:
        cmd += ["--dangerously-skip-permissions"]
    return cmd


def _humanize(line: str) -> str | None:
    """stream-json 이벤트 한 줄을 사람이 읽기 좋은 형태로 변환한다.

    [변환하는 이벤트]
      - type=assistant: Claude가 출력하는 텍스트 또는 도구 호출
      - type=result: 최종 완료/에러 결과

    [무시하는 이벤트]
      - system 메시지, tool_result 등 내부 메타데이터 (노이즈 제거)
    """
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
    """claude CLI를 stream-json 모드로 실행하고 결과를 반환한다.

    [반환값]
        (is_error, result_text, return_code)
        - is_error: True면 에러(한도 포함), False면 성공
        - result_text: stream-json의 최종 result 이벤트 내용
        - return_code: 프로세스 종료 코드

    [타임아웃]
        RUN_TIMEOUT 초(기본 1200초=20분) 초과 시 강제 종료.
        hang 방지 — 응답 없는 claude 프로세스가 영원히 살아있지 않도록.

    [라이브 로그]
        실행 중 이벤트를 _humanize()로 변환해 batchlog 파일에 실시간 기록.
        Terminal.app이 tail -f로 이 파일을 보여주므로 사용자가 실황을 볼 수 있다.
    """
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
        """타임아웃 시 호출되는 콜백. 프로세스를 강제 종료한다."""
        killed["v"] = True
        try:
            proc.kill()
        except Exception:
            pass

    # RUN_TIMEOUT 초 후에 _kill을 호출하는 타이머 설정
    timer = threading.Timer(config.RUN_TIMEOUT, _kill)
    timer.start()
    is_error = True   # result 이벤트를 못 보면 에러로 간주
    result_text = ""
    try:
        with open(batchlog, "a", encoding="utf-8") as lf:
            for line in proc.stdout:
                # 사람이 읽기 좋은 형태로 변환해 라이브 로그 파일에 기록
                human = _humanize(line.rstrip("\n"))
                if human:
                    lf.write(human + "\n")
                    lf.flush()
                # stream-json의 최종 result 이벤트에서 성공/실패 여부와 결과 텍스트 추출
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
    """작업 하나를 실행한다.

    [반환값]
        (상태, 결과텍스트)
        - 상태 ∈ {"done", "limited", "error"}

    [실행 전략]
    1. job.resume=True면 '--resume 세션ID'로 원래 대화를 이어서 시도한다.
    2. resume 실패(is_error=True, 한도 에러 아님) → 새 세션으로 재실행(폴백).
       단, 이 경우 맥락(대화 기록)은 없이 프롬프트만 전달된다.
    3. 실행 결과로 상태를 판단:
       - 한도 에러: "limited" → 작업 보존, 재예약
       - 기타 에러: "error" → 시도 횟수 기록, MAX_ATTEMPTS 초과 시 제거
       - 성공: "done" → 결과를 .md 파일로 저장
    """
    tag = f"resume {job.session_id[:8]}" if (job.resume and job.session_id) else "새 세션"
    with open(batchlog, "a", encoding="utf-8") as f:
        f.write(f"\n[{idx}/{total}] ({tag}) {job.prompt[:90]}\n  cwd={job.cwd}\n")
    _log(f"실행 [{job.cwd}] {job.prompt[:60]}")

    # 1차 시도 (resume 또는 새 세션)
    ie, res, rc = _run_streaming(_build_cmd(job, job.resume), job.cwd, batchlog)

    # resume 실패 시 새 세션으로 폴백
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
    """큐에 있는 작업들을 순서대로 모두 실행한다 (드레인 = 큐 비우기).

    [전체 흐름]
    1. 큐 락을 잡는다 (동시에 여러 드레인이 실행되는 것을 방지).
    2. 큐에서 작업 목록을 읽어온다.
    3. Terminal.app 모니터링 창을 열고 텔레그램으로 시작 알림을 보낸다.
    4. 각 작업을 순서대로 실행:
       - "limited": 아직 한도 → 이 작업과 이후 작업을 remaining에 보존, 중단
       - "error": 실패 → attempts 증가, MAX_ATTEMPTS 초과 시 큐에서 제거
       - "done": 성공 → 결과를 .md 파일로 저장
    5. remaining에 남은 작업을 다시 큐에 쓴다.
    6. 아직 한도면 RETRY_DELAY_MIN분 뒤 재예약, 완료면 예약 해제.
    7. 텔레그램으로 최종 요약 보고.
    """
    store = JobStore()
    sched = get_scheduler()
    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    # 이번 드레인의 라이브 로그 파일 (타임스탬프로 구분)
    batchlog = config.RESULTS_DIR / f"run-{_ts()}.log"
    done = errors = dropped = 0
    stopped = False  # True가 되면 이후 작업은 실행하지 않고 remaining에 보존

    try:
        with util.queue_lock(timeout=5):
            jobs = store.list()
            if not jobs:
                sched.cancel()
                return {"drained": 0, "remaining": 0, "stopped": False}

            # 드레인 시작 로그
            with open(batchlog, "w", encoding="utf-8") as f:
                f.write(
                    f"=== claude-token-queue 재실행 "
                    f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} · {len(jobs)}건 ===\n"
                )
            _open_monitor(batchlog)

            # 텔레그램: 재실행 시작 알림 + 큐 전체 목록 미리 보기
            start_lines = [f"🔔 토큰 리셋 — 큐 자동 재실행 시작\n총 {len(jobs)}건 순차 실행합니다\n"]
            for i, job in enumerate(jobs, 1):
                prm = _fmt_prompt(job.prompt, maxlen=150)
                folder = _fmt_folder(job.cwd)
                sid = (job.session_id or "")[:8] or "새 세션"
                start_lines.append(f"  {i}) 📁 {folder} · 🆔 {sid}\n     💬 \"{prm}\"")
            telegram.send("\n".join(start_lines))

            remaining: list[Job] = []
            total = len(jobs)
            for idx, job in enumerate(jobs, 1):
                if stopped:
                    # 이전 작업에서 한도 감지 → 이후 작업은 건드리지 않고 보존
                    remaining.append(job)
                    continue

                header = _job_header(job, idx, total)

                # ── 시작 알림 ──
                telegram.send(f"▶️ 재실행 시작\n{header}")

                status, res = _run_job(job, idx, total, batchlog)

                if status == "limited":
                    # 아직 한도 → 이 작업 이후 전부 보류
                    _log("아직 한도 — 작업 보존, 중단")
                    remaining.append(job)
                    stopped = True
                    telegram.send(
                        f"⏳ 아직 한도 — 보류됨 [{idx}/{total}]\n{header}\n\n"
                        f"다음 리셋 시각에 자동으로 재시도합니다"
                    )

                elif status == "error":
                    job.attempts += 1
                    errors += 1
                    if job.attempts >= config.MAX_ATTEMPTS:
                        # 너무 많이 실패 → 큐에서 제거하고 수동 처리 요청
                        dropped += 1
                        _log(f"{job.attempts}회 연속 에러 → 큐에서 제거(수동 필요)")
                        telegram.send(
                            f"⛔ {config.MAX_ATTEMPTS}회 연속 실패 → 큐에서 제거됨 [{idx}/{total}]\n"
                            f"{header}\n\n"
                            f"⚠️ 수동으로 확인·처리가 필요합니다\n"
                            f"❌ 오류 내용:\n{res[:300]}"
                        )
                    else:
                        remaining.append(job)
                        _log(f"에러({job.attempts}/{config.MAX_ATTEMPTS}) — 작업 보존")
                        telegram.send(
                            f"❌ 재실행 실패 [{idx}/{total}] "
                            f"({job.attempts}/{config.MAX_ATTEMPTS}회 시도)\n"
                            f"{header}\n\n"
                            f"⚠️ 오류 내용:\n{res[:300]}\n\n"
                            f"다음 재실행 시 다시 시도합니다"
                        )

                else:  # done
                    done += 1
                    # 결과를 .md 파일로 저장
                    out_path = config.RESULTS_DIR / f"{_ts()}-{(job.session_id or 'job')[:8]}.md"
                    out_path.write_text(
                        f"# {job.prompt}\n\n"
                        f"- cwd: {job.cwd}\n"
                        f"- session: {job.session_id}\n\n"
                        f"---\n\n{res}\n",
                        encoding="utf-8",
                    )
                    _log("완료")
                    # 결과 텍스트: 앞부분 500자 표시 (너무 길면 잘라서 보고)
                    res_preview = (res or "").strip()
                    if len(res_preview) > 500:
                        res_preview = res_preview[:500] + "\n…(이하 생략)"
                    telegram.send(
                        f"✅ 재실행 완료 [{idx}/{total}]\n{header}\n\n"
                        f"📝 Claude 응답:\n{res_preview}"
                    )

            # 남은 작업을 큐에 다시 저장
            store._write_records([j.to_record() for j in remaining])
            store._clear_legacy()

    except TimeoutError:
        _log("락 획득 실패 (다른 드레인/스캔 중) → 스킵")
        return {"skipped": True, "remaining": store.count()}

    # ── 후처리: 재예약 or 예약 해제 ──
    if stopped:
        # 아직 한도 → RETRY_DELAY_MIN분 뒤에 다시 시도
        t = datetime.datetime.now() + datetime.timedelta(minutes=config.RETRY_DELAY_MIN)
        sched.schedule(t.hour, t.minute)
        _log(f"한도 미해제 → {config.RETRY_DELAY_MIN}분 뒤({t:%H:%M}) 재예약")
    elif store.count() == 0:
        # 큐가 완전히 빔 → 예약 해제
        sched.cancel()
        _log("큐 전부 처리 → 예약 해제")

    # ── 최종 요약 ──
    summary = f"완료 {done}건 · 에러 {errors}건 · 제거 {dropped}건 · 남음 {store.count()}건"
    try:
        with open(batchlog, "a", encoding="utf-8") as f:
            f.write(f"\n=== 끝: {summary} ===\n")
    except Exception:
        pass
    _notify("claude-token-queue 재실행", summary)
    telegram.send(f"🏁 재실행 종료\n\n{summary}")
    _log(f"드레인 종료: {summary}")

    return {
        "drained": done,
        "errors": errors,
        "dropped": dropped,
        "remaining": store.count(),
        "log": str(batchlog),
    }


def run_claude(cwd: str, prompt: str) -> str:
    """MCP run_task 도구용 1회 즉시 실행.
    stream-json 대신 일반 json 모드로 실행한다 (MCP 호출은 대기 시간이 짧아야 함).
    """
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
    """ctq-runner 진입점. launchd가 예약 시각에 이 함수를 호출한다."""
    drain()


if __name__ == "__main__":
    main()
