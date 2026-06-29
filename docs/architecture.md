# ctq (claude-token-queue) 완전 구조 가이드

> MCP 완전 초보, Python 몰라도 OK. A부터 Z까지 설명.

---

## 1. ctq가 뭐야? (한 줄 요약)

> **Claude가 토큰 한도에 막혔을 때, 해당 요청을 자동으로 저장해두고 한도가 풀리는 시각에 자동으로 다시 실행해주는 시스템.**

---

## 2. 왜 필요해?

Claude에는 사용량 한도(토큰 한도)가 있다. 작업하다가 한도에 막히면:

- ❌ **기존 방식**: 나중에 직접 기억해서 다시 요청해야 함. 잊어버리거나 타이밍을 놓치기 쉬움.
- ✅ **ctq 사용 시**: 막힌 순간 자동으로 저장 → 한도 풀리는 시각에 자동 재실행 → 텔레그램으로 결과 보고.

---

## 3. 전체 동작 흐름

```
[Claude 앱에서 작업 중]
         │
         ▼
[토큰 한도 초과 (429 에러 발생)]
         │
         ▼
[워처(watcher)가 30초마다 스캔 중 감지]
   ↳ ~/.claude/projects/ 아래의 대화 기록 파일(.jsonl)에서 429 흔적 발견
         │
         ▼
[요청을 큐(queue.jsonl)에 자동 등록]
   ↳ 텔레그램 알림: "🚨 토큰 한도 감지 — 큐에 자동 등록됨"
         │
         ▼
[리셋 시각에 launchd가 ctq-runner 실행]
   ↳ 예: Claude가 "20:10에 리셋됩니다"라고 했으면 → 20:10에 자동 실행
         │
         ▼
[claude CLI로 저장된 요청 재실행]
   ↳ 가능하면 원래 세션 이어서(resume), 실패하면 새 세션으로
         │
         ▼
[완료/실패 텔레그램 보고]
   ↳ "✅ 재실행 완료 [1/1]" + Claude 응답 내용 미리보기
```

---

## 4. 파일별 역할

```
mcp-server/src/claude_token_queue/
├── server.py       ← MCP 서버. Claude가 ctq를 도구로 쓸 수 있게 해줌.
├── config.py       ← 모든 설정값 (환경변수로 오버라이드 가능)
├── watcher.py      ← 감지 데몬. 30초마다 트랜스크립트 스캔.
├── runner.py       ← 실행기. 큐의 작업을 claude CLI로 순서대로 실행.
├── store.py        ← 큐 저장소. queue.jsonl 읽기/쓰기.
├── transcript.py   ← 트랜스크립트 파서. .jsonl 파일에서 429 이벤트 추출.
├── daemon.py       ← 워처 데몬 설치/제거 (launchd plist 관리).
├── telegram.py     ← 텔레그램 알림 발송.
└── util.py         ← 공통 유틸. 시각 파싱, 큐 락 등.
```

### server.py — MCP 서버

MCP(Model Context Protocol)는 Claude 같은 AI가 외부 프로그램을 '도구'로 쓸 수 있게 하는 표준 규격이다.

이 파일이 하는 일:
- `@mcp.tool()` 데코레이터가 붙은 함수들을 Claude에게 도구로 노출한다.
- Claude Code가 ctq-mcp 프로세스에 연결하면 이 도구들을 사용할 수 있게 된다.
- 실제 로직은 없고, 각 함수가 다른 모듈(watcher, runner, store 등)을 호출한다.

**노출하는 도구 목록:**

| 도구 | 설명 |
|------|------|
| `install_watcher` | 백그라운드 감지 데몬 설치 |
| `uninstall_watcher` | 감지 데몬 제거 |
| `watcher_status` | 감지 데몬 상태 확인 |
| `scan_now` | 지금 즉시 스캔 |
| `get_status` | 큐 전체 상태 조회 |
| `get_plan` | 실행 계획 조회 |
| `list_tasks` | 작업 목록 조회 |
| `get_logs` | 로그 조회 |
| `run_task` | 즉시 실행 (한도 시 자동 큐 등록) |
| `enqueue_task` | 큐에만 등록 |
| `schedule_run` | 재실행 시각 예약 |
| `run_queue_now` | 즉시 큐 실행 |
| `remove_task` | 특정 작업 제거 |
| `clear_tasks` | 큐 전체 비우기 |
| `cancel_schedule` | 예약 해제 |
| `send_telegram` | 텔레그램 메시지 발송 |
| `telegram_status` | 텔레그램 설정 상태 |

