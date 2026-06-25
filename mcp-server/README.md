# claude-token-queue MCP 서버

`claude-token-queue` 기능을 **MCP 도구**로 노출하는 파이썬 서버. Claude Code 같은 MCP 클라이언트에서 채팅으로 작업 큐잉·예약·실행 관리.

핵심: **백그라운드 감지 데몬(워처)** 이 클로드 앱 세션 기록(`~/.claude/projects/**/*.jsonl`)을 감시하다가,
어떤 세션이든 토큰 한도(429)에 걸리면 **사용자가 아무것도 안 해도** 실패한 프롬프트를 자동으로 큐에 담고
리셋 시각에 원래 세션을 resume 해서 자동 재실행한다.

## 동작 흐름 (무인 자동)

1. 클로드 앱으로 작업하다 토큰이 소진된다 (하나/여러 세션이 중단되거나 신규 요청 실패).
2. 워처가 트랜스크립트에서 한도 이벤트(`isApiErrorMessage`+`apiErrorStatus:429`)를 감지한다.
3. 실패한 사람 프롬프트 + cwd + sessionId + 리셋시각(`resets 7:40pm (Asia/Seoul)`)을 추출해 큐에 등록한다.
4. 가장 이른 리셋 시각에 launchd 트리거를 건다.
5. 리셋 시각이 되면 `claude --resume <sessionId> -p "<프롬프트>"` 로 원래 세션을 이어 자동 재실행한다.
6. 언제든 `get_plan` 으로 "무엇이 언제 실행될지" 확인할 수 있다.

> 감지는 **데몬 설치 이후** 발생한 한도만 대상으로 한다(과거 일괄 실행 방지). 중복은 `(sessionId, promptId)`로 제거.

## 설치

### 1) 안정 설치 (데몬용, 권장)
KeepAlive 데몬은 항상 떠 있어야 하므로 ephemeral `uvx`가 아니라 **고정 설치**가 필요하다.

```bash
uv tool install --from "git+https://github.com/naryeo628/claude-token-queue.git#subdirectory=mcp-server" claude-token-queue-mcp
```

→ `~/.local/bin/` 에 `ctq-mcp`, `ctq-watch`, `ctq-runner` 설치됨.

### 2) Claude Code에 MCP 등록
```bash
claude mcp add ctq -- ctq-mcp
```
또는 `.mcp.json` / `~/.claude.json`:
```json
{ "mcpServers": { "ctq": {
  "command": "ctq-mcp",
  "env": { "CTQ_CLAUDE_BIN": "/absolute/path/to/claude" }
}}}
```

### 3) 감지 데몬 켜기
Claude Code 채팅에서: **"install_watcher 실행해줘"** → 데몬 설치(launchd KeepAlive). 끝.

## 도구

| 도구 | 설명 |
|------|------|
| `install_watcher()` | **감지 데몬 설치** — 한도 자동 감지+큐+예약 시작 |
| `uninstall_watcher()` | 데몬 제거 (큐 유지) |
| `watcher_status()` | 데몬 동작 상태 |
| `scan_now()` | 지금 즉시 트랜스크립트 스캔 |
| `get_plan()` | **무엇을 언제 실행할지** — 실행 순서 + 다음 예정시각 |
| `get_status()` | 큐 + 예약 + next_run + 데몬 상태 |
| `list_tasks()` | 대기 작업 목록 |
| `get_logs(lines?, which?)` | 로그 tail (`which`=`runner`/`watcher`) |
| `run_task(prompt, cwd?, auto_schedule?)` | 지금 실행. 한도면 자동 큐+예약 |
| `enqueue_task(prompt, cwd?, session_id?)` | 큐에만 등록 (session_id 주면 resume) |
| `schedule_run(at)` | 재실행 예약. `at`=`HH:MM`/`+30m`/`+2h` |
| `run_queue_now()` | 지금 즉시 큐 드레인 |
| `remove_task(index)` / `clear_tasks()` | 작업 제거 / 큐 비우기 |
| `cancel_schedule()` | 예약 해제 |

## 환경변수 (전부 선택)

