"""Company Builder Agent — designs team structures via conversation.

Uses ClaudeCodeBridge (via bridge_factory) to generate responses.
Emits two message types:
  - builder_stream: complete text for the chat panel
  - builder_team: structured JSON with agents[] and edges[]
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from src.config.settings import get_settings
from src.utils.bridge_factory import get_bridge, MODEL_MAP

logger = logging.getLogger(__name__)

BUILDER_SYSTEM_PROMPT = """\
당신은 AI 기업의 **조직 설계 전문가(Chief Architect)**입니다.
사용자의 비즈니스 목표를 분석하여, 최적의 AI 에이전트 팀을 설계합니다.

## 설계 철학

### 기획-실행-검증 삼각구조
고품질 결과물을 위해 팀에는 세 가지 역할이 필요합니다:

| 역할 | 설명 | 팀에서의 위치 |
|------|------|-------------|
| **기획자 (Planner)** | 작업 프레임워크 설계, 분석 기준 정의 | 팀의 시작점 — 다른 워커에게 방향 제시 |
| **실행자 (Executor)** | 데이터 수집, 분석, 코드 작성 등 실제 작업 | 팀의 핵심 — 가능한 병렬 실행 |
| **검증자 (Reviewer)** | 결과물의 정확성, 논리 일관성 객관적 판단 | 팀의 끝점 — 실행자 결과를 검증 |

**간단한 팀**: 실행자 2~3명 (검증은 시스템 자동)
**표준 팀**: 실행자 2~3명 + 검증자 1명
**고품질 팀**: 기획자 1명 → 실행자 2~3명 → 검증자 1명

### Coordinator-Worker 패턴
모든 팀은 **리더 1명 + 전문 워커 N명** 구조입니다.
- **리더**: 팀 목표를 이해하고, 워커에게 구체적 지시를 내립니다.
- **워커**: 각자의 전문 도구를 사용하여 독립적으로 작업합니다.
- 워커 간 작업은 **가능한 병렬 실행**하여 속도를 극대화합니다.

### 최소 인원 원칙
- 3~5명이 가장 효율적입니다. 8명 초과 시 조율 비용이 급증합니다.
- 1명이 10분 내 완료 가능한 작업은 분리하지 마세요.
- **같은 도구를 쓰는 워커가 2명 이상이면**: 통합을 고려하세요.

### 중복 방지
- 워커 A가 수집하는 데이터를 워커 B가 또 수집하면 안 됩니다.
- 각 워커의 **작업 경계(scope)**를 명확히 구분하세요.

## 도구 카테고리 (tool_category)

| 카테고리 | 용도 | 주요 도구 | 적합한 역할 예시 |
|---------|------|----------|----------------|
| **research** | 웹 검색, 스크래핑 | WebSearch, WebFetch, Firecrawl | 시장 조사원, 경쟁사 분석가 |
| **data** | 데이터 분석, 시각화 | Python, Chart.js, KOSIS | 데이터 애널리스트, 통계 분석가 |
| **finance** | 재무/경제 데이터 | pykrx, DART, yfinance, ECOS | 재무 분석가, 투자 리서처 |
| **development** | 코드 작성, GitHub | GitHub, FileOps, Bash | 백엔드 개발자, DevOps |
| **security** | 보안 분석, CVE | NVD, GitHub Security | 보안 엔지니어 |
| **legal** | 법률/특허 조사 | 특허 검색, 법률 DB | 법률 조사원 |
| **hr** | 인사/노동 통계 | BLS, KOSIS 고용 | HR 분석가, 채용 리서처 |

## 역할(role) 작성 기준

**나쁜 역할 정의**: "시장 조사를 담당" → 범위 불명확, 무엇을 조사하는지 모름
**좋은 역할 정의**: "글로벌 생성 AI 시장의 규모, 성장률, 주요 기업 점유율을 수집하고 1차 분석" → 구체적 범위, 산출물 명확

**role 필수 요소**:
1. **대상**: 무엇을 다루는지 (시장, 기업, 기술, 데이터 등)
2. **행동**: 무엇을 하는지 (수집, 분석, 비교, 작성 등)
3. **산출물**: 무엇을 만드는지 (보고서, 데이터셋, 코드 등)

## edges (협업 구조)

edges의 `from→to`는 **업무 분배** 관계입니다.
- 리더 → 워커: "리더가 워커에게 작업을 분배"
- 워커 → 워커: "선행 워커의 결과를 후행 워커가 사용" (의존성)
- **순환 참조 금지** — 시스템이 자동으로 감지하여 제거합니다.

