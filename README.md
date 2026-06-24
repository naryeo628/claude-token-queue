# claude-token-queue

토큰(사용량) 한도로 막힌 Claude Code 작업을 **한도 리셋 시각에 자동 재실행**하는 macOS 도구.

- **자동 감지**: `ctq run "..."` 으로 돌리면 한도 에러를 래퍼가 직접 보고 큐에 자동 등록.
- **정시 재실행**: launchd 가 네가 지정한 리셋 시각에 큐를 자동으로 비움.
- **안전망**: 정각에 아직 한도면 작업 보존 후 10분 뒤 자동 재시도.
- **1회성**: 큐 다 비우면 예약 자동 해제. 다음 리셋 땐 시각만 다시 입력.

## 설치

```bash
curl -fsSL https://raw.githubusercontent.com/naryeo628/claude-token-queue/main/install.sh | bash
```

설치 후 새 터미널을 열거나 `source ~/.zshrc`.

전제: `claude` CLI(Claude Code) 가 PATH 에 있어야 함. macOS 전용(launchd 사용).

## 사용법

```bash
ctq run "LQ-7180 쿼리 최적화 검토하고 결과 알려줘"   # 실행. 한도면 자동 큐+예약
ctq add "리팩터 X 마무리"                              # 큐에만 수동 등록
ctq at 14:30                                          # 재실행 시각 예약 (+30m, +2h 가능)
ctq status                                            # 큐/예약 확인
ctq log                                               # 실행 로그
ctq drain                                             # 지금 즉시 큐 실행
ctq cancel                                            # 예약 취소
ctq clear                                             # 큐 비우기
```

### 전형적 흐름
1. `ctq run "..."` — 한도 걸리면 자동으로 큐 등록 + (가능하면) 리셋 시각 예약.
2. 리셋 시각 자동추출 실패하면 → `ctq at 14:30` 직접 입력.
3. 리셋 시각에 launchd 가 큐를 순서대로 실행. 끝나면 예약 자동 해제.

## 동작 원리

| 구성 | 역할 |
|------|------|
| `cq.sh` (`ctq run`) | `claude -p` 실행 → 한도 에러 출력 감지 → 큐 등록 + 시각 자동 예약 |
| `add.sh` (`ctq add`) | 큐(`~/.claude-queue/jobs.txt`)에 `cwd|||프롬프트` 추가 |
| `schedule.sh` (`ctq at`) | 입력 시각으로 launchd plist 작성·로드 |
| `runner.sh` (`ctq drain`) | 예약 시각에 큐 비움. 한도 미해제면 보존+10분 뒤 재예약 |

## 한계

- **리셋 시각 자동추출은 보장 안 됨** — 에러 메시지 포맷이 버전마다 달라 실패할 수 있음. 그땐 `ctq at`로 직접 입력(어차피 시각은 보통 알고 있음).
- **대화형 TUI 자체는 자동 캡처 불가** — 한도 막힘을 잡는 훅이 없음. 자동화하려면 작업을 `ctq run` 으로 돌릴 것.
- `claude -p` 는 1회성 헤드리스 = 세션 컨텍스트 없음. 큐 프롬프트는 독립적으로 이해되게 작성(티켓번호·파일경로 포함).

## MCP 서버 (Claude Code 등에서 도구로 사용)

같은 기능을 MCP 도구로 노출하는 파이썬 서버가 [`mcp-server/`](mcp-server/)에 있다. CLI(`ctq`)와 같은 큐·예약 상태를 공유한다.

```bash
claude mcp add ctq -- uvx --from "git+https://github.com/naryeo628/claude-token-queue.git#subdirectory=mcp-server" ctq-mcp
```

채팅으로 `run_task` / `enqueue_task` / `schedule_run` / `get_status` 등 호출. 자세한 도구·설정은 [mcp-server/README.md](mcp-server/README.md).

## 제거

```bash
curl -fsSL https://raw.githubusercontent.com/naryeo628/claude-token-queue/main/uninstall.sh | bash
```

## 라이선스

MIT