---

### config.py — 설정

모든 설정값이 여기 있다. 전부 환경변수로 바꿀 수 있다.

**중요 설정:**

| 설정 | 환경변수 | 기본값 | 설명 |
|------|----------|--------|------|
| 큐 디렉토리 | `CTQ_DIR` | `~/.claude-queue` | 큐 파일들이 저장되는 폴더 |
| Claude 실행 파일 | `CTQ_CLAUDE_BIN` | `claude` | claude CLI 경로 |
| 기본 모델 | `CTQ_CLAUDE_MODEL` | `claude-sonnet-4-6` | 새 세션 실행 시 사용할 모델 |
| resume 여부 | `CTQ_RESUME` | `1` (켜짐) | 원래 세션 이어서 실행할지 |
| 스캔 주기 | `CTQ_WATCH_INTERVAL` | `30` (초) | 워처 스캔 간격 |
| 최대 실행 시간 | `CTQ_RUN_TIMEOUT` | `1200` (초) | 작업당 타임아웃 (20분) |
| 최대 재시도 | `CTQ_MAX_ATTEMPTS` | `3` | 연속 실패 허용 횟수 |
| 텔레그램 토큰 | `CTQ_TELEGRAM_TOKEN` | (없음) | 봇 토큰 |
| 텔레그램 채팅 | `CTQ_TELEGRAM_CHAT` | (없음) | 채팅 ID |

---

### watcher.py — 감지 데몬

항상 백그라운드에서 돌면서 Claude 대화 기록을 훑는다.

**주요 함수:**

```
tick()
  └─ _load_state()      : 이전 상태(처리한 에러 목록 등) 읽기
  └─ scan_once()        : 트랜스크립트 파일 스캔
       └─ iter_session_files() : 대화 기록 파일 목록
       └─ find_limit_events()  : 각 파일에서 429 이벤트 추출
       └─ store.add()          : 큐에 등록
  └─ telegram.send()    : 텔레그램 알림
  └─ get_scheduler().schedule() : launchd 예약
  └─ _save_state()      : 현재 상태 저장
```

**파일 변경 감지 최적화:**
- 각 파일의 수정 시각(mtime)을 기록해둔다.
- 이전과 mtime이 같으면 → 내용 변경 없음 → 파싱 생략 (CPU 절약).

**과거 에러 방지:**
- 워처 시작 시각(start_time)을 기록한다.
- 그 이전에 발생한 에러는 '이미 처리됨'으로 기록하고 큐에는 넣지 않는다.
- 이유: 오래된 에러가 갑자기 실행되면 엉뚱한 결과가 나올 수 있음.

---

### runner.py — 실행기

큐에 쌓인 작업들을 실제로 claude CLI로 실행한다.

**주요 함수:**

```
drain()                      : 큐 전체 드레인 (순서대로 실행)
  └─ _run_job()              : 작업 하나 실행
       └─ _build_cmd()       : claude CLI 명령어 조립
       └─ _run_streaming()   : 실행 + 라이브 로그 + 타임아웃
```

**resume 전략:**
```
job.resume=True이면:
  claude --resume <session_id> -p <prompt> --output-format stream-json --verbose
  ※ --model 생략 (원 세션 모델과 충돌 방지)
  
  실패(is_error=True, 한도 아님)이면 → 새 세션으로 폴백:
  claude --model claude-sonnet-4-6 -p <prompt> --output-format stream-json --verbose
```