## 출력 형식

팀 구조를 제안할 때 반드시 아래 JSON 블록을 응답에 포함하세요:

```team_json
{
  "agents": [
    {
      "id": "agent_temp_001",
      "name": "에이전트 이름",
      "role": "구체적 역할 설명 (대상 + 행동 + 산출물)",
      "role_type": "executor",
      "tool_category": "카테고리",
      "emoji": "이모지"
    }
  ],
  "edges": [
    {"from": "agent_temp_001", "to": "agent_temp_002"}
  ]
}
```

**role_type 값**: `planner` (기획), `executor` (실행), `reviewer` (검증)
```

## 팀 설계 레퍼런스 (산업별 템플릿)

사용자의 목적에 따라 아래 템플릿을 참고하되, 그대로 복사하지 말고 목적에 맞게 조정하세요.

### 시장조사 팀 (3~4명)
- 기획자: 분석 프레임워크 설계 (research) → planner
- 시장 리서처: 시장 규모, 성장률, 주요 기업 데이터 수집 (research) → executor
- 경쟁사 분석가: 경쟁사 제품, 전략, 차별화 포인트 분석 (research) → executor
- 데이터 검증관: 수치 교차검증, 출처 확인, 논리 일관성 점검 (research) → reviewer

### 재무분석 팀 (3~4명)
- 재무 데이터 수집가: 재무제표, 주가, 거시경제 지표 수집 (finance) → executor
- 재무 분석가: 수익성/안정성/성장성 지표 산출, 밸류에이션 (finance) → executor
- 데이터 정합성 검증관: 수치 정확성, 출처 일치 여부 검증 (research) → reviewer

### 기술개발 팀 (3~4명)
- 아키텍트: 시스템 설계, 기술 스택 선정 (development) → planner
- 백엔드 개발자: 서버 로직, API 구현 (development) → executor
- 프론트엔드 개발자: UI/UX 구현 (development) → executor
- 코드 리뷰어: 코드 품질, 보안, 성능 검증 (development) → reviewer

## 대화 진행 방식

### 1단계: 목적 파악
"어떤 목적의 팀을 만드실 건가요?" — 팀의 비즈니스 목표를 확인합니다.
필요하면 추가 질문: "정기적으로 반복되는 작업인가요, 일회성인가요?"

### 2단계: 팀 설계 제안
목적을 파악하면 바로 팀 구조를 제안합니다.
- 각 에이전트의 역할과 **왜 이 역할이 필요한지** 설명
- 에이전트 간 협업 방식 (병렬/순차) 설명
- team_json 블록 포함

### 3단계: 반복 수정
사용자가 수정을 요청하면 **기존 구조를 유지**하면서 수정합니다.
수정 후 전체 team_json을 다시 출력합니다.

"캔버스에 팀이 표시됩니다. 수정이 필요하면 말씀해주세요."로 안내합니다.
"""


class BuilderSession:
    """Manages a company builder conversation session."""

    def __init__(self, user_id: str = ""):
        self.user_id = user_id
        self.history: list[dict[str, str]] = []
        self._bridge = get_bridge()

    async def stream_response(self, user_message: str, ws, workspace_files: list[str] | None = None) -> None:
        """Generate builder response and send over WebSocket.

        CLI bridge does not support token-by-token streaming, so we
        collect the full response via raw_query() then send it at once.
        Sends builder_stream tokens, then if team JSON is found,
        also sends a builder_team message.
        """
        from src.utils.workspace import read_files_as_context

        effective_message = user_message
        if workspace_files:
            file_ctx = read_files_as_context("builder", workspace_files)
            if file_ctx:
                effective_message = user_message + "\n\n" + file_ctx

        self.history.append({"role": "user", "content": effective_message})

        # Build conversation context: include recent history in the user message
        # so the CLI bridge (single user_message) sees the full conversation.
        conv_parts: list[str] = []
        for m in self.history[:-1]:  # all except the latest user message
            role_label = "User" if m["role"] == "user" else "Assistant"
            conv_parts.append(f"[{role_label}]: {m['content']}")
        conv_parts.append(f"[User]: {effective_message}")
        combined_message = "\n\n".join(conv_parts)

        full_text = ""
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                full_text = await self._bridge.raw_query(
                    system_prompt=BUILDER_SYSTEM_PROMPT,
                    user_message=combined_message,
                    model="sonnet",
                    allowed_tools=[],
                    max_turns=1,
                    timeout=120,
                )

                # Send the complete response as a single stream message
                try:
                    await ws.send_json({
                        "type": "builder_stream",
                        "data": {"token": full_text, "done": False},
                    })
                except Exception:
                    return  # WS disconnected

                # Signal stream end
                try:
                    await ws.send_json({
                        "type": "builder_stream",
                        "data": {"token": "", "done": True},
                    })
                except Exception:
                    return  # WS already closed

                self.history.append({"role": "assistant", "content": full_text})

                # Extract team JSON if present
                team_data = _extract_team_json(full_text)
                if team_data:
                    try:
                        await ws.send_json({
                            "type": "builder_team",
                            "data": team_data,
                        })
                    except Exception:
                        pass

                return  # Success — exit retry loop

            except Exception as e:
                logger.warning("Builder agent attempt %d/%d failed: %s", attempt + 1, max_retries + 1, e)
                if attempt < max_retries:
                    full_text = ""  # Reset for retry
                    try:
                        await ws.send_json({
                            "type": "builder_stream",
                            "data": {"token": "재시도 중...\n", "done": False},
                        })
                    except Exception:
                        return
                    import asyncio
                    await asyncio.sleep(1.0)
                else:
                    logger.exception("Builder agent error (all retries exhausted)")
                    try:
                        await ws.send_json({
                            "type": "error",
                            "data": {"message": "팀 설계 중 오류가 발생했습니다. 다시 시도해주세요."},
                        })
                    except Exception:
                        pass


def _extract_team_json(text: str) -> dict[str, Any] | None:
    """Extract and validate team structure JSON from ```team_json ... ``` blocks."""
    marker_start = "```team_json"
    marker_end = "```"

    idx = text.find(marker_start)
    if idx == -1:
        return None

    start = idx + len(marker_start)
    end = text.find(marker_end, start)
    if end == -1:
        return None

    raw = text[start:end].strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Failed to parse team JSON from builder response")
        return None

    if not isinstance(data.get("agents"), list) or len(data["agents"]) == 0:
        logger.warning("Team JSON missing agents array")
        return None

    # Validate and sanitize agents
    valid_categories = {"research", "data", "finance", "development", "security", "legal", "hr"}
    valid_role_types = {"planner", "executor", "reviewer"}
    agent_ids = set()
    for agent in data["agents"]:
        if not agent.get("id"):
            agent["id"] = f"agent_temp_{uuid.uuid4().hex[:6]}"
        if not agent.get("name"):
            agent["name"] = agent["id"]
        if not agent.get("role"):
            agent["role"] = "AI 에이전트"
        if agent.get("tool_category") not in valid_categories:
            agent["tool_category"] = "research"
        if agent.get("role_type") not in valid_role_types:
            agent["role_type"] = "executor"
        if not agent.get("emoji"):
            agent["emoji"] = "⚙️"
        agent_ids.add(agent["id"])

    # Validate edges — remove invalid ones
    valid_edges = []
    for edge in data.get("edges", []):
        from_id = edge.get("from", "")
        to_id = edge.get("to", "")
        if from_id in agent_ids and to_id in agent_ids and from_id != to_id:
            valid_edges.append(edge)
        else:
            logger.warning("Removed invalid edge: %s → %s", from_id, to_id)
    data["edges"] = valid_edges

    # Detect circular references (simple DFS)
    adj: dict[str, list[str]] = {aid: [] for aid in agent_ids}
    for e in valid_edges:
        adj[e["from"]].append(e["to"])

    def _has_cycle(node: str, visited: set, stack: set) -> bool:
        visited.add(node)
        stack.add(node)
        for neighbor in adj.get(node, []):
            if neighbor in stack:
                return True
            if neighbor not in visited and _has_cycle(neighbor, visited, stack):
                return True
        stack.discard(node)
        return False

    has_cycle = False
    visited: set[str] = set()
    for aid in agent_ids:
        if aid not in visited:
            if _has_cycle(aid, visited, set()):
                has_cycle = True
                break
    if has_cycle:
        logger.warning("Circular reference detected in team edges, removing all edges")
        data["edges"] = []

    return data


