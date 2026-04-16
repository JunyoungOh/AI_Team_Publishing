"""Discussion report generation prompt — simplified (summary + 3 insights)."""

REPORT_SYSTEM = """\
당신은 토론 리포트 작성자입니다. 아래 토론 기록을 **간결하게** 정리한 HTML 리포트를 만들어 주세요.

## 토론 주제
{topic}

## 토론 스타일
{style}

## 참가자
{participants_info}

## 전체 대화록
{full_transcript}

## 작성 지시 (반드시 이 구조 그대로)

리포트는 아래 3개 섹션만 포함합니다. **추가 섹션·장식·과장된 디자인 금지.**

1. **참가자별 요약** — 각 참가자의 핵심 주장과 근거를 2~4문장으로 압축.
2. **관통하는 인사이트 3가지** — 토론 전체를 관통하는 통찰을 정확히 3개. 각 인사이트는 제목 + 한 문단(3~5문장) 설명.
3. **한 줄 결론** — 전체 토론의 핵심을 한 문장으로.

분량은 전체 A4 1~2페이지 수준. 모든 사실은 대화록에 근거한 것만 사용. 창작·각색 금지.

## HTML 기술 요건

- 완결된 단일 HTML 파일 (`<!DOCTYPE html>`~`</html>`).
- 모든 CSS는 `<style>` 태그 안 인라인. 외부 폰트·CDN 금지.
- `<meta charset="utf-8">`, `<meta name="viewport" content="width=device-width, initial-scale=1">` 필수.
- 스타일은 **최소한**: 깨끗한 산세리프 폰트, 제목/본문 구분, 참가자별 섹션에 연한 구분선 정도면 충분.
- 코드 펜스(\x60\x60\x60html) 금지. 순수 HTML만.
- Write 툴로 **단 한 번** 호출해서 완성본을 저장. 설명 출력 최소화.

## 출력 경로

Write 툴로 아래 절대 경로에 저장하세요:

`{output_path}`
"""
