# claude-token-queue MCP 서버

`claude-token-queue` 기능을 **MCP 도구**로 노출하는 파이썬 서버. Claude Code 같은 MCP 클라이언트에서 채팅으로 작업 큐잉·예약·실행 관리.

bash CLI(`ctq`)와 **같은 상태(`~/.claude-queue/`, 같은 launchd 라벨)를 공유**하므로 둘을 섞어 써도 된다.

## 설치 (Claude Code에 등록)

### A. uvx — 클론 없이 바로 (권장)
```bash
claude mcp add ctq -- uvx --from "git+https://github.com/naryeo628/claude-token-queue.git#subdirectory=mcp-server" ctq-mcp
```

### B. 로컬 설치 (개발)
```bash
cd mcp-server
uv pip install -e .        # 또는: pip install -e .
claude mcp add ctq -- ctq-mcp
```

### C. .mcp.json 수동 등록
```json
{
  "mcpServers": {
    "ctq": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/naryeo628/claude-token-queue.git#subdirectory=mcp-server", "ctq-mcp"]
    }
  }
}
```

확인: `claude mcp list` → `ctq` connected.

## 도구

| 도구 | 설명 |
|------|------|
| `run_task(prompt, cwd?, auto_schedule?)` | 지금 실행. 한도면 자동 큐 등록 + 리셋 시각 추출해 예약 |
| `enqueue_task(prompt, cwd?)` | 큐에만 등록 (실행 안 함) |
| `schedule_run(at)` | 재실행 예약. `at` = `HH:MM` / `+30m` / `+2h` |
| `run_queue_now()` | 예약 안 기다리고 지금 즉시 큐 드레인 |
| `list_tasks()` | 대기 작업 목록 |
| `remove_task(index)` | 특정 작업 제거 |
| `clear_tasks()` | 큐 비우기 |
| `cancel_schedule()` | 예약 해제 |
| `get_plan()` | **무엇을 언제 실행할지** — 작업을 실행 순서대로 + 다음 예정시각 |
| `get_status()` | 큐 + 예약 상태 + 다음 실행 예정(next_run) |
| `get_logs(lines?)` | 러너 로그 tail |

### 사용 예 (채팅)
> "이 작업 토큰 한도 걸리면 큐에 넣고 15시에 다시 돌려줘"
→ 모델이 `enqueue_task` + `schedule_run("15:00")` 호출.

## 환경변수 (전부 선택)

| 변수 | 기본값 | 용도 |
|------|--------|------|
| `CTQ_DIR` | `~/.claude-queue` | 상태 디렉토리 (CLI와 공유) |
| `CTQ_LABEL` | `com.user.claudequeue` | launchd 라벨 |
| `CTQ_PLIST` | `~/Library/LaunchAgents/<label>.plist` | plist 경로 |
| `CTQ_CLAUDE_BIN` | `claude` | claude 실행 파일 경로 |
| `CTQ_LIMIT_PATTERN` | `usage limit\|rate.?limit\|...` | 한도 감지 정규식 |
| `CTQ_RETRY_DELAY_MIN` | `10` | 정각에도 한도면 재시도 간격(분) |
| `CTQ_DEFAULT_CWD` | (서버 cwd) | cwd 미지정 시 기본 작업 디렉토리 |
| `CTQ_SCHEDULER` | (OS 자동) | 스케줄러 백엔드 강제 (`launchd`) |

## 구조 / 확장

```
src/claude_token_queue/
  config.py            설정 (env 오버라이드)
  util.py              시각 파싱 · 큐 락(mkdir, bash와 호환)
  store.py             JobStore — 파일 기반(cwd|||prompt). SQLite 등으로 교체 가능
  schedulers/
    base.py            Scheduler 추상 인터페이스
    launchd.py         macOS launchd 구현
    __init__.py        팩토리 (cron/systemd 추가 지점)
  runner.py            큐 드레인 (launchd가 호출), 한도 미해제면 재예약
  server.py            FastMCP 서버 + 도구
```

- **새 OS 지원**: `schedulers/base.Scheduler` 구현 → `get_scheduler()` 팩토리에 등록 (예 Linux `cron.py`).
- **저장소 교체**: `JobStore` 인터페이스 유지하면 SQLite/Redis 등으로 교체 가능.
- **한도 메시지 포맷 변경**: `CTQ_LIMIT_PATTERN`만 수정.

## 한계
- 현재 스케줄러는 macOS launchd만 구현 (Linux는 cron/systemd 백엔드 추가 필요).
- `claude -p`는 1회성 헤드리스 = 세션 컨텍스트 없음 → 프롬프트 독립 작성.
- 리셋 시각 자동추출은 에러 포맷 의존 → 실패 시 `schedule_run`으로 직접.
