"""감지 데몬(watcher)을 launchd KeepAlive 에이전트로 설치/제거/조회."""
from __future__ import annotations
import subprocess
from xml.sax.saxutils import escape

from . import config
from .schedulers.launchd import _env_vars, _resolve_cmd  # claude 절대경로·PATH·CTQ_* 전파

_TMPL = """<?xml version="1.0" encoding="UTF-8"?>
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
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardErrorPath</key><string>{err}</string>
  <key>StandardOutPath</key><string>{out}</string>
</dict></plist>
"""


def _args() -> list[str]:
    return _resolve_cmd("ctq-watch", "claude_token_queue.watcher")


class WatcherAgent:
    def __init__(self) -> None:
        self.label = config.WATCHER_LABEL
        self.plist = config.WATCHER_PLIST

    def install(self) -> dict:
        config.ensure_dir()
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        args = "\n".join(f"    <string>{escape(a)}</string>" for a in _args())
        env = "\n".join(
            f"    <key>{escape(k)}</key><string>{escape(v)}</string>"
            for k, v in _env_vars().items()
        )
        self.plist.write_text(
            _TMPL.format(
                label=escape(self.label),
                args=args,
                env=env,
                err=escape(str(config.QDIR / "watcher.err.log")),
                out=escape(str(config.QDIR / "watcher.out.log")),
            ),
            encoding="utf-8",
        )
        subprocess.run(["launchctl", "unload", str(self.plist)], capture_output=True)
        subprocess.run(["launchctl", "load", str(self.plist)], capture_output=True)
        return self.status()

    def uninstall(self) -> dict:
        subprocess.run(["launchctl", "unload", str(self.plist)], capture_output=True)
        if self.plist.exists():
            self.plist.unlink()
        return {"ok": True, "running": False}

    def status(self) -> dict:
        r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
        return {
            "label": self.label,
            "running": self.label in (r.stdout or ""),
            "plist": str(self.plist),
            "plist_exists": self.plist.exists(),
            "interval_sec": config.WATCH_INTERVAL,
        }
