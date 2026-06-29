"""시각 파싱 · 큐 락 유틸."""
from __future__ import annotations
import contextlib
import datetime
import errno
import os
import re
import time

from . import config


def parse_when(arg: str) -> tuple[int, int]:
    """'HH:MM' 절대시각 또는 '+30m'/'+2h' 상대시각 → (hour, minute)."""
    arg = arg.strip()
    if arg.startswith("+"):
        unit = arg[-1].lower()
        num = int(arg[1:-1])
        now = datetime.datetime.now()
        if unit == "m":
            t = now + datetime.timedelta(minutes=num)
        elif unit == "h":
            t = now + datetime.timedelta(hours=num)
        else:
            raise ValueError("상대 단위는 m 또는 h (예: +30m, +2h)")
        return t.hour, t.minute
    hh, _, mm = arg.partition(":")
    h, m = int(hh), int(mm or 0)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"시각 범위 오류: {arg}")
    return h, m


def parse_reset(text: str) -> tuple[int, int] | None:
    """한도 에러 메시지에서 리셋 시각 추출 시도. 실패하면 None (포맷 보장 안 됨)."""
    m = re.search(r"(\d{1,2}):(\d{2})\s*([apAP])?m?", text)
    if m:
        h, mm, ap = int(m.group(1)), int(m.group(2)), m.group(3)
        if ap and ap.lower() == "p" and h < 12:
            h += 12
        if 0 <= h <= 23 and 0 <= mm <= 59:
            return h, mm
    m = re.search(r"\b(\d{1,2})\s*([apAP])m\b", text)
    if m:
        h = int(m.group(1))
        if m.group(2).lower() == "p" and h < 12:
            h += 12
        if 0 <= h <= 23:
            return h, 0
    return None


def extract_text(content) -> str:
    """클로드 메시지 content(str 또는 [{type,text}, ...])에서 텍스트만 뽑아 합침."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for it in content:
            if isinstance(it, dict):
                parts.append(it.get("text") or it.get("content") or "")
            else:
                parts.append(str(it))
        return " ".join(p for p in parts if p)
    return str(content or "")


def parse_reset_message(text) -> tuple[int, int] | None:
    """한도 에러 메시지에서 리셋 시각 추출 → 로컬 (hour, minute).
    예: "You've hit your session limit · resets 7:40pm (Asia/Seoul)".
    타임존이 있으면 로컬 시각으로 변환. 실패하면 None."""
    text = extract_text(text)
    m = re.search(r"reset[s]?\s+(\d{1,2})(?::(\d{2}))?\s*([apAP])m", text)
    if not m:
        return None
    h, mm, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
    if ap == "p" and h < 12:
        h += 12
    if ap == "a" and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= mm <= 59):
        return None
    tzm = re.search(r"\(([^)]+)\)", text)
    if tzm:
        try:
            from zoneinfo import ZoneInfo

            src = ZoneInfo(tzm.group(1).strip())
            now_src = datetime.datetime.now(src)
            target = now_src.replace(hour=h, minute=mm, second=0, microsecond=0)
            if target <= now_src:
                target += datetime.timedelta(days=1)
            local = target.astimezone()  # 로컬 타임존으로 변환
            return local.hour, local.minute
        except Exception:
            pass
    return h, mm


def _pid_alive(pid: int) -> bool:
    """pid 프로세스가 살아있는지. (kill 0 = 시그널 안 보내고 존재 확인만)"""
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM  # 존재하지만 권한 없음 = 살아있음
    return True


@contextlib.contextmanager
def queue_lock(timeout: float = 30.0):
    """mkdir 원자 락 → CLI/MCP/러너 동시 접근 방지.
    락을 잡은 프로세스 pid를 락 안에 기록. 다른 쪽이 락을 만나면 그 pid가 살아있는지 보고,
    죽었으면(러너가 비정상 종료해 락이 안 풀린 stale 상태) 락을 회수한다 → 영구 데드락 방지."""
    config.ensure_dir()
    pidfile = config.LOCK / "pid"
    waited = 0.0
    while True:
        try:
            os.mkdir(config.LOCK)
            with contextlib.suppress(Exception):
                pidfile.write_text(str(os.getpid()))
            break
        except FileExistsError:
            # 락 주인이 죽었는지 확인 → 죽었으면 stale 락 회수 후 재시도
            stale = False
            try:
                holder = int(pidfile.read_text().strip())
                stale = not _pid_alive(holder)
            except Exception:
                stale = False  # pid 아직 안 쓰인 생성 직후 race → 그냥 대기
            if stale:
                with contextlib.suppress(Exception):
                    pidfile.unlink()
                with contextlib.suppress(Exception):
                    os.rmdir(config.LOCK)
                continue
            if waited >= timeout:
                raise TimeoutError("큐가 잠겨 있음 (드레인 진행 중일 수 있음)")
            time.sleep(0.5)
            waited += 0.5
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            pidfile.unlink()
        with contextlib.suppress(FileNotFoundError):
            os.rmdir(config.LOCK)
