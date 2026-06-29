"""감지 데몬 (watcher.py)
======================
이 파일은 '워처(watcher)' 라고 부르는 백그라운드 감시자 역할을 한다.

[역할]
  - Mac이 켜져 있는 동안 launchd(macOS 작업 스케줄러)가 이 프로세스를 항상 살려둔다.
  - 30초마다 Claude 앱이 저장하는 대화 기록 파일(트랜스크립트)을 훑어보고,
    '토큰 한도 초과(429 에러)' 흔적이 있으면 해당 요청을 큐에 자동으로 등록한다.
  - 등록 후에는 Claude가 알려준 리셋 시각에 자동 재실행을 예약한다.

[핵심 로직]
  tick()  ← launchd가 루프로 계속 호출하는 1회 스캔 단위
    └─ scan_once()  ← 트랜스크립트 파일들을 읽어 한도 이벤트 추출 + 큐 등록

[과거 에러 일괄 실행 방지]
  워처가 처음 시작될 때 start_time을 기록.
  그 시각 이전에 발생한 한도 에러는 이미 지난 것으로 간주 → 무시.

[중복 방지]
  (session_id, prompt_id) 조합을 '처리됨' 집합에 저장.
  같은 요청이 다시 스캔돼도 두 번 큐에 넣지 않는다.
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
    """워처 전용 로그 파일(watcher.log)에 타임스탬프와 함께 한 줄 기록."""
    config.ensure_dir()
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with (config.QDIR / "watcher.log").open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")


def _load_state() -> dict:
    """watch-state.json에서 이전 실행 상태를 읽어온다.
    상태에는 '이미 처리한 에러 키 목록(processed)'과 '파일별 최종 수정시각(mtimes)'이 담겨 있다.
    파일이 없거나 깨지면 빈 딕셔너리({})를 반환 → 처음 시작처럼 동작.
    """
    if config.WATCH_STATE.exists():
        try:
            return json.loads(config.WATCH_STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(st: dict) -> None:
    """현재 상태를 watch-state.json에 저장.
    '.tmp' 파일에 먼저 쓴 뒤 rename → 쓰다가 프로세스가 죽어도 기존 파일은 안전.
    """
    config.ensure_dir()
    tmp = config.WATCH_STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(config.WATCH_STATE)


def _fmt_prompt(prompt: str, maxlen: int = 300) -> str:
    """프롬프트를 텔레그램 표시용으로 정리.
    - 줄바꿈/공백 정규화
    - maxlen 초과 시 말줄임표로 자름
    """
    p = " ".join((prompt or "").split())
    return (p[:maxlen] + "…") if len(p) > maxlen else p


def _fmt_folder(cwd: str) -> str:
    """전체 경로에서 마지막 폴더 이름만 꺼냄. 텔레그램 메시지를 짧게 유지하기 위해."""
    return os.path.basename(cwd) if cwd else "?"


def scan_once(state: dict, store: JobStore) -> tuple[int, tuple[int, int] | None, list[dict]]:
    """트랜스크립트를 한 번 스캔해 새 한도 이벤트를 큐에 등록한다.

    반환값:
        (신규 등록 건수, 가장 이른 리셋 시각(h,m) or None, 등록된 작업 목록)

    [동작 흐름]
    1. 모든 Claude 세션 트랜스크립트 파일(.jsonl)을 열거한다.
    2. 파일의 수정시각(mtime)이 이전 스캔과 동일하면 변경 없음 → 스킵.
    3. 변경된 파일에서 429/rate_limit 에러 이벤트를 찾는다.
    4. 이미 처리한 이벤트(processed 집합)는 건너뛴다.
    5. 워처 시작 전에 발생한 에러는 무시(과거 에러 일괄 실행 방지).
    6. 새 이벤트면 JobStore에 추가 + processed에 키 기록.
    """
    # processed: 이미 큐에 넣은 이벤트 키("session_id:prompt_id") 집합
    processed = set(state.get("processed", []))

    # start_time: 워처가 시작된 시각 (이 시각 이전 에러는 과거 것 → 무시)
    start_iso = state.get("start_time")
    start = datetime.datetime.fromisoformat(start_iso) if start_iso else None
    if start and start.tzinfo is None:
        # 트랜스크립트의 timestamp는 UTC timezone-aware → 비교하려면 start도 aware로 변환
        start = start.astimezone()

    # mtimes: 파일경로 → 마지막 확인 시각. 변경 없는 파일은 다시 파싱하지 않음(성능 최적화).
    mtimes = state.get("mtimes", {})

    new = 0
    resets: list[tuple[int, int]] = []  # 이번 스캔에서 발견된 리셋 시각들
    queued: list[dict] = []             # 이번 스캔에서 새로 등록된 작업 정보 (텔레그램용)

    for p in iter_session_files():
        sp = str(p)
        try:
            mt = p.stat().st_mtime  # 파일 마지막 수정 시각(Unix timestamp)
        except FileNotFoundError:
            continue  # 스캔 도중 파일이 삭제됐으면 그냥 넘어감

        if mtimes.get(sp) == mt:
            continue  # 수정 시각이 같으면 내용 변경 없음 → 건너뜀

        # 이 파일에서 429 에러 이벤트들을 전부 추출
        for ev in find_limit_events(p):
            if ev.key in processed:
                continue  # 이미 처리한 이벤트

            # 워처 시작 전에 발생한 에러 → 처리됨으로만 기록하고 큐에는 넣지 않음
            if start and ev.ts and ev.ts < start:
                processed.add(ev.key)
                continue

            # 리셋 시각을 "HH:MM" 문자열로 변환
            reset_str = f"{ev.reset[0]:02d}:{ev.reset[1]:02d}" if ev.reset else None

            # JobStore에 추가 (dedup=True → 같은 prompt_id면 중복 추가 안 함)
            rec = store.add(
                ev.prompt,
                ev.cwd or os.getcwd(),
                session_id=ev.session_id,
                prompt_id=ev.prompt_id,
                reset=reset_str,
                source="watcher",
                resume=config.RESUME,     # 원래 세션 이어서 실행할지 여부
                created_at=datetime.datetime.now().isoformat(timespec="seconds"),
                dedup=True,
            )
            processed.add(ev.key)

            if rec is not None:  # None이면 중복 → 실제로 등록된 경우만
                new += 1
                _log(f"감지: session={ev.session_id} reset={reset_str} | {ev.prompt[:60]}")
                queued.append({
                    "prompt": ev.prompt,
                    "session_id": ev.session_id or "",
                    "cwd": ev.cwd or "",
                    "branch": ev.git_branch or "",   # 어떤 git 브랜치에서 작업하다 막혔는지
                    "reset": reset_str,
                })
                if ev.reset:
                    resets.append(ev.reset)

        mtimes[sp] = mt  # 이 파일의 mtime을 기록 → 다음 스캔에서 변경 감지용

    # 상태 갱신 (다음 tick()에서 _save_state로 디스크에 저장됨)
    state["processed"] = sorted(processed)
    state["mtimes"] = mtimes
    return new, (min(resets) if resets else None), queued


def tick() -> int:
    """1회 스캔 + 필요 시 예약. 신규 큐잉 건수를 반환한다.

    [이 함수가 하는 일]
    1. watch-state.json에서 이전 상태를 읽어온다.
    2. 처음 실행이면 start_time을 기록한다 (과거 에러 무시 기준선).
    3. scan_once()로 트랜스크립트를 스캔해 새 에러를 큐에 등록한다.
    4. 새로 등록된 건이 있으면 텔레그램으로 상세 알림을 보낸다.
    5. 리셋 시각이 파악됐으면 launchd에 재실행 예약을 걸어둔다.
    6. 상태를 저장한다.
    """
    state = _load_state()
    if "start_time" not in state:
        # 최초 실행 → 지금 시각을 기준선으로 기록. 이 이전 에러들은 무시.
        state["start_time"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        _log("워처 시작 — 이후 발생하는 한도만 감지")

    store = JobStore()
    new, min_reset, queued = scan_once(state, store)

    if new:
        # ── 텔레그램 알림: 어떤 요청이 큐에 들어갔는지 상세 보고 ──
        lines = [f"🚨 토큰 한도 감지 — 큐에 {new}건 자동 등록됨\n"]

        for i, q in enumerate(queued, 1):
            prm = _fmt_prompt(q["prompt"])
            folder = _fmt_folder(q["cwd"])
            sid = (q["session_id"] or "")[:8]
            branch = q.get("branch", "")

            # 각 요청 블록을 구분선으로 나눠 읽기 쉽게 표시
            block = [
                f"━━━━━━━━━━━━━━━━━━━━",
                f"📋 [{i}/{new}] 등록된 요청:\n",
                f'💬 요청 내용:\n"{prm}"\n',
                f"📁 작업 폴더: {folder}",
            ]
            if branch:
                block.append(f"🌿 브랜치: {branch}")
            block.append(f"🆔 세션 ID: {sid}")
            if q["reset"]:
                block.append(f"⏰ 리셋 예정: {q['reset']}")

            lines.append("\n".join(block))

        lines.append("━━━━━━━━━━━━━━━━━━━━")

        if min_reset:
            h, m = min_reset
            get_scheduler().schedule(h, m)   # launchd에 재실행 예약
            _log(f"{new}건 큐 등록 → 재실행 예약 {h:02d}:{m:02d}")
            lines.append(f"\n⏰ {h:02d}:{m:02d}에 자동 재실행 예정 (큐 총 {store.count()}건)")
        else:
            _log(f"{new}건 큐 등록 (리셋 시각 미파싱 → 수동 예약 필요)")
            lines.append("\n⚠️ 리셋 시각 자동 추출 실패\n'ctq at HH:MM'으로 직접 예약 필요")

        telegram.send("\n".join(lines))

    _save_state(state)
    return new


def main() -> None:
    """워처 데몬 진입점. launchd KeepAlive로 항상 살아있으며 무한루프로 tick()을 반복한다."""
    _log(f"watcher 데몬 시작 (interval={config.WATCH_INTERVAL}s)")
    while True:
        try:
            tick()
        except Exception as e:
            # 데몬은 어떤 에러가 나도 죽으면 안 됨 → 로그만 남기고 계속 실행
            _log(f"tick 오류: {e!r}")
        time.sleep(config.WATCH_INTERVAL)


if __name__ == "__main__":
    main()
