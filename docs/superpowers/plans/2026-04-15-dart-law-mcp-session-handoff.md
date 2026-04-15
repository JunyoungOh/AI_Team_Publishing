# DART + 법령 MCP 이관 세션 핸드오프

**작성일**: 2026-04-15
**작성자**: Claude Opus 4.6 (이전 세션)
**브랜치**: `feat/dart-mcp`
**상태**: ⚠️ 불확실 — 사용자가 "엔진 이관 전보다 퀄리티가 나쁨" 보고

---

## 🎯 한 줄 요약

DART + 법령 탭을 커스텀 XML 래퍼 엔진에서 native MCP 프로토콜로 이관했고, 이관 과정에서 숨어있던 4개 레거시 버그를 수정했지만, **사용자가 체감 퀄리티 악화를 보고**하며 세션이 중단됨. **다음 세션에서 rollback vs 추가 수정을 결정해야 함.**

---

## 📋 브랜치 상태

```
feat/dart-mcp (origin 과 동기화 상태, 10 커밋 ahead of main)

8dbfb17 simplify(dart+law): 시스템 프롬프트 대폭 간소화 (~3000자 → ~900자)   ← 마지막
9d2186a fix(dart+law): 디스클레이머 이후 과도한 탐색 차단 — 코드 kill 스위치
26d5d5a fix(law): stale 툴 인디케이터 (mode-law.js)
f94b6b0 fix(dart): 메인 사업보고서 XML 선택 (감사보고서 대신)
fe1f798 fix(law): 참고 원문 링크 하얀 페이지 — lsInfoP?lsiSeq=MST
d66e40e fix(law): 조문 본문 추출 — 항/호/목 계층 재귀 파싱
e041f53 fix(law): 쟁점 → 법령명 매핑 프롬프트 가이드
db3ef0d feat(law): MCP 서버 기반 재작성 + 양쪽 죽은 코드 대거 정리
231cf09 fix(dart): 디스클레이머 이후 stale 툴 인디케이터 차단
f372bb4 feat(dart): Open DART MCP 서버 기반 재작성
```

**순 변경량**: 대략 +1500 / -2500 (메인 대비 약 -1000줄)

---

## 🚨 결정적 사용자 피드백 (무시 금지)

```
"완전 엔진 도입 전보다 퀄리티가 안좋아졌네."

"응답을 뭘 많이 조합해. 그냥 API가 주는 답을 흡수해서 보기좋게 나열부터
해주고 거기에 코멘트 3,4문장 붙이고 끝내. 대체 무슨 복잡한 로직을 하고 있는거야."

"법령도 마찬가지야 지금. 같은 에러 나고 있어."
```

**재현 시나리오** (마지막으로 실패한 것):
- DART: "네이버 현재 사외이사 리스트 알려줘"
- 증상: "NAVER corp_code 확인 → 최신 사업보고서 가져오기 → 사외이사 정보 가져오기" narration 이후
  `⚠️ 모델이 응답을 조합하는 도중 내부 한도에 도달했습니다` 에러로 중단

- 법령: "2025년 이후 개정된 근로기준법" — 같은 패턴, CLI 크래시

---

## ✅ 기술적으로 검증된 것들

1. **MCP 서버 자체는 정확히 작동** (stdio 직접 테스트 기준)
   - `DartClient + CorpCodeIndex` 재사용 성공, 실제 `corp_code=00266961, NAVER` 반환
   - `LawClient` 재사용 성공, 실제 `근로기준법 MST=265959` 반환

2. **수정된 레거시 버그들 (이관 전부터 존재)**
   - `_normalise_article` 가 `항` 배열을 무시해 제23조 본문을 빈값으로 돌려주던 버그
   - `_build_law_url` 이 `/법령/{name}/{date}` 라는 죽은 URL 포맷 사용 → 하얀 페이지
   - `_extract_document_text` 가 ZIP 의 첫 XML(감사보고서)만 읽어 사업보고서 본문 놓침
   - `_build_law_url` 이 `lsiSeq` 파라미터에 `law_id` 를 넘겨 "축우도살제한법" 반환 (실제는 MST 필요)

3. **Python 재현 테스트에서 end-to-end 성공** (live 앱에선 다른 결과)
   - "갑자기 해고당했어요" → 제23/26/28조 본문 인용 + 구제신청 안내 ✓
   - "네이버 사외이사 리스트" → 변재상·이사무엘·김이배 명단 ✓

