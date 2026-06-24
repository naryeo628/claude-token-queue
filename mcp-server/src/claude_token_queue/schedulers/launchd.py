"""macOS launchd 스케줄러 백엔드."""
from __future__ import annotations
import subprocess
import sys

from .. import config
from .base import Scheduler

_PLIST_TMPL = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
{args}
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>{hour}</integer><key>Minute</key><integer>{minute}</integer></dict>
  <key>StandardErrorPath</key><string>{err}</string>
  <key>StandardOutPath</key><string>{out}</string>
</dict></plist>
"""


def _runner_args() -> list[str]:
    # 현재 파이썬으로 러너 모듈 실행. 절대경로가 plist에 박혀서 재부팅/로그아웃 후에도 동작.
    return [sys.executable, "-m", "claude_token_queue.runner"]


class LaunchdScheduler(Scheduler):
    def __init__(self) -> None:
        self.label = config.LABEL
        self.plist = config.PLIST

    def schedule(self, hour: int, minute: int) -> None:
        config.ensure_dir()
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        args = "\n".join(f"    <string>{a}</string>" for a in _runner_args())
        self.plist.write_text(
            _PLIST_TMPL.format(
                label=self.label,
                args=args,
                hour=hour,
                minute=minute,
                err=str(config.QDIR / "err.log"),
                out=str(config.QDIR / "launchd.out.log"),
            ),
            encoding="utf-8",
        )
        subprocess.run(["launchctl", "unload", str(self.plist)], capture_output=True)
        subprocess.run(["launchctl", "load", str(self.plist)], capture_output=True)

    def cancel(self) -> None:
        subprocess.run(["launchctl", "unload", str(self.plist)], capture_output=True)

    def status(self) -> dict:
        r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
        return {
            "backend": "launchd",
            "label": self.label,
            "loaded": self.label in (r.stdout or ""),
            "plist": str(self.plist),
            "plist_exists": self.plist.exists(),
        }

    def trigger_now(self) -> bool:
        """로드돼 있으면 즉시 1회 실행 (launchctl start)."""
        if not self.status()["loaded"]:
            return False
        subprocess.run(["launchctl", "start", self.label], capture_output=True)
        return True
