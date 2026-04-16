'use strict';

/* Sidebar "running" indicator signal — see mode-chatbot.js for receiver. */
function _lawSignalRunning(on) {
  try {
    if (window.chatbotSignal) window.chatbotSignal('law', on);
  } catch (_) { /* noop */ }
}

/* ═══════════════════════════════════════════════════
   §  LAW MANAGER — AI 법령 tab (law.go.kr backed)
   ═══════════════════════════════════════════════════ */
class LawManager {
  constructor() {
    this.ws = null;
    this._chat = null;
    this._input = null;
    this._sendBtn = null;
    this._statusEl = null;
    this._currentAssistantEl = null;
    this._streamText = '';
    this._isStreaming = false;
    this._effortMode = 'flash';   // 'flash' | 'think'
    this._reconnectAttempts = 0;
    this._reconnectTimer = null;
    this._toolStatusEl = null;
  }

  static mountInShell(container) {
    if (!LawManager._instance) {
      LawManager._instance = new LawManager();
      LawManager._injectStyles();
    }
    LawManager._instance._mount(container);
  }

  static _injectStyles() {
    if (document.getElementById('law-mode-styles')) return;
    const css = `
      .law-shell {
        display: flex; flex-direction: column; height: 100%; min-height: 0;
        background: var(--bg); color: var(--text); font-family: inherit;
      }
      .law-toolbar {
        display: flex; align-items: center; gap: 10px;
        padding: 10px 16px; border-bottom: 1px solid var(--border);
        background: var(--surface); flex-wrap: wrap;
      }
      .law-title {
        font-size: 0.95em; font-weight: 700; color: var(--text);
        display: flex; align-items: center; gap: 6px;
      }
      .law-title .law-title-dim { color: var(--dim); font-weight: 500; font-size: 0.85em; }
      .law-title-hint {
        font-size: 0.78em; font-weight: 500; color: var(--dim);
        padding: 3px 8px; border-radius: 6px;
        background: rgba(250, 204, 21, 0.08);
        border: 1px solid rgba(250, 204, 21, 0.25);
        line-height: 1.35;
      }
      .law-mode-toggle {
        display: flex; background: var(--overlay-2); border-radius: 8px;
        padding: 2px; height: 28px;
      }
      .law-mode-toggle button {
        border: none; background: transparent; color: var(--dim);
        font-size: 0.75em; font-weight: 600; padding: 3px 12px;
        border-radius: 6px; cursor: pointer; font-family: inherit;
        line-height: 1; transition: all 0.15s;
      }
      .law-mode-toggle button.active {
        background: var(--surface); color: var(--text);
        box-shadow: 0 1px 3px rgba(0,0,0,0.25);
      }
      .law-status {
        font-size: 11px; color: var(--dim); margin-left: auto;
      }
      .law-body {
        flex: 1; min-height: 0; display: flex; overflow: hidden;
      }
      .law-chat {
        flex: 1; min-height: 0; min-width: 0; overflow-y: auto;
        padding: 16px 20px; display: flex; flex-direction: column; gap: 12px;
        scrollbar-width: thin; scrollbar-color: var(--overlay-3) transparent;
      }
      .law-chat::-webkit-scrollbar { width: 6px; }
      .law-chat::-webkit-scrollbar-thumb { background: var(--overlay-3); border-radius: 3px; }
      /* ── Chat messages ── */
      .law-msg {
        padding: 10px 14px; border-radius: 14px;
        font-size: 0.92em; line-height: 1.7;
        word-wrap: break-word; white-space: pre-wrap;
      }
      .law-msg-user {
        align-self: flex-end; max-width: 80%;
        background: linear-gradient(135deg, rgba(96,165,250,0.18), rgba(96,165,250,0.08));
        border: 1px solid rgba(96,165,250,0.3); color: var(--text);
      }
      /* AI response becomes a document block — no bubble card, full width */
      .law-msg-ai {
        align-self: stretch; max-width: 100%;
        background: transparent; border: none; color: var(--text);
        padding: 6px 6px 14px;
        white-space: normal;
        letter-spacing: -0.002em;
        animation: law-fade-in 0.28s cubic-bezier(0.2, 0.7, 0.3, 1);
      }
      @keyframes law-fade-in {
        from { opacity: 0; transform: translateY(4px); }
        to   { opacity: 1; transform: translateY(0); }
      }

      /* ── Typography hierarchy ── */
      .law-msg-ai p { margin: 8px 0; }
      .law-msg-ai p:first-child { margin-top: 0; }
      .law-msg-ai p:last-child  { margin-bottom: 0; }

      .law-msg-ai h1,
      .law-msg-ai h2,
      .law-msg-ai h3,
      .law-msg-ai h4 {
        color: var(--text);
        letter-spacing: -0.012em;
        font-weight: 700;
      }
      .law-msg-ai h1 {
        font-size: 1.35em; margin: 18px 0 10px;
        padding-bottom: 8px;
        border-bottom: 2px solid var(--brand);
      }
      .law-msg-ai h2 {
        font-size: 1.12em; margin: 18px 0 8px;
        padding: 2px 0 2px 12px;
        border-left: 3px solid var(--brand);
      }
      .law-msg-ai h3 {
        font-size: 1em; margin: 14px 0 6px;
        color: var(--text);
      }
      .law-msg-ai h4 {
        font-size: 0.92em; margin: 10px 0 4px;
        color: var(--dim);
      }
      .law-msg-ai h1:first-child,
      .law-msg-ai h2:first-child,
      .law-msg-ai h3:first-child { margin-top: 2px; }

      /* ── Blockquote: default (narrative) ── */
      .law-msg-ai blockquote {
        border-left: 3px solid var(--overlay-4);
        padding: 6px 14px; margin: 10px 0;
        background: rgba(0, 0, 0, 0.02);
        color: var(--text);
        border-radius: 0 6px 6px 0;
      }
      .law-msg-ai blockquote p:first-child { margin-top: 0; }
      .law-msg-ai blockquote p:last-child  { margin-bottom: 0; }

      /* ── Blockquote: legal citation ([인용] ...) ── */
      .law-msg-ai blockquote.law-cite-block {
        position: relative;
        margin: 14px 0 14px 18px;
        padding: 14px 18px 14px 22px;
        border-left: 4px solid var(--brand);
        background: linear-gradient(135deg,
          rgba(245, 184, 0, 0.08),
          rgba(245, 184, 0, 0.02));
        border-radius: 2px 12px 12px 2px;
        font-family: ui-serif, 'Noto Serif KR', 'Source Serif Pro', Georgia, serif;
        font-size: 0.98em;
        line-height: 1.78;
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04),
                    0 0 0 1px rgba(245, 184, 0, 0.1) inset;
      }
      .law-msg-ai blockquote.law-cite-block::before {
        content: "§";
        position: absolute;
        left: -18px;
        top: 10px;
        width: 30px; height: 30px;
        border-radius: 50%;
        background: var(--brand); color: #1a1200;
        display: flex; align-items: center; justify-content: center;
        font-weight: 900; font-size: 15px;
        font-family: Georgia, serif;
        box-shadow: 0 2px 6px rgba(245, 184, 0, 0.3);
      }
      .law-msg-ai blockquote.law-cite-block p:first-child {
        font-family: inherit; /* narrative font for the header line */
        font-weight: 700;
        font-size: 0.88em;
        color: var(--dim);
        margin-bottom: 6px;
        letter-spacing: 0.01em;
        text-transform: none;
      }

      /* ── Blockquote: disclaimer (⚠️ ... not legal advice) ── */
      .law-msg-ai blockquote.law-disclaimer-block {
        border-left: 3px solid #f59e0b;
        background: rgba(245, 158, 11, 0.07);
        padding: 10px 14px;
        margin: 18px 0 4px;
        border-radius: 0 8px 8px 0;
        color: var(--text);
        font-size: 0.86em;
        opacity: 0.94;
      }
      .law-msg-ai blockquote.law-disclaimer-block p { margin: 2px 0; }

      /* ── Lists ── */
      .law-msg-ai ul, .law-msg-ai ol {
        margin: 8px 0; padding-left: 24px;
      }
      .law-msg-ai ul li, .law-msg-ai ol li {
        margin: 4px 0; padding-left: 3px;
      }
      .law-msg-ai ul li::marker { color: var(--brand); }
      .law-msg-ai ol li::marker { color: var(--brand); font-weight: 700; }

      /* ── Inline code & blocks ── */
      .law-msg-ai code {
        background: var(--overlay-3);
        padding: 1px 6px; border-radius: 4px;
        font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, monospace;
        font-size: 0.86em; color: var(--text);
      }
      .law-msg-ai pre {
        background: var(--overlay-2);
        border: 1px solid var(--overlay-3);
        border-radius: 8px; padding: 12px 14px; overflow-x: auto;
        margin: 10px 0; font-size: 0.85em;
      }
      .law-msg-ai pre code { background: none; padding: 0; color: var(--text); }

      /* ── Links ── */
      .law-msg-ai a {
        color: var(--blue); text-decoration: none;
        border-bottom: 1px dashed rgba(96, 165, 250, 0.4);
        padding-bottom: 1px;
        transition: all 0.15s ease;
        word-break: break-all;
      }
      .law-msg-ai a:hover {
        border-bottom-color: var(--blue);
        border-bottom-style: solid;
        background: rgba(96, 165, 250, 0.06);
      }
      .law-msg-ai a.law-link-ext::after {
        content: " ↗";
        font-size: 0.82em;
        opacity: 0.75;
        margin-left: 1px;
      }

      /* ── Tables — polished ── */
      .law-msg-ai table {
        border-collapse: separate;
        border-spacing: 0;
        width: 100%; margin: 12px 0;
        background: var(--surface);
        border: 1px solid var(--overlay-3);
        border-radius: 10px;
        overflow: hidden;
        font-size: 0.9em;
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.03);
      }
      .law-msg-ai th {
        background: var(--overlay-2);
        font-weight: 700;
        padding: 11px 14px;
        text-align: left;
        vertical-align: top;
        border-bottom: 2px solid var(--overlay-3);
        color: var(--text);
        letter-spacing: -0.005em;
      }
      .law-msg-ai td {
        padding: 10px 14px;
        border-bottom: 1px solid var(--overlay-2);
        vertical-align: top;
        color: var(--text); line-height: 1.6;
      }
      .law-msg-ai tr:last-child td { border-bottom: none; }
      .law-msg-ai tbody tr:hover td {
        background: rgba(245, 184, 0, 0.035);
      }

      /* ── HR ── */
      .law-msg-ai hr {
        border: none;
        height: 1px;
        background: linear-gradient(to right,
          transparent, var(--overlay-3), transparent);
        margin: 18px 0;
      }

      /* ── Emphasis ── */
      .law-msg-ai strong { color: var(--text); font-weight: 700; }
      .law-msg-ai em { color: var(--dim); font-style: italic; }
      .law-msg-sys {
        align-self: center; color: var(--dim); font-size: 0.78em;
        font-style: italic; max-width: 100%;
      }
      .law-typing {
        align-self: flex-start; display: flex; align-items: center; gap: 8px;
        color: var(--dim); font-size: 0.82em; padding: 6px 10px;
      }
      .law-typing .law-typing-dots {
        display: inline-flex; gap: 3px;
      }
      .law-typing .law-typing-dots span {
        width: 5px; height: 5px; border-radius: 50%;
        background: var(--dim); animation: law-bounce 1.2s infinite ease-in-out;
      }
      .law-typing .law-typing-dots span:nth-child(2) { animation-delay: 0.15s; }
      .law-typing .law-typing-dots span:nth-child(3) { animation-delay: 0.3s; }
      @keyframes law-bounce {
        0%, 60%, 100% { transform: translateY(0); opacity: 0.5; }
        30% { transform: translateY(-4px); opacity: 1; }
      }
      .law-tool-label {
        color: var(--brand); font-size: 11px; font-weight: 600;
      }
      .law-input-bar {
        display: flex; align-items: flex-end; gap: 8px;
        padding: 12px 16px; border-top: 1px solid var(--border);
        background: var(--surface);
      }
      .law-input-bar textarea {
        flex: 1; padding: 10px 14px; background: var(--overlay-2);
        color: var(--text); border: 1px solid var(--overlay-3);
        border-radius: 12px; font-family: inherit; font-size: 0.9em;
        resize: none; min-height: 20px; max-height: 140px;
        line-height: 1.5; outline: none;
      }
      .law-input-bar textarea:focus { border-color: var(--brand); }
      .law-input-bar button {
        padding: 10px 18px; border: none; border-radius: 12px;
        background: var(--brand); color: #000; font-weight: 700;
        cursor: pointer; font-family: inherit; font-size: 0.85em;
      }
      .law-input-bar button:disabled { opacity: 0.5; cursor: not-allowed; }
    `;
    const style = document.createElement('style');
    style.id = 'law-mode-styles';
    style.textContent = css;
    document.head.appendChild(style);
  }