---

## ❌ 미해결 / 악화된 것들

1. **Live 앱에서 CLI 크래시 반복 발생**
   - Python 재현 테스트는 통과하는데 실제 브라우저 live 앱에서는 실패
   - 차이의 원인 미파악 (학습 모드 훅? 다른 환경 변수? 세션 상태?)

2. **LLM goal-seeking 이 멈추지 않음**
   - 프롬프트에 "디스클레이머 이후 도구 호출 금지" 를 여러 차례 강화했지만 위반
   - 코드 레벨 kill 스위치 (`_disclaimer_seen` + `_kill_proc`) 추가했지만 정황상 효과 불확실

3. **사용자 체감 퀄리티 악화**
   - 구 엔진은 환각이 있지만 "빠르고 잘 되는 것처럼 보였음"
   - 새 MCP 엔진은 정확하지만 "느리고 자주 크래시"
   - **이게 이번 세션 최대 실패 지점**

---

## 🔁 가능한 다음 행동 (우선순위 순)

### Option A: main 으로 완전 복귀 (가장 안전)
```bash
git checkout main
git branch -D feat/dart-mcp   # 선택: 로컬 브랜치 삭제. origin 에는 보존됨
```
- 이 세션의 모든 변경 폐기
- 구 엔진 (XML `<tool_call>` 래퍼) 상태로 복귀 → 사용자 "이전" 상태 복원
- `feat/dart-mcp` 브랜치는 `origin` 에 백업돼 있으니 나중에 재방문 가능
- **권장**: 사용자가 MCP 이관이 실패작이라고 판단하면 즉시 이 경로

### Option B: 부분 rollback — URL/본문 파싱 픽스만 유지
```bash
git checkout main
git cherry-pick d66e40e   # 조문 본문 항/호/목 파싱 (법령 client.py)
git cherry-pick fe1f798   # 하얀 페이지 URL (법령 client.py)
git cherry-pick f94b6b0   # 메인 사업보고서 XML 선택 (DART tools.py)
```
- **MCP 이관은 포기**, **client/tools 레벨 버그 수정만 승계**
- 이 3개 커밋은 구 엔진에서도 그대로 유효함 (engine 독립적)
- 구 엔진 + 3개 버그 픽스 = 이전 상태보다 약간 더 정확
- **권장**: "구 엔진이 실제로 숨은 버그가 있었고, 그걸 고치는 것만 취하자" 경우

### Option C: 단순화된 프롬프트로 live 재테스트 (feat/dart-mcp 유지)
- `8dbfb17` 에서 프롬프트를 ~900자로 줄인 상태
- 서버 재기동 + 하드 리프레시 후 "네이버 사외이사" 재시도
- 성공하면 브랜치 유지 → PR → merge
- 실패하면 → Option A 또는 B
- **권장**: 이 단순화 시도 한 번에 마지막 기회를 주고 싶다면

### Option D: 완전히 새 접근
- 구 엔진도, 새 MCP 도 둘 다 포기하고 **WebFetch 기반** 접근 (엔진 없음)
- LLM 이 Open DART / law.go.kr REST API 를 `WebFetch` 로 직접 호출
- 이 세션 중반에 고려했다가 이관을 선택했음
- **리스크**: 완전히 새 설계, 시간 소모

---

## 📁 수정된 주요 파일 (브랜치 기준)

### 신규
- `src/dart/mcp_server.py` — DART MCP stdio 서버 (~110줄)
- `src/dart/mcp_session.py` — DART 세션 (~500줄, 프롬프트 900자 버전)
- `src/law/mcp_server.py` — 법령 MCP stdio 서버 (~110줄)
- `src/law/mcp_session.py` — 법령 세션 (~500줄, 프롬프트 1000자 버전)

### 수정
- `.mcp.json` — DART, Law MCP 서버 엔트리 추가
- `src/ui/routes/dart.py` — `DartSession` → `DartMcpSession`
- `src/ui/routes/law.py` — `LawSession` → `LawMcpSession`
- `src/dart/client.py` — corp_code 8자리 검증, `_normalise_date_range` 스마트 보정
- `src/dart/tools.py` — `_pick_main_xml`, `_extract_document_text` 재귀 파싱
- `src/law/client.py` — `_collect_paragraph_text` 재귀, `_build_law_url` lsInfoP?lsiSeq=MST
- `src/ui/static/js/mode-dart.js` — done:true 시 `.dart-typing` 정리
- `src/ui/static/js/mode-law.js` — 동일 수정

