'use strict';

/* Sidebar "running" indicator signal — see mode-chatbot.js for receiver. */
function _secSignalRunning(on) {
  try {
    if (window.chatbotSignal) window.chatbotSignal('secretary', on);
  } catch (_) { /* noop */ }
}

/* ═══════════════════════════════════════════════════
   §  SECRETARY MANAGER — Chat-only UI in CardShell
   ═══════════════════════════════════════════════════ */
class SecretaryManager {
  constructor() {
    this.ws = null;
    this._chat = null;
    this._input = null;
    this._sendBtn = null;
    this._statusEl = null;
    this._currentAssistantEl = null;
    this._isStreaming = false;
    this._chatMode = 'flash'; // "flash" or "think"
    this._currentPersonaId = null;
    this._hasMessages = false;
    this._reconnectTimer = null;
    this._reconnectAttempts = 0;
    this._streamRawText = '';
    this._inHtmlBlock = false;
  }

  /* ═══════════════════════════════════════════════════
     Shell integration — called by CardView._bootMode()
     ═══════════════════════════════════════════════════ */

  static mountInShell(container) {
    if (!SecretaryManager._instance) {
      SecretaryManager._instance = new SecretaryManager();
    }
    SecretaryManager._instance._mount(container);
  }

  _mount(container) {
    // Clear container
    while (container.firstChild) container.removeChild(container.firstChild);

    // Build shell structure
    const shell = document.createElement('div');
    shell.className = 'secretary-shell';

    // ── Toolbar ──
    const toolbar = document.createElement('div');
    toolbar.className = 'sec-toolbar';

    // Persona select
    const personaSelect = document.createElement('select');
    personaSelect.id = 'sec-persona-select';
    personaSelect.className = 'cv-select';
    const defaultOpt = document.createElement('option');
    defaultOpt.value = '';
    defaultOpt.textContent = 'AI Secretary';
    personaSelect.appendChild(defaultOpt);
    toolbar.appendChild(personaSelect);

    // Status
    const status = document.createElement('span');
    status.id = 'sec-status';
    status.style.cssText = 'font-size:11px;color:var(--cv-dim);';
    status.textContent = '';
    toolbar.appendChild(status);

    // Mode toggle (Flash / Think)
    const toggle = document.createElement('div');
    toggle.className = 'sec-mode-toggle';
    toggle.id = 'sec-mode-toggle';

    const flashBtn = document.createElement('button');
    flashBtn.className = 'sec-mode-btn active';
    flashBtn.dataset.mode = 'flash';
    flashBtn.textContent = '\u26A1 Flash';
    toggle.appendChild(flashBtn);

    const thinkBtn = document.createElement('button');
    thinkBtn.className = 'sec-mode-btn';
    thinkBtn.dataset.mode = 'think';
    thinkBtn.textContent = '\uD83D\uDCA1 Think';
    toggle.appendChild(thinkBtn);

    toolbar.appendChild(toggle);
    shell.appendChild(toolbar);

    // ── Chat area ──
    const chatArea = document.createElement('div');
    chatArea.className = 'sec-chat-area';
    chatArea.id = 'sec-chat';
    shell.appendChild(chatArea);

    // ── Input bar ──
    const inputBar = document.createElement('div');
    inputBar.className = 'sec-input-bar';

    const textarea = document.createElement('textarea');
    textarea.id = 'sec-input';
    textarea.rows = 1;
    textarea.placeholder = '\uBA54\uC2DC\uC9C0\uB97C \uC785\uB825\uD558\uC138\uC694...';
    inputBar.appendChild(textarea);

    const sendBtn = document.createElement('button');
    sendBtn.className = 'sec-send-btn';
    sendBtn.id = 'sec-send-btn';
    sendBtn.textContent = '\uC804\uC1A1';
    inputBar.appendChild(sendBtn);

    shell.appendChild(inputBar);
    container.appendChild(shell);

    // Store DOM refs
    this._chat = chatArea;
    this._input = textarea;
    this._sendBtn = sendBtn;
    this._statusEl = status;

    // Wire up events
    this._setupUI(personaSelect, toggle);

    // Connect WebSocket
    this.connect();
  }

