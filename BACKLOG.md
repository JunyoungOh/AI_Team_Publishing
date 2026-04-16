# Enterprise Agent System — Backlog

> 2026-04-07 기준. 새 세션에서 이 파일을 읽고 미구현 사항을 파악하세요.

## 완료된 주요 변경

### 2026-04-16 세션
- **개발의뢰 rate limit 자동 재개**: 최초개발 세션별 `state.json`(work_dir 옆)로 진행 상태·rate limit 이력·window_start 영속화. 소진 시 wall-clock 기반 추정 + 30분 캡 + 지수 backoff (15/30/60/120/120분). 6h 내 5회 실패 시 자동 중지(소프트 가드).
- **수동 "지금 시도" 버튼**: 대기 패널에서 asyncio.Event로 즉시 재시도. 30초 debounce + 세션 락으로 동시성 안전.
- **브라우저 재접속**: `/api/dev-sessions/active` REST + WS `observe_dev` 메시지로 실행 중 세션에 라이브 재연결. 최초개발 태스크는 WebSocket 끊겨도 서버측 asyncio.Task 유지.
- **부팅 시 orphan 정리**: 이전 서버 프로세스가 남긴 running/waiting state.json을 error로 마크 (서버 재시작 = 태스크 유실이므로 상태 일관성 유지).
- 테스트: dev_state 28개 단위 + dev_runner_resume 5개 monkey-patch 통합. `/tmp/claude-usage.json` 의존 제거 (imaginer와 usage API는 정보 표시용으로만 잔존).
- 상세: `src/upgrade/dev_state.py`, `src/upgrade/dev_runner.py`, `src/ui/server.py`, `src/ui/static/js/mode-upgrade.js`.

### 이전 세션
- **싱글 세션 아키텍처**: 멀티 CLI subprocess → 단일 CLI 세션 (8.5분→2.6분)
- **실시간 스트리밍**: stream-json 파싱 → WebSocket → 활동 대시보드 (도구별 카드+타이머)
- **나만의 방식 재설계**: 에이전트 구조 → 분석 전략 프리셋 (관점 카드 UI)
- **스케줄팀**: 정기 자동 실행 + "지금 실행" + 프로그레스 오버레이
- **출력 형식**: HTML/PDF/Markdown/CSV/JSON 5종 지원
- **폴더명**: session_id → task 제목 기반
- **불필요 MCP 제거**: 6개→3개 (fetch, brave, serper 제거)
- **Findings 중복 제거**: dedup.py (difflib 기반)
- **rate limit 대응**: 에러 파싱 → 자동 대기 → 재시도

### 2026-04-07 세션
- **Delta 비교**: 스케줄 실행 시 이전 MD 파일을 Read로 읽어 "변동 사항" 섹션 자동 생성
- **Append 모드**: `output_mode: append` 시 기존 파일에 데이터 누적
- **HTML + MD 이중 생성**: 스케줄 실행 시 `results_YYYY-MM-DD.html` + `.md` 동시 생성 (MD는 다음 실행에서 CLI가 읽을 요약본)
- **시/분/요일 직관 UI**: 크론식 입력 → 시간 셀렉트 + 요일 토글 버튼으로 전환
- **output_mode 셀렉트**: "매번 새로(비교)" / "누적 추가" 선택 UI
- **보고서 보기 + 폴더 열기**: 스케줄팀에 보고서 링크 + Finder 폴더 열기 버튼 추가 (삭제 시 알림)
- **명확화 → 상세 설명**: 고정 질문 3개 → 자유 입력 textarea (스케줄)
- **풀와이드 채팅 UI**: 인스턴트 + 나만의 방식 모드에서 캔버스를 제거하고 채팅 단일 뷰로 통합
- **모드별 채팅 히스토리 분리**: `ChatPanel.switchMode()`로 탭별 독립 메시지 컨테이너, 탭 전환 시 히스토리 유지
- **시스템 메시지 스타일**: 말풍선 제거, 플레인 텍스트 왼쪽 정렬 (GPT/Gemini 스타일)
- **중지 버튼 헤더 이동**: 하단 중앙 → 상단 바 우측 끝
- **intake 에코 제거**: EventBridge에서 intake 노드 메시지 필터링
- **내부 시스템 메시지 필터링**: [CEO], [Analyst], [Blackboard] 등 내부 로그 사용자에게 미노출
- **네이밍 변경**: 나만의 팀 → 나만의 방식, 새 팀 만들기 → 일하는 방식 만들기 등
- **버그 수정**: `add_run_record` dict 인자 수정, 활동 대시보드 탭 간 겹침 수정, StaticFiles 보고서 GET 충돌 수정, 날짜 파일명 보고서 서빙 추가