**작업 결과 분류:**
- `"done"`: 성공 → 결과를 `~/.claude-queue/results/*.md`에 저장
- `"limited"`: 아직 한도 → 작업 보존 + 재예약
- `"error"`: 실패 → attempts 증가, MAX_ATTEMPTS 초과 시 큐에서 제거

---

### store.py — 큐 저장소

**큐 파일 구조:**

```
~/.claude-queue/
├── queue.jsonl     ← 정식 큐 (JSONL, 한 줄 = 작업 하나)
├── jobs.txt        ← 레거시 큐 (bash CLI용, "cwd|||prompt" 형식)
└── lock.d/         ← 락 디렉토리 (동시 접근 방지용)
    └── pid         ← 락을 잡은 프로세스 ID
```

**queue.jsonl 예시:**
```json
{"cwd":"/Users/mac/workspace/project/logispot","prompt":"LQ-7158 작업 계속해줘","session_id":"c941d019-...","prompt_id":"4eda1ba3-...","reset":"20:10","source":"watcher","resume":true,"created_at":"2026-06-29T15:41:49"}
```

**원자적 쓰기:**
1. `.jsonl.tmp` 파일에 새 내용 쓰기
2. `rename(.jsonl.tmp → queue.jsonl)` 

→ 쓰는 도중 프로세스가 죽어도 기존 큐 파일이 깨지지 않음.

**중복 방지:**
- `prompt_id`가 같으면 큐에 중복 추가하지 않는다.
- 이유: 같은 요청이 여러 세션에서 동시에 429를 받으면 한 번만 실행해야 한다.

---

### transcript.py — 트랜스크립트 파서

Claude 대화 기록 파일을 읽어 429 에러를 찾는다.

**대화 기록 파일 위치:**
```
~/.claude/projects/<project-hash>/<session-id>.jsonl
```

**한 줄(이벤트)의 JSON 구조 예시:**
```json
// 사람이 입력한 프롬프트
{"type":"user","message":{"content":"LQ-7158 작업 계속해줘"},"promptId":"4eda1ba3-...","sessionId":"c941d019-..."}

// 429 에러 이벤트
{"type":"error","isApiErrorMessage":true,"apiErrorStatus":429,"sessionId":"c941d019-...","cwd":"/Users/mac/...","gitBranch":"develop-ljw","timestamp":"2026-06-29T06:41:49.000Z"}
```

**감지 로직:**
```
모든 이벤트를 순서대로 읽으면서:

이벤트 i가 isApiErrorMessage=true AND apiErrorStatus=429이면:
  ← 역방향으로 탐색해서 직전 사람 프롬프트 찾기 (j = i-1, i-2, ...)
  
  에러 이후(i+1 이상)에 또 다른 사람 프롬프트가 있으면:
    → 한도가 풀린 뒤 대화를 계속한 것 → 무시
  없으면:
    → 이 에러로 세션이 멈춘 것 → LimitEvent 생성 → 큐에 등록
```

---

### daemon.py — launchd 관리

macOS의 launchd 시스템 데몬 등록/해제를 처리한다.

**launchd란?**
macOS의 서비스 관리 시스템. Windows의 서비스(Service), Linux의 systemd와 비슷한 개념.
`KeepAlive=true`를 설정하면 프로세스가 죽어도 자동으로 다시 살린다.

**관련 파일:**
```
~/Library/LaunchAgents/
├── com.claude-token-queue.watcher.plist  ← 워처 데몬 (항상 살아있음)
└── com.user.claudequeue.plist            ← 재실행 예약 (예약 시각에 1회 실행)
```

**워처 plist 핵심 설정:**
```xml
<key>KeepAlive</key><true/>      ← 죽으면 자동 재시작
<key>RunAtLoad</key><true/>      ← 로드하자마자 즉시 실행
<key>ThrottleInterval</key><integer>10</integer>  ← 재시작 최소 간격 10초
```

---

### telegram.py — 텔레그램 알림

텔레그램 Bot API를 직접 HTTP로 호출한다 (외부 라이브러리 없음).

**발송 실패 시:**
에러가 나도 조용히 `False`를 반환. ctq 동작에 영향 없음.