  _setupUI(personaSelect, toggle) {
    this._sendBtn.addEventListener('click', () => this._sendMessage());
    this._input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        this._sendMessage();
      }
    });
    /* Auto-resize textarea */
    this._input.addEventListener('input', () => {
      this._input.style.height = 'auto';
      this._input.style.height = Math.min(this._input.scrollHeight, 120) + 'px';
    });
    /* Mode toggle (Flash / Think) */
    toggle.querySelectorAll('button').forEach(btn => {
      btn.addEventListener('click', () => {
        toggle.querySelectorAll('button').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this._chatMode = btn.dataset.mode;
      });
    });

    /* Persona dropdown */
    personaSelect.addEventListener('change', (e) => {
      const personaId = e.target.value || null;
      if (this._hasMessages) {
        if (!confirm('\uB2F4\uB2F9\uC744 \uBCC0\uACBD\uD558\uBA74 \uD604\uC7AC \uB300\uD654\uAC00 \uCD08\uAE30\uD654\uB429\uB2C8\uB2E4. \uBCC0\uACBD\uD558\uC2DC\uACA0\uC2B5\uB2C8\uAE4C?')) {
          e.target.value = this._currentPersonaId || '';
          return;
        }
      }
      this._currentPersonaId = personaId;
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ type: 'sec_set_persona', data: { persona_id: personaId } }));
      }
    });
  }

  async _loadSecPersonaDropdown() {
    const select = document.getElementById('sec-persona-select');
    if (!select) return;
    while (select.options.length > 1) select.remove(1);
    try {
      const resp = await fetch('/api/personas/usable');
      if (!resp.ok) return;
      const data = await resp.json();
      const personas = data.personas || [];
      const mine = personas.filter(p => p.mine);
      const shared = personas.filter(p => !p.mine);

      if (mine.length) {
        const grp = document.createElement('optgroup');
        grp.label = '\uB0B4 \uD398\uB974\uC18C\uB098';
        mine.forEach(p => {
          const opt = document.createElement('option');
          opt.value = p.id;
          opt.textContent = '\uD83E\uDDEC ' + p.name;
          grp.appendChild(opt);
        });
        select.appendChild(grp);
      }
      if (shared.length) {
        const grp = document.createElement('optgroup');
        grp.label = '\uACF5\uC720 \uD398\uB974\uC18C\uB098';
        shared.forEach(p => {
          const opt = document.createElement('option');
          opt.value = p.id;
          opt.textContent = '\uD83E\uDDEC ' + p.name + (p.owner_name ? ' (by ' + p.owner_name + ')' : '');
          grp.appendChild(opt);
        });
        select.appendChild(grp);
      }
    } catch (e) { console.warn('Failed to load personas:', e); }
  }

  connect() {
    if (this._reconnectTimer) { clearTimeout(this._reconnectTimer); this._reconnectTimer = null; }
    this._reconnectAttempts = (this._reconnectAttempts || 0);

    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    this.ws = new WebSocket(`${proto}://${location.host}/ws/sec`);
    this._statusEl.textContent = 'Connecting...';

    this.ws.onopen = () => {
      this._statusEl.textContent = 'Connected';
      this._reconnectAttempts = 0;
    };
    this.ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        this._handle(msg);
      } catch {}
    };
    this.ws.onclose = () => {
      this._statusEl.textContent = 'Disconnected';
      this._scheduleReconnect();
    };
    this.ws.onerror = () => {
      this._statusEl.textContent = 'Error';
    };
  }

  _scheduleReconnect() {
    if (this._reconnectAttempts >= 10) {
      this._addSystemMessage('\uC11C\uBC84\uC640\uC758 \uC5F0\uACB0\uC774 \uB04A\uC5B4\uC84C\uC2B5\uB2C8\uB2E4. \uD398\uC774\uC9C0\uB97C \uC0C8\uB85C\uACE0\uCE68\uD574\uC8FC\uC138\uC694.');
      return;
    }
    const delay = Math.min(3000 * Math.pow(1.5, this._reconnectAttempts), 30000);
    this._reconnectAttempts++;
    this._statusEl.textContent = `\uC7AC\uC5F0\uACB0 \uC911... (${this._reconnectAttempts}/10)`;
    this._reconnectTimer = setTimeout(() => this.connect(), delay);
  }

  _handle(msg) {
    switch (msg.type) {
      case 'sec_init':
        this._statusEl.textContent = 'Ready';
        this._loadSecPersonaDropdown();
        if (msg.data?.restored) {
          this._addSystemMessage('\uC774\uC804 \uB300\uD654\uB97C \uBD88\uB7EC\uC624\uB294 \uC911...');
        }
        this._addSystemMessage('\uC548\uB155\uD558\uC138\uC694! \uBB34\uC5C7\uC774\uB4E0 \uBB3C\uC5B4\uBCF4\uC138\uC694.');
        break;

      case 'sec_history_restored':
        this._handleHistoryRestored(msg.data);
        break;

      case 'sec_stream':
        this._handleStream(msg.data);
        break;

      case 'sec_task_started':
        this._handleTaskStarted(msg.data);
        break;

      case 'sec_task_progress':
        this._handleTaskProgress(msg.data);
        break;

      case 'sec_task_complete':
        this._handleTaskComplete(msg.data);
        break;

      case 'sec_tool_status':
        this._handleToolStatus(msg.data);
        break;

      case 'sec_disc_setup':
        this._handleDiscSetup(msg.data);
        break;

      case 'sec_company_prep_start':
        this._handleCompanyPrepStart(msg.data);
        break;

      case 'sec_company_questions':
        this._handleCompanyQuestions(msg.data);
        break;

      case 'sec_calendar':
        this._handleCalendarEvent(msg.data);
        break;

      case 'sec_report':
        this._handleReportEvent(msg.data);
        break;

      case 'sec_char_loaded':
        // No-op — character customization removed
        break;

      case 'sec_persona_changed':
        {
          const pd = msg.data;
          this._currentPersonaId = pd.persona_id;
          const chatEl = this._chat;
          if (chatEl) chatEl.textContent = '';
          this._hasMessages = false;

          // Show warning if persona load failed
          if (pd.warning) {
            this._addSystemMessage('\u26A0\uFE0F ' + pd.warning);
            const sel = document.getElementById('sec-persona-select');
            if (sel) sel.value = '';
          }
        }
        break;

      case 'heartbeat':
        break;

      case 'error':
        this._addSystemMessage('\uC624\uB958: ' + (msg.data?.message || 'Unknown'));
        break;
    }
  }

  _handleToolStatus(data) {
    /* Update typing indicator to show tool status */
    const typing = this._chat.querySelector('.sec-typing');
    if (typing) {
      const existing = typing.querySelector('.sec-tool-label');
      if (existing) {
        existing.textContent = data.status;
      } else {
        const label = document.createElement('span');
        label.className = 'sec-tool-label';
        label.style.cssText = 'color:var(--cv-accent);margin-left:8px;font-size:0.85em;';
        label.textContent = data.status;
        typing.appendChild(label);
      }
    }
  }

  _handleDiscSetup(data) {
    /* Render an inline discussion setup form card in the chat */
    const card = document.createElement('div');
    card.className = 'sec-disc-setup-card';

    /* Title */
    const title = document.createElement('div');
    title.className = 'disc-setup-title';
    title.textContent = '\uD83D\uDCAC AI Discussion \uC124\uC815';
    card.appendChild(title);

    /* Topic */
    const topicLabel = document.createElement('label');
    topicLabel.textContent = '\uD1A0\uB860 \uC8FC\uC81C';
    topicLabel.className = 'disc-setup-label';
    const topicInput = document.createElement('input');
    topicInput.type = 'text';
    topicInput.className = 'disc-setup-input';
    topicInput.value = data.topic || '';
    card.appendChild(topicLabel);
    card.appendChild(topicInput);

    /* Style + Time row */
    const row = document.createElement('div');
    row.className = 'disc-setup-row';

    const styleWrap = document.createElement('div');
    styleWrap.className = 'disc-setup-field';
    const styleLabel = document.createElement('label');
    styleLabel.textContent = '\uD1A0\uB860 \uC2A4\uD0C0\uC77C';
    styleLabel.className = 'disc-setup-label';
    const styleSelect = document.createElement('select');
    styleSelect.className = 'disc-setup-select';
    [['free','\uC790\uC720\uD1A0\uB860'],['debate','\uCC2C\uBC18\uD1A0\uB860'],['brainstorm','\uBE0C\uB808\uC778\uC2A4\uD1A0\uBC0D']].forEach(([v,t]) => {
      const opt = document.createElement('option');
      opt.value = v; opt.textContent = t;
      if (v === (data.style || 'free')) opt.selected = true;
      styleSelect.appendChild(opt);
    });
    styleWrap.appendChild(styleLabel);
    styleWrap.appendChild(styleSelect);

    const timeWrap = document.createElement('div');
    timeWrap.className = 'disc-setup-field';
    const timeLabel = document.createElement('label');
    timeLabel.textContent = '\uC81C\uD55C \uC2DC\uAC04 (\uBD84)';
    timeLabel.className = 'disc-setup-label';
    const timeInput = document.createElement('input');
    timeInput.type = 'number';
    timeInput.className = 'disc-setup-input disc-setup-time';
    timeInput.min = '3'; timeInput.max = '60';
    timeInput.value = data.time_limit_min || 5;
    timeWrap.appendChild(timeLabel);
    timeWrap.appendChild(timeInput);

    row.appendChild(styleWrap);
    row.appendChild(timeWrap);
    card.appendChild(row);

    /* Participants */
    const partLabel = document.createElement('label');
    partLabel.textContent = '\uCC38\uAC00\uC790';
    partLabel.className = 'disc-setup-label';
    card.appendChild(partLabel);

    const partList = document.createElement('div');
    partList.className = 'disc-setup-participants';

    const addParticipantRow = (name, persona) => {
      const pRow = document.createElement('div');
      pRow.className = 'disc-setup-participant';
      const nameIn = document.createElement('input');
      nameIn.type = 'text'; nameIn.placeholder = '\uC774\uB984';
      nameIn.className = 'disc-setup-input disc-setup-pname';
      nameIn.value = name || '';
      const personaIn = document.createElement('input');
      personaIn.type = 'text'; personaIn.placeholder = '\uD398\uB974\uC18C\uB098 (\uC804\uBB38\uBD84\uC57C, \uC131\uD5A5 \uB4F1)';
      personaIn.className = 'disc-setup-input disc-setup-ppersona';
      personaIn.value = persona || '';
      const removeBtn = document.createElement('button');
      removeBtn.className = 'disc-setup-remove';
      removeBtn.textContent = '\u00D7';
      removeBtn.addEventListener('click', () => {
        if (partList.children.length > 2) pRow.remove();
      });
      pRow.appendChild(removeBtn);
      pRow.appendChild(nameIn);
      pRow.appendChild(personaIn);
      partList.appendChild(pRow);
    };

    (data.participants || []).forEach(p => addParticipantRow(p.name, p.persona));
    card.appendChild(partList);

    const addBtn = document.createElement('button');
    addBtn.className = 'disc-setup-add-btn';
    addBtn.textContent = '+ \uCC38\uAC00\uC790 \uCD94\uAC00';
    addBtn.addEventListener('click', () => {
      if (partList.children.length < 6) addParticipantRow('', '');
    });
    card.appendChild(addBtn);

    /* Action buttons */
    const actions = document.createElement('div');
    actions.className = 'disc-setup-actions';

    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'disc-setup-cancel';
    cancelBtn.textContent = '\uCDE8\uC18C';
    cancelBtn.addEventListener('click', () => {
      card.remove();
      this._setInputEnabled(true);
    });

    const startBtn = document.createElement('button');
    startBtn.className = 'disc-setup-start';
    startBtn.textContent = '\uD1A0\uB860 \uC2DC\uC791';
    startBtn.addEventListener('click', () => {
      const participants = [];
      partList.querySelectorAll('.disc-setup-participant').forEach(pRow => {
        const n = pRow.querySelector('.disc-setup-pname').value.trim();
        const p = pRow.querySelector('.disc-setup-ppersona').value.trim();
        if (n) participants.push({ name: n, persona: p });
      });
      if (participants.length < 2) { alert('\uCC38\uAC00\uC790\uAC00 \uCD5C\uC18C 2\uBA85 \uD544\uC694\uD569\uB2C8\uB2E4.'); return; }
      const topic = topicInput.value.trim();
      if (!topic) { alert('\uD1A0\uB860 \uC8FC\uC81C\uB97C \uC785\uB825\uD558\uC138\uC694.'); return; }

      /* Disable form */
      startBtn.disabled = true;
      startBtn.textContent = '\uC2DC\uC791 \uC911...';
      card.style.opacity = '0.7';
      card.style.pointerEvents = 'none';

      this.ws.send(JSON.stringify({
        type: 'sec_disc_confirm',
        data: {
          topic,
          style: styleSelect.value,
          time_limit_min: parseInt(timeInput.value, 10) || 5,
          participants,
        },
      }));
    });

    actions.appendChild(cancelBtn);
    actions.appendChild(startBtn);
    card.appendChild(actions);

    this._chat.appendChild(card);
    this._chat.scrollTop = this._chat.scrollHeight;
  }

  _handleCompanyPrepStart(data) {
    const el = document.createElement('div');
    el.className = 'sec-msg sec-msg-ai';
    el.id = 'sec-company-prep-indicator';
    el.textContent = '\uD83C\uDFE2 AI Company \uBD84\uC11D \uC911... \u23F3';
    this._chat.appendChild(el);
    this._chat.scrollTop = this._chat.scrollHeight;
  }

  _handleCompanyQuestions(data) {
    /* Remove the prep spinner */
    const spinner = document.getElementById('sec-company-prep-indicator');
    if (spinner) spinner.remove();

    /* Render an inline Company clarification card */
    const card = document.createElement('div');
    card.className = 'sec-disc-setup-card sec-company-q-card';

    /* Title */
    const title = document.createElement('div');
    title.className = 'disc-setup-title';
    title.textContent = '\uD83C\uDFE2 AI Company \u2014 \uBA85\uD655\uD654 \uC9C8\uBB38';
    card.appendChild(title);

    /* Task summary */
    const taskInfo = document.createElement('div');
    taskInfo.className = 'sec-company-task-info';
    const taskText = (data.task || '').substring(0, 120);
    const domainsText = (data.domains || []).join(', ');
    const complexityText = data.complexity || '?';

    const taskLine = document.createElement('div');
    const taskStrong = document.createElement('strong');
    taskStrong.textContent = '\uD0DC\uC2A4\uD06C: ';
    taskLine.appendChild(taskStrong);
    taskLine.appendChild(document.createTextNode(taskText));
    taskInfo.appendChild(taskLine);

    const metaLine = document.createElement('div');
    const domainStrong = document.createElement('strong');
    domainStrong.textContent = '\uB3C4\uBA54\uC778: ';
    metaLine.appendChild(domainStrong);
    metaLine.appendChild(document.createTextNode(domainsText + ' | '));
    const complexStrong = document.createElement('strong');
    complexStrong.textContent = '\uBCF5\uC7A1\uB3C4: ';
    metaLine.appendChild(complexStrong);
    metaLine.appendChild(document.createTextNode(complexityText));
    if (data.deep_research) {
      const drStrong = document.createElement('strong');
      drStrong.textContent = ' | \uD83D\uDD2C \uB525\uB9AC\uC11C\uCE58';
      metaLine.appendChild(drStrong);
    }
    taskInfo.appendChild(metaLine);
    card.appendChild(taskInfo);

    /* Questions by domain */
    const qContainer = document.createElement('div');
    qContainer.className = 'sec-company-questions';
    const answerInputs = {};

    (data.questions || []).forEach(dq => {
      const domainSection = document.createElement('div');
      domainSection.className = 'sec-company-domain-section';
      const domainTitle = document.createElement('div');
      domainTitle.className = 'sec-company-domain-title';
      domainTitle.textContent = '\uD83D\uDCC2 ' + dq.domain;
      domainSection.appendChild(domainTitle);

      answerInputs[dq.domain] = [];
      (dq.questions || []).forEach((q, i) => {
        const qRow = document.createElement('div');
        qRow.className = 'sec-company-q-row';
        const qLabel = document.createElement('div');
        qLabel.className = 'sec-company-q-label';
        qLabel.textContent = (i + 1) + '. ' + q;
        qRow.appendChild(qLabel);
        const aInput = document.createElement('input');
        aInput.type = 'text';
        aInput.className = 'disc-setup-input sec-company-answer';
        aInput.placeholder = '\uB2F5\uBCC0\uC744 \uC785\uB825\uD558\uC138\uC694';
        qRow.appendChild(aInput);
        domainSection.appendChild(qRow);
        answerInputs[dq.domain].push(aInput);
      });
      qContainer.appendChild(domainSection);
    });
    card.appendChild(qContainer);

    /* Action buttons */
    const actions = document.createElement('div');
    actions.className = 'disc-setup-actions';

    const submitBtn = document.createElement('button');
    submitBtn.className = 'disc-setup-start';
    submitBtn.textContent = '\uB2F5\uBCC0 \uC81C\uCD9C & \uC2E4\uD589';
    submitBtn.addEventListener('click', () => {
      const answers = {};
      const questions = {};
      for (const [domain, inputs] of Object.entries(answerInputs)) {
        answers[domain] = inputs.map(inp => inp.value.trim());
        const dq = (data.questions || []).find(q => q.domain === domain);
        questions[domain] = dq ? dq.questions : [];
      }

      submitBtn.disabled = true;
      submitBtn.textContent = '\uC2E4\uD589 \uC911...';
      card.style.opacity = '0.7';
      card.style.pointerEvents = 'none';

      this.ws.send(JSON.stringify({
        type: 'sec_company_answers',
        data: {
          task: data.task,
          answers,
          questions,
          rationale: data.rationale || '',
          complexity: data.complexity || 'medium',
          domains: data.domains || [],
          deep_research: data.deep_research || false,
        },
      }));
    });

    actions.appendChild(submitBtn);
    card.appendChild(actions);

    this._chat.appendChild(card);
    this._chat.scrollTop = this._chat.scrollHeight;
  }

  _handleStream(data) {
    if (!this._currentAssistantEl) {
      /* Remove typing indicator if present */
      const typing = this._chat.querySelector('.sec-typing');
      if (typing) typing.remove();

      /* Create new assistant message bubble */
      const el = document.createElement('div');
      el.className = 'sec-msg sec-msg-ai';
      this._chat.appendChild(el);
      this._currentAssistantEl = el;
      this._streamRawText = '';
      this._inHtmlBlock = false;
      this._isStreaming = true;
      _secSignalRunning(true);
      this._setInputEnabled(false);
    }

    if (data.done) {
      /* Render accumulated text as markdown */
      if (this._currentAssistantEl && this._streamRawText && typeof marked !== 'undefined') {
        try {
          /* marked.parse is the standard API for markdown-to-HTML rendering */
          const rendered = marked.parse(this._streamRawText);
          this._currentAssistantEl.innerHTML = rendered; // eslint-disable-line no-unsanitized/property
          this._renderHtmlBlocks(this._currentAssistantEl);
        } catch {}
      }
      this._currentAssistantEl = null;
      this._streamRawText = '';
      this._inHtmlBlock = false;
      this._isStreaming = false;
      _secSignalRunning(false);
      this._setInputEnabled(true);
      this._input.focus();
    } else if (data.token) {
      this._streamRawText += data.token;

      /* Hide HTML code blocks during streaming */
      if (!this._inHtmlBlock) {
        if (this._streamRawText.includes('```html')) {
          const before = this._streamRawText.split('```html')[0];
          this._currentAssistantEl.textContent = before + '\n\u2728 \uC2DC\uAC01\uD654 \uC0DD\uC131 \uC911...';
          this._inHtmlBlock = true;
        } else {
          this._currentAssistantEl.textContent += data.token;
        }
      } else {
        const afterOpen = this._streamRawText.split('```html').slice(1).join('```html');
        if (afterOpen.includes('```')) {
          const before = this._streamRawText.split('```html')[0];
          this._currentAssistantEl.textContent = before + '\n\u2728 \uC2DC\uAC01\uD654 \uC900\uBE44 \uC644\uB8CC';
        }
      }
      this._chat.scrollTop = this._chat.scrollHeight;
    }
  }

  /* ── Task injection card handlers ── */

  _handleTaskStarted(data) {
    const { task_id, mode, description } = data;
    const modeLabel = mode === 'company' ? 'AI Company' : 'AI Discussion';

    const card = document.createElement('div');
    card.className = 'sec-task-card';
    card.dataset.taskId = task_id;

    const header = document.createElement('div');
    header.className = 'task-header';
    const badge = document.createElement('span');
    badge.className = 'task-mode-badge';
    badge.textContent = modeLabel;
    const goBtn = document.createElement('button');
    goBtn.className = 'task-go-btn';
    goBtn.textContent = '\uBCF4\uB7EC\uAC00\uAE30 \u2192';
    goBtn.addEventListener('click', () => {
      if (window._modeManager) {
        const targetMode = mode === 'company' ? 'company' : 'discussion';
        window._modeManager.selectMode(targetMode, { inject: task_id });
      }
    });
    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'task-cancel-btn';
    cancelBtn.textContent = '\uCDE8\uC18C';
    cancelBtn.addEventListener('click', () => this._cancelTask(task_id));
    header.appendChild(badge);
    header.appendChild(goBtn);
    header.appendChild(cancelBtn);

    const desc = document.createElement('div');
    desc.className = 'task-desc';
    desc.textContent = description;

    const bar = document.createElement('div');
    bar.className = 'task-progress-bar';
    const fill = document.createElement('div');
    fill.className = 'task-progress-fill';
    fill.style.width = '5%';
    bar.appendChild(fill);

    const statusText = document.createElement('div');
    statusText.className = 'task-status-text';
    statusText.textContent = '\uC2E4\uD589 \uC911...';

    card.appendChild(header);
    card.appendChild(desc);
    card.appendChild(bar);
    card.appendChild(statusText);
    this._chat.appendChild(card);
    this._chat.scrollTop = this._chat.scrollHeight;
  }

  _handleTaskProgress(data) {
    const { task_id, progress, status } = data;
    const card = this._chat.querySelector(`.sec-task-card[data-task-id="${task_id}"]`);
    if (!card) return;

    const fill = card.querySelector('.task-progress-fill');
    if (fill) fill.style.width = Math.round(progress * 100) + '%';

    const statusEl = card.querySelector('.task-status-text');
    if (statusEl) statusEl.textContent = status || '\uC2E4\uD589 \uC911...';
  }

  _handleTaskComplete(data) {
    const { task_id, summary, report_path } = data;
    const card = this._chat.querySelector(`.sec-task-card[data-task-id="${task_id}"]`);
    if (!card) return;

    card.classList.add('completed');

    const fill = card.querySelector('.task-progress-fill');
    if (fill) fill.style.width = '100%';

    const statusEl = card.querySelector('.task-status-text');
    if (statusEl) statusEl.textContent = '\uC644\uB8CC';

    const cancelBtn = card.querySelector('.task-cancel-btn');
    if (cancelBtn) cancelBtn.disabled = true;

    if (summary) {
      const result = document.createElement('div');
      result.className = 'task-result';
      result.textContent = summary;
      card.appendChild(result);
    }

    if (report_path) {
      const link = document.createElement('a');
      link.className = 'task-report-link';
      link.textContent = '\uD83D\uDCC4 \uB9AC\uD3EC\uD2B8 \uBCF4\uAE30';
      link.href = report_path;
      link.target = '_blank';
      card.appendChild(link);
    }

    this._chat.scrollTop = this._chat.scrollHeight;
  }

  _cancelTask(taskId) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({
      type: 'sec_cancel_task',
      data: { task_id: taskId }
    }));
    const card = this._chat.querySelector(`.sec-task-card[data-task-id="${taskId}"]`);
    if (card) {
      const statusEl = card.querySelector('.task-status-text');
      if (statusEl) statusEl.textContent = '\uCDE8\uC18C \uC911...';
      const cancelBtn = card.querySelector('.task-cancel-btn');
      if (cancelBtn) cancelBtn.disabled = true;
    }
  }

  /* ── Calendar & Report event handlers ── */

  _handleCalendarEvent(data) {
    /* Calendar events are informational — the actual text response
       comes via sec_stream. */
  }

  _handleReportEvent(data) {
    const { saved_path, filename } = data;
    if (!saved_path) return;
    const el = document.createElement('div');
    el.className = 'sec-msg sec-msg-ai';
    const link = document.createElement('a');
    link.href = saved_path;
    link.target = '_blank';
    link.style.cssText = 'color:var(--cv-accent);text-decoration:none;';
    link.textContent = '\uD83D\uDCC4 ' + (filename || '\uB9AC\uD3EC\uD2B8 \uC5F4\uAE30');
    el.appendChild(link);
    this._chat.appendChild(el);
    this._chat.scrollTop = this._chat.scrollHeight;
  }

  _handleHistoryRestored(data) {
    const { messages, count } = data;
    if (!messages || !messages.length) return;
    this._hasMessages = true;

    /* Remove "loading" system message */
    const last = this._chat.lastElementChild;
    if (last && last.textContent.includes('\uBD88\uB7EC\uC624\uB294')) last.remove();

    /* Render each restored message */
    for (const m of messages) {
      if (m.role === 'user') {
        const el = document.createElement('div');
        el.className = 'sec-msg sec-msg-user';
        el.textContent = m.content;
        this._chat.appendChild(el);
      } else if (m.role === 'assistant') {
        const el = document.createElement('div');
        el.className = 'sec-msg sec-msg-ai';
        if (typeof marked !== 'undefined') {
          try {
            el.innerHTML = marked.parse(m.content); // eslint-disable-line no-unsanitized/property
            this._renderHtmlBlocks(el);
          } catch { el.textContent = m.content; }
        } else {
          el.textContent = m.content;
        }
        this._chat.appendChild(el);
      }
    }

    /* Add separator */
    const sep = document.createElement('div');
    sep.className = 'sec-msg sec-msg-ai';
    sep.style.cssText = 'text-align:center;color:var(--cv-dim);font-size:11px;max-width:100%;background:transparent;border:none;';
    sep.textContent = '--- \uC774\uC804 \uB300\uD654 ' + count + '\uAC1C \uBCF5\uC6D0\uB428 ---';
    this._chat.appendChild(sep);
    this._chat.scrollTop = this._chat.scrollHeight;
  }

  _sendMessage() {
    const text = this._input.value.trim();
    if (!text || this._isStreaming) return;
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      this._addSystemMessage('\uC11C\uBC84\uC640 \uC5F0\uACB0\uC774 \uB04A\uC5B4\uC84C\uC2B5\uB2C8\uB2E4. \uD398\uC774\uC9C0\uB97C \uC0C8\uB85C\uACE0\uCE68\uD574\uC8FC\uC138\uC694.');
      return;
    }

    /* Add user bubble */
    this._hasMessages = true;
    const userEl = document.createElement('div');
    userEl.className = 'sec-msg sec-msg-user';
    userEl.textContent = text;
    this._chat.appendChild(userEl);

    /* Show typing indicator */
    const typing = document.createElement('div');
    typing.className = 'sec-typing';
    const icon = document.createElement('span');
    icon.style.cssText = 'color:var(--cv-accent);font-weight:600;';
    icon.textContent = this._chatMode === 'think' ? '\uD83D\uDCA1' : '\u26A1';
    typing.appendChild(icon);
    typing.appendChild(document.createTextNode(' '));
    const dots = document.createElement('div');
    dots.className = 'typing-dots';
    for (let i = 0; i < 3; i++) dots.appendChild(document.createElement('span'));
    typing.appendChild(dots);
    this._chat.appendChild(typing);

    this._chat.scrollTop = this._chat.scrollHeight;

    /* Send to server */
    this.ws.send(JSON.stringify({
      type: 'sec_message',
      data: { content: text, mode: this._chatMode }
    }));

    /* Clear input */
    this._input.value = '';
    this._input.style.height = 'auto';
  }

  _addSystemMessage(text) {
    const el = document.createElement('div');
    el.className = 'sec-msg sec-msg-ai';
    el.style.cssText = 'font-size:12px;color:var(--cv-dim);background:transparent;border:none;max-width:100%;';
    el.textContent = text;
    this._chat.appendChild(el);
    this._chat.scrollTop = this._chat.scrollHeight;
  }

  _setInputEnabled(enabled) {
    this._input.disabled = !enabled;
    this._sendBtn.disabled = !enabled;
  }

  /** Replace HTML code blocks with sandboxed iframes */
  _renderHtmlBlocks(container) {
    container.querySelectorAll('pre code.language-html').forEach(codeEl => {
      const html = codeEl.textContent;
      if (!html || html.length < 20) return;
      const pre = codeEl.closest('pre');
      if (!pre) return;

      const wrapper = document.createElement('div');
      wrapper.className = 'sec-visual-frame';
      const iframe = document.createElement('iframe');
      iframe.sandbox = 'allow-scripts';
      iframe.srcdoc = html;
      iframe.style.cssText = 'width:100%;border:none;border-radius:12px;min-height:120px;';
      iframe.scrolling = 'no';

      iframe.addEventListener('load', () => {
        try {
          const h = iframe.contentDocument.documentElement.scrollHeight;
          iframe.style.height = Math.min(h + 8, 500) + 'px';
        } catch { iframe.style.height = '200px'; }
      });

      wrapper.appendChild(iframe);
      pre.replaceWith(wrapper);
    });
  }
}
