#!/bin/bash
# ─────────────────────────────────────────────────
#  Enterprise Agent System — Local Version
#  더블클릭으로 서버 시작 + 브라우저 자동 열기
# ─────────────────────────────────────────────────

# 이 스크립트가 있는 폴더로 이동
cd "$(dirname "$0")"

PORT=8000
URL="http://localhost:$PORT"

# ── 터미널 타이틀 설정 ──
echo -ne "\033]0;🏢 Enterprise Agent (Local)\007"

clear
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━��━"
echo "  🏢 Enterprise Agent System — Local Version"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── 이미 실행 중인 서버 확인 ──
if lsof -i :$PORT -sTCP:LISTEN >/dev/null 2>&1; then
    echo "⚠️  포트 $PORT 이미 사용 중 — 기존 서버에 연결합니다."
    echo ""
    open -a "Google Chrome" "$URL"
    echo "🌐 브라우저에서 열렸습니다: $URL"
    echo ""
    echo "이 창을 닫아도 됩니다."
    read -n 1 -s -r -p "아무 키나 누르면 닫힙니다..."
    exit 0
fi

# ── .env 로드 (환경변수) ──
if [ -f .env ]; then
    export $(grep -v '^#' .env | grep -v '^\s*$' | xargs)
    echo "✓ .env 환경변수 로드 완료"
fi

# ── Claude Code CLI 확인 ──
if ! command -v claude &>/dev/null; then
    echo ""
    echo "❌ claude CLI가 설치되어 있지 않습니다."
    echo "   설치: npm install -g @anthropic-ai/claude-code"
    echo ""
    read -n 1 -s -r -p "아무 키나 누르면 닫힙니다..."
    exit 1
fi
echo "✓ Claude Code CLI: $(claude --version 2>/dev/null || echo 'OK')"

# ── Python 확인 ──
if ! command -v python3 &>/dev/null; then
    echo "❌ python3가 설치되어 있지 않습니다."
    read -n 1 -s -r -p "아무 키나 누르면 닫힙니다..."
    exit 1
fi
echo "✓ Python: $(python3 --version 2>&1)"

# ── 프로젝트 루트 설정 (MCP 서버가 .mcp.json을 찾을 수 있도록) ──
export ENTERPRISE_AGENT_ROOT="$(pwd)"

# ── 서버 시작 (백그라운드) ──
echo ""
echo "🚀 서버 시작 중... (포트 $PORT)"
echo ""

python3 -m uvicorn src.ui.server:app \
    --host 0.0.0.0 \
    --port $PORT \
    --log-level info &

SERVER_PID=$!

# ── 서버 준비될 때까지 대기 ──
echo "⏳ 서버 준비 대기 중..."
for i in $(seq 1 30); do
    if curl -s -o /dev/null "$URL/health" 2>/dev/null || curl -s -o /dev/null "$URL" 2>/dev/null; then
        break
    fi
    sleep 1
done

# ── 브라우저 열기 ──
sleep 1
open -a "Google Chrome" "$URL"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✅ 서버 실행 중: $URL"
echo "  📋 종료하려면 Ctrl+C 또는 이 창을 닫으세요"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── 종료 시 서버 프로세스 정리 ──
cleanup() {
    echo ""
    echo "🛑 서버 종료 중..."
    kill $SERVER_PID 2>/dev/null
    wait $SERVER_PID 2>/dev/null
    echo "👋 종료 완료"
}
trap cleanup EXIT INT TERM

# ── 서버 로그 출력 (포그라운드) ──
wait $SERVER_PID