# ── Strategy Builder (분석 전략 프리셋 설계) ──

# 타입별 전략 유형: general | schedule | overtime
VALID_STRATEGY_TYPES = {"general", "schedule", "overtime"}

_STRATEGY_BASE_PROMPT = """\
당신은 **분석 방식 설계 전문가**입니다.
사용자의 비즈니스 목표를 분석하여, AI가 작업할 때 사용할 **분석 프레임워크(일하는 방식)**를 설계합니다.

## 설계 철학

### 관점(Perspective) 기반 분석
좋은 분석은 하나의 주제를 **여러 관점**에서 바라봅니다.
- 각 관점은 독립적인 분석 축 (예: 시장, 고객, 재무, 기술)
- AI가 각 관점별로 서브에이전트를 병렬 실행하여 정보를 수집합니다
- 관점이 3~5개일 때 가장 효율적. 7개 초과 시 분석이 산만해집니다.

### 관점 설계 원칙
1. **겹치지 않게**: 관점 A와 B의 조사 범위가 중복되면 안 됩니다
2. **빠짐없이**: 핵심 분석 축이 누락되면 안 됩니다 (MECE 원칙)
3. **구체적으로**: "시장 분석"보다 "글로벌 생성 AI 시장 규모·성장률·점유율 조사"가 좋습니다

## 출력 형식

방식을 제안할 때 반드시 아래 JSON 블록을 응답에 포함하세요:

```strategy_json
{{
  "name": "전략 이름",
  "description": "이 전략이 무엇을 분석하는지 1-2문장",
  "type": "{strategy_type}",
  "perspectives": [
    {{
      "name": "관점 이름 (간결하게)",
      "icon": "이모지 1개",
      "instruction": "AI에게 전달할 구체적 분석 지시 (무엇을 조사하고, 어떤 데이터를 수집하고, 어떤 형태로 정리할지)"
    }}
  ],
  "depth": "light | standard | deep",
  "output_format": "summary | executive_report | data_table | presentation",
  "special_instructions": "추가 지시사항 (선택)"
}}
```

### depth 설명
- **light**: 핵심만 빠르게 (~2분)
- **standard**: 적절한 깊이 (~4분)
- **deep**: 다각도 심층 분석 (~7분)

### output_format 설명
- **summary**: 핵심 요약 (1-2페이지)
- **executive_report**: 경영진 보고서 (커버+목차+상세)
- **data_table**: 데이터 중심 (테이블+차트)
- **presentation**: 발표 자료 스타일

## 대화 진행 방식

### 1단계: 명확화 질문 (필수)
사용자의 첫 입력을 받으면, **전략을 바로 설계하지 말고** 먼저 2~3개의 명확화 질문을 하세요.
질문의 목적: 어떤 분석 방식을 원하는지 정확히 파악하기 위함.

{clarify_section}

**중요**: 1단계에서는 strategy_json을 출력하지 마세요. 질문만 하세요.

### 2단계: 방식 설계
사용자의 답변을 반영하여 관점별 분석 프레임워크를 설계합니다.
- 각 관점이 **왜 필요한지** 간단히 설명
- strategy_json 블록 포함

{design_section}

### 3단계: 수정
사용자 요청에 따라 관점 추가/삭제/수정 후 전체 strategy_json을 다시 출력합니다.

"방식이 저장되었습니다. 작업을 지시하시면 이 방식으로 분석을 시작합니다."로 안내합니다.
"""

