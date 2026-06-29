"""작업 큐 저장소 (JSONL). 세션ID·리셋시각·resume 여부 등 풍부한 필드.
bash CLI의 레거시 jobs.txt(cwd|||prompt)도 함께 읽어 같이 실행한다.
확장: JobStore 인터페이스 유지하면 SQLite 등으로 교체 가능."""
from __future__ import annotations
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config, util


@dataclass
class Job:
    index: int
    cwd: str
    prompt: str
    session_id: str | None = None
    prompt_id: str | None = None
    reset: str | None = None          # "HH:MM" (로컬)
    source: str = "manual"            # watcher | run | manual | legacy
    resume: bool = False              # 원래 세션 resume 여부
    created_at: str | None = None
    attempts: int = 0                 # 연속 에러 횟수 (MAX_ATTEMPTS 초과 시 제거)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_record(self) -> dict:
        d = asdict(self)
        d.pop("index", None)
        return d


class JobStore:
    def __init__(self, path: Path | None = None, legacy: Path | None = None):
        self.path = path or config.QUEUE
        self.legacy = legacy if legacy is not None else config.JOBS

    # --- 내부: 락 없이 raw 읽기/쓰기 (락은 호출부 책임) ---
    def _read_records(self) -> list[dict]:
        recs: list[dict] = []
        if self.path.exists():
            for ln in self.path.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    recs.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
        return recs

    def _read_legacy(self) -> list[dict]:
        out: list[dict] = []
        if self.legacy and self.legacy.exists():
            for ln in self.legacy.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                cwd, _, prompt = ln.partition(config.DELIM)
                out.append({"cwd": cwd, "prompt": prompt, "source": "legacy", "resume": False})
        return out

    def _write_records(self, recs: list[dict]) -> None:
        config.ensure_dir()
        tmp = self.path.with_suffix(".jsonl.tmp")
        tmp.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def _clear_legacy(self) -> None:
        if self.legacy and self.legacy.exists():
            self.legacy.write_text("", encoding="utf-8")

    @staticmethod
    def _to_job(r: dict, index: int) -> Job:
        return Job(
            index=index,
            cwd=r.get("cwd", ""),
            prompt=r.get("prompt", ""),
            session_id=r.get("session_id"),
            prompt_id=r.get("prompt_id"),
            reset=r.get("reset"),
            source=r.get("source", "manual"),
            resume=bool(r.get("resume", False)),
            created_at=r.get("created_at"),
            attempts=int(r.get("attempts", 0)),
        )

    # --- 공개 API ---
    def list(self) -> list[Job]:
        recs = self._read_records() + self._read_legacy()
        return [self._to_job(r, i) for i, r in enumerate(recs, 1)]

    def count(self) -> int:
        return len(self._read_records()) + len(self._read_legacy())

    def _has_key(self, session_id, prompt_id) -> bool:
        # prompt_id 단독으로 dedup → 같은 프롬프트가 여러 세션/워크트리에 걸려도 1건만.
        # (prompt_id는 사용자 제출 단위 고유. 교차세션 동일 prompt_id = 같은 제출의 복제.)
        if prompt_id:
            return any(r.get("prompt_id") == prompt_id for r in self._read_records())
        return False

    def add(self, prompt: str, cwd: str, *, session_id=None, prompt_id=None,
            reset=None, source="manual", resume=False, created_at=None,
            dedup=True) -> dict | None:
        """큐에 추가. dedup=True면 같은 (session_id, prompt_id) 있으면 None 반환(중복 스킵)."""
        with util.queue_lock():
            if dedup and self._has_key(session_id, prompt_id):
                return None
            recs = self._read_records()
            rec = {
                "cwd": cwd, "prompt": prompt, "session_id": session_id,
                "prompt_id": prompt_id, "reset": reset, "source": source,
                "resume": resume, "created_at": created_at,
            }
            recs.append(rec)
            self._write_records(recs)
        return rec

    def remove(self, index: int) -> dict:
        with util.queue_lock():
            recs = self._read_records()
            leg_lines = [
                ln for ln in (self.legacy.read_text(encoding="utf-8").splitlines()
                              if (self.legacy and self.legacy.exists()) else [])
                if ln.strip()
            ]
            total = len(recs) + len(leg_lines)
            if not (1 <= index <= total):
                raise IndexError(f"잘못된 번호: {index} (1~{total})")
            if index <= len(recs):
                removed = recs.pop(index - 1)
                self._write_records(recs)
            else:
                li = index - len(recs) - 1
                line = leg_lines.pop(li)
                self.legacy.write_text("".join(l + "\n" for l in leg_lines), encoding="utf-8")
                cwd, _, prompt = line.partition(config.DELIM)
                removed = {"cwd": cwd, "prompt": prompt, "source": "legacy"}
        return removed

    def clear(self) -> int:
        with util.queue_lock():
            n = self.count()
            self._write_records([])
            self._clear_legacy()
        return n