**텔레그램 메시지 종류:**

| 상황 | 이모지 | 메시지 |
|------|--------|--------|
| 한도 감지 → 큐 등록 | 🚨 | 등록된 요청 상세 + 리셋 예정 시각 |
| 재실행 시작 | 🔔 | 총 N건 실행 예정 + 전체 목록 미리보기 |
| 각 작업 시작 | ▶️ | 요청 내용 + 폴더 + 세션 ID + 실행 방식 |
| 작업 완료 | ✅ | 요청 내용 + Claude 응답 500자 미리보기 |
| 아직 한도 | ⏳ | 보류된 요청 + 다음 재시도 안내 |
| 실패(재시도 예정) | ❌ | 시도 횟수 + 오류 내용 |
| 실패(최대 초과, 제거) | ⛔ | 수동 처리 필요 안내 |
| 전체 완료 | 🏁 | 완료/에러/제거/남음 건수 요약 |

---

### util.py — 유틸

여러 파일에서 공통으로 쓰는 기능들.

**queue_lock() — 큐 락:**
- 동시에 여러 프로세스(MCP 서버, ctq-runner, scan 등)가 큐 파일을 수정하면 데이터가 깨진다.
- `mkdir`은 macOS에서 원자적(atomic) 연산 → 락 구현에 사용.
- PID 파일로 락 주인을 기록 → 주인 프로세스가 죽었으면(stale) 락을 회수해 데드락 방지.

**parse_reset_message() — 리셋 시각 파싱:**
- Claude가 "resets 7:40pm (Asia/Seoul)"처럼 알려주는 리셋 시각을 추출한다.
- 타임존이 있으면 로컬 시각으로 변환한다.

---

## 5. 데이터 흐름 (코드 레벨)

```
[429 에러 발생]
       │
       ▼ 30초 내
watcher.tick()
  └─ transcript.find_limit_events(file)
       → LimitEvent(session_id, cwd, branch, prompt, prompt_id, reset, ...)
  └─ store.add(prompt, cwd, session_id=..., resume=True, ...)
       → queue.jsonl에 한 줄 추가
  └─ telegram.send("🚨 한도 감지...")
  └─ get_scheduler().schedule(h, m)
       → launchd plist 생성 → 예약 시각에 ctq-runner 실행

[리셋 시각 도달]
       │
       ▼
launchd → ctq-runner → runner.drain()
  └─ store.list() : queue.jsonl에서 Job 목록 읽기
  └─ telegram.send("🔔 재실행 시작...")
  └─ for job in jobs:
       runner._run_job(job, ...)
         └─ _build_cmd(job, resume=True)
              → ["claude", "--resume", session_id, "-p", prompt, "--output-format", "stream-json", "--verbose"]
         └─ _run_streaming(cmd, cwd, batchlog)
              → subprocess.Popen(cmd) → 라인별 stream-json 이벤트 처리
              → 최종 result 이벤트에서 is_error + result_text 추출
         └─ 결과 분류: done / limited / error
       telegram.send("✅ 완료..." or "⏳ 한도..." or "❌ 실패...")
  └─ store._write_records(remaining)  : 남은 작업 다시 저장
  └─ telegram.send("🏁 종료 요약")
```

---

## 6. 주요 파일 경로

```
~/.claude-queue/
├── queue.jsonl        ← 정식 큐 (현재 대기 중인 작업들)
├── jobs.txt           ← 레거시 큐 (bash ctq run이 쓰는 포맷)
├── runner.log         ← 재실행 로그 (ctq log로 확인)
├── watcher.log        ← 감지 데몬 로그
├── watch-state.json   ← 워처 상태 (처리한 에러 목록, 파일 mtime 등)
├── lock.d/            ← 큐 락 디렉토리
└── results/           ← 재실행 결과 저장 (.md, .log 파일)
    ├── 20260629-201015-c941d019.md   ← 완료된 작업 결과
    └── run-20260629-201015.log       ← 드레인 실행 라이브 로그

~/Library/LaunchAgents/
├── com.claude-token-queue.watcher.plist  ← 워처 데몬
└── com.user.claudequeue.plist            ← 재실행 예약

~/.claude/projects/
└── <project-hash>/
    └── <session-id>.jsonl   ← Claude 대화 기록 (워처가 스캔)
```

