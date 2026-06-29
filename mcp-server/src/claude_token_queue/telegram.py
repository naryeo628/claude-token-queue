"""텔레그램 보고 (telegram.py)
==============================
ctq가 큐 등록·재실행 결과를 텔레그램으로 알려주는 알림 모듈.

[설정 방법]
  환경변수 두 가지만 설정하면 된다:
    CTQ_TELEGRAM_TOKEN : BotFather에서 발급받은 봇 토큰
    CTQ_TELEGRAM_CHAT  : 메시지를 받을 채팅 ID (개인 또는 그룹)

  설정 방법 1 — ~/.zshrc에 추가:
    export CTQ_TELEGRAM_TOKEN="1234567890:AAAA..."
    export CTQ_TELEGRAM_CHAT="987654321"

  설정 방법 2 — launchd plist EnvironmentVariables에 추가:
    ctq install_watcher를 통해 plist가 생성된 뒤 직접 편집

[안전성]
  - 이 파일에 토큰을 하드코딩하지 말 것. 환경변수로만 주입.
  - 발송 실패해도 ctq 실행에는 전혀 영향 없음 (조용히 False 반환).
  - 네트워크 없을 때도 10초 타임아웃으로 대기 후 넘어감.
"""
from __future__ import annotations
import urllib.parse
import urllib.request

from . import config


def enabled() -> bool:
    """텔레그램 발송 가능한 상태인지 확인한다. 토큰과 채팅 ID가 모두 설정되어 있어야 True."""
    return bool(config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT)


def send(text: str) -> bool:
    """텔레그램으로 메시지를 발송한다.

    [동작]
    - enabled()=False이면 조용히 False 반환 (에러 없음)
    - Telegram Bot API의 sendMessage 엔드포인트를 POST로 호출
    - 10초 타임아웃: 네트워크 문제로 hang하지 않도록

    [반환값]
      True: 발송 성공 (HTTP 200)
      False: 미설정이거나 발송 실패 (ctq 실행에는 영향 없음)
    """
    if not enabled():
        return False
    try:
        data = urllib.parse.urlencode({
            "chat_id": config.TELEGRAM_CHAT,
            "text": text,
            "disable_web_page_preview": "true",  # URL이 있어도 미리보기 안 뜨게
        }).encode()
        url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10) as r:
            return getattr(r, "status", 200) == 200
    except Exception:
        return False  # 네트워크 오류, 토큰 만료 등 → 조용히 실패
