"""작업 큐 저장소. 기본은 파일 기반(bash CLI와 동일한 'cwd|||prompt' 포맷)으로 상호운용.
확장: JobStore 인터페이스를 따르는 SQLite 등 다른 백엔드로 교체 가능."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from pathlib import Path

from . import config, util


@dataclass
class Job:
    index: int
    cwd: str
    prompt: str

    def to_dict(self) -> dict:
        return asdict(self)


class JobStore:
    def __init__(self, path: Path | None = None):
        self.path = path or config.JOBS

    # --- 내부: 락 없이 raw 읽기/쓰기 (락은 호출부 책임) ---
    def _read_lines(self) -> list[str]:
        if not self.path.exists():
            return []
        return [ln for ln in self.path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def _write(self, lines: list[str]) -> None:
        config.ensure_dir()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("".join(ln + "\n" for ln in lines), encoding="utf-8")
        tmp.replace(self.path)  # 원자적 교체

    @staticmethod
    def _parse(line: str, index: int) -> Job:
        cwd, _, prompt = line.partition(config.DELIM)
        return Job(index, cwd, prompt)

    # --- 공개 API ---
    def list(self) -> list[Job]:
        return [self._parse(ln, i) for i, ln in enumerate(self._read_lines(), 1)]

    def count(self) -> int:
        return len(self._read_lines())

    def add(self, prompt: str, cwd: str) -> Job:
        with util.queue_lock():
            lines = self._read_lines()
            lines.append(f"{cwd}{config.DELIM}{prompt}")
            self._write(lines)
        return Job(len(lines), cwd, prompt)

    def remove(self, index: int) -> Job:
        with util.queue_lock():
            lines = self._read_lines()
            if not (1 <= index <= len(lines)):
                raise IndexError(f"잘못된 번호: {index} (1~{len(lines)})")
            removed = lines.pop(index - 1)
            self._write(lines)
        return self._parse(removed, index)

    def clear(self) -> int:
        with util.queue_lock():
            n = len(self._read_lines())
            self._write([])
        return n
