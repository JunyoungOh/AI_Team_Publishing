"""Single CLI session execution prompts.

Replaces the multi-subprocess pipeline with a single Claude Code CLI session
that handles research, synthesis, and report generation using native tools.
"""

SINGLE_SESSION_SYSTEM = """\
당신은 Enterprise Agent System의 실행 엔진입니다.
사용자의 요청을 분석하고, 정보를 수집하고, 최종 결과를 구조화된 JSON 파일로 저장합니다.
생성된 JSON은 서버 측 프로페셔널 템플릿으로 자동 렌더링되어 사용자에게 HTML 보고서로 제공됩니다.
따라서 당신은 **콘텐츠와 구조에만 집중**하면 됩니다. CSS/HTML/디자인은 전혀 신경 쓰지 마세요.

## 실행 전략

### 1단계: 분석 (30초)
사용자 요청과 Q&A를 분석하여 수집해야 할 정보 항목을 정리하세요.

### 2단계: 병렬 수집 (핵심)
독립적인 정보 수집은 **Agent 도구로 서브에이전트를 병렬 발동**하세요.
- 한 메시지에 여러 Agent를 호출하면 동시에 실행됩니다
- 각 Agent에게 구체적이고 명확한 검색 지시를 내리세요
- Agent 결과를 수신한 후 종합하세요

예시:
```
Agent 1: "카카오 주가 최근 동향 검색하고 핵심 수치를 정리해줘"
Agent 2: "카카오 경영진 변동 관련 최신 기사를 찾아줘"
Agent 3: "카카오 AI 전략 발표 내용을 검색해줘"
```

### 3단계: 검증
수집된 정보의 출처를 교차 검증하세요.
같은 사실의 반복(중복)을 제거하세요.

### 4단계: 리포트 저장
Write 도구로 구조화된 JSON 파일을 생성하세요. HTML/CSS 작성 금지.

## 도구 활용 가이드
- **WebSearch**: 웹 검색 (기본 검색 도구)
- **WebFetch**: URL에서 콘텐츠 추출
- **Agent**: 서브에이전트 병렬 실행 (정보 수집 분산)
- **Write**: 파일 생성 (최종 보고서)
- **Bash**: 디렉토리 생성, 시스템 명령
- **mcp__firecrawl__firecrawl_scrape**: JS 렌더링이 필요한 사이트 스크래핑

## 보고서 품질 기준
- **데이터 충실성**: 수치, 출처, 날짜를 반드시 포함
- **최신 데이터 우선**: 반드시 현재 시점 기준의 최신 정보를 검색하세요. 검색 시 연도를 명시하세요.
- **중복 금지**: 같은 사실을 다른 표현으로 반복하지 마세요
- **사용자 관점**: 시스템 내부 용어(워커, 에이전트, 도구명) 언급 금지
- **실질적 인사이트**: 단순 사실 나열이 아닌 분석과 시사점 포함
"""


REPORT_JSON_GUIDE_V2 = """\
## 보고서 저장 규격 (report.json)

Write 도구로 **단 하나의 파일** 만 생성하세요: `report.json`
HTML, CSS, <style>, 인라인 디자인, 색상 코드, 그라데이션 등 모든 디자인 요소 작성 금지.
서버가 이 JSON을 읽어 프로페셔널한 컨설팅 문서 스타일의 HTML로 자동 변환합니다.

### 스키마

```json
{
  "title": "보고서 제목 (한 줄)",
  "executive_summary": "핵심 요약. 마크다운 사용 가능 (3~5문장 또는 짧은 단락).",
  "sections": [
    {
      "heading": "섹션 제목",
      "body_md": "마크다운으로 작성된 본문. 표는 마크다운 표 또는 아래 table 필드 사용.",
      "table": {
        "headers": ["컬럼1", "컬럼2"],
        "rows": [["값A", "값B"], ["값C", "값D"]]
      },
      "sources": ["https://example.com/1", "https://example.com/2"]
    }
  ],
  "recommendations": [
    "권고사항 1 (한 문장)",
    "권고사항 2"
  ],
  "sources": [
    "https://global-source-1",
    "https://global-source-2"
  ]
}
```

### 필드 규칙
- `title`: **필수**. 제목만, 부제 없이.
- `executive_summary`: 권장. 핵심 결론 한 단락. 마크다운 사용 가능.
- `sections`: 권장. 최소 2개 이상 섹션. 각 섹션은 `heading` + `body_md` 가 기본.
- `sections[].table`: **선택**. 표 구조가 필요한 데이터일 때만. headers 와 rows 를 함께 줄 것.
- `sections[].sources`: **선택**. 그 섹션 전용 출처. 본문에 직접 링크를 박아도 됨.
- `recommendations`: 권장. 짧고 실행 가능한 문장으로.
- `sources`: 보고서 전체 출처 모음. 본문에서 인용한 모든 URL.

### 작성 규칙
1. 먼저 `mkdir -p {report_dir}` 실행
2. **모든 데이터를 수집한 뒤 마지막에 한 번만 Write** — 중간 임시 파일 작성 금지.
3. JSON 은 UTF-8 로 저장. 한국어 그대로, escape 처리 금지.
4. body_md 안에는 마크다운 표/리스트/볼드/링크 자유롭게 사용. HTML 태그는 쓰지 말 것.
5. 모든 수치에 출처 표기. body_md 안에 인라인으로 `(출처: URL)` 형태 또는 `sources` 배열 사용.
6. 한국어 기본. 전문용어는 원문 병기.
7. 같은 사실을 반복 서술하지 말 것.
"""

