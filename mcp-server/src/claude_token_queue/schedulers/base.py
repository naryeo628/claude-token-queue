"""스케줄러 추상 인터페이스. 새 백엔드(cron/systemd/at)는 이걸 구현하면 됨."""
from __future__ import annotations
from abc import ABC, abstractmethod


class Scheduler(ABC):
    @abstractmethod
    def schedule(self, hour: int, minute: int) -> None:
        """매일 hour:minute 에 러너를 실행하도록 1회성 트리거 등록."""

    @abstractmethod
    def cancel(self) -> None:
        """예약 해제."""

    @abstractmethod
    def status(self) -> dict:
        """현재 예약 상태 반환."""