---

## 스킬 탭 — Plan 2: 카드 실행 ✅ 완료 (2026-04-09)

**참고**: docs/superpowers/plans/2026-04-09-skill-tab-plan2.md

**완료 범위**:
- `src/skill_builder/skill_loader.py` — registry → SKILL.md 본문 + skill_metadata.json 로드, IsolationMode 결정
- `src/skill_builder/run_history.py` — `data/skills/runs/<slug>/<run_id>.json` atomic CRUD
- `src/skill_builder/execution_streamer.py` — `single_session._stream_session` 패턴 복제, 콜백 기반 + DI 가능한 proc factory
- `src/skill_builder/execution_runner.py` — 오케스트레이터, ISOLATED vs WITH_MCPS 분기
- `/ws/skill-execute` WebSocket + `/api/skill-builder/runs/{slug}` REST GET
- 카드 인라인 펼침 UI: 큰 입력 textarea(220px) + 실시간 활동 로그 + 마크다운 결과 + 실행 횟수 카운트
- 격리 정책: `required_mcps == []` → cwd=/tmp + 빌트인 도구만 / `required_mcps != []` → 프로젝트 루트 + 명시된 mcp__ 도구만
- 설치 위치: `data/skills/installed/skill-tab-{slug}/` (Plan 1의 `~/.claude/skills/`는 권한 거부 때문에 마이그레이션됨)
- handoff prompt rule 7: "초안 → 사용자 승인 → 저장" 3단계 흐름
- handoff prompt rule 3: skill-creator의 한국어 인터뷰를 단일 세션 내에서 강제 수행
- WebSocketDisconnect 시 runner task 자동 cancel로 서브프로세스 정리
- 32개 단위 테스트 (Tasks 1-5 모두 TDD)
- E2E 검증 완료 (Playwright): 인사 → 인터뷰 → 답변 → 초안 → 승인 → 저장 → 카드 등장 → 클릭 → 실행 → 결과 → 카운트 갱신

**자동 트리거 차단의 다층 방어**:
- 방어 1 (생성 시 description): handoff 프롬프트 rule 8이 description에서 트리거 어구 금지 (검증됨 — 생성된 description "입력받은 텍스트의 전체 글자수를 세어 ... 형식으로 반환합니다"는 중립)
- 방어 2 (저장 위치): `data/skills/installed/`는 Claude Code의 자동 발견 영역 밖이라 description matching 자체가 불가능 (방어 2가 가장 강력한 층)
- 방어 3 (실행 시): `--add-dir ~/.claude/skills/`를 절대 사용하지 않음
- 방어 4 (격리 모드): cwd=/tmp + 빌트인 도구만 → CLAUDE.md / 다른 스킬 / 프로젝트 파일 모두 차단
- 방어 5 (system prompt 직접 주입): 카드 클릭 시 SKILL.md 본문을 메모리로 읽어 system prompt에 주입. CLI는 file system에서 SKILL.md를 발견하지 않음

## 스킬 탭 — Plan 3: 카드 실행 스케줄링 (미착수)