  /** Safely render markdown string into a target element without using innerHTML. */
  static _renderMarkdown(target, markdownText) {
    while (target.firstChild) target.removeChild(target.firstChild);
    if (typeof marked === 'undefined' || !markdownText) {
      target.textContent = markdownText || '';
      return;
    }
    let html = '';
    try { html = marked.parse(markdownText); } catch { target.textContent = markdownText; return; }
    const parsed = new DOMParser().parseFromString(html, 'text/html');
    Array.from(parsed.body.childNodes).forEach((node) => {
      target.appendChild(document.importNode(node, true));
    });
    LawManager._enhanceRenderedMarkdown(target);
  }

  /** Post-render enhancements: classify blockquotes, decorate links. */
  static _enhanceRenderedMarkdown(root) {
    // 1. Classify blockquotes based on leading content so the CSS
    //    variants (.law-cite-block, .law-disclaimer-block) kick in.
    root.querySelectorAll('blockquote').forEach((bq) => {
      const text = (bq.textContent || '').trim();
      if (!text) return;
      if (text.startsWith('[인용]') || /^\s*\[\s*인용\s*\]/.test(text)) {
        bq.classList.add('law-cite-block');
      } else if (
        text.startsWith('⚠️') ||
        text.includes('법률 자문이 아닙니다') ||
        text.includes('변호사와 상담')
      ) {
        bq.classList.add('law-disclaimer-block');
      }
    });

    // 2. Decorate links: open in new tab + mark external http(s) links
    //    so the ↗ indicator shows up via CSS.
    root.querySelectorAll('a[href]').forEach((anchor) => {
      anchor.setAttribute('target', '_blank');
      anchor.setAttribute('rel', 'noopener noreferrer');
      const href = anchor.getAttribute('href') || '';
      if (/^https?:\/\//i.test(href)) {
        anchor.classList.add('law-link-ext');
      }
    });
  }

