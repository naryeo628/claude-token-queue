"""스케줄러 팩토리. OS/설정에 맞는 백엔드 선택. 새 백엔드는 여기에 등록."""
from __future__ import annotations
import platform

from .. import config
from .base import Scheduler
from .launchd import LaunchdScheduler


def get_scheduler() -> Scheduler:
    backend = config.SCHEDULER_BACKEND or (
        "launchd" if platform.system() == "Darwin" else None
    )
    if backend == "launchd":
        return LaunchdScheduler()
    raise NotImplementedError(
        f"스케줄러 백엔드 미지원: {backend or platform.system()} "
        "(현재 launchd만 구현, cron/systemd는 향후 확장)"
    )


__all__ = ["Scheduler", "LaunchdScheduler", "get_scheduler"]