_CLARIFY_GENERAL = """\
질문 예시:
- "이 분석의 주요 목적은 무엇인가요? (투자 판단, 내부 보고, 경쟁 분석 등)"
- "특별히 중점을 두고 싶은 관점이 있나요?"
- "분석 깊이는 어느 정도를 원하시나요? (빠른 개요 vs 심층 분석)"
- "결과물 형식 선호가 있나요? (요약 보고서, 데이터 표, 발표 자료)"\
"""

_CLARIFY_SCHEDULE = """\
이 방식은 **정기적으로 반복 실행**되는 스케줄 작업용입니다.
매일/매주 자동으로 돌아가며 변화를 감지하고 보고하는 분석 방식을 설계합니다.

질문 예시:
- "정기적으로 모니터링하고 싶은 대상은 무엇인가요? (경쟁사, 시장 동향, 뉴스, 가격 등)"
- "변화 감지 시 특별히 알림받고 싶은 기준이 있나요? (예: 가격 10% 이상 변동)"
- "이전 실행 결과와 비교하여 변화를 추적하는 것이 중요한가요?"
- "결과물은 어떤 형식으로 받고 싶나요? (간략 요약 vs 상세 보고서)"\
"""

_CLARIFY_OVERTIME = """\
이 방식은 **목표 달성까지 반복 심화**하는 야근팀 작업용입니다.
한 번에 끝나지 않는 심층 리서치, 대규모 분석, 품질 목표 달성이 목적입니다.

질문 예시:
- "최종적으로 달성하고 싶은 분석 결과의 수준은? (데이터 포인트 수, 정보 깊이 등)"
- "분석 범위를 점진적으로 확장할 건가요, 하나의 주제를 깊이 파고들 건가요?"
- "반복 실행 시 이전 결과에서 부족한 부분을 자동으로 보강하면 좋겠나요?"
- "중간 결과를 누적할 건가요, 매 반복마다 전체를 새로 작성할 건가요?"\
"""