  _mount(container) {
    while (container.firstChild) container.removeChild(container.firstChild);

    const shell = document.createElement('div');
    shell.className = 'law-shell';

    /* Toolbar */
    const toolbar = document.createElement('div');
    toolbar.className = 'law-toolbar';

    const title = document.createElement('div');
    title.className = 'law-title';
    title.appendChild(document.createTextNode('⚖️ 법령상담 '));
    const titleDim = document.createElement('span');
    titleDim.className = 'law-title-dim';
    titleDim.textContent = '— law.go.kr 원문 기반';
    title.appendChild(titleDim);
    toolbar.appendChild(title);

    const hint = document.createElement('span');
    hint.className = 'law-title-hint';
    hint.textContent = '💡 빅데이터를 읽어오는 기능이므로 AI가 집중할 수 있도록 다른 모드와 함께 사용하기보단 단독 사용을 권장합니다.';
    toolbar.appendChild(hint);

    const effortToggle = this._buildToggle([
      ['flash', '⚡ Flash'],
      ['think', '💡 Think'],
    ], this._effortMode, (value) => {
      this._effortMode = value;
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ type: 'law_set_mode', data: { mode: value } }));
      }
    });
    toolbar.appendChild(effortToggle);

    const status = document.createElement('span');
    status.className = 'law-status';
    status.textContent = 'Connecting...';
    toolbar.appendChild(status);
    this._statusEl = status;

    shell.appendChild(toolbar);

    /* Body: chat only — cited articles are now shown inline via the
       .law-cite-block blockquote styling inside the AI message. */
    const body = document.createElement('div');
    body.className = 'law-body';

    const chat = document.createElement('div');
    chat.className = 'law-chat';
    body.appendChild(chat);
    this._chat = chat;

    shell.appendChild(body);

    /* Input bar */
    const inputBar = document.createElement('div');
    inputBar.className = 'law-input-bar';
    const textarea = document.createElement('textarea');
    textarea.rows = 1;
    textarea.placeholder = '법령명·조문을 입력하거나, 상황을 자연어로 설명하세요…';
    inputBar.appendChild(textarea);
    const sendBtn = document.createElement('button');
    sendBtn.textContent = '전송';
    inputBar.appendChild(sendBtn);
    shell.appendChild(inputBar);

    this._input = textarea;
    this._sendBtn = sendBtn;

    container.appendChild(shell);

    /* Events */
    sendBtn.addEventListener('click', () => this._sendMessage());
    textarea.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        this._sendMessage();
      }
    });
    textarea.addEventListener('input', () => {
      textarea.style.height = 'auto';
      textarea.style.height = Math.min(textarea.scrollHeight, 140) + 'px';
    });

    this._connect();
  }

  _buildToggle(options, current, onChange) {
    const wrap = document.createElement('div');
    wrap.className = 'law-mode-toggle';
    options.forEach(([value, label]) => {
      const btn = document.createElement('button');
      btn.textContent = label;
      btn.dataset.value = value;
      if (value === current) btn.classList.add('active');
      btn.addEventListener('click', () => {
        wrap.querySelectorAll('button').forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
        onChange(value);
      });
      wrap.appendChild(btn);
    });
    return wrap;
  }

  _connect() {
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    let url = `${proto}://${location.host}/ws/law`;
    try {
      const token = (window.Auth && Auth.token) || localStorage.getItem('auth-token');
      if (token) url += `?token=${encodeURIComponent(token)}`;
    } catch {}
    this.ws = new WebSocket(url);
    this._statusEl.textContent = 'Connecting...';
    this.ws.onopen = () => {
      this._statusEl.textContent = 'Connected';
      this._reconnectAttempts = 0;
    };
    this.ws.onmessage = (e) => {
      try { this._handle(JSON.parse(e.data)); } catch {}
    };
    this.ws.onclose = () => {
      this._statusEl.textContent = 'Disconnected';
      _lawSignalRunning(false);
      this._scheduleReconnect();
    };
    this.ws.onerror = () => { this._statusEl.textContent = 'Error'; };
  }

  _scheduleReconnect() {
    if (this._reconnectAttempts >= 8) return;
    const delay = Math.min(2000 * Math.pow(1.5, this._reconnectAttempts), 20000);
    this._reconnectAttempts += 1;
    this._statusEl.textContent = `재연결 중... (${this._reconnectAttempts}/8)`;
    this._reconnectTimer = setTimeout(() => this._connect(), delay);
  }

  _handle(msg) {
    switch (msg.type) {
      case 'law_init':
        this._statusEl.textContent = 'Ready';
        this._addSystemMsg(
          msg.data?.has_key
            ? '✓ law.go.kr Open API 연결 완료. 키워드 또는 상황을 입력하세요.'
            : '⚠️ 관리자가 LAW_OC(국가법령정보 OC키)를 설정해야 조회가 가능합니다.'
        );
        break;
      case 'law_stream':
        this._handleStream(msg.data || {});
        break;
      case 'law_tool_status':
        this._handleToolStatus(msg.data || {});
        break;
      case 'law_citation':
        // Sidebar removed — inline [인용] blocks cover this now. The
        // backend still pushes these messages; we silently drop them.
        break;
      case 'law_error':
        this._addSystemMsg('⚠️ ' + (msg.data?.message || '오류'));
        _lawSignalRunning(false);
        this._setInputEnabled(true);
        break;
      case 'heartbeat':
        break;
    }
  }

  _handleStream(data) {
    if (!this._currentAssistantEl) {
      const typing = this._chat.querySelector('.law-typing');
      if (typing) typing.remove();
      const el = document.createElement('div');
      el.className = 'law-msg law-msg-ai law-msg-streaming';
      this._chat.appendChild(el);
      this._currentAssistantEl = el;
      this._streamText = '';
      this._isStreaming = true;
      this._lastRenderAt = 0;
    }
    if (data.done) {
      if (this._currentAssistantEl && this._streamText) {
        LawManager._renderMarkdown(this._currentAssistantEl, this._streamText);
      }
      if (this._currentAssistantEl) {
        this._currentAssistantEl.classList.remove('law-msg-streaming');
      }
      this._currentAssistantEl = null;
      this._streamText = '';
      this._isStreaming = false;
      // Subprocess 종료 시점 — 답변 이후에 LLM 이 추가 tool_use 를 날려서
      // 남은 .law-typing 인디케이터들을 모두 제거. 그 툴들은 이미 백엔드에서
      // 실행 완료됐으며 UI 에 인디케이터만 stale 로 남아있는 상태.
      this._chat.querySelectorAll('.law-typing').forEach((el) => el.remove());
      _lawSignalRunning(false);
      this._setInputEnabled(true);
      this._input.focus();
    } else if (data.token) {
      this._streamText += data.token;
      // Progressive markdown rendering — re-parse the accumulating buffer
      // so headings, blockquotes (인용), and tables formalize as soon as
      // their boundaries arrive. Throttle to ~30 ms to avoid thrashing
      // the DOM when deltas burst in.
      const now = performance.now();
      if (now - (this._lastRenderAt || 0) > 30) {
        LawManager._renderMarkdown(this._currentAssistantEl, this._streamText);
        this._lastRenderAt = now;
      }
      this._chat.scrollTop = this._chat.scrollHeight;
    }
  }

  _handleToolStatus(data) {
    let typing = this._chat.querySelector('.law-typing');
    if (!typing) {
      typing = document.createElement('div');
      typing.className = 'law-typing';
      const label = document.createElement('span');
      label.className = 'law-tool-label';
      label.textContent = data.status || '법령 조회 중…';
      typing.appendChild(label);
      const dots = document.createElement('span');
      dots.className = 'law-typing-dots';
      for (let i = 0; i < 3; i++) dots.appendChild(document.createElement('span'));
      typing.appendChild(dots);
      this._chat.appendChild(typing);
      this._toolStatusEl = label;
    } else if (this._toolStatusEl) {
      this._toolStatusEl.textContent = data.status || '법령 조회 중…';
    }
    this._chat.scrollTop = this._chat.scrollHeight;
  }

  _sendMessage() {
    const text = (this._input.value || '').trim();
    if (!text || this._isStreaming) return;
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      this._addSystemMsg('서버 연결이 끊어졌습니다. 잠시 후 다시 시도하세요.');
      return;
    }

    const userEl = document.createElement('div');
    userEl.className = 'law-msg law-msg-user';
    userEl.textContent = text;
    this._chat.appendChild(userEl);
    this._chat.scrollTop = this._chat.scrollHeight;

    this.ws.send(JSON.stringify({
      type: 'law_message',
      data: {
        content: text,
        effort: this._effortMode,
      },
    }));

    this._input.value = '';
    this._input.style.height = 'auto';
    this._setInputEnabled(false);
    _lawSignalRunning(true);
  }

  _addSystemMsg(text) {
    const el = document.createElement('div');
    el.className = 'law-msg law-msg-sys';
    el.textContent = text;
    this._chat.appendChild(el);
    this._chat.scrollTop = this._chat.scrollHeight;
  }

  _setInputEnabled(enabled) {
    this._input.disabled = !enabled;
    this._sendBtn.disabled = !enabled;
  }
}

LawManager._instance = null;
window.LawManager = LawManager;
