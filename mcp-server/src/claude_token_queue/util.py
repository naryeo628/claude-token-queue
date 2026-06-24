"""시각 파싱 · 큐 락 유틸."""
from __future__ import annotations
import contextlib
import datetime
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


@contextlib.contextmanager
def queue_lock(timeout: float = 30.0):
    """bash 러너와 동일한 mkdir 원자 락 → CLI/MCP 동시 드레인 방지."""
    config.ensure_dir()
    waited = 0.0
    while True:
        try:
            os.mkdir(config.LOCK)
            break
        except FileExistsError:
            if waited >= timeout:
                raise TimeoutError("큐가 잠겨 있음 (드레인 진행 중일 수 있음)")
            time.sleep(0.5)
            waited += 0.5
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.rmdir(config.LOCK)
