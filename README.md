# Enterprise Agent System

> Claude Code 기반 멀티모드 AI 워크스페이스 — 브라우저 하나로 리서치·토론·개발·법령·데이터분석까지

로컬 PC에서 돌아가는 웹 앱입니다. `start.command`를 더블클릭하면 서버가 뜨고 크롬이 자동으로 열립니다. 터미널 명령을 외울 필요 없이 아이콘 하나로 시작·종료할 수 있도록 설계되어 있습니다.

---

## 🚀 가장 빠른 시작 (macOS, 5분)

> 이미 Python 3.11+, Node.js 18+, Claude Code CLI가 설치되어 있다면 이 블록만 따라 하면 됩니다.

```bash
# 1) 저장소 받기
cd ~/Desktop
git clone https://github.com/JunyoungOh/AI_Team.git Langgraph
cd Langgraph

# 2) 파이썬 의존성 설치 (웹 UI 포함)
pip3 install -e ".[ui]"

# 3) 환경변수 파일 생성 — 아래 [🔑 환경변수] 섹션의 템플릿을 복사해서
#    프로젝트 루트에 ".env" 파일로 저장합니다 (값은 빈 채로 둬도 OK)
#    Mac 터미널에서 빠르게:  nano .env   ← 붙여넣고 Ctrl+O → Enter → Ctrl+X

# 4) start.command 에 실행 권한 부여 (★ 필수, 새 PC에서 자주 누락됨)
chmod +x start.command

# 5) Finder 에서 start.command 더블클릭
open .
```

더블클릭 후 터미널이 열리면서 아래 흐름이 자동으로 진행됩니다.

```
✓ .env 환경변수 로드 완료
✓ Claude Code CLI: 1.x.x
✓ Python: Python 3.x.x
🚀 서버 시작 중... (포트 8000)
⏳ 서버 준비 대기 중...
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ✅ 서버 실행 중: http://localhost:8000
  📋 종료하려면 Ctrl+C 또는 이 창을 닫으세요
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

Chrome 창이 자동으로 열리면서 웹 UI가 뜨면 준비 완료입니다.

---

## 📋 사전 준비

| 항목 | 버전 | 확인 명령어 | 설치 안내 |
|------|------|-------------|-----------|
| Python | 3.11+ | `python3 --version` | [python.org](https://www.python.org/downloads/) |
| Node.js | 18+ | `node --version` | [nodejs.org](https://nodejs.org) (LTS) |
| Claude Code CLI | 최신 | `claude --version` | `npm install -g @anthropic-ai/claude-code` |
| Git | any | `git --version` | macOS: `xcode-select --install` |

Claude Code CLI는 최초 1회 `claude` 명령으로 로그인해야 합니다 (`/exit` 로 종료). 이 앱은 Claude Code의 로그인 세션을 그대로 재사용하므로 Anthropic API 키를 별도로 만들 필요가 없습니다.

---

## 🔑 환경변수 설정 (`.env` 만들기)

> **요약**: 이 앱은 Claude Code CLI 로그인 세션을 그대로 쓰기 때문에 **Anthropic API 키는 필요 없습니다**. 아래 3개 키는 **모두 선택 사항**이며, 특정 모드를 켤 때만 필요합니다. 하나도 없이 `start.command`를 실행해도 AI 회사·모의토론·비서·엔지니어링·데이터랩·미래상상 등 핵심 모드는 정상 동작합니다.

### 1단계: `.env` 파일 만들기

프로젝트 폴더(저장소 루트) 안에 `.env` 라는 텍스트 파일을 만듭니다. 파일 이름은 **반드시 점(.)으로 시작**해야 합니다.

```bash
cd ~/Desktop/Langgraph
nano .env          # ← 아래 2단계 템플릿을 붙여넣기
# 저장: Ctrl + O → Enter,  종료: Ctrl + X
```

> Windows는 메모장에서 **"모든 파일(*.*)"** 로 저장하고 파일명을 `.env` 로 지정하세요 (확장자 `.txt` 가 붙지 않도록 주의).

### 2단계: 아래 템플릿을 `.env` 에 복사-붙여넣기

```bash
# ─────────────────────────────────────────────
#  Enterprise Agent System — 환경변수
#  모두 선택 사항. 쓰지 않는 키는 빈 채로 두면 됩니다.
# ─────────────────────────────────────────────