---

## 7. 텔레그램 메시지 해석 가이드

### 큐 등록 알림 (워처가 보냄)
```
🚨 토큰 한도 감지 — 큐에 2건 자동 등록됨

━━━━━━━━━━━━━━━━━━━━
📋 [1/2] 등록된 요청:

💬 요청 내용:
"LQ-7158 작업 계속해줘"

📁 작업 폴더: logispot         ← 어떤 프로젝트
🌿 브랜치: develop-ljw         ← 어떤 브랜치에서 작업 중이었는지
🆔 세션 ID: c941d019           ← Claude 세션 (앞 8자리)
⏰ 리셋 예정: 20:10            ← 이 시각에 자동 재실행됨
━━━━━━━━━━━━━━━━━━━━

⏰ 20:10에 자동 재실행 예정 (큐 총 2건)
```

### 재실행 시작 알림 (러너가 보냄)
```
🔔 토큰 리셋 — 큐 자동 재실행 시작
총 2건 순차 실행합니다

  1) 📁 logispot · 🆔 c941d019
     💬 "LQ-7158 작업 계속해줘"
  2) 📁 logispot · 🆔 8bdc667e
     💬 "OrderController@encodedGet 커맨드 만들어줘"
```

### 각 작업 진행 알림
```
▶️ 재실행 시작
━━━━━━━━━━━━━━━━━━━━
[1/2] 1번째 작업

💬 요청 내용:
"LQ-7158 작업 계속해줘"

📁 작업 폴더: logispot
🆔 세션 ID: c941d019
🔄 실행 방식: 이전 세션 이어서 (resume)  ← resume인지 새 세션인지
```

### 완료 알림
```
✅ 재실행 완료 [1/2]
━━━━━━━━━━━━━━━━━━━━
[1/2] 1번째 작업

💬 요청 내용:
"LQ-7158 작업 계속해줘"

📁 작업 폴더: logispot
🆔 세션 ID: c941d019
🔄 실행 방식: 이전 세션 이어서 (resume)

📝 Claude 응답:
작업을 완료했습니다. `app/Services/...` 파일을 수정하고...
(500자 미리보기, 이후 생략)
```

### 실패 알림
```
❌ 재실행 실패 [2/2] (1/3회 시도)
...
⚠️ 오류 내용:
API Error: Connection closed mid-response...

다음 재실행 시 다시 시도합니다
```

### 최종 요약
```
🏁 재실행 종료

완료 2건 · 에러 0건 · 제거 0건 · 남음 0건
```

---

## 8. 자주 묻는 것

**Q: resume이 항상 실패하는 것 같아요**
A: `--resume session_id`와 `--model`을 같이 쓰면 원 세션 모델과 불일치해 rc=1 실패.
   현재 코드는 resume 시 `--model`을 생략하도록 수정되어 있음.

**Q: 같은 요청이 큐에 두 번 들어갔어요**
A: 워처가 여러 세션 파일에서 같은 prompt_id를 발견하면 dedup으로 한 번만 등록.
   prompt_id가 다르면 별도 요청으로 인식됨.

**Q: 텔레그램 알림이 안 와요**
A: `CTQ_TELEGRAM_TOKEN`과 `CTQ_TELEGRAM_CHAT` 환경변수 확인.
   ctq MCP의 `telegram_status` 도구로 설정 상태 확인 가능.

**Q: 작업이 큐에 있는데 실행이 안 돼요**
A: `ctq status`로 예약 상태 확인. 예약이 없으면 `ctq at HH:MM`으로 수동 예약.

**Q: 토큰을 너무 많이 써요**
A: 기본 모델이 `claude-sonnet-4-6`로 설정되어 있음.
   더 저렴한 모델 원하면 `CTQ_CLAUDE_MODEL=claude-haiku-4-5-20251001` 설정.