**전제**: Plan 2 완료
**범위**:
- 기존 스케줄팀(`card-mode-schedule`) 인프라에 "스킬 실행" 작업 타입 추가
- 스케줄 등록 UI에서 스킬 + 입력값 사전 지정
- 자동 실행 시 `execution_runner.run_skill()` 호출 + 결과를 run_history에 저장
- 스킬 실행 결과를 스케줄 보고서와 동일 구조로 HTML/MD 이중 생성
- run history 자동 회전 (예: 카드당 최근 50건 유지)
- WebSocket 재연결 지원 (페이지 새로고침 시 진행 중 실행 복구)
- 임시 작업 디렉터리 + 최소 .mcp.json 기반 강화 격리 (WITH_MCPS 모드)
- 카드 UI에서 timeout 사용자 조정

## 스킬 탭 — Plan 4 후보: 파일 입력 + 카드 메타 표시 (미착수, 우선순위 높음)

E2E 테스트(2026-04-09)에서 발견된 사용자 피드백 + xlsx 변환 스킬 검증 결과:

### 1. 파일 입력 지원 — 디자인 결정 필요

xlsx 변환 스킬 검증 결과, **격리 모드에서도 파일이 cwd(/tmp) 안에 있으면 접근 가능** 함이 입증됨. 두 옵션 비교:

**옵션 A: 매번 절대 경로 입력 (현재 동작)**
- 사용자가 textarea에 파일 절대 경로를 적음
- 단점 1: cwd=/tmp 격리 모드는 /tmp 외부 파일을 못 읽음 → 사용자는 파일을 /tmp에 미리 복사해야 함
- 단점 2: "/Users/.../report.xlsx" 같은 긴 경로 입력 부담
- 장점: 백엔드 변경 0

**옵션 B: 앱 안에 conventional 폴더 (`data/skills/file_for_skill/`)**
- 사용자가 파일을 `data/skills/file_for_skill/` 폴더에 미리 복사
- textarea에는 파일명만 적음 (예: "report.xlsx")
- 백엔드: cwd를 `data/skills/file_for_skill/`로 변경 OR `--add-dir` 으로 노출
- 장점 1: 사용자가 짧은 파일명만 입력
- 장점 2: 폴더 한 곳에 파일을 모아 관리 (Finder/Explorer로 열어 폴더 정리 가능)
- 장점 3: 격리 강도 유지 — `data/skills/file_for_skill/`만 노출, 다른 디스크 영역은 차단
- 단점: 사용자가 파일을 미리 복사하는 단계 필요

**옵션 C: 브라우저 업로드 (드래그앤드롭)**
- 카드 폼에 `<input type="file">` 또는 drop zone
- 백엔드가 `data/skills/uploads/<run_id>/`에 임시 저장
- 장점: UX가 가장 친숙
- 단점: 큰 파일 처리, temp cleanup, multipart POST 등 인프라 부담

**권장 시작점**: **옵션 B + 옵션 C 결합**. 옵션 B는 단순해서 빠르게 출시 가능하고, 옵션 C는 후속으로 추가. 옵션 A(절대 경로)는 cwd 변경 없이 지원해도 부수효과 — 사용자가 파일을 conventional 폴더에 두든 /tmp에 두든 둘 다 작동.

### 2. 사용 안내(usage_notes)의 영구 표시 — 카드 메타 확장

**문제**: skill-creator가 저장 직후 마지막 채팅 메시지에 "(사전에 pip install openpyxl이 설치되어 있어야 합니다.)" 같은 중요 안내를 함. 하지만 카드를 다시 열면 이 정보가 휘발되어 사용자가 잊어버림.

**해결**:
- `skill_metadata.json`에 `usage_notes: ["사전에 pip install openpyxl이 필요합니다", "..."]` 배열 필드 추가
- handoff prompt rule 7 Stage 3에 "필요한 사용 안내·전제 조건은 반드시 `skill_metadata.json`의 `usage_notes` 배열에 기록"라고 명시
- `SkillRecord`에 `usage_notes: list[str]` 필드 추가, registry.json에 함께 저장
- mode-skill.js의 카드 렌더에 `usage_notes` 영역 추가 — 카드 안에 노란색 박스로 "📌 사용 안내" 섹션
- 카드 인라인 펼침 시에도 보임