# 법령상담 모드(AI Law) 에서만 사용
LAW_OC=

# 리서치 계열 모드에서 고품질 웹 스크래핑
FIRECRAWL_API_KEY=

# DART 재무제표 조회 모드(AI DART) + 기업 재무 분석 도구
DART_API_KEY=
```

### 3단계: 필요한 키만 아래 가이드대로 발급받아 채우기

---

#### 🟦 `LAW_OC` — 법령상담 모드 전용

| | |
|---|---|
| **어떤 기능에?** | **법령상담 모드(AI Law)** 에서 국가법령정보센터의 법·조문·판례를 인용하기 위해 필요 |
| **무엇인가?** | 국가법령정보(law.go.kr) OPEN API 의 인증자(OC). 신청자의 이메일 앞부분을 그대로 OC 값으로 사용합니다 (예: `hong@gmail.com` → `hong`) |
| **발급처** | https://open.law.go.kr/LSO/openApi/guideList.do → "OPEN API 활용신청" |
| **비용** | **무료** |
| **발급 소요** | 담당자 수동 검토로 보통 **2~5일** |
| **없으면?** | 법령상담 모드에서 *"관리자가 LAW_OC를 설정해야 조회가 가능합니다"* 안내가 뜹니다. **다른 모드는 모두 정상 동작.** 당장 쓰지 않을 거면 빈 채로 둬도 됩니다. |

---

#### 🟧 `FIRECRAWL_API_KEY` — 웹 스크래핑 품질 향상 (범용)

| | |
|---|---|
| **어떤 기능에?** | AI 회사·비서·미래상상 등 **웹 페이지를 읽어오는 모든 리서치 도구**. JS 렌더링 페이지, SPA, PDF, 동적 콘텐츠를 깔끔한 마크다운으로 변환 |
| **무엇인가?** | Firecrawl 상용 스크래퍼의 API 키 |
| **발급처** | https://firecrawl.dev → Google 계정으로 회원가입 → Dashboard → **API Keys** 복사 |
| **비용** | **무료 500회/월** (초과분은 유료 플랜) |
| **발급 소요** | 즉시 (회원가입 후 바로 발급) |
| **없으면?** | 일반 HTTP fetch로 **자동 폴백** — 동작은 하지만 SPA·로그인 뒤 콘텐츠·복잡한 레이아웃은 품질이 떨어질 수 있습니다. 간단한 뉴스/블로그 스크래핑은 키 없이도 충분합니다. |

---

#### 🟩 `DART_API_KEY` — 기업 재무 조회 모드 전용

| | |
|---|---|
| **어떤 기능에?** | **AI DART 재무제표 조회 모드** + 리서치 계열에서 상장사 재무 데이터를 가져올 때 |
| **무엇인가?** | 금융감독원 전자공시시스템(DART)의 Open DART 인증키 |
| **발급처** | https://opendart.fss.or.kr → 회원가입 → "인증키 신청/관리" → 이메일 인증 |
| **비용** | **무료** (일 20,000회 호출 제한) |
| **발급 소요** | **승인 검토 대기** — 신청 후 수 시간 ~ 수일이 걸릴 수 있으니 미리 신청해 두세요 |
| **없으면?** | DART 모드에서 *"관리자가 DART_API_KEY를 설정해야 조회가 가능합니다"* 안내가 뜹니다. 다른 모드는 정상 — 리서치에서 재무 데이터가 필요하면 Claude Code 내장 웹 검색으로 대체됩니다. |

---

> 💡 **키를 나중에 추가/변경했다면 `start.command`를 재시작해야 적용됩니다.** 서버는 기동 시점에만 `.env`를 읽습니다.
>
> 🔒 `.env`는 `.gitignore`에 등록되어 있어 GitHub에 올라가지 않습니다. 파일명을 반드시 `.env`로 유지하세요 — `env.txt`, `settings.env` 같은 변형은 자동 로드되지 않습니다.

---

## 🛠 문제 해결 (자주 발생하는 3가지)

### ① "권한 없음 / Permission denied"

```bash
chmod +x start.command
```

새 PC에 저장소를 클론하거나 ZIP으로 받았을 때 가장 흔한 증상입니다. 위 한 줄이면 해결됩니다.

### ② macOS Gatekeeper — "확인되지 않은 개발자"

Finder에서 `start.command`를 더블클릭했을 때 아래 같은 경고가 뜬다면:

> `"start.command"은(는) 확인되지 않은 개발자가 배포했기 때문에 열 수 없습니다.`

**해결법 3가지 중 하나**를 사용하세요.

1. **우클릭 → 열기** (가장 간단)
   - Finder에서 `start.command` 우클릭 → `열기` → 경고창에서 한 번 더 `열기` 클릭.
   - 최초 1회만 승인하면 이후 더블클릭으로 바로 실행됩니다.

2. **quarantine 속성 제거** (터미널)
   ```bash
   xattr -d com.apple.quarantine start.command
   ```

3. **시스템 설정**에서 허용
   - `시스템 설정` → `개인정보 보호 및 보안` → 하단의 `차단된 "start.command"를 열도록 허용`.

### ③ 포트 8000 충돌 — "Address already in use"

start.command는 포트 8000을 사용합니다. 다른 개발 서버(Django, Flask 등)와 겹치면 다음 두 가지 중 하나로 해결합니다.

**방법 A. 기존 프로세스 종료** (가장 빠름)
```bash
# 8000 포트를 누가 쓰는지 확인
lsof -i :8000