_DESIGN_GENERAL = """\
각 관점은 범용 분석 축으로 설계하세요.\
"""

_DESIGN_SCHEDULE = """\
각 관점은 **정기 모니터링에 적합하게** 설계하세요:
- 관점별 instruction에 "이전 결과 대비 변화"를 추적하는 지시를 포함
- 반복 실행해도 의미 있는 데이터 수집이 되도록 시간적 범위를 명시 (예: "최근 1주일")
- 변화 감지 기준이나 알림 조건을 instruction에 포함하면 좋습니다

**중요 - 역할 경계**:
당신의 역할은 **방식(관점) 설계**에 한정됩니다. 다음 사항은 **절대 묻거나 언급하지 마세요**:
- 실행 시간(예: "몇 시에", "오전/오후", "매일 몇 시")
- 실행 주기(예: "매일/매주/매월")
- 요일 선택
- 스케줄 등록/활성화

실행 시간과 주기는 사용자가 **별도의 '스케줄팀' 탭**에서 이 방식을 선택한 뒤 직접 설정합니다.
방식 카드(strategy_json)를 출력한 뒤에는 "이 방식을 저장한 다음, 스케줄팀 탭에서 실행 시간을 설정하시면 됩니다." 정도로만 안내하세요.\
"""

_DESIGN_OVERTIME = """\
각 관점은 **심층 반복 탐색에 적합하게** 설계하세요:
- 관점별 instruction에 "이전 iteration에서 수집한 데이터를 기반으로 추가 탐색" 지시를 포함
- 한 번의 실행으로 완성되지 않아도 되며, 반복할수록 깊어지는 구조로 설계
- 품질 기준(예: 데이터 포인트 N개 이상, 출처 M개 이상)을 instruction에 명시하면 좋습니다

**중요 - 역할 경계**:
당신의 역할은 **방식(관점) 설계**에 한정됩니다. 다음 사항은 **절대 묻거나 언급하지 마세요**:
- 최대 반복 횟수 (예: "몇 번 반복할까요?")
- 실행 시작/중단 시점
- 야근팀 실행 버튼 / 실행 등록 절차

반복 횟수와 실행은 사용자가 **별도의 '야근팀' 탭**에서 이 방식을 선택한 뒤 직접 설정합니다.
방식 카드(strategy_json)를 출력한 뒤에는 "이 방식을 저장한 다음, 야근팀 탭에서 반복 횟수와 목표를 설정하시면 됩니다." 정도로만 안내하세요.\
"""

_CLARIFY_MAP = {
    "general": _CLARIFY_GENERAL,
    "schedule": _CLARIFY_SCHEDULE,
    "overtime": _CLARIFY_OVERTIME,
}

_DESIGN_MAP = {
    "general": _DESIGN_GENERAL,
    "schedule": _DESIGN_SCHEDULE,
    "overtime": _DESIGN_OVERTIME,
}


def build_strategy_prompt(strategy_type: str = "general") -> str:
    """타입에 맞는 전략 설계 프롬프트를 조합하여 반환."""
    if strategy_type not in VALID_STRATEGY_TYPES:
        strategy_type = "general"
    return _STRATEGY_BASE_PROMPT.format(
        strategy_type=strategy_type,
        clarify_section=_CLARIFY_MAP[strategy_type],
        design_section=_DESIGN_MAP[strategy_type],
    )


# 하위 호환: 기존 코드에서 STRATEGY_BUILDER_PROMPT를 직접 참조하는 곳 대비
STRATEGY_BUILDER_PROMPT = build_strategy_prompt("general")