# Alias for backward compatibility — anyone importing the old name still works
REPORT_HTML_GUIDE = REPORT_JSON_GUIDE_V2

REPORT_MARKDOWN_GUIDE = """\
## Markdown 문서 규격

마크다운(.md) 파일을 생성하세요.

### 구조
- `# 제목` — 문서 제목
- `## Executive Summary` — 핵심 요약
- `## 섹션명` — 주제별 상세 분석
- 테이블: `| 항목 | 값 |` 마크다운 테이블 형식
- 출처: 각 데이터 뒤에 `(출처: URL)` 표기
- `## 참고자료` — URL 목록

### 규칙
- 한국어 기본, 전문용어 원문 병기
- 데이터는 마크다운 테이블로 구조화
- 모든 수치에 출처 명시
"""

REPORT_CSV_GUIDE = """\
## CSV 데이터 규격

CSV(.csv) 파일을 생성하세요.

### 규칙
- 첫 행은 헤더 (컬럼명)
- UTF-8 인코딩 (한글 지원)
- 쉼표(,) 구분, 필드 내 쉼표는 큰따옴표로 감싸기
- 날짜 형식: YYYY-MM-DD
- 출처 URL은 별도 컬럼으로
- 데이터가 여러 카테고리면 'category' 컬럼 추가
"""

REPORT_JSON_GUIDE = """\
## JSON 데이터 규격

JSON(.json) 파일을 생성하세요.

### 구조
```
{
  "title": "분석 제목",
  "generated_at": "2026-04-07",
  "summary": "핵심 요약",
  "data": [ ... ],
  "sources": [ ... ],
  "recommendations": [ ... ]
}
```

### 규칙
- UTF-8 인코딩
- 들여쓰기 2칸
- 날짜 형식: ISO 8601 (YYYY-MM-DD)
- data 배열 안에 구조화된 항목들
- 모든 항목에 source 필드 포함
"""

# 형식별 매핑.
# html/pdf 는 CLI 가 직접 HTML 을 만들지 않고 구조화된 report.json 만 저장 →
# Python 측 report_renderer 가 프로페셔널 템플릿으로 results.html 을 생성한다.
# pdf 는 그 results.html 을 다시 PDF 로 변환.
OUTPUT_FORMAT_MAP = {
    "html": {"ext": "report.json", "guide": REPORT_JSON_GUIDE_V2},
    "pdf": {"ext": "report.json", "guide": REPORT_JSON_GUIDE_V2},
    "markdown": {"ext": "results.md", "guide": REPORT_MARKDOWN_GUIDE},
    "csv": {"ext": "results.csv", "guide": REPORT_CSV_GUIDE},
    "json": {"ext": "results.json", "guide": REPORT_JSON_GUIDE},
}


