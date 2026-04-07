# Enterprise Agent System — Backlog

> 2026-04-07 기준. 새 세션에서 이 파일을 읽고 미구현 사항을 파악하세요.

## 완료된 주요 변경

### 이전 세션
- **싱글 세션 아키텍처**: 멀티 CLI subprocess → 단일 CLI 세션 (8.5분→2.6분)
- **실시간 스트리밍**: stream-json 파싱 → WebSocket → 활동 대시보드 (도구별 카드+타이머)
- **나만의 방식 재설계**: 에이전트 구조 → 분석 전략 프리셋 (관점 카드 UI)
- **야근팀**: 목표 달성까지 반복 iteration (raw md → 평가 → 보고서)
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
- **보고서 보기 + 폴더 열기**: 스케줄팀/야근팀에 보고서 링크 + Finder 폴더 열기 버튼 추가 (삭제 시 알림)
- **명확화 → 상세 설명**: 고정 질문 3개 → 자유 입력 textarea (스케줄/야근 공통)
- **풀와이드 채팅 UI**: 인스턴트 + 나만의 방식 모드에서 캔버스를 제거하고 채팅 단일 뷰로 통합
- **모드별 채팅 히스토리 분리**: `ChatPanel.switchMode()`로 탭별 독립 메시지 컨테이너, 탭 전환 시 히스토리 유지
- **시스템 메시지 스타일**: 말풍선 제거, 플레인 텍스트 왼쪽 정렬 (GPT/Gemini 스타일)
- **중지 버튼 헤더 이동**: 하단 중앙 → 상단 바 우측 끝
- **intake 에코 제거**: EventBridge에서 intake 노드 메시지 필터링
- **내부 시스템 메시지 필터링**: [CEO], [Analyst], [Blackboard] 등 내부 로그 사용자에게 미노출
- **네이밍 변경**: 나만의 팀 → 나만의 방식, 새 팀 만들기 → 일하는 방식 만들기 등
- **버그 수정**: `add_run_record` dict 인자 수정, 활동 대시보드 탭 간 겹침 수정, StaticFiles 보고서 GET 충돌 수정, 날짜 파일명 보고서 서빙 추가

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
- **현상**: 새로고침 시 실행 중이던 인스턴트 작업이 UI에서 분리됨 (백엔드는 계속 실행, 결과는 저장되지만 UI에 표시 불가)
- **조사 필요**: 재접속 시 진행 중인 세션을 복구하는 메커니즘

#### ~~3-2. 탭 전환 시 컨텍스트 유지~~ ✅ 완료 (모드별 채팅 히스토리 분리)

#### 3-3. 스케줄 완료 알림
- **현재**: 스케줄 자동 실행 완료 시 알림 없음 (보고서만 저장)
- **목표**: 브라우저 Notification API 또는 소리로 알림

#### ~~3-4. 야근팀 완료 이벤트 누락~~ — 재검증 필요

#### ~~3-5. 활동 대시보드 타이머~~ ✅ 완료

---

### 4. 스케줄팀/야근팀

#### 4-1. SessionStart hook 에러
- **현상**: 전략 설계(나만의 방식) 등 headless CLI 호출 시 SessionStart hook이 exit 1 반환 → 실패
- **근본 원인**: headless 모드에서 사용자의 글로벌 hook 설정이 실행됨
- **해결 방향**: CLI에 hook 비활성 옵션 사용 또는 에러 무시 처리
- **영향 범위**: 나만의 방식 전략 생성, 스케줄/야근팀 AI 질문 생성 (현재 고정 질문/자유 입력으로 우회)

#### 4-2. 스케줄 status "running" 잔류
- **보정**: 보고서 파일이 존재하면 status를 completed로 강제 변경
- **파일**: `src/ui/server.py`, `src/scheduler/runner.py`

#### 4-3. CLI 5시간 사용량 리셋 정밀 감지
- **파일**: `src/overtime/runner.py`

#### 4-4. 야근팀 개발 작업 지원
- **목표**: "테스트 통과"를 평가 기준으로 사용하는 개발 모드

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
| `src/overtime/runner.py` | 야근팀 iteration 엔진 |
| `src/overtime/prompts.py` | 야근팀 프롬프트 |
| `src/company_builder/builder_agent.py` | 전략 설계 에이전트 |
| `src/company_builder/storage.py` | strategy/overtime CRUD |
| `src/company_builder/scheduler.py` | 스케줄→ScheduledJob 변환 (delta/append 포함) |
| `src/company_builder/schedule_storage.py` | 스케줄 CRUD |
| `src/scheduler/models.py` | PreContext (previous_report_path, output_mode) |
| `src/ui/server.py` | WebSocket 엔드포인트, 보고서 서빙 |
| `src/ui/sim_runner.py` | 그래프 실행 + WS 브릿지 |
| `src/ui/event_bridge.py` | 그래프 이벤트 → UI 이벤트 변환 (intake 에코 필터) |
| `src/ui/static/js/mode-company-card.js` | 인스턴트/나만의 방식 모드 (풀와이드 채팅, 모드별 히스토리) |
| `src/ui/static/js/card-chat-panel.js` | ChatPanel (switchMode, 모드별 컨테이너) |
| `src/ui/static/js/card-event-handler.js` | WS 이벤트 → UI 매핑 (인라인 대시보드) |
| `src/ui/static/js/card-builder.js` | 전략 설계 + 저장 |
| `src/ui/static/js/mode-overtime.js` | 야근팀 UI |
| `src/ui/static/js/mode-schedule.js` | 스케줄팀 UI (시/분/요일, output_mode, 상세설명) |
| `src/ui/static/css/card-view.css` | 풀와이드 레이아웃, 인라인 대시보드, 시스템 메시지 스타일 |
| `src/utils/pdf_converter.py` | HTML→PDF 변환 (Playwright) |
| `src/utils/dedup.py` | findings 중복 제거 |
| `src/config/settings.py` | `use_single_session` 토글 |
| `.mcp.json` | MCP 서버 설정 (firecrawl, github, mem0) |
