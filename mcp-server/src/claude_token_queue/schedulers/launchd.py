"""macOS launchd 스케줄러 백엔드.

핵심: launchd가 예약 시각에 띄우는 러너는 '새 프로세스'라 현재 환경/PATH를 못 물려받는다
(launchd 기본 PATH는 /usr/bin:/bin:... 수준). 그래서 plist에 EnvironmentVariables로
claude 절대경로·CTQ_* 설정·확장 PATH를 박아 넣어야 러너가 claude를 찾고 동일 설정으로 동작한다.
"""
from __future__ import annotations
import datetime
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
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


def _resolve_cmd(name: str, module: str) -> list[str]:
    """안정 설치된 콘솔 스크립트 우선(uv tool install → ~/.local/bin).
    없으면 현재 파이썬 -m 모듈로 폴백. launchd는 항상 떠 있어야 하므로
    uvx 임시환경(sys.executable)이 GC되면 깨질 수 있어 안정 경로를 선호한다."""
    exe = shutil.which(name)
    if exe:
        return [exe]
    cand = Path.home() / ".local" / "bin" / name
    if cand.exists():
        return [str(cand)]
    return [sys.executable, "-m", module]


def _runner_args() -> list[str]:
    return _resolve_cmd("ctq-runner", "claude_token_queue.runner")


def _claude_abs() -> str:
    return shutil.which(config.CLAUDE_BIN) or config.CLAUDE_BIN


def _runner_path() -> str:
    # claude / python 의 디렉토리 + 표준 위치를 합친 PATH (launchd 최소 PATH 보완)
    dirs = [
        os.path.dirname(_claude_abs()),
        os.path.dirname(sys.executable),
        str(Path.home() / ".local" / "bin"),
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
    # 러너/워처가 동일 설정으로 동작하도록 현재 config 값을 그대로 전파
    return {
        "PATH": _runner_path(),
        "CTQ_DIR": str(config.QDIR),
        "CTQ_LABEL": config.LABEL,
        "CTQ_PLIST": str(config.PLIST),
        "CTQ_CLAUDE_BIN": _claude_abs(),
        "CTQ_CLAUDE_MODEL": config.CLAUDE_MODEL,
        "CTQ_LIMIT_PATTERN": config.LIMIT_PATTERN,
        "CTQ_RETRY_DELAY_MIN": str(config.RETRY_DELAY_MIN),
        "CTQ_RESUME": "1" if config.RESUME else "0",
        "CTQ_MONITOR": "1" if config.MONITOR else "0",
        "CTQ_SKIP_PERMISSIONS": "1" if config.SKIP_PERMISSIONS else "0",
        "CTQ_PROJECTS_DIR": str(config.PROJECTS_DIR),
        "CTQ_WATCH_INTERVAL": str(config.WATCH_INTERVAL),
        "CTQ_RUN_TIMEOUT": str(config.RUN_TIMEOUT),
        "CTQ_MAX_ATTEMPTS": str(config.MAX_ATTEMPTS),
        "CTQ_TELEGRAM_TOKEN": config.TELEGRAM_TOKEN,
        "CTQ_TELEGRAM_CHAT": config.TELEGRAM_CHAT,
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

    def next_run(self) -> dict | None:
        """plist의 StartCalendarInterval을 읽어 다음 발생 시각 계산."""
        if not self.plist.exists():
            return None
        try:
            data = plistlib.loads(self.plist.read_bytes())
        except Exception:
            return None
        sci = data.get("StartCalendarInterval")
        if not sci:
            return None
        entries = sci if isinstance(sci, list) else [sci]
        now = datetime.datetime.now()
        candidates = []
        for e in entries:
            h = e.get("Hour")
            if h is None:
                continue
            m = e.get("Minute", 0)
            cand = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            if cand <= now:
                cand += datetime.timedelta(days=1)  # 오늘 시각 지났으면 내일
            candidates.append(cand)
        if not candidates:
            return None
        nxt = min(candidates)
        return {
            "loaded": self.status()["loaded"],
            "scheduled_time": f"{nxt.hour:02d}:{nxt.minute:02d}",
            "next_run": nxt.isoformat(timespec="minutes"),
            "in_minutes": int((nxt - now).total_seconds() // 60),
        }

    def trigger_now(self) -> bool:
        """로드돼 있으면 즉시 1회 실행 (launchctl start)."""
        if not self.status()["loaded"]:
            return False
        subprocess.run(["launchctl", "start", self.label], capture_output=True)
        return True
