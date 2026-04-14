'use strict';

/* ═══════════════════════════════════════════════════
   §  CHATBOT WIDGET — Floating onboarding/guide chatbot
   ═══════════════════════════════════════════════════

   Lives in the sidebar above .cs-brand; expands into a fixed-position
   overlay. "Running mode" indication happens OUTSIDE this widget — we
   listen for `chatbot:mode-state` events at module level and toggle a
   `.cs-item-running` CSS class on the matching sidebar button, giving
   a subtle glow + pulse dot while that mode is executing. The widget
   itself knows nothing about running state — clean separation.

   Public API for mode JS files (unchanged):
     window.chatbotSignal(slug, running, status?)
   or equivalently:
     window.dispatchEvent(new CustomEvent('chatbot:mode-state', {
       detail: { mode: 'upgrade', running: true }
     }));

   WebSocket: /ws/chatbot  (bot_init / bot_stream / bot_message events)

   All DOM built via helper `h()` and SVG built via helper `svg()` — no
   innerHTML anywhere, no XSS surface. Minimal safe markdown renderer
   in `renderMarkdownInto()` handles headings, lists, bold, code, and
   `[slug]` mode-tag chips, using String.matchAll() for iteration.
*/

(function () {
  const MODE_LABELS = {
    builder: '내 방식', schedule: '스케줄팀',
    upgrade: '자동개발', skill: '스킬',
    discussion: '토론', foresight: '포사이트', persona: '페르소나',
    secretary: '비서', law: '법령',
  };
  const SVG_NS = 'http://www.w3.org/2000/svg';

  /* ── Typewriter tuning (easy to tweak) ── */
  const TYPEWRITER_TICK_MS = 35;   // delay between char-paint ticks
  const TYPEWRITER_CHARS_PER_TICK = 1;  // chars advanced per tick
  //  → 35ms / 1 char = ~28 chars/sec (comfortable Korean reading pace)

  /* ── DOM helpers (no innerHTML) ── */
  function h(tag, attrs, children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const k in attrs) {
        if (k === 'className') el.className = attrs[k];
        else if (k === 'dataset') Object.assign(el.dataset, attrs[k]);
        else if (k.startsWith('on') && typeof attrs[k] === 'function') {
          el.addEventListener(k.slice(2).toLowerCase(), attrs[k]);
        } else if (attrs[k] != null) {
          el.setAttribute(k, attrs[k]);
        }
      }
    }
    (children || []).forEach((c) => {
      if (c == null) return;
      el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    });
    return el;
  }
  function svg(paths, size) {
    const s = document.createElementNS(SVG_NS, 'svg');
    s.setAttribute('width', size || 18);
    s.setAttribute('height', size || 18);
    s.setAttribute('viewBox', '0 0 24 24');
    s.setAttribute('fill', 'none');
    s.setAttribute('stroke', 'currentColor');
    s.setAttribute('stroke-width', '2');
    s.setAttribute('stroke-linecap', 'round');
    s.setAttribute('stroke-linejoin', 'round');
    paths.forEach((p) => {
      const [tag, attrs] = p;
      const e = document.createElementNS(SVG_NS, tag);
      for (const k in attrs) e.setAttribute(k, attrs[k]);
      s.appendChild(e);
    });
    return s;
  }

  /* Smooth partial markdown during streaming: speculatively close unclosed
     bold/code markers, and hide trailing unclosed [mode] tags. This prevents
     visible flicker when characters like `**` are typed one-by-one. */
  function smoothPartialMarkdown(text) {
    // 1. Hide trailing unclosed mode tag: "We recommend [inst" → "We recommend "
    let out = text.replace(/\[[a-z_]*$/, '');

    // 2. Speculatively close unclosed **bold**
    const doubles = (out.match(/\*\*/g) || []).length;
    if (doubles % 2 === 1) out += '**';

    // 3. Speculatively close unclosed `inline code`
    const backticks = (out.match(/`/g) || []).length;
    if (backticks % 2 === 1) out += '`';

    return out;
  }

  /* ── Minimal safe markdown → DOM renderer (no innerHTML) ──
     Supports: headings (#, ##, ###), ul (-/*), ol (1.), blockquote (>),
     horizontal rule (---), paragraphs. Inline: **bold**, *italic*, `code`,
     [slug] mode-tag chips. Uses matchAll (no regex.exec) for safety. */
  function renderMarkdownInto(text, parent, onModeTagClick) {
    const blocks = text.split(/\n\n+/);
    blocks.forEach((block) => {
      const trimmed = block.trim();
      if (!trimmed) return;

      // Horizontal rule
      if (/^-{3,}$/.test(trimmed) || /^\*{3,}$/.test(trimmed)) {
        parent.appendChild(document.createElement('hr'));
        return;
      }

      // Heading
      const hMatch = trimmed.match(/^(#{1,3})\s+(.+)$/);
      if (hMatch) {
        const heading = document.createElement('h' + hMatch[1].length);
        renderInline(hMatch[2], heading, onModeTagClick);
        parent.appendChild(heading);
        return;
      }

      // Blockquote (> line, possibly multi-line)
      if (/^>\s?/.test(trimmed)) {
        const bq = document.createElement('blockquote');
        const inner = trimmed.split('\n')
          .map(l => l.replace(/^>\s?/, ''))
          .join(' ');
        renderInline(inner, bq, onModeTagClick);
        parent.appendChild(bq);
        return;
      }

      // Unordered list
      const firstLine = trimmed.split('\n')[0];
      if (/^[-*]\s/.test(firstLine)) {
        const ul = document.createElement('ul');
        trimmed.split('\n').forEach((line) => {
          const m = line.match(/^[-*]\s+(.+)$/);
          if (m) {
            const li = document.createElement('li');
            renderInline(m[1], li, onModeTagClick);
            ul.appendChild(li);
          }
        });
        parent.appendChild(ul);
        return;
      }

      // Ordered list
      if (/^\d+\.\s/.test(firstLine)) {
        const ol = document.createElement('ol');
        trimmed.split('\n').forEach((line) => {
          const m = line.match(/^\d+\.\s+(.+)$/);
          if (m) {
            const li = document.createElement('li');
            renderInline(m[1], li, onModeTagClick);
            ol.appendChild(li);
          }
        });
        parent.appendChild(ol);
        return;
      }

      // Paragraph (collapse single newlines to spaces)
      const p = document.createElement('p');
      renderInline(trimmed.replace(/\n/g, ' '), p, onModeTagClick);
      parent.appendChild(p);
    });
  }

  /* Tokenize inline markdown using matchAll (no regex.exec).
     Order matters: bold (**) before italic (*) so ** isn't eaten by *. */
  function renderInline(text, parent, onModeTagClick) {
    const regex = /(\*\*[^*]+\*\*|\*[^*\n]+\*|`[^`]+`|\[[a-z_]+\])/g;
    const matches = Array.from(text.matchAll(regex));
    let cursor = 0;
    matches.forEach((m) => {
      const idx = m.index;
      if (idx > cursor) parent.appendChild(document.createTextNode(text.slice(cursor, idx)));
      const tok = m[0];
      if (tok.startsWith('**')) {
        const b = document.createElement('strong');
        b.textContent = tok.slice(2, -2);
        parent.appendChild(b);
      } else if (tok.startsWith('*')) {
        const i = document.createElement('em');
        i.textContent = tok.slice(1, -1);
        parent.appendChild(i);
      } else if (tok.startsWith('`')) {
        const c = document.createElement('code');
        c.textContent = tok.slice(1, -1);
        parent.appendChild(c);
      } else {
        const slug = tok.slice(1, -1);
        if (MODE_LABELS[slug]) {
          const a = document.createElement('a');
          a.href = '#' + slug;
          a.className = 'chatbot-modetag';
          a.textContent = MODE_LABELS[slug];
          a.addEventListener('click', (ev) => {
            ev.preventDefault();
            if (onModeTagClick) onModeTagClick(slug);
          });
          parent.appendChild(a);
        } else {
          parent.appendChild(document.createTextNode(tok));
        }
      }
      cursor = idx + tok.length;
    });
    if (cursor < text.length) parent.appendChild(document.createTextNode(text.slice(cursor)));
  }

  /* ══════════════════════════════════════════════════
     Widget class
     ══════════════════════════════════════════════════ */
  class ChatbotWidget {
    constructor() {
      this.ws = null;
      this._expanded = false;
      this._isStreaming = false;
      this._isSending = false;           // guard against IME/form double-submit
      this._streamRawText = '';          // full text accumulated from server
      this._typedLength = 0;             // chars already painted by typewriter
      this._typewriterTimer = null;
      this._doneReceived = false;        // true when server sent done=true
      this._currentAssistantEl = null;
      this._typingEl = null;             // "..." dots indicator element
      this._reconnectAttempts = 0;
      this._reconnectTimer = null;
      this._collapsedEl = null;
      this._panelEl = null;
      this._messagesEl = null;
      this._inputEl = null;
      this._sendBtn = null;
      this._statusEl = null;
    }

    mount() {
      const sidebar = document.getElementById('card-sidebar');
      if (!sidebar) return;
      const brand = sidebar.querySelector('.cs-brand');
      if (!brand) return;
      this._buildCollapsed(sidebar, brand);
      this._buildPanel();
    }

    _buildCollapsed(sidebar, brand) {

      const iconWrap = h('span', { className: 'chatbot-btn-icon', 'aria-hidden': 'true' }, [
        svg([
          ['path', { d: 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z' }],
          ['path', { d: 'M8 10h.01' }],
          ['path', { d: 'M12 10h.01' }],
          ['path', { d: 'M16 10h.01' }],
        ], 20),
      ]);
      const label = h('span', { className: 'chatbot-btn-label' }, ['가이드']);
      const btn = h('button', {
        className: 'chatbot-btn',
        type: 'button',
        onclick: () => this.toggle(),
      }, [iconWrap, label]);

      const wrap = h('div', {
        className: 'chatbot-collapsed',
        id: 'chatbot-collapsed',
        title: '가이드 챗봇 — 클릭해서 열기',
      }, [btn]);

      sidebar.insertBefore(wrap, brand);
      this._collapsedEl = wrap;
    }

    _buildPanel() {
      const statusEl = h('span', { className: 'chatbot-status', id: 'chatbot-status' }, ['연결 대기']);
      this._statusEl = statusEl;

      const closeBtn = h('button', {
        className: 'chatbot-close',
        type: 'button',
        title: '접기',
        'aria-label': '접기',
        onclick: () => this.collapse(),
      }, [
        svg([
          ['line', { x1: 18, y1: 6, x2: 6, y2: 18 }],
          ['line', { x1: 6, y1: 6, x2: 18, y2: 18 }],
        ], 16),
      ]);

      const header = h('header', { className: 'chatbot-header' }, [
        h('div', { className: 'chatbot-title' }, [
          h('span', { className: 'chatbot-title-dot' }),
          h('span', null, ['가이드 챗봇']),
          statusEl,
        ]),
        closeBtn,
      ]);

      const intro = h('div', { className: 'chatbot-intro' }, [
        '이 앱의 기능을 쉽게 설명해드리고, 해결하고 싶은 과제를 말씀해주시면 어떤 기능(또는 기능 조합)을 쓰면 좋을지 안내해드려요.',
      ]);

      const messages = h('div', { className: 'chatbot-messages', id: 'chatbot-messages' });
      this._messagesEl = messages;

      const input = h('textarea', {
        id: 'chatbot-input',
        className: 'chatbot-input',
        rows: 2,
        placeholder: '궁금한 기능이나 해결하고 싶은 과제를 적어주세요…',
        onkeydown: (ev) => {
          // IME composition guard (CRITICAL for Korean/Japanese/Chinese):
          // during composition the browser may fire keydown Enter twice —
          // once with isComposing=true to commit, once to actually submit.
          // Skipping during composition prevents the duplicate send.
          if (ev.isComposing || ev.keyCode === 229) return;
          if (ev.key === 'Enter' && !ev.shiftKey) {
            ev.preventDefault();
            this._send();
          }
        },
      });
      this._inputEl = input;

      const sendBtn = h('button', {
        type: 'submit',
        className: 'chatbot-send',
        title: '보내기',
        'aria-label': '보내기',
      }, [
        svg([
          ['line', { x1: 22, y1: 2, x2: 11, y2: 13 }],
          ['polygon', { points: '22 2 15 22 11 13 2 9 22 2' }],
        ], 18),
      ]);
      this._sendBtn = sendBtn;

      const form = h('form', {
        className: 'chatbot-inputbar',
        id: 'chatbot-form',
        onsubmit: (ev) => { ev.preventDefault(); this._send(); },
      }, [input, sendBtn]);

      const panel = h('aside', {
        className: 'chatbot-panel',
        id: 'chatbot-panel',
        'aria-hidden': 'true',
      }, [header, intro, messages, form]);

      document.body.appendChild(panel);
      this._panelEl = panel;
    }

    /* ── Expand / collapse ── */
    toggle() { this._expanded ? this.collapse() : this.expand(); }

    expand() {
      this._expanded = true;
      this._collapsedEl?.classList.add('is-expanded');
      this._panelEl?.classList.add('is-open');
      this._panelEl?.setAttribute('aria-hidden', 'false');
      if (!this.ws || this.ws.readyState === WebSocket.CLOSED) this._connect();
      setTimeout(() => this._inputEl?.focus(), 50);
    }

    collapse() {
      this._expanded = false;
      this._collapsedEl?.classList.remove('is-expanded');
      this._panelEl?.classList.remove('is-open');
      this._panelEl?.setAttribute('aria-hidden', 'true');
    }


    /* ── WebSocket ── */
    _connect() {
      if (this._reconnectTimer) { clearTimeout(this._reconnectTimer); this._reconnectTimer = null; }
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      try {
        this.ws = new WebSocket(`${proto}://${location.host}/ws/chatbot`);
      } catch (err) {
        console.warn('[chatbot] ws construct failed', err);
        return;
      }
      this._setStatus('연결 중…');
      this.ws.onopen = () => { this._setStatus('준비됨'); this._reconnectAttempts = 0; };
      this.ws.onmessage = (ev) => {
        try { this._handle(JSON.parse(ev.data)); } catch {}
      };
      this.ws.onclose = () => { this._setStatus('연결 끊김'); this._scheduleReconnect(); };
      this.ws.onerror = () => this._setStatus('오류');
    }

    _scheduleReconnect() {
      if (!this._expanded) return;
      if (this._reconnectAttempts >= 5) return;
      const delay = Math.min(2000 * Math.pow(1.5, this._reconnectAttempts), 15000);
      this._reconnectAttempts++;
      this._reconnectTimer = setTimeout(() => this._connect(), delay);
    }

    _setStatus(text) { if (this._statusEl) this._statusEl.textContent = text; }

    _handle(msg) {
      switch (msg.type) {
        case 'bot_init':
          this._addSystemMessage('안녕하세요! 이 앱의 기능을 안내해드리는 가이드 챗봇이에요. 처음이시면 "이 앱은 뭐 하는 앱이에요?"처럼 물어보셔도 되고, 해결하고 싶은 과제를 바로 적어주셔도 돼요.');
          break;
        case 'bot_stream':
          this._handleStream(msg.data || {});
          break;
        case 'bot_reset_done':
          while (this._messagesEl.firstChild) this._messagesEl.removeChild(this._messagesEl.firstChild);
          break;
        case 'error':
          this._hideTypingIndicator();
          this._resetStreamState();
          this._addSystemMessage('⚠️ ' + (msg.data?.message || '알 수 없는 오류'));
          break;
      }
    }

    /* Reset streaming-related state after a failure, so the user can retry. */
    _resetStreamState() {
      if (this._typewriterTimer) {
        clearTimeout(this._typewriterTimer);
        this._typewriterTimer = null;
      }
      this._currentAssistantEl = null;
      this._streamRawText = '';
      this._typedLength = 0;
      this._doneReceived = false;
      this._isStreaming = false;
      this._isSending = false;
      this._setInputEnabled(true);
    }

    _handleStream(data) {
      // First token of a new response: remove typing indicator, create bubble
      if (!this._currentAssistantEl) {
        this._hideTypingIndicator();
        const el = h('div', { className: 'chatbot-msg chatbot-msg-ai' });
        this._messagesEl.appendChild(el);
        this._currentAssistantEl = el;
        this._streamRawText = '';
        this._typedLength = 0;
        this._doneReceived = false;
        this._isStreaming = true;
      }

      if (data.done) {
        // Mark done — typewriter will finalize and render markdown when it
        // catches up to the full text. This lets the animation complete
        // smoothly even if the server sent one huge chunk.
        this._doneReceived = true;
        this._startTypewriter();
        return;
      }

      if (data.token) {
        this._streamRawText += data.token;
        this._startTypewriter();
      }
    }

    /* Pseudo-streaming typewriter: paints 1 char per ~35ms tick, and
       re-renders the accumulated text as MARKDOWN on every tick via
       _renderPartial (with speculative-close smoothing for unclosed
       bold/code/mode-tags). Big server chunks thus feel like live
       token-by-token streaming with rich formatting appearing inline.
       Safe to call repeatedly — it no-ops if already running. */
    _startTypewriter() {
      if (this._typewriterTimer) return;
      const tick = () => {
        this._typewriterTimer = null;
        if (!this._currentAssistantEl) return;

        const target = this._streamRawText;
        if (this._typedLength < target.length) {
          const next = Math.min(this._typedLength + TYPEWRITER_CHARS_PER_TICK, target.length);
          this._typedLength = next;
          this._renderPartial(target.slice(0, next));
          this._typewriterTimer = setTimeout(tick, TYPEWRITER_TICK_MS);
          return;
        }

        // Caught up. If server already sent done, finalize.
        if (this._doneReceived) this._finalizeCurrentMessage();
      };
      this._typewriterTimer = setTimeout(tick, TYPEWRITER_TICK_MS);
    }

    /* Re-render the bubble's DOM with the given partial text as markdown.
       Called on every typewriter tick so formatting appears live during
       streaming. Preserves user scroll intent (doesn't auto-scroll if the
       user has scrolled up to read earlier content). */
    _renderPartial(partialText) {
      if (!this._currentAssistantEl || !this._messagesEl) return;

      const m = this._messagesEl;
      const atBottom = (m.scrollHeight - m.scrollTop - m.clientHeight) < 40;

      // Apply speculative-close smoothing to avoid flicker on unclosed markers
      const smoothed = smoothPartialMarkdown(partialText);

      // Clear and re-render
      while (this._currentAssistantEl.firstChild) {
        this._currentAssistantEl.removeChild(this._currentAssistantEl.firstChild);
      }
      renderMarkdownInto(smoothed, this._currentAssistantEl, (slug) => {
        const item = document.querySelector(`#card-sidebar .cs-item[data-card-mode="${slug}"]`);
        if (item) item.click();
        this.collapse();
      });

      if (atBottom) m.scrollTop = m.scrollHeight;
    }

    _finalizeCurrentMessage() {
      if (!this._currentAssistantEl) return;
      // Typewriter has already rendered the full text via _renderPartial —
      // nothing more to render, just reset state.
      this._currentAssistantEl = null;
      this._streamRawText = '';
      this._typedLength = 0;
      this._doneReceived = false;
      this._isStreaming = false;
      this._isSending = false;
      this._setInputEnabled(true);
      this._inputEl?.focus();
    }

    /* Typing indicator (shown after user sends, before first token arrives) */
    _showTypingIndicator() {
      if (this._typingEl) return;
      const dot1 = h('span');
      const dot2 = h('span');
      const dot3 = h('span');
      const el = h('div', { className: 'chatbot-msg chatbot-msg-ai chatbot-typing' }, [dot1, dot2, dot3]);
      this._messagesEl.appendChild(el);
      this._typingEl = el;
      this._messagesEl.scrollTop = this._messagesEl.scrollHeight;
    }

    _hideTypingIndicator() {
      if (!this._typingEl) return;
      if (this._typingEl.parentNode) this._typingEl.parentNode.removeChild(this._typingEl);
      this._typingEl = null;
    }

    _send() {
      // Double-submit guard: blocks rapid repeat (form submit + keydown,
      // IME quirks, accidental double-click on send button).
      if (this._isSending || this._isStreaming) return;
      const text = (this._inputEl?.value || '').trim();
      if (!text) return;
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
        this._addSystemMessage('⚠️ 연결이 아직 준비되지 않았어요. 잠시 후 다시 시도해주세요.');
        return;
      }
      this._isSending = true;
      this._setInputEnabled(false);
      this._addUserMessage(text);
      this._inputEl.value = '';
      this._showTypingIndicator();
      this.ws.send(JSON.stringify({ type: 'bot_message', data: { content: text } }));
    }

    _setInputEnabled(enabled) {
      if (this._inputEl) this._inputEl.disabled = !enabled;
      if (this._sendBtn) this._sendBtn.disabled = !enabled;
    }

    _addUserMessage(text) {
      const el = h('div', { className: 'chatbot-msg chatbot-msg-user' }, [text]);
      this._messagesEl.appendChild(el);
      this._messagesEl.scrollTop = this._messagesEl.scrollHeight;
    }

    _addSystemMessage(text) {
      const el = h('div', { className: 'chatbot-msg chatbot-msg-system' }, [text]);
      this._messagesEl.appendChild(el);
      this._messagesEl.scrollTop = this._messagesEl.scrollHeight;
    }
  }

  /* ── Sidebar running-state indicator (module-level, independent of widget) ──
     Toggles `.cs-item-running` on the sidebar button matching the given slug.
     Works even if the chatbot widget fails to mount — the indicator is
     purely a sidebar feature listening to a shared event bus. */
  function _applyRunningState(slug, running) {
    if (typeof slug !== 'string' || !MODE_LABELS[slug]) return;
    const item = document.querySelector('#card-sidebar .cs-item[data-card-mode="' + slug + '"]');
    if (!item) return;  // sidebar not yet mounted or slug doesn't match any button
    item.classList.toggle('cs-item-running', !!running);
  }

  window.addEventListener('chatbot:mode-state', (ev) => {
    const detail = ev && ev.detail;
    if (!detail) return;
    _applyRunningState(detail.mode, detail.running);
  });

  /* ── Auto-mount (defensive: never break host page on failure) ── */
  const widget = new ChatbotWidget();
  window.__chatbotWidget = widget;

  /* Public API for mode JS files to signal running state.
     Usage from any mode JS:
       window.chatbotSignal('upgrade', true);   // started → button glows
       window.chatbotSignal('upgrade', false);  // finished → glow stops
     The optional `status` parameter is accepted for forward compatibility
     but currently ignored (we only toggle a CSS class). */
  window.chatbotSignal = function (mode, running, _status) {
    window.dispatchEvent(new CustomEvent('chatbot:mode-state', {
      detail: { mode, running: !!running },
    }));
  };

  function safeMount() {
    try {
      widget.mount();
    } catch (err) {
      console.error('[chatbot] mount failed, widget disabled:', err);
    }
  }

  function init() {
    try {
      if (document.getElementById('card-sidebar')) {
        safeMount();
        return;
      }
      let tries = 0;
      const iv = setInterval(() => {
        tries++;
        if (document.getElementById('card-sidebar')) {
          clearInterval(iv);
          safeMount();
        } else if (tries > 25) {
          clearInterval(iv);
        }
      }, 200);
    } catch (err) {
      console.error('[chatbot] init failed:', err);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