### 3. 카드 description 정제 (현재 = 사용자의 첫 description, 권장 = SKILL.md frontmatter description)

현재 카드 제목은 사용자가 첫 입력한 긴 한국어 문장 ("받은 텍스트의 글자수를 세어서..."). skill-creator가 정제한 frontmatter description ("입력받은 텍스트의 전체 글자수를 세어 ...")이 더 정확하고 간결함. 카드 제목을 frontmatter description으로 교체하거나, 둘 다 보여주는 옵션 (제목 = 정제, 부제목 = 사용자 원문).

### 4. UI 진행 표시 개선

스킬 만들기 단계에서 "skill-creator 시작 중... (약 20초)" 외에 진행 상황 인디케이터 부재. 응답 대기 중 사용자가 "멈춘 건가?" 의문을 가질 수 있음.
- 진행 단계 표시기 (인사 → 인터뷰 → 답변 → 초안 → 저장)
- 또는 spinner + "응답 대기 중 (Ns 경과)" 카운터

### 5. 이력 상세 조회 페이지 (낮은 우선순위)

현재 카드는 카운트만 표시. 백엔드에는 이미 `data/skills/runs/<slug>/<run_id>.json`으로 이력이 다 저장되고 있음. 향후 필요해지면 별도 페이지/모달로 과거 실행을 조회 가능.

### 6. 스킬 수정/삭제 기능

현재 카드 수정/삭제 UI 없음. 파일 직접 수정 + registry 수정 필요.

### 7. 응답 출력 정제 — 한국어 강제 + 사고 과정 노출 금지

**문제**: xlsx 변환 스킬 실행 시 결과 첫 줄이 모델의 메타 reasoning으로 시작:
> "The skill was already loaded in the system prompt. Let me follow its instructions and run the conversion script. 변환 결과입니다: ..."

두 가지 누수가 한꺼번에:
- (a) 영어 reasoning 노출 (handoff prompt는 만들기 단계만 한국어 강제, SKILL.md 본문에는 지시 없음)
- (b) 메타 사고("The skill was already loaded...")가 사용자가 원하는 결과(마크다운 표) 앞에 섞임

**해결**: handoff prompt rule 7 Stage 3에 지시 추가 — SKILL.md 본문 마지막에 항상 두 섹션을 자동 포함:
- `## 응답 언어`: 모든 응답·사고는 한국어로 작성
- `## 응답 형식`: 사용자가 요청한 출력만 반환. "이 스킬을 따르겠습니다", "skill is loaded", "Let me..." 같은 메타 메시지/내부 reasoning 일체 금지. 결과만.

### 8. Skill 도구의 의도치 않은 노출 (낮은 우선순위, 정보용)

xlsx 검증 시 Claude Code CLI가 `Skill` 도구를 자동 노출함이 발견됨 (allowed_tools=`["Read","Write","Edit","Bash","Glob","Grep"]`인데도). 다행히 cwd=/tmp + `--add-dir` 미사용 덕분에 다른 스킬 발견은 못 함 → 격리는 유지됨. 이건 Claude Code CLI 동작이라 우리가 비활성화할 수 없을 가능성이 높지만, **모니터링 필요**.

## 학습된 사항 (다음 plan에 적용할 것)

