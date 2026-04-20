from __future__ import annotations

import threading

from pydantic_settings import BaseSettings

_cached_settings: Settings | None = None
_settings_lock = threading.Lock()


def get_settings() -> Settings:
    """Return a cached Settings singleton (reads .env once)."""
    global _cached_settings
    if _cached_settings is None:
        with _settings_lock:
            if _cached_settings is None:
                _cached_settings = Settings()
    return _cached_settings


class Settings(BaseSettings):
    # Claude Code model identifiers (opus, sonnet, haiku)
    ceo_model: str = "sonnet"  # B 그룹: 리포트 합성, 평가, 수정, P-E-S-R 판정 (high effort로 품질 보완)
    leader_model: str = "sonnet"
    worker_model: str = "sonnet"
    reporter_model: str = "sonnet"

    # Fast path
    fast_path_enabled: bool = False  # 비활성화 — 하이브리드 리뷰 파이프라인으로 모든 태스크 정식 처리
    fast_path_excluded_domains: list[str] = [
        "engineering",  # 코드 작성/빌드 등 도구 필수
        "research",     # 웹 검색/데이터 수집이 필수
        "data",         # ML/데이터 파이프라인 도구 필요
        "finance",      # 실시간 재무 데이터 필요
        "security",     # 보안 도구/스캐닝 필요
    ]

    # Iteration limits
    max_plan_revisions: int = 2
    max_workers_per_leader: int = 3

    # Membership & Auth
    entry_code: str = ""              # 입장코드 (관리자가 수시 변경, 비어있으면 입장코드 검증 스킵)
    membership_enabled: bool = False  # 회원제 기본 비활성 (True면 로그인/입장코드 요구)
    jwt_secret: str = ""              # JWT 서명 키 (비어있으면 서버 시작 시 자동 생성)
    user_db_path: str = "data/users.db"

    # Registration restriction (e.g. "kakaocorp.com" → only @kakaocorp.com emails allowed)
    allowed_email_domain: str = ""  # Empty = any username allowed

    # Transport — Local version: 항상 CLI 모드
    use_api_direct: bool = False  # Local: 항상 False (Claude Code CLI 인증 사용)
    anthropic_api_key: str = ""   # Local에서는 불필요 (CLI 인증 사용)
    prefer_subprocess: bool = True  # subprocess 직접 사용

    # SDK (Anthropic API direct mode) settings
    sdk_max_tokens_structured: int = 16384   # Structured output max tokens
    sdk_max_tokens_text: int = 8192          # Text output max tokens
    sdk_model_map: dict[str, str] = {        # Model alias → full model ID
        "opus": "claude-opus-4-6-20250116",
        "sonnet": "claude-sonnet-4-6-20250514",
        "haiku": "claude-haiku-4-5-20251001",
    }

    # Model overrides for specific operations
    ceo_route_model: str = "sonnet"  # 도메인 분류 — Sonnet으로 충분, 속도 우선
    ceo_decomposition_model: str = "sonnet"  # 작업 분해 — JSON 스키마 준수 + 속도
    # Review-stage model tiers: all sonnet (rollback to opus if quality drops)
    confirm_plan_model_single: str = "sonnet"
    confirm_plan_model_cross: str = "sonnet"
    gap_analysis_model_single: str = "sonnet"
    gap_analysis_model_cross: str = "sonnet"

    # Effort level (CLI 2.1.68+: Opus 4.6 defaults to "medium" extended thinking)
    # "low" = no extended thinking (fast), "medium" = moderate thinking, "high" = deep reasoning
    default_effort: str = "low"          # Fallback for unspecified calls — speed-critical
    # CEO 초반부 (Sonnet): medium effort — 분류/생성/분해 속도 우선
    ceo_routing_effort: str = "medium"        # 도메인 분류 + 모드 선택
    ceo_question_effort: str = "medium"       # 질문 생성
    ceo_decomposition_effort: str = "medium"  # 작업 분해
    ceo_confirm_effort: str = "medium"        # 계획 승인/거부
    # Worker (Sonnet): medium effort — 데이터 수집 중심
    worker_effort: str = "medium"
    # B 그룹 (Sonnet high): 리포트 합성, 평가, 수정 — extended thinking으로 품질 보완
    reporter_effort: str = "high"

    # Timeout tiers (seconds) — matched to task complexity
    llm_call_timeout: int = 120       # T1 Simple: CEO routing, leader Q&A, worker assembly
    planning_timeout: int = 360       # T2 Moderate: plan creation, plan revision (needs margin for retries)
    scheduled_planning_timeout: int = 240  # T2-fast: scheduled mode (no user interaction, faster fail-retry)
    ceo_review_timeout: int = 360     # T2.5 CEO review: confirm_plan (needs margin for retries)
    reporter_timeout: int = 300       # T2 Reporter: final_report synthesis (sonnet, 5min for 10+ workers)
    execution_timeout: int = 1200     # T3 Complex: worker execution with tools (20min, doubled from 600)
    degraded_timeout: int = 600       # T3-fallback: worker execution with builtin search tools (10min, doubled from 300)
    parallel_task_timeout: int = 1200  # Aggregate timeout for staggered parallel batches (20min, doubled from 600)

    # Concurrency
    max_parallel_api_calls: int = 5   # Max concurrent Claude Code calls (MCP removed in Phase 1; npx lock contention no longer applies)

    # Execution resilience
    enable_tool_fallback: bool = True    # Retry without tools when tool execution fails
    parallel_stagger_delay: float = 1.0  # Base delay (seconds) between parallel launches (scales with task count)
    retry_jitter_max: float = 2.0       # Max random delay (seconds) before first retry attempt

    # CLI stability (cli-stability feature)
    cli_min_version: str = "2.1.60"           # Minimum CLI version (stream-json, --effort support)
    max_output_size: int = 5_000_000          # 5MB stdout truncation limit
    retry_max_attempts: int = 3               # Max retries for rate_limit errors
    retry_base_delay: float = 2.0             # Base delay for exponential backoff (seconds)
    retry_max_delay: float = 30.0             # Max backoff delay cap (seconds)
    retry_server_error_attempts: int = 1      # Max retries for server errors
    circuit_breaker_threshold: int = 5        # Consecutive failures before circuit opens
    circuit_breaker_cooldown: float = 60.0    # Seconds before half-open retry

    # Worker max turns (limits Claude CLI interaction rounds per worker)
    # Hierarchy: default(50) < dev(56) < search(60) — all doubled for complex tasks
    worker_max_turns: int = 50          # Default: doubled 25→50 (prevents unnecessary retries for content-heavy workers)
    worker_max_turns_search: int = 60   # Search-heavy workers: doubled 30→60 (search+synthesis cycles)
    worker_max_turns_dev: int = 56      # Dev workers: doubled 28→56 (code+test+fix cycles)
    ceo_max_turns: int = 10             # CEO/Leader: planning, questions, confirm (StructuredOutput may need 6-8 turns)
    ceo_route_max_turns: int = 10       # CEO routing: opus needs ~3 turns, margin for retries
    reporter_max_turns: int = 12        # CEO report: rich report_html with labeled data

    # Single Session Mode (싱글 CLI 세션으로 전체 실행)
    # 메인 파이프라인은 항상 싱글 세션 모드로 동작. 레거시 다중 워커 경로는 제거됨.
    # Base timeout (초) — single_session_node에서 complexity에 따라 0.7~1.5배 스케일.
    # 5관점 이상 전략의 병렬 리서치 + HTML 합성에 필요한 넉넉한 여유 확보.
    single_session_timeout: int = 1500        # 싱글 세션 기본 타임아웃 (25분) — LLM 이 results.html 을 직접 작성하는 토큰 여유 포함
    single_session_max_turns: int = 100       # 싱글 세션 기본 최대 턴 수

    # P-E-S-R (Planner-Executor-Synthesizer-Reviewer) review loop
    enable_review_loop: bool = True            # False = skip P-E-S-R loop, use flat/staged execution
    per_loop_max_iterations: int = 2           # 최대 루프 반복 횟수 (high complexity만 2회)
    reviewer_verdict_timeout: int = 90         # Reviewer structured_query 타임아웃 (초)
    synthesizer_model: str = "sonnet"           # Synthesizer 모델 (high effort extended thinking으로 품질 보완)
    synthesizer_effort: str = "high"           # Synthesizer extended thinking effort
    synthesizer_timeout: int = 180             # Synthesizer 타임아웃 (초)

    # Staged execution (dependency-based worker ordering)
    enable_staged_execution: bool = True       # False = always flat parallel (legacy behavior)
    dev_execution_timeout: int = 1200          # Dev worker timeout: 20min (doubled from 600)
    stage_timeout: int = 1800                  # Per-stage timeout: 30min (doubled from 900)
    max_total_staged_timeout: int = 2400       # Total pipeline timeout: 40min (doubled from 1200)

    # Domain Analyst (per-domain synthesis + gap analysis)
    analyst_model: str = "sonnet"           # Domain Analyst (Opus→Sonnet 전환, effort high)
    analyst_timeout: int = 600             # 도메인 합성 타임아웃 (10분 — Opus report_html 포함)
    analyst_effort: str = "high"           # Extended thinking effort (Opus→Sonnet 전환 보상)
    analyst_max_turns: int = 8             # Analyst max turns (report_html 생성에 여유)

    # Deep Research (하이브리드 모드 — 리서치 작업을 단일 세션으로 실행)
    deep_research_model: str = "sonnet"        # Sonnet 4.5 (1M context, Opus 대비 80% 비용 절감)
    deep_research_timeout: int = 2400          # 40분 (단일 세션 충분)
    deep_research_max_turns: int = 200         # 능동적 반복 리서치 허용
    deep_research_effort: str = "high"         # extended thinking (budget=32000)
    deep_research_parallel_timeout: int = 3000  # 다중 도메인 전체 파이프라인 50분
    deep_research_tools: list[str] = [
        "WebSearch", "WebFetch", "Bash",
        "mcp__firecrawl__firecrawl_scrape",
        "mcp__firecrawl__firecrawl_crawl",
    ]

    # Breadth Research (넓은 조사 모드 — 경량 검색+수집 파이프라인)
    breadth_total_timeout: int = 600          # 전체 파이프라인 타임아웃 (10분)
    breadth_max_concurrent_scrapes: int = 5   # 동시 스크래핑 수
    breadth_scrape_timeout: int = 15          # URL당 스크래핑 타임아웃 (초)
    breadth_chunk_size: int = 1000            # 청크 분할 크기 (자)
    breadth_filter_model: str = "haiku"       # 필터링용 모델
    breadth_synthesis_model: str = "sonnet"   # 합성용 모델
    breadth_synthesis_timeout: int = 300      # 합성 타임아웃 (5분)

    # Loop guards
    max_ceo_rejections: int = 2
    max_escalations: int = 2
    max_result_revision_cycles: int = 2  # Reduced from 3 — failed workers are skipped, so 2 is sufficient

    # Context management
    max_messages_in_context: int = 20
    max_plan_chars_in_context: int = 2000
    max_result_chars_in_context: int = 30000   # 3000→30000: no truncation for CEO report
    max_plan_chars_for_ceo: int = 800
    max_result_chars_for_ceo: int = 1000

    # Agent quality
    plan_approval_threshold: int = 7  # Default for domains not in overrides
    domain_approval_thresholds: dict[str, int] = {
        "security": 8,
        "legal": 8,
        "hr": 6,
        "marketing": 6,
        "operations": 6,
    }

    # MCP Server API Keys (read by MCP servers via .mcp.json env mapping)
    brave_api_key: str = ""
    firecrawl_api_key: str = ""
    github_personal_access_token: str = ""
    mem0_api_key: str = ""
    serper_api_key: str = ""

    # Domain-specific API keys (Phase 2)
    dart_api_key: str = ""
    kosis_api_key: str = ""
    ecos_api_key: str = ""

    # Korean Law (law.go.kr) — Open API 인증키(OC)
    law_oc: str = ""
    law_cache_ttl_search: int = 3600      # 검색 결과 1시간
    law_cache_ttl_full: int = 86400       # 조문 원문 24시간
    law_session_ttl_minutes: int = 30     # WS 세션 비활성 타임아웃
    law_request_timeout: int = 20         # law.go.kr HTTP 타임아웃

    # Open DART (opendart.fss.or.kr) — 전자공시 API
    # dart_api_key는 202행에 이미 선언되어 있음
    dart_cache_ttl_search: int = 3600     # 공시 목록 1시간
    dart_cache_ttl_full: int = 86400      # 공시 원문/기업개황 24시간
    dart_session_ttl_minutes: int = 30    # WS 세션 비활성 타임아웃
    dart_request_timeout: int = 20        # Open DART HTTP 타임아웃

    # Optional: LangSmith
    langchain_tracing_v2: bool = False
    langchain_api_key: str = ""
    langchain_project: str = "enterprise-agent"

    # Telegram 알림 (완료 이벤트를 봇으로 전송 — 싱글유저 로컬 가정)
    telegram_bot_token: str = ""           # BotFather에서 발급받은 봇 토큰
    telegram_chat_id: str = ""             # getUpdates로 확인한 본인 chat_id
    telegram_notify_enabled: bool = True   # 통합 on/off (토큰이 비었으면 자동 off)
    telegram_request_timeout: int = 5      # sendMessage HTTP 타임아웃 (짧게 — fire-and-forget)

    # Scheduler
    scheduler_db_path: str = "data/scheduler.db"
    checkpoint_db_path: str = "data/checkpoints.db"
    scheduler_timezone: str = "Asia/Seoul"
    scheduler_misfire_grace_time: int = 60

    # Safety
    scheduler_max_consecutive_failures: int = 3  # auto-pause after N failures

    # PDF Report Export
    report_export_enabled: bool = True
    report_output_dir: str = "data/reports"
    max_reports_to_keep: int = 5           # auto-cleanup: keep only last N report folders
    worker_output_dir: str = "data/outputs"  # constrained dir for worker-generated files

    # Notifications
    scheduler_notify_on_success: bool = False
    scheduler_notification_log_path: str = "data/notifications.jsonl"

    # Worker result cache (session scope, prevents duplicate execution)
    enable_worker_cache: bool = True
    worker_cache_ttl: int = 3600  # 1 hour (session lifetime)

    # Blackboard (removed — predecessor_context already handles inter-stage sharing)

    # Adaptive model selection (disabled: all workers use sonnet for consistent speed)
    # Previously: high→opus caused timeouts, low→haiku caused quality issues
    enable_adaptive_model: bool = False
    complexity_model_map: dict[str, str] | None = None  # Unused when adaptive disabled

    # Worker Reflection (Producer-Critic 패턴)
    enable_worker_reflection: bool = True       # Reflection 전체 토글
    reflection_model: str = "haiku"             # 평가 모델
    reflection_timeout: int = 15               # Haiku 평가 타임아웃 (초)
    reflection_min_repair_budget: int = 60     # repair 최소 시간 예산 (초)
    enable_deep_research_reflection: bool = True  # Deep Research Reflection 토글

    # User review of worker results (human-in-the-loop after worker execution)
    enable_user_review: bool = True

    # Domain plugin system (YAML-based domain registration)
    enable_plugins: bool = True
    plugin_dir: str = "domains/"

    # Execution metrics (persistent history across sessions)
    enable_metrics: bool = True
    metrics_db_path: str = "data/metrics.db"

    # DataLab
    datalab_sandbox_timeout: int = 120        # Hard kill timeout for run_python (seconds, pandas+openpyxl needs time on low-CPU)
    datalab_sandbox_memory_mb: int = 0        # Memory limit (0=unlimited, pandas needs ~300MB on import alone)
    datalab_max_upload_size_mb: int = 50      # Max file upload size
    datalab_max_uploads_per_session: int = 10 # Max files per session
    datalab_session_ttl_minutes: int = 30     # Inactive session auto-cleanup
    datalab_model: str = "sonnet"             # JARVIS model — 코드 레벨에서만 변경 (UI 설정 노출 금지)
    datalab_effort: str = "low"               # Extended thinking — low=비활성(안정), medium/high=활성(thinking 블록 처리 필요)

    # Foresight — Sonnet 주력 (앙상블+Platt Scaling으로 품질 보완, AIA Forecaster 패턴)
    foresight_env_model: str = "sonnet"        # Phase 1: 환경 구축 (검색, 정리, 구조화)
    foresight_env_effort: str = "low"          # Phase 1: Extended thinking 불필요
    foresight_predict_model: str = "sonnet"    # Phase 2: Sonnet (앙상블 다회 실행 비용 최적화)
    foresight_predict_effort: str = "low"      # Phase 2: 개별 실행 경량화, 앙상블로 품질 확보
    foresight_sonnet_agents: int = 4         # Sonnet 에이전트 수 (깊은 추론 + Contrarian 승격)
    foresight_haiku_agents: int = 4          # Haiku 에이전트 수 (넓은 탐색, 프레이밍 변형)
    foresight_ensemble_max_turns: int = 6    # 에이전트당 최대 턴 (검색+추론)
    foresight_haiku_max_turns: int = 4       # Haiku는 빠르게 결론 (비용 절감)
    foresight_supervisor_threshold: float = 0.2  # 스프레드 > 이 값이면 Supervisor 호출
    foresight_session_ttl_minutes: int = 30   # 비활성 세션 자동 정리
    foresight_max_storage_per_user_mb: int = 200  # 1인당 환경 저장 한도 (Railway Pro 기준)

    # ── Dandelion (Imagination) ──
    dandelion_imaginer_model: str = "haiku"     # 테마 세션 모델 (Haiku = 빠름, Sonnet = 깊이)
    dandelion_seeds_per_theme: int = 10         # 테마당 상상 개수
    dandelion_session_timeout: int = 180        # 테마 세션 타임아웃 (리서치+상상, 초)
    dandelion_max_concurrency: int = 2          # 동시 테마 세션 수
    dandelion_max_turns: int = 8                # 세션당 최대 턴 수

    # ── Roundtable ──
    roundtable_model: str = "sonnet"
    roundtable_moderator_model: str = "sonnet"
    roundtable_synthesis_model: str = "sonnet"
    roundtable_effort: str = "medium"
    roundtable_moderator_effort: str = "medium"
    roundtable_synthesis_effort: str = "high"

    roundtable_min_rounds: int = 4              # 오프닝=round0, 실질 토론 최소 3라운드 보장
    roundtable_max_rounds: int = 5
    roundtable_consensus_threshold: float = 0.85  # 너무 쉽게 합의 종료하지 않도록 상향
    roundtable_min_participants: int = 3
    roundtable_max_participants: int = 4

    roundtable_speak_timeout: int = 90
    roundtable_moderator_timeout: int = 60
    roundtable_opening_timeout: int = 120
    roundtable_synthesis_timeout: int = 300
    roundtable_total_timeout: int = 2700

    roundtable_speak_max_turns: int = 3
    roundtable_moderator_max_turns: int = 3
    roundtable_synthesis_max_turns: int = 5

    # ── Worker-integrated mode timeouts ──
    roundtable_research_speak_timeout: int = 180
    workshop_worker_timeout: int = 180
    relay_worker_timeout: int = 180
    adversarial_opening_worker_timeout: int = 180

    # ── Adversarial ──
    adversarial_model: str = "sonnet"
    adversarial_effort: str = "medium"
    adversarial_debate_rounds: int = 3
    adversarial_min_per_side: int = 1
    adversarial_max_per_side: int = 2
    adversarial_speak_timeout: int = 90
    adversarial_opening_timeout: int = 120
    adversarial_closing_timeout: int = 120
    adversarial_judge_timeout: int = 300
    adversarial_judge_model: str = "sonnet"
    adversarial_judge_effort: str = "high"
    adversarial_total_timeout: int = 1500
    adversarial_speak_max_turns: int = 3
    adversarial_judge_max_turns: int = 5

    # ── Workshop ──
    workshop_drafter_model: str = "sonnet"
    workshop_reviewer_model: str = "sonnet"
    workshop_finalizer_model: str = "sonnet"
    workshop_effort: str = "medium"
    workshop_drafter_effort: str = "medium"
    workshop_reviewer_effort: str = "medium"
    workshop_finalizer_effort: str = "medium"
    workshop_min_iterations: int = 1
    workshop_max_iterations: int = 3
    workshop_pass_threshold: float = 0.8
    workshop_max_reviewers: int = 2
    workshop_draft_timeout: int = 600
    workshop_review_timeout: int = 300
    workshop_revise_timeout: int = 600
    workshop_finalize_timeout: int = 300
    workshop_total_timeout: int = 1800
    workshop_draft_max_turns: int = 15
    workshop_review_max_turns: int = 3
    workshop_revise_max_turns: int = 10
    workshop_finalize_max_turns: int = 8

    # ── Relay ──
    relay_model: str = "sonnet"
    relay_synthesis_model: str = "sonnet"
    relay_effort: str = "medium"
    relay_synthesis_effort: str = "high"
    relay_max_stages: int = 5
    relay_stage_retry: int = 1
    relay_stage_timeout: int = 600
    relay_handoff_timeout: int = 60
    relay_synthesis_timeout: int = 300
    relay_total_timeout: int = 2400
    relay_stage_max_turns: int = 20
    relay_handoff_max_turns: int = 3
    relay_synthesis_max_turns: int = 5

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