# 프로세스 종료 (PID 는 위 명령 결과에서 확인)
kill -9 <PID>
```

**방법 B. 포트 바꾸기** — `start.command` 상단의 `PORT=8000`을 원하는 번호로 수정합니다. 예: `PORT=8765`.

```bash
# 텍스트 에디터로 직접 편집하거나 한 줄 sed 로:
sed -i '' 's/^PORT=8000/PORT=8765/' start.command
```

> 💡 **start.command가 이미 떠 있는 걸 감지하면** 새로 띄우지 않고 `http://localhost:8000`에 브라우저만 다시 엽니다. "이미 사용 중" 경고가 뜰 때 무조건 포트를 바꿀 필요는 없습니다 — 기존 탭이 살아 있다면 그대로 쓰면 됩니다.

---

## 🪟 Windows / Linux 사용자 안내

`start.command`는 **macOS 전용 bash 스크립트**입니다. Windows/Linux에서는 아래 명령으로 직접 실행하세요.

```bash
# .env 로드 (Linux/WSL)
export $(grep -v '^#' .env | xargs)

# 서버 기동
python3 -m uvicorn src.ui.server:app --host 0.0.0.0 --port 8000
```

브라우저를 열어 `http://localhost:8000` 으로 접속합니다. Windows PowerShell 사용자는 `.env` 로드 방식이 달라 [INSTALL_GUIDE.txt](./INSTALL_GUIDE.txt) 의 Windows 섹션을 참고하세요.

> Windows용 `start.bat` 기여는 환영합니다! 🙌

---

## 🧑‍💻 주요 모드

브라우저에서 접속하면 사이드바에 다음 모드들이 표시됩니다.

| 모드 | 용도 |
|------|------|
| **AI 회사** (인스턴트/내 방식) | CEO-Leader-Worker 다중 에이전트가 리서치·보고서 작성 |
| **AI 모의토론** | 여러 역할 에이전트가 실시간 토론 (관람형/참여형) |
| **AI 비서** | 빠른 채팅 기반 범용 어시스턴트 |
| **AI 엔지니어링** | 요구사항 → 코드 → 테스트까지 단계별 자동 개발 |
| **AI 데이터랩** | 업로드한 데이터 분석 (Zero-Retention) |
| **미래상상** (Dandelion) | 트렌드 분석·시나리오 탐색 |
| **법령상담** | 국가법령정보 기반 인용·해석 (`LAW_OC` 필요) |
| **가이드 챗봇** | 어떤 모드를 쓸지 모를 때 추천해주는 온보딩 |

---

## 📚 더 자세한 가이드

터미널이 처음인 분을 위한 **완전 초보 설치 가이드**가 별도로 준비되어 있습니다.

👉 [INSTALL_GUIDE.txt](./INSTALL_GUIDE.txt) — Python·Node.js·Claude Code 설치부터 API 키 발급까지 단계별 설명

---

## 🛑 종료 방법

- **권장**: start.command 터미널 창에서 `Ctrl + C` 한 번
- 또는 터미널 창을 그냥 닫기 (EXIT trap이 서버를 자동 정리)
- 브라우저 탭만 닫으면 서버는 계속 떠 있습니다

---

## 📄 라이선스 / 문의

- 이슈: https://github.com/JunyoungOh/AI_Team/issues