1. **`--add-dir`와 권한 시스템의 차이**: `--add-dir`는 디렉터리 접근/탐색은 허용하지만, `~/.claude/` 같은 시스템 보호 영역에 대한 *쓰기*는 별도 prompt로 차단됨. headless 모드(`-p`)는 prompt에 답할 TTY가 없어 거부됨. 시스템 영역에 쓰려는 헤드리스 흐름은 금물.
2. **untracked working tree의 위험**: Plan 1 전체가 git에 한 번도 커밋되지 않은 채 working tree에서만 살아있어, 다른 세션의 작업 중에 일부가 손실될 수 있음. 새 모듈은 작성 즉시 커밋하는 습관 필요.
3. **closure 캡처 버그 패턴**: `historyToggle.onclick = function(){ renderHistory(historyEl, runs) }`처럼 closure로 fetch 결과를 캡처하면, 이후 갱신이 반영 안 됨. 매번 fresh fetch 함수를 호출하는 게 더 견고. 또는 기능 자체를 단순화(이 경우엔 토글 제거)하면 closure 문제도 자동 해결.
4. **plan-document-reviewer가 잡은 patch 경로 버그**: 함수 내부에서 import한 함수를 patch할 때는 *source 모듈* 경로를 patch해야 한다. `src.ui.server.run_skill` 패치는 작동 안 함 → `src.skill_builder.execution_runner.run_skill`을 패치해야 함. 사전 리뷰의 가치 입증.

---

## 미구현 사항

### 1. 아키텍처

#### 1-1. 기획-실행-검증 프롬프트 강화
- **현재**: 싱글 세션 프롬프트에 "분석→수집→검증→보고서" 지시가 있지만, 모델의 자율 판단에 맡겨져 있음
- **목표**: depth=deep일 때 명시적 검증 단계 강제. 수집 후 자가 검증 → 부족하면 추가 수집
- **파일**: `src/prompts/single_session_prompts.py`

#### 1-2. 레거시 코드 정리
- **현재**: `use_single_session=True`일 때 사용되지 않는 모듈이 다수 존재
- **제거 후보**: 
  - `src/engine/review_loop.py` — PESR 루프 (싱글 세션이 대체)
  - `src/utils/blackboard.py` — 파이프라인 블랙보드 (세션 컨텍스트가 대체)
  - `src/utils/collection_blackboard.py` — findings 축적 (세션 내 자연어로 대체)
  - `src/utils/dependency_graph.py` — Kahn's algorithm (Agent 서브에이전트가 대체)
  - `src/utils/progress.py` — WorkerProgressTracker 대부분 (활동 대시보드가 대체)
- **주의**: `use_single_session=False` 레거시 모드와 공존해야 하므로 삭제가 아닌 분리 필요

#### 1-3. CLI 터미널 뷰 (실행 내용 투명화)
- **현재**: 싱글 세션 내부에서 CLI가 자율 실행. 도구 사용 카드만 표시되고 사고 과정은 보이지 않음
- **목표**: CLI의 stream-json text 블록을 채팅 타임라인에 인라인으로 표시하여 "CLI가 뭘 하고 있는지" 투명하게 보임
- **구현 방향**: `card-event-handler.js`의 activity 이벤트에서 text 블록도 채팅에 전달
- **파일**: `src/graphs/nodes/single_session.py` (`_stream_session`), `card-event-handler.js`

---

### 2. 데이터/출력

#### ~~2-1. Delta 비교~~ ✅ 완료
#### ~~2-2. 누적 데이터 (Append 모드)~~ ✅ 완료

#### 2-3. Jinja2 템플릿 분리 (Phase 2)
- **상태**: 싱글 세션이 충분히 좋은 HTML을 생성하고 있어 우선순위 낮아짐

---

### 3. UI/UX

#### 3-1. 브라우저 새로고침 시 실행 분리
- **현상**: 새로고침 시 실행 중이던 나만의 방식 작업이 UI에서 분리됨 (백엔드는 계속 실행, 결과는 저장되지만 UI에 표시 불가)
- **조사 필요**: 재접속 시 진행 중인 세션을 복구하는 메커니즘

#### ~~3-2. 탭 전환 시 컨텍스트 유지~~ ✅ 완료 (모드별 채팅 히스토리 분리)

#### 3-3. 스케줄 완료 알림
- **현재**: 스케줄 자동 실행 완료 시 알림 없음 (보고서만 저장)
- **목표**: 브라우저 Notification API 또는 소리로 알림

#### ~~3-5. 활동 대시보드 타이머~~ ✅ 완료

---

### 4. 스케줄팀

