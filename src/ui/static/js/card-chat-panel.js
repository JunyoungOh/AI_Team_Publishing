/**
 * CardChatPanel — right-side chat panel for card view.
 * Supports per-mode message history via switchMode().
 *
 * Usage:
 *   const panel = new CardChatPanel(containerEl, {
 *     onSend: (text) => { ... }
 *   });
 */
class CardChatPanel {
  /**
   * @param {HTMLElement} containerEl — element to build the panel inside
   * @param {{ onSend?: (text: string) => void }} opts
   */
  constructor(containerEl, opts = {}) {
    this.el = containerEl;
    this.onSend = opts.onSend || (() => {});
    this._modeContainers = {}; // mode → DOM element
    this._currentMode = null;

    // --- build DOM ---
    while (this.el.firstChild) this.el.removeChild(this.el.firstChild);

    // header
    const header = document.createElement('div');
    header.className = 'cc-header';
    this.titleEl = document.createElement('span');
    this.titleEl.className = 'cc-header-title';
    this.titleEl.textContent = 'Chat';
    const title = this.titleEl;
    const closeBtn = document.createElement('button');
    closeBtn.className = 'cc-close';
    closeBtn.title = '\ub2eb\uae30'; // 닫기
    closeBtn.textContent = '\u00d7'; // ×
    header.appendChild(title);
    header.appendChild(closeBtn);

    // messages area (default container)
    this.messagesEl = document.createElement('div');
    this.messagesEl.className = 'cc-messages';

    // input area
    const inputWrap = document.createElement('div');
    inputWrap.className = 'cc-input-wrap';
    this.inputEl = document.createElement('input');
    this.inputEl.className = 'cc-input';
    this.inputEl.placeholder = '\uba54\uc2dc\uc9c0 \uc785\ub825...'; // 메시지 입력...
    this.sendBtn = document.createElement('button');
    this.sendBtn.className = 'cc-send';
    this.sendBtn.textContent = '\uc804\uc1a1'; // 전송
    this.stopBtn = document.createElement('button');
    this.stopBtn.id = 'card-stop-btn';
    this.stopBtn.title = '\uc2e4\ud589 \uc911\uc9c0'; // 실행 중지
    this.stopBtn.textContent = '\u25a0 \uc911\uc9c0'; // ■ 중지
    this.stopBtn.style.display = 'none';
    inputWrap.appendChild(this.inputEl);
    inputWrap.appendChild(this.sendBtn);
    inputWrap.appendChild(this.stopBtn);

    this.el.appendChild(header);
    this.el.appendChild(this.messagesEl);
    this.el.appendChild(inputWrap);

    // --- event listeners ---
    closeBtn.addEventListener('click', () => this.toggle(false));

    this.sendBtn.addEventListener('click', () => this._handleSend());

    this.inputEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.isComposing) {
        e.preventDefault();
        this._handleSend();
      }
    });
  }

  /** @private */
  _handleSend() {
    const text = this.inputEl.value.trim();
    if (!text) return;
    this.addMessage(text, 'user');
    this.showThinking();
    this.onSend(text);
    this.inputEl.value = '';
  }

  /**
   * Switch to a different mode's message container.
   * Creates a new container if first visit, otherwise restores existing.
   * @param {string} mode — e.g. 'instant', 'builder'
   */
  switchMode(mode) {
    if (this._currentMode === mode) return;

    // Hide current container (including initial default one)
    if (this.messagesEl) {
      if (this._currentMode) {
        this._modeContainers[this._currentMode] = this.messagesEl;
      }
      this.messagesEl.style.display = 'none';
    }

    // Restore or create target container
    if (this._modeContainers[mode]) {
      this.messagesEl = this._modeContainers[mode];
      this.messagesEl.style.display = '';
    } else {
      var newContainer = document.createElement('div');
      newContainer.className = 'cc-messages';
      newContainer.dataset.mode = mode;
      this._modeContainers[mode] = newContainer;
      this.messagesEl = newContainer;
      // Insert before input wrap
      var inputWrap = this.el.querySelector('.cc-input-wrap');
      if (inputWrap) {
        this.el.insertBefore(newContainer, inputWrap);
      } else {
        this.el.appendChild(newContainer);
      }
    }

    this._currentMode = mode;
  }

  /**
   * Add a message bubble.
   * @param {string} text
   * @param {'user'|'system'} type
   * @param {{ welcome?: boolean }} opts
   */
  addMessage(text, type, opts = {}) {
    // 시스템 응답이 오면 thinking indicator 제거
    if (type === 'system') this.hideThinking();

    const msg = document.createElement('div');
    msg.className = `cc-message cc-message-${type}`;
    if (opts.welcome) msg.classList.add('cc-welcome');
    // 마크다운 렌더링 (marked.js — Secretary/builder와 동일 패턴, 백엔드 생성 콘텐츠)
    if (opts.markdown && typeof marked !== 'undefined') {
      try {
        msg.innerHTML = marked.parse(text); // eslint-disable-line no-unsanitized/property
      } catch (_) { msg.textContent = text; }
      msg.classList.add('cc-markdown');
    } else if (opts.preserveNewlines) {
      msg.style.whiteSpace = 'pre-wrap';
      msg.textContent = text;
    } else {
      msg.textContent = text;
    }
    this.messagesEl.appendChild(msg);
    this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
  }

  /**
   * Open or close the panel.
   * @param {boolean} open
   */
  toggle(open) {
    const app = document.getElementById('card-app');
    if (app) {
      // chat-fullwidth 모드에서는 chat-open 불필요 (이미 전체 영역)
      if (!app.classList.contains('chat-fullwidth')) {
        app.classList.toggle('chat-open', open);
      }
    }
    this.el.classList.toggle('collapsed', !open);
  }

  /**
   * Change the panel header title.
   * @param {string} text
   */
  setTitle(text) {
    if (this.titleEl) this.titleEl.textContent = text;
  }

  /**
   * Change input placeholder text.
   * @param {string} text
   */
  setInputPlaceholder(text) {
    if (this.inputEl) this.inputEl.placeholder = text;
  }

  /**
   * Enable or disable the chat input and send button.
   * @param {boolean} disabled
   */
  setInputDisabled(disabled) {
    if (this.inputEl) {
      this.inputEl.disabled = disabled;
      this.inputEl.style.opacity = disabled ? '0.5' : '';
      this.inputEl.style.cursor = disabled ? 'not-allowed' : '';
    }
    if (this.sendBtn) {
      this.sendBtn.disabled = disabled;
      this.sendBtn.style.opacity = disabled ? '0.5' : '';
      this.sendBtn.style.cursor = disabled ? 'not-allowed' : '';
    }
  }

  /**
   * Hide all action button groups (e.g., "이 방식 저장하기", "방식 수정 요청").
   * Used to prevent conflicts during pipeline execution.
   */
  hideActionButtons() {
    if (!this.messagesEl) return;
    this.messagesEl.querySelectorAll('.cc-action-btns').forEach(el => {
      el.style.display = 'none';
    });
  }

  /**
   * Re-show action button groups after execution ends.
   */
  showActionButtons() {
    if (!this.messagesEl) return;
    this.messagesEl.querySelectorAll('.cc-action-btns').forEach(el => {
      el.style.display = '';
    });
  }

  /**
   * Add a clickable report link as a message.
   * @param {string} reportPath — URL or path to the report
   */
  addReportLink(reportPath, localPath) {
    this.hideThinking();
    const msg = document.createElement('div');
    msg.className = 'cc-message cc-message-system cc-report-link';
    const link = document.createElement('a');
    link.href = reportPath;
    link.target = '_blank';
    link.rel = 'noopener';
    link.textContent = '📄 보고서 보기';
    msg.appendChild(link);

    if (localPath) {
      const folderBtn = document.createElement('button');
      folderBtn.className = 'cc-folder-btn';
      folderBtn.textContent = '📁 폴더 열기';
      folderBtn.addEventListener('click', () => {
        fetch('/api/open-folder', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: localPath }),
        });
      });
      msg.appendChild(folderBtn);
    }

    this.messagesEl.appendChild(msg);
    this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
  }

  /**
   * Add action buttons (e.g., for builder mode quick actions).
   * @param {Array<{label: string, icon: string, action: function}>} buttons
   */
  addActionButtons(buttons) {
    const wrap = document.createElement('div');
    wrap.className = 'cc-action-btns';
    buttons.forEach(btn => {
      const el = document.createElement('button');
      el.className = 'cc-action-btn';
      el.textContent = (btn.icon || '') + ' ' + btn.label;
      el.addEventListener('click', () => {
        if (btn.action) btn.action();
      });
      wrap.appendChild(el);
    });
    this.messagesEl.appendChild(wrap);
    this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
  }

  /**
   * Show output format selector chips.
   * @param {Array<{id: string, label: string, icon: string, default?: boolean}>} options
   */
  showFormatSelector(options) {
    this._selectedFormat = 'html';
    const wrap = document.createElement('div');
    wrap.className = 'cc-format-selector';
    const label = document.createElement('div');
    label.className = 'cc-format-label';
    label.textContent = '출력 형식 선택';
    wrap.appendChild(label);
    const chips = document.createElement('div');
    chips.className = 'cc-format-chips';
    options.forEach(opt => {
      const chip = document.createElement('button');
      chip.className = 'cc-format-chip';
      if (opt.default) chip.classList.add('selected');
      chip.dataset.formatId = opt.id;
      chip.textContent = (opt.icon || '') + ' ' + opt.label;
      chip.addEventListener('click', () => {
        chips.querySelectorAll('.cc-format-chip').forEach(c => c.classList.remove('selected'));
        chip.classList.add('selected');
        this._selectedFormat = opt.id;
      });
      chips.appendChild(chip);
    });
    wrap.appendChild(chips);
    this.messagesEl.appendChild(wrap);
    this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
  }

  /** Get the currently selected output format. */
  getSelectedFormat() {
    return this._selectedFormat || 'html';
  }

  /**
   * Show a typing/thinking indicator in chat.
   * Calling multiple times is safe — only one indicator at a time.
   */
  showThinking() {
    this.hideThinking();
    const msg = document.createElement('div');
    msg.className = 'cc-message cc-message-system cc-thinking';
    const dots = document.createElement('span');
    dots.className = 'cc-thinking-dots';
    for (var i = 0; i < 3; i++) dots.appendChild(document.createElement('span'));
    msg.appendChild(dots);
    this.messagesEl.appendChild(msg);
    this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
  }

  /** Remove the thinking indicator if present. */
  hideThinking() {
    var el = this.messagesEl.querySelector('.cc-thinking');
    if (el) el.remove();
  }

  /** Remove all messages from current mode's container. */
  clear() {
    while (this.messagesEl.firstChild) {
      this.messagesEl.removeChild(this.messagesEl.firstChild);
    }
  }
}
