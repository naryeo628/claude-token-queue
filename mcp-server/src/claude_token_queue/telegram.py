"""텔레그램 보고. 토큰/chat은 config(=env)에서만 — repo에 토큰 하드코딩 금지."""
from __future__ import annotations
import urllib.parse
import urllib.request

from . import config


def enabled() -> bool:
    return bool(config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT)


def send(text: str) -> bool:
    """텔레그램 메시지 발송. 설정 없거나 실패하면 조용히 False (실행엔 영향 없음)."""
    if not enabled():
        return False
    try:
        data = urllib.parse.urlencode(
            {"chat_id": config.TELEGRAM_CHAT, "text": text, "disable_web_page_preview": "true"}
        ).encode()
        url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10) as r:
            return getattr(r, "status", 200) == 200
    except Exception:
        return False