#### 4-1. SessionStart hook 에러
- **현상**: 전략 설계(나만의 방식) 등 headless CLI 호출 시 SessionStart hook이 exit 1 반환 → 실패
- **근본 원인**: headless 모드에서 사용자의 글로벌 hook 설정이 실행됨
- **해결 방향**: CLI에 hook 비활성 옵션 사용 또는 에러 무시 처리
- **영향 범위**: 나만의 방식 전략 생성, 스케줄팀 AI 질문 생성 (현재 고정 질문/자유 입력으로 우회)

#### 4-2. 스케줄 status "running" 잔류
- **보정**: 보고서 파일이 존재하면 status를 completed로 강제 변경
- **파일**: `src/ui/server.py`, `src/scheduler/runner.py`

#### ~~4-3. CLI 5시간 사용량 리셋 정밀 감지~~ ✅ 완료 (2026-04-16, 최초개발 한정)
- 자체 wall-clock 추적 + 30분 캡 + 지수 backoff으로 해결. 상세는 2026-04-16 세션 항목 참조.
- 강화소(`src/upgrade/runner.py`)는 아직 레거시 `_get_rate_limit_wait` 유지 — 후속 작업에서 마이그레이션 고려.

#### 4-5. 외부 서비스 전송 (Slack/Email)

---

### 5. 나만의 방식

#### 5-1. 전략 사이드바 목록 렌더링
- **현재**: `_renderSidebarStrategyList()` 함수가 빈 상태 (스텁)
- **목표**: 저장된 전략 목록을 사이드바에 표시, 클릭하면 로드
- **파일**: `src/ui/static/js/card-builder.js`

#### 5-2. 전략 수정 요청 흐름
- **현재**: "✏️ 전략 수정 요청" 버튼 → 입력 안내만 표시
- **목표**: 수정 요청 입력 → StrategyBuilderSession에 전달 → 전략 업데이트 → 카드 갱신
- **파일**: `src/ui/static/js/card-builder.js`, `src/company_builder/builder_agent.py`

---

## 파일 참조

| 주요 파일 | 역할 |
|-----------|------|
| `src/graphs/nodes/single_session.py` | 싱글 세션 실행 노드 (스트리밍) |
| `src/prompts/single_session_prompts.py` | 실행/출력 프롬프트 (delta/append/날짜 파일) |
| `src/company_builder/builder_agent.py` | 전략 설계 에이전트 |
| `src/company_builder/storage.py` | strategy/company CRUD |
| `src/company_builder/scheduler.py` | 스케줄→ScheduledJob 변환 (delta/append 포함) |
| `src/company_builder/schedule_storage.py` | 스케줄 CRUD |
| `src/scheduler/models.py` | PreContext (previous_report_path, output_mode) |
| `src/ui/server.py` | WebSocket 엔드포인트, 보고서 서빙 |
| `src/ui/sim_runner.py` | 그래프 실행 + WS 브릿지 |
| `src/ui/event_bridge.py` | 그래프 이벤트 → UI 이벤트 변환 (intake 에코 필터) |
| `src/ui/static/js/mode-company-card.js` | 나만의 방식 모드 (풀와이드 채팅, 모드별 히스토리) |
| `src/ui/static/js/card-chat-panel.js` | ChatPanel (switchMode, 모드별 컨테이너) |
| `src/ui/static/js/card-event-handler.js` | WS 이벤트 → UI 매핑 (인라인 대시보드) |
| `src/ui/static/js/card-builder.js` | 전략 설계 + 저장 |
| `src/ui/static/js/mode-schedule.js` | 스케줄팀 UI (시/분/요일, output_mode, 상세설명) |
| `src/ui/static/css/card-view.css` | 풀와이드 레이아웃, 인라인 대시보드, 시스템 메시지 스타일 |
| `src/utils/pdf_converter.py` | HTML→PDF 변환 (Playwright) |
| `src/utils/dedup.py` | findings 중복 제거 |
| `src/config/settings.py` | `use_single_session` 토글 |
| `.mcp.json` | MCP 서버 설정 (firecrawl, github, mem0) |