### 삭제 (죽은 코드)
- `src/dart/engine.py` (593줄)
- `src/dart/session.py` (121줄)
- `src/dart/prompts/system.py` + `__init__.py`
- `src/law/engine.py` (613줄)
- `src/law/session.py` (136줄)
- `src/law/prompts/system.py` + `__init__.py`

---

## 🧠 핵심 교훈 (다음 세션 참고)

1. **LLM 환각이 다른 레이어 버그를 은폐할 수 있음**
   - 이 세션에서 발견한 4개 레거시 버그는 환각이 "그럴듯한 답"을 만들어 은폐됨
   - MCP 이관이 환각을 차단하자 숨어있던 정확도 문제가 폭로됨
   - 교훈: 환각 차단 후 갑자기 "이전보다 나빠 보이는" 경우, 숨은 버그가 드러난 것일 수 있음

2. **프롬프트 길이는 LLM 행동 복잡도에 비례**
   - 3000자 프롬프트 → LLM 이 모든 규칙 따르려다 agent-style 복잡 reasoning
   - 900자 프롬프트 → 예상은 단순화지만 효과 미검증
   - 교훈: agent 를 원하지 않으면 프롬프트도 agent-level 로 쓰면 안 됨

3. **기술적 correctness vs 사용자 체감 퀄리티의 trade-off**
   - MCP 이관은 기술적으로 옳음 (환각 차단, 코드 축소)
   - 하지만 사용자는 "이전이 나았다" 고 평가
   - 교훈: 사용자 피드백이 기술적 판단과 충돌하면 사용자가 맞음

4. **code guard 가 prompt rule 보다 확실하지만 완벽하진 않음**
   - `_disclaimer_seen` + `_kill_proc` 는 시도했지만 live 에서 효과 불확실
   - 교훈: 코드 guard 도 완전한 방어 아님. 행동 패턴 자체를 바꿔야 할 때는 모델/프롬프트를 단순화하는 게 먼저

5. **라이브 앱과 Python 재현 테스트가 다른 결과를 낼 수 있음**
   - 이 세션에서 Python 재현은 여러 번 6/6/7/7 통과
   - 하지만 사용자 live 앱은 같은 질문에서 크래시
   - 원인 미파악 — 환경 변수? hook? session state?
   - 교훈: Python 재현이 통과해도 live 검증이 진짜

---

## 🚀 새 세션 시작 시 권장 첫 명령

```bash
# 1. 현재 브랜치 상태 확인
cd /Users/elvis.costello/Desktop/backup_web_local
git branch --show-current
git log --oneline -10

# 2. 이 핸드오프 문서 먼저 읽기
cat docs/superpowers/plans/2026-04-15-dart-law-mcp-session-handoff.md

# 3. 사용자에게 결정 요청:
#    "Option A/B/C/D 중 어느 것으로 진행할까요?"

# 4. 결정에 따라 실행
#    A: git checkout main
#    B: git checkout main && git cherry-pick d66e40e fe1f798 f94b6b0
#    C: (브랜치 유지) 사용자에게 서버 재기동 요청 + live 테스트 결과 수집
#    D: 새 설계 논의
```

---

## ⚠️ 주의사항

- **Python 재현 테스트만 보고 성공 선언하지 말 것** — 이 세션에서 여러 번 이 실수 반복
- **사용자 피드백이 technical metric 과 충돌하면 사용자가 우선**
- `feat/dart-mcp` 브랜치는 `origin` 에 있으니 로컬에서 삭제해도 복구 가능
- 사용자가 세션 중 여러 번 불만 표명 — **새 세션에서는 첫 행동 전에 확인 필수**

---

## 📞 새 세션에서 사용자에게 물어볼 것

1. **"이 md 문서를 읽어봤으니, Option A/B/C/D 중 어느 것으로 갈지 결정해주세요"**
2. **"기술적 정확도 vs 응답 속도 중 무엇을 우선시할까요?"**
3. **"MCP 이관 자체를 포기해도 될까요, 아니면 한 번 더 시도해볼까요?"**