class StrategyBuilderSession:
    """분석 전략 프리셋 설계 대화 세션.

    Claude Code CLI의 --session-id/--resume를 활용하여 CLI 측에서
    대화 문맥을 유지합니다. 파이썬 쪽에서는 사용자 메시지만 보관하고,
    매 턴마다 전체 히스토리를 재전송하지 않습니다.
    """

    def __init__(self, user_id: str = "", strategy_type: str = "general"):
        self.user_id = user_id
        self.strategy_type = (
            strategy_type if strategy_type in VALID_STRATEGY_TYPES else "general"
        )
        self.history: list[dict[str, str]] = []
        self._bridge = get_bridge()
        # CLI 세션 ID (첫 호출 시 생성, 이후 턴에서 --resume으로 재사용)
        self._cli_session_id: str | None = None

    def set_strategy_type(self, strategy_type: str) -> None:
        """전략 타입을 변경하고 대화 히스토리를 초기화."""
        self.strategy_type = (
            strategy_type if strategy_type in VALID_STRATEGY_TYPES else "general"
        )
        self.history.clear()
        # 타입 변경 시 세션도 초기화 (새 시스템 프롬프트 적용 위해)
        self._cli_session_id = None

    async def stream_response(self, user_message: str, ws, workspace_files: list[str] | None = None) -> None:
        """전략 설계 응답 생성 및 WebSocket 전송.

        첫 호출에서는 새 session_id를 생성하고 시스템 프롬프트와 함께 전달.
        이후 호출은 --resume으로 동일 세션에 이어 붙이며 사용자 메시지만 전송.
        """
        from src.utils.workspace import read_files_as_context

        effective_message = user_message
        if workspace_files:
            file_ctx = read_files_as_context("builder", workspace_files)
            if file_ctx:
                effective_message = user_message + "\n\n" + file_ctx

        self.history.append({"role": "user", "content": effective_message})
        system_prompt = build_strategy_prompt(self.strategy_type)

        # 첫 호출: 새 세션 시작 / 이후 호출: 기존 세션 resume
        is_first_turn = self._cli_session_id is None
        if is_first_turn:
            self._cli_session_id = str(uuid.uuid4())

        try:
            full_text = await self._bridge.raw_query(
                system_prompt=system_prompt,
                user_message=effective_message,
                model="sonnet",
                allowed_tools=[],
                max_turns=3,
                timeout=120,
                session_id=self._cli_session_id if is_first_turn else None,
                resume=self._cli_session_id if not is_first_turn else None,
            )

            try:
                await ws.send_json({
                    "type": "builder_stream",
                    "data": {"token": full_text, "done": False},
                })
                await ws.send_json({
                    "type": "builder_stream",
                    "data": {"token": "", "done": True},
                })
            except Exception:
                return

            self.history.append({"role": "assistant", "content": full_text})

            strategy_data = _extract_strategy_json(full_text)
            if strategy_data:
                try:
                    await ws.send_json({
                        "type": "builder_strategy",
                        "data": strategy_data,
                    })
                except Exception:
                    pass

        except Exception as e:
            logger.exception("Strategy builder error: %s", e)
            try:
                await ws.send_json({
                    "type": "error",
                    "data": {"message": "전략 설계 중 오류가 발생했습니다. 다시 시도해주세요."},
                })
            except Exception:
                pass


def _extract_strategy_json(text: str) -> dict[str, Any] | None:
    """Extract strategy JSON from ```strategy_json ... ``` blocks."""
    marker_start = "```strategy_json"
    marker_end = "```"

    idx = text.find(marker_start)
    if idx == -1:
        return None

    start = idx + len(marker_start)
    end = text.find(marker_end, start)
    if end == -1:
        return None

    raw = text[start:end].strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Failed to parse strategy JSON")
        return None

    # 필수 필드 검증
    if not isinstance(data.get("perspectives"), list) or len(data["perspectives"]) == 0:
        logger.warning("Strategy JSON missing perspectives")
        return None

    if not data.get("name"):
        data["name"] = "분석 전략"

    # type 검증 — 없으면 general
    if data.get("type") not in VALID_STRATEGY_TYPES:
        data["type"] = "general"

    # perspectives 검증
    valid_depths = {"light", "standard", "deep"}
    valid_formats = {"summary", "executive_report", "data_table", "presentation"}

    for p in data["perspectives"]:
        if not p.get("name"):
            p["name"] = "관점"
        if not p.get("icon"):
            p["icon"] = "📌"
        if not p.get("instruction"):
            p["instruction"] = ""

    if data.get("depth") not in valid_depths:
        data["depth"] = "standard"
    if data.get("output_format") not in valid_formats:
        data["output_format"] = "executive_report"

    return data
