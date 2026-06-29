"""큐 저장소 (store.py)
======================
작업 큐를 읽고 쓰는 '데이터 계층' 역할을 한다.

[저장 포맷]
  - 정식 큐: ~/.claude-queue/queue.jsonl
    각 줄이 하나의 작업 JSON. 세션ID, 리셋시각, resume 여부 등 풍부한 정보.
  - 레거시 큐: ~/.claude-queue/jobs.txt
    bash CLI(ctq run)가 쓰는 포맷. "cwd|||prompt" 한 줄씩.
    MCP 러너가 함께 읽어서 실행하므로 두 방식 모두 동작한다.

[핵심 특징]
  - 원자적 쓰기: .tmp 파일에 먼저 쓴 뒤 rename → 쓰다가 죽어도 기존 데이터 안전.
  - 중복 방지: prompt_id 단독으로 dedup → 같은 요청이 여러 세션/워크트리에서 들어와도 1건만 등록.
  - 확장성: JobStore 인터페이스를 유지하면 SQLite 등으로 교체 가능.
"""
from __future__ import annotations
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config, util


@dataclass
class Job:
    """큐에 담긴 작업 하나를 표현하는 데이터 클래스.

    [필드 설명]
      index      : 큐 내 1-기준 순서 번호 (조회·제거용)
      cwd        : 작업을 실행할 디렉토리 경로 (Claude가 이 경로에서 실행됨)
      prompt     : Claude에게 보낼 요청 문자열
      session_id : 원래 대화 세션 UUID (resume용; None이면 새 세션)
      prompt_id  : 요청 고유 ID (중복 방지용)
      reset      : 토큰 리셋 예정 시각 "HH:MM" (워처가 추출)
      source     : 어디서 등록됐는지 (watcher | run | manual | legacy)
      resume     : True면 session_id의 대화를 이어서 실행
      created_at : 등록 시각 (ISO8601 문자열)
      attempts   : 연속 에러 횟수. MAX_ATTEMPTS 초과 시 제거됨.
    """
    index: int
    cwd: str
    prompt: str
    session_id: str | None = None
    prompt_id: str | None = None
    reset: str | None = None          # "HH:MM" 형식 (로컬 시각)
    source: str = "manual"            # watcher | run | manual | legacy
    resume: bool = False              # True면 원래 세션 이어서 실행
    created_at: str | None = None
    attempts: int = 0                 # 연속 에러 횟수 (MAX_ATTEMPTS 초과 시 제거)

    def to_dict(self) -> dict:
        """MCP API 응답용 딕셔너리로 변환."""
        return asdict(self)

    def to_record(self) -> dict:
        """queue.jsonl에 저장할 딕셔너리로 변환. index는 파일에 저장 안 함(순서로 결정)."""
        d = asdict(self)
        d.pop("index", None)
        return d


class JobStore:
    """큐 파일 읽기/쓰기/추가/제거를 담당하는 저장소 클래스.

    [파일 구조]
      path   : queue.jsonl (정식 큐, JSONL 형식)
      legacy : jobs.txt   (레거시 큐, "cwd|||prompt" 형식)

    두 파일을 합쳐서 순서대로 읽는다.
    """

    def __init__(self, path: Path | None = None, legacy: Path | None = None):
        self.path = path or config.QUEUE       # 정식 큐 경로
        self.legacy = legacy if legacy is not None else config.JOBS  # 레거시 큐 경로

    # ── 내부 메서드: 락 없이 raw 읽기/쓰기 ──
    # 호출하는 쪽(add, remove, clear)에서 util.queue_lock()으로 락을 잡아야 한다.

    def _read_records(self) -> list[dict]:
        """queue.jsonl에서 레코드 목록을 읽어온다.
        빈 줄이나 깨진 JSON 줄은 조용히 무시한다.
        """
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
        """jobs.txt에서 레거시 레코드를 읽어온다.
        포맷: "cwd|||prompt" 한 줄씩.
        """
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
        """레코드 목록을 queue.jsonl에 원자적으로 쓴다.
        .tmp 파일에 먼저 쓴 뒤 rename → 쓰다가 죽어도 기존 파일이 깨지지 않음.
        """
        config.ensure_dir()
        tmp = self.path.with_suffix(".jsonl.tmp")
        tmp.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs),
            encoding="utf-8",
        )
        tmp.replace(self.path)  # 원자적 rename

    def _clear_legacy(self) -> None:
        """jobs.txt를 비운다. 정식 큐로 이전된 레거시 작업을 정리할 때 사용."""
        if self.legacy and self.legacy.exists():
            self.legacy.write_text("", encoding="utf-8")

    @staticmethod
    def _to_job(r: dict, index: int) -> Job:
        """딕셔너리를 Job 객체로 변환한다. 없는 필드는 기본값으로 채운다."""
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

    # ── 공개 API ──

    def list(self) -> list[Job]:
        """큐의 모든 작업을 순서대로 반환한다. (정식 큐 + 레거시 큐 합산)"""
        recs = self._read_records() + self._read_legacy()
        return [self._to_job(r, i) for i, r in enumerate(recs, 1)]

    def count(self) -> int:
        """현재 큐에 있는 작업 수를 반환한다."""
        return len(self._read_records()) + len(self._read_legacy())

    def _has_key(self, session_id, prompt_id) -> bool:
        """같은 prompt_id의 작업이 이미 큐에 있는지 확인한다.

        [중복 방지 전략]
        prompt_id만으로 dedup하는 이유:
          같은 프롬프트가 여러 세션이나 워크트리에서 동시에 429를 받더라도
          큐에는 1건만 들어가야 한다. session_id까지 같이 보면 교차세션 중복을 막을 수 없다.
        """
        if prompt_id:
            return any(r.get("prompt_id") == prompt_id for r in self._read_records())
        return False

    def add(
        self,
        prompt: str,
        cwd: str,
        *,
        session_id=None,
        prompt_id=None,
        reset=None,
        source="manual",
        resume=False,
        created_at=None,
        dedup=True,
    ) -> dict | None:
        """작업을 큐에 추가한다.

        [반환값]
          - 추가 성공: 저장된 레코드 딕셔너리
          - 중복(dedup=True이고 같은 prompt_id 존재): None

        [동시성 안전성]
        util.queue_lock()으로 락을 잡아 동시에 여러 프로세스가 쓰지 못하도록 한다.
        """
        with util.queue_lock():
            if dedup and self._has_key(session_id, prompt_id):
                return None  # 중복 → 추가하지 않음
            recs = self._read_records()
            rec = {
                "cwd": cwd,
                "prompt": prompt,
                "session_id": session_id,
                "prompt_id": prompt_id,
                "reset": reset,
                "source": source,
                "resume": resume,
                "created_at": created_at,
            }
            recs.append(rec)
            self._write_records(recs)
        return rec

    def remove(self, index: int) -> dict:
        """index 번호의 작업을 큐에서 제거하고 제거된 레코드를 반환한다.
        index는 list()의 순서 번호(1-기준).
        정식 큐와 레거시 큐를 합산한 순서로 계산한다.
        """
        with util.queue_lock():
            recs = self._read_records()
            leg_lines = [
                ln for ln in (
                    self.legacy.read_text(encoding="utf-8").splitlines()
                    if (self.legacy and self.legacy.exists()) else []
                )
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
        """큐 전체를 비우고 비운 작업 수를 반환한다."""
        with util.queue_lock():
            n = self.count()
            self._write_records([])
            self._clear_legacy()
        return n