def build_execution_prompt(
    user_task: str,
    user_answers: list[str] | None = None,
    clarifying_questions: list[str] | None = None,
    domains: list[str] | None = None,
    complexity: str = "low",
    report_dir: str = "data/reports/default",
    strategy: dict | None = None,
    output_format: str = "html",
    previous_report_path: str | None = None,
    output_mode: str = "replace",
    is_scheduled: bool = False,
) -> str:
    """싱글 세션에 전달할 실행 프롬프트 조립."""

    # Q&A 컨텍스트
    qa_block = ""
    if clarifying_questions and user_answers:
        qa_pairs = []
        for i, q in enumerate(clarifying_questions):
            a = user_answers[i] if i < len(user_answers) else "(미답변)"
            qa_pairs.append(f"Q: {q}\nA: {a}")
        qa_block = "\n\n## 명확화 Q&A\n" + "\n\n".join(qa_pairs)

    # 전략 프리셋이 있으면 관점별 지시를 주입
    strategy_block = ""
    if strategy:
        perspectives = strategy.get("perspectives", [])
        if perspectives:
            lines = [f"\n\n## 분석 프레임워크: {strategy.get('name', '분석 전략')}"]
            lines.append(f"{strategy.get('description', '')}\n")
            lines.append("### 분석 관점 (각 관점별로 Agent 서브에이전트를 병렬 실행하세요)")
            for p in perspectives:
                lines.append(f"- **{p.get('icon', '📌')} {p.get('name', '')}**: {p.get('instruction', '')}")
            special = strategy.get("special_instructions", "")
            if special:
                lines.append(f"\n### 특별 지시\n{special}")
            strategy_block = "\n".join(lines)

        # 전략의 depth/output_format으로 복잡도 오버라이드
        depth_override = strategy.get("depth")
        if depth_override:
            depth_to_complexity = {"light": "low", "standard": "medium", "deep": "high"}
            complexity = depth_to_complexity.get(depth_override, complexity)

    # 복잡도별 가이드
    depth_map = {
        "high": "심층 분석이 필요합니다. 다각도로 조사하고, 데이터 간 상관관계를 분석하세요. Agent 서브에이전트를 적극 활용하여 병렬 수집하세요.",
        "medium": "적절한 깊이의 분석이 필요합니다. 핵심 데이터를 충실히 수집하세요.",
        "low": "간결하고 핵심적인 정보 수집에 집중하세요. 불필요한 확장을 피하세요.",
    }
    depth_guide = depth_map.get(complexity, depth_map["low"])

    # 도메인 가이드 (전략이 있으면 생략)
    domain_block = ""
    if domains and not strategy:
        domain_block = f"\n\n## 분석 도메인\n{', '.join(domains)}"

    from datetime import date
    today = date.today().isoformat()

    # 출력 형식에 따른 파일명 + 가이드
    fmt = OUTPUT_FORMAT_MAP.get(output_format, OUTPUT_FORMAT_MAP["html"])
    output_filename = fmt["ext"]
    output_guide = fmt["guide"]

    # 스케줄 실행 시: 표준 report.json 외에 다음 실행이 비교용으로 참조할
    # 날짜 포함 markdown 스냅샷도 함께 생성한다. HTML 스냅샷은 Python
    # 렌더러가 report.json 으로부터 자동 생성하므로 CLI 가 만들 필요 없다.
    dated_output_block = ""
    if is_scheduled and output_format in ("html", "pdf"):
        dated_md = f"results_{today}.md"
        dated_output_block = f"""
**스케줄 실행이므로 다음 파일도 함께 생성하세요:**
- `{report_dir}/{dated_md}` — 다음 실행에서 비교용으로 참조할 Markdown 스냅샷

MD 파일 규칙:
- report.json 과 동일한 내용을 순수 Markdown 형식으로 작성
- 테이블, 수치, 출처를 모두 포함 (정보 손실 없이)
- CSS/HTML 태그 없이 순수 Markdown만 사용
"""

    # Delta 비교 블록: 이전 MD 파일이 있으면 비교 지시
    delta_block = ""
    if previous_report_path:
        delta_block = f"""

## 이전 실행 결과 비교 (Delta)
이전 실행의 요약 파일이 다음 경로에 있습니다:
`{previous_report_path}`

**반드시 다음 단계를 수행하세요:**
1. Read 도구로 이전 파일을 읽으세요
2. 이번에 수집한 데이터와 이전 데이터를 비교하세요
3. 보고서에 **"변동 사항 (Delta)"** 섹션을 추가하세요:
   - 새로 추가된 정보
   - 변경된 수치/사실 (이전 값 → 현재 값)
   - 삭제/소멸된 항목
4. 변동이 없으면 "주요 변동 없음"으로 표기하세요
"""

    # Append 모드 블록: 기존 파일에 데이터 누적
    append_block = ""
    if output_mode == "append" and previous_report_path:
        append_block = f"""

## 누적 모드 (Append)
기존 데이터 파일이 다음 경로에 있습니다:
`{previous_report_path}`

**이번 실행에서 수집한 데이터를 기존 파일에 추가하세요:**
- CSV: 기존 파일을 Read로 읽고, 새 행을 아래에 추가하여 같은 경로에 Write
- JSON: 기존 JSON의 data 배열에 새 항목을 추가하여 같은 경로에 Write
- Markdown: 기존 파일을 Read로 읽고, `---` 구분선 뒤에 새 날짜 섹션을 추가하여 Write
- 날짜 컬럼/필드를 반드시 포함하여 언제 추가된 데이터인지 구분되게 하세요
- 기존 데이터를 수정하거나 삭제하지 마세요
"""

    return f"""## 작업
{user_task}

## 현재 날짜
{today} — 이 날짜 기준으로 최신 정보를 검색하세요. 검색 시 "{today[:4]}년" 등 연도를 포함하세요.
{qa_block}
{strategy_block}
{domain_block}

## 분석 깊이
{depth_guide}
{delta_block}
{append_block}
## 출력
최종 결과를 다음 경로에 파일로 생성하세요:
`{report_dir}/{output_filename}`

먼저 `mkdir -p {report_dir}` 를 실행하세요.
{dated_output_block}
{output_guide}
"""
