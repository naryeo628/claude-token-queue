"""macOS launchd 스케줄러 백엔드.

핵심: launchd가 예약 시각에 띄우는 러너는 '새 프로세스'라 현재 환경/PATH를 못 물려받는다
(launchd 기본 PATH는 /usr/bin:/bin:... 수준). 그래서 plist에 EnvironmentVariables로
claude 절대경로·CTQ_* 설정·확장 PATH를 박아 넣어야 러너가 claude를 찾고 동일 설정으로 동작한다.
"""
from __future__ import annotations
import os
import shutil
import subprocess
import sys
from xml.sax.saxutils import escape

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
  <key>EnvironmentVariables</key>
  <dict>
{env}
  </dict>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>{hour}</integer><key>Minute</key><integer>{minute}</integer></dict>
  <key>StandardErrorPath</key><string>{err}</string>
  <key>StandardOutPath</key><string>{out}</string>
</dict></plist>
"""


def _runner_args() -> list[str]:
    # 현재 파이썬으로 러너 모듈 실행. 절대경로가 plist에 박혀 재부팅/로그아웃 후에도 동작.
    return [sys.executable, "-m", "claude_token_queue.runner"]


def _claude_abs() -> str:
    return shutil.which(config.CLAUDE_BIN) or config.CLAUDE_BIN


def _runner_path() -> str:
    # claude / python 의 디렉토리 + 표준 위치를 합친 PATH (launchd 최소 PATH 보완)
    dirs = [
        os.path.dirname(_claude_abs()),
        os.path.dirname(sys.executable),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    seen, out = set(), []
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return os.pathsep.join(out)


def _env_vars() -> dict[str, str]:
    # 러너가 동일 설정으로 동작하도록 현재 config 값을 그대로 전파
    return {
        "PATH": _runner_path(),
        "CTQ_DIR": str(config.QDIR),
        "CTQ_LABEL": config.LABEL,
        "CTQ_PLIST": str(config.PLIST),
        "CTQ_CLAUDE_BIN": _claude_abs(),
        "CTQ_LIMIT_PATTERN": config.LIMIT_PATTERN,
        "CTQ_RETRY_DELAY_MIN": str(config.RETRY_DELAY_MIN),
    }


class LaunchdScheduler(Scheduler):
    def __init__(self) -> None:
        self.label = config.LABEL
        self.plist = config.PLIST

    def schedule(self, hour: int, minute: int) -> None:
        config.ensure_dir()
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        args = "\n".join(f"    <string>{escape(a)}</string>" for a in _runner_args())
        env = "\n".join(
            f"    <key>{escape(k)}</key><string>{escape(v)}</string>"
            for k, v in _env_vars().items()
        )
        self.plist.write_text(
            _PLIST_TMPL.format(
                label=escape(self.label),
                args=args,
                env=env,
                hour=hour,
                minute=minute,
                err=escape(str(config.QDIR / "err.log")),
                out=escape(str(config.QDIR / "launchd.out.log")),
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