| 변수 | 기본값 | 용도 |
|------|--------|------|
| `CTQ_DIR` | `~/.claude-queue` | 상태 디렉토리 |
| `CTQ_PROJECTS_DIR` | `~/.claude/projects` | 감시할 트랜스크립트 경로 |
| `CTQ_WATCH_INTERVAL` | `30` | 워처 스캔 주기(초) |
| `CTQ_RESUME` | `1` | 재실행 시 원래 세션 resume(컨텍스트 복원). claude CLI 2.x 필요 (1.x는 replay 400 → 0으로 두고 새 세션) |
| `CTQ_CLAUDE_MODEL` | `claude-opus-4-8` | 재실행 모델 (구 CLI 기본모델 죽어 명시 필수) |
| `CTQ_MONITOR` | `1` | 재실행 시 모니터링 터미널 자동 오픈 |
| `CTQ_SKIP_PERMISSIONS` | `1` | 무인 실행이 실제 작업하도록 도구 권한 자동승인 (보안 주의) |
| `CTQ_LABEL` | `com.user.claudequeue` | 재실행 트리거 launchd 라벨 |
| `CTQ_WATCHER_LABEL` | `com.claude-token-queue.watcher` | 감지 데몬 라벨 |
| `CTQ_CLAUDE_BIN` | `claude` | claude 실행 파일 경로 |
| `CTQ_LIMIT_PATTERN` | `usage limit\|rate.?limit\|...` | 재실행 결과의 한도 감지 정규식 |
| `CTQ_RETRY_DELAY_MIN` | `10` | 정각에도 한도면 재시도 간격(분) |
| `CTQ_RUN_TIMEOUT` | `1200` | 작업당 최대 실행 시간(초). 초과 시 강제 종료(hang 방지) |
| `CTQ_MAX_ATTEMPTS` | `3` | 연속 에러 허용 횟수. 초과 시 큐에서 제거+수동필요 알림 |
| `CTQ_TELEGRAM_TOKEN` | (없음) | 텔레그램 봇 토큰. **repo에 두지 말 것** — env/plist로만 |
| `CTQ_TELEGRAM_CHAT` | (없음) | 텔레그램 chat id. 둘 다 있어야 텔레그램 보고 작동 |
| `CTQ_SCHEDULER` | (OS 자동) | 스케줄러 백엔드 강제 (`launchd`) |

## 구조 / 확장

```
src/claude_token_queue/
  config.py        설정 (env 오버라이드)
  util.py          시각·리셋메시지 파싱(타임존 변환) · 큐 락(mkdir)
  transcript.py    트랜스크립트 파싱 → 한도 이벤트(프롬프트/cwd/세션/리셋) 추출
  store.py         JobStore — JSONL(풍부한 필드) + 레거시 jobs.txt 읽기
  schedulers/
    base.py        Scheduler 추상 인터페이스
    launchd.py     macOS launchd (EnvironmentVariables 임베드, next_run)
    __init__.py    팩토리 (cron/systemd 추가 지점)
  watcher.py       감지 데몬 루프 (스캔 → 큐 → 예약)
  daemon.py        워처를 launchd KeepAlive 에이전트로 설치/제거
  runner.py        큐 드레인 — resume 우선, 한도 미해제면 재예약
  server.py        FastMCP 서버 + 도구 15종
```

- **새 OS 지원**: `schedulers/base.Scheduler` 구현 → 팩토리 등록 (예 Linux `cron.py`).
- **저장소 교체**: `JobStore` 인터페이스 유지하면 SQLite/Redis 등으로 교체 가능.
- **한도 포맷 변경**: 감지는 구조적 필드, 리셋시각은 `util.parse_reset_message` 정규식만 손보면 됨.

## 한계
- macOS launchd 전용 (Linux는 cron/systemd 백엔드 추가 필요).
- 감지 데몬은 고정 설치(`uv tool install`) 필요 — ephemeral uvx로는 KeepAlive가 깨질 수 있음.
- resume 재실행은 **claude CLI 2.x 필요**(1.x 헤드리스 resume은 히스토리 내 끊긴 tool_use로 400). 2.x면 원래 세션 컨텍스트를 복원해 이어감. 세션 못 열면 새 세션 폴백.
- CTQ_CLAUDE_BIN은 2.x claude 절대경로로 (구버전이면 죽은 기본모델 404 → CTQ_CLAUDE_MODEL 명시 필수).
- 완전 자동 실행: 리셋 시각에 큐의 명령이 무인으로 실행됨 — 끄려면 `uninstall_watcher` / `cancel_schedule`.
