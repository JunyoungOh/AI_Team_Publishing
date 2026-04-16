'use strict';

/* Sidebar "running" indicator signal — see mode-chatbot.js for receiver. */
function _discSignalRunning(on) {
  try {
    if (window.chatbotSignal) window.chatbotSignal('discussion', on);
  } catch (_) { /* noop */ }
}

/* ═══════════════════════════════════════════════════
   §  DISCUSSION MANAGER — 4-split grid rewrite
   Setup → Live (4-split) → Report, all inside card shell
   ═══════════════════════════════════════════════════ */
class DiscussionManager {
  /* ── Built-in presets (read-only) ─────────────────── */
  static BUILTIN_PRESETS = [
    {
      id: '__builtin_ai_jobs',
      name: 'AI 일자리 대체 토론 (4인)',
      topic: 'AI가 인간의 일자리를 대체할 것인가?',
      style: 'debate',
      time_limit_min: 15,
      participants: [
        { name: '김교수', persona: 'AI 윤리 전문가, 보수적이고 학술적인 관점. 기술 발전의 사회적 영향을 연구해왔으며, 윤리적 프레임워크를 중시한다.' },
        { name: '이대표', persona: 'IT 스타트업 CEO, 진보적이고 실용적인 관점. AI 도입으로 생산성을 높인 경험이 있으며, 기술 낙관론자이다.' },
        { name: '김노동', persona: '제조공장에서 10년을 일한 블루칼라 노동자. 평생 컴퓨터와는 상관없이 필드에서 몸을 이용해 일해왔다. 현장 노동자의 관점에서 AI 자동화를 바라본다.' },
        { name: '이주임', persona: '회사원 10년차 화이트칼라 노동자. 컴퓨터를 사용하는 사무 업무 위주로 근무해왔고, AI 도입에 관심이 있으면서도 자리 위협을 느끼고 있다.' },
      ],
    },
  ];

  static PARTICIPANT_COLORS = ['#4A90D9', '#E94560', '#2ECC71', '#9B59B6', '#F39C12', '#1ABC9C'];

  constructor() {
    this.ws = null;
    this._container = null;
    this._timerIv = null;
    this._startTime = 0;
    this._timeLimitSec = 0;
    this._participantColors = {};
    this._participantNames = {};
    this._participantPersonas = {};
    this._participantOrder = [];  // ordered array of participant IDs
    this._lastSpeakerId = null;
    this._presets = this._loadPresets();
    this._discMode = 'basic';
    this._step3Loading = false;
    this._step3Participants = null;
    this._clonePersonas = [];
    this._selectedCloneIds = [];
    this._discConnected = false;
    this._lastError = null;
    this._injectionMode = false;
    this._humanTimerIv = null;
    this._reportShown = false;
    this._reportTimerIv = null;
    this._reportTimerStart = 0;
    this._reportStageText = '';
  }

  /* ═══════════════════════════════════════════════════
     Shell integration — called by CardView._bootMode()
     ═══════════════════════════════════════════════════ */

  static mountInShell(container) {
    if (!DiscussionManager._instance) {
      DiscussionManager._instance = new DiscussionManager();
    }
    DiscussionManager._instance._mount(container);
  }

  _mount(container) {
    this._container = container;
    while (container.firstChild) container.removeChild(container.firstChild);
    this._showSetup();
  }

  /* ═══════════════════════════════════════════════════
     §  SETUP SCREEN — rendered inside container
     ═══════════════════════════════════════════════════ */

  _showSetup() {
    var c = this._container;
    while (c.firstChild) c.removeChild(c.firstChild);
    var self = this;

    var setup = document.createElement('div');
    setup.className = 'disc-setup';

    /* ── Header (spans full width) ── */
    var header = document.createElement('div');
    header.className = 'disc-setup-header';
    var title = document.createElement('div');
    title.className = 'disc-setup-title';
    title.textContent = 'AI \uD1A0\uB860';
    header.appendChild(title);
    var subtitle = document.createElement('div');
    subtitle.className = 'disc-setup-subtitle';
    subtitle.textContent = '\uD1A0\uB860 \uC8FC\uC81C\uC640 \uCC38\uAC00\uC790\uB97C \uC124\uC815\uD558\uACE0 \uC2DC\uC791\uD558\uC138\uC694';
    header.appendChild(subtitle);
    setup.appendChild(header);

    /* ── Left Column: Settings ── */
    var left = document.createElement('div');
    left.className = 'disc-setup-left';

    // Mode select
    var modeSection = document.createElement('div');
    modeSection.className = 'disc-setup-section';
    var modeLabel = document.createElement('label');
    modeLabel.className = 'disc-label';
    modeLabel.textContent = '\uD1A0\uB860 \uBC29\uC2DD';
    modeSection.appendChild(modeLabel);
    var modeSelect = document.createElement('div');
    modeSelect.className = 'disc-mode-select';
    var modes = [
      { id: 'basic', label: 'AI \uD1A0\uB860' },
      { id: 'participate', label: '\uCC38\uC5EC\uD615 \uD1A0\uB860' },
      // 'clone' (\uD398\uB974\uC18C\uB098 \uD074\uB860\uB2DD) parked \u2014 \uC5C5\uADF8\uB808\uC774\uB4DC \uD6C4 \uC7AC\uB178\uCD9C \uC608\uC815
    ];
    modes.forEach(function(m) {
      var btn = document.createElement('button');
      btn.className = 'disc-mode-btn' + (self._discMode === m.id ? ' active' : '');
      btn.textContent = m.label;
      btn.addEventListener('click', function() {
        modeSelect.querySelectorAll('.disc-mode-btn').forEach(function(b) { b.classList.remove('active'); });
        btn.classList.add('active');
        self._discMode = m.id;
        self._updateParticipantSection();
      });
      modeSelect.appendChild(btn);
    });
    modeSection.appendChild(modeSelect);
    left.appendChild(modeSection);

    // Topic
    var topicSection = document.createElement('div');
    topicSection.className = 'disc-setup-section';
    var topicLabel = document.createElement('label');
    topicLabel.className = 'disc-label';
    topicLabel.textContent = '\uD1A0\uB860 \uC8FC\uC81C';
    topicSection.appendChild(topicLabel);
    var topicArea = document.createElement('textarea');
    topicArea.id = 'disc-topic';
    topicArea.rows = 4;
    topicArea.placeholder = '\uC608: AI\uAC00 \uC778\uAC04\uC758 \uC77C\uC790\uB9AC\uB97C \uB300\uCCB4\uD560 \uAC83\uC778\uAC00?';
    topicArea.style.cssText = 'width:100%;background:var(--cv-bg);border:1px solid var(--cv-border);border-radius:8px;color:var(--cv-text);padding:12px;font-size:14px;resize:none;font-family:inherit;box-sizing:border-box;line-height:1.5;';
    topicSection.appendChild(topicArea);
    left.appendChild(topicSection);

    // Style + Time row
    var rowSection = document.createElement('div');
    rowSection.className = 'disc-setup-section';
    rowSection.style.cssText = 'display:flex;gap:20px;';
    var styleDiv = document.createElement('div');
    styleDiv.style.flex = '1';
    var styleLbl = document.createElement('label');
    styleLbl.className = 'disc-label';
    styleLbl.textContent = '\uD1A0\uB860 \uC2A4\uD0C0\uC77C';
    styleDiv.appendChild(styleLbl);
    var styleSelect = document.createElement('div');
    styleSelect.className = 'disc-style-select';
    [{ id: 'free', label: '\uC790\uC720' }, { id: 'debate', label: '\uCC2C\uBC18' }, { id: 'brainstorm', label: '\uBE0C\uB808\uC778\uC2A4\uD1A0\uBC0D' }].forEach(function(s) {
      var btn = document.createElement('button');
      btn.className = 'disc-style-btn' + (s.id === 'free' ? ' active' : '');
      btn.dataset.style = s.id;
      btn.textContent = s.label;
      btn.addEventListener('click', function() {
        styleSelect.querySelectorAll('.disc-style-btn').forEach(function(b) { b.classList.remove('active'); });
        btn.classList.add('active');
      });
      styleSelect.appendChild(btn);
    });
    styleDiv.appendChild(styleSelect);
    rowSection.appendChild(styleDiv);

    var timeDiv = document.createElement('div');
    var timeLbl = document.createElement('label');
    timeLbl.className = 'disc-label';
    timeLbl.textContent = '\uC2DC\uAC04 \uC81C\uD55C';
    timeDiv.appendChild(timeLbl);
    var timeRow = document.createElement('div');
    timeRow.style.cssText = 'display:flex;align-items:center;gap:6px;';
    var timeInput = document.createElement('input');
    timeInput.id = 'disc-time';
    timeInput.type = 'number';
    timeInput.value = '15';
    timeInput.min = '3';
    timeInput.max = '60';
    timeInput.style.cssText = 'width:64px;background:var(--cv-bg);border:1px solid var(--cv-border);border-radius:6px;color:var(--cv-text);padding:8px;font-size:13px;text-align:center;';
    timeRow.appendChild(timeInput);
    var timeUnit = document.createElement('span');
    timeUnit.style.cssText = 'font-size:12px;color:var(--cv-dim);';
    timeUnit.textContent = '\uBD84';
    timeRow.appendChild(timeUnit);
    timeDiv.appendChild(timeRow);
    rowSection.appendChild(timeDiv);
    left.appendChild(rowSection);

    // Presets
    var presetSection = document.createElement('div');
    presetSection.className = 'disc-setup-section';
    presetSection.id = 'disc-presets-area';
    var presetLabel = document.createElement('label');
    presetLabel.className = 'disc-label';
    presetLabel.textContent = '\uBE60\uB978 \uC2DC\uC791';
    presetSection.appendChild(presetLabel);
    left.appendChild(presetSection);
    setup.appendChild(left);

    /* ── Right Column: Participants ── */
    var right = document.createElement('div');
    right.className = 'disc-setup-right';

    var partSection = document.createElement('div');
    partSection.className = 'disc-setup-section';
    partSection.id = 'disc-participant-section';
    partSection.style.flex = '1';
    var partHeader = document.createElement('div');
    partHeader.style.cssText = 'display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;';
    var partLabel = document.createElement('label');
    partLabel.className = 'disc-label';
    partLabel.style.marginBottom = '0';
    partLabel.textContent = '\uCC38\uAC00\uC790 (\uCD5C\uB300 4\uBA85)';
    partHeader.appendChild(partLabel);
    var addBtn = document.createElement('button');
    addBtn.className = 'disc-mode-btn';
    addBtn.textContent = '+ \uCD94\uAC00';
    addBtn.addEventListener('click', function() { self._addParticipantCard(); });
    partHeader.appendChild(addBtn);
    partSection.appendChild(partHeader);
    var partCards = document.createElement('div');
    partCards.className = 'disc-participants';
    partCards.id = 'disc-participant-cards';
    partSection.appendChild(partCards);
    right.appendChild(partSection);

    var rightActions = document.createElement('div');
    rightActions.style.cssText = 'display:flex;gap:8px;';
    var recommendBtn = document.createElement('button');
    recommendBtn.className = 'disc-mode-btn';
    recommendBtn.id = 'disc-recommend-btn';
    recommendBtn.textContent = '\uCC38\uAC00\uC790 \uCD94\uCC9C';
    recommendBtn.addEventListener('click', function() { self._recommendParticipants(); });
    rightActions.appendChild(recommendBtn);
    var savePresetBtn = document.createElement('button');
    savePresetBtn.className = 'disc-mode-btn';
    savePresetBtn.textContent = '\uD504\uB9AC\uC14B \uC800\uC7A5';
    savePresetBtn.addEventListener('click', function() { self._saveCurrentAsPreset(); });
    rightActions.appendChild(savePresetBtn);
    right.appendChild(rightActions);
    setup.appendChild(right);

    /* ── Footer (spans full width) ── */
    var footer = document.createElement('div');
    footer.className = 'disc-setup-footer';
    var startBtn = document.createElement('button');
    startBtn.className = 'disc-start-btn';
    startBtn.textContent = '\uD1A0\uB860 \uC2DC\uC791';
    startBtn.id = 'disc-start-btn';
    startBtn.addEventListener('click', function() { self._startDiscussion(); });
    footer.appendChild(startBtn);
    setup.appendChild(footer);

    /* ── Recent reports (1-week retention) — full width below footer ── */
    var historySection = document.createElement('div');
    historySection.className = 'disc-history-section';
    historySection.id = 'disc-history-section';
    var hHeader = document.createElement('div');
    hHeader.className = 'disc-history-header';
    var hTitle = document.createElement('div');
    hTitle.className = 'disc-history-title';
    hTitle.textContent = '\uCD5C\uADFC \uD1A0\uB860 (\uCD5C\uADFC 1\uC8FC\uC77C)';
    hHeader.appendChild(hTitle);
    var hRefresh = document.createElement('button');
    hRefresh.className = 'disc-history-refresh';
    hRefresh.title = '\uC0C8\uB85C\uACE0\uCE68';
    hRefresh.textContent = '\u21BB';
    hRefresh.addEventListener('click', function() { self._loadHistory(); });
    hHeader.appendChild(hRefresh);
    historySection.appendChild(hHeader);
    var hList = document.createElement('div');
    hList.className = 'disc-history-list';
    hList.id = 'disc-history-list';
    historySection.appendChild(hList);
    setup.appendChild(historySection);

    c.appendChild(setup);

    /* Render initial participants + presets + history */
    this._renderDefaultParticipants();
    this._renderPresetsInSetup();
    this._loadHistory();
  }

  /* ═══════════════════════════════════════════════════
     §  HISTORY VIEWER — fetches /api/reports/discussion
     ═══════════════════════════════════════════════════ */

  async _loadHistory() {
    var listEl = document.getElementById('disc-history-list');
    if (!listEl) return;
    listEl.textContent = '\uBD88\uB7EC\uC624\uB294 \uC911...';
    try {
      var resp = await fetch('/api/reports/discussion', { credentials: 'same-origin' });
      if (!resp.ok) {
        listEl.textContent = resp.status === 401
          ? '\uB85C\uADF8\uC778\uC774 \uD544\uC694\uD569\uB2C8\uB2E4.'
          : '\uBD88\uB7EC\uC624\uAE30 \uC2E4\uD328 (HTTP ' + resp.status + ')';
        return;
      }
      var data = await resp.json();
      this._renderHistoryList(data.reports || []);
    } catch (e) {
      listEl.textContent = '\uB124\uD2B8\uC6CC\uD06C \uC624\uB958: ' + (e && e.message || e);
    }
  }

  _renderHistoryList(reports) {
    var listEl = document.getElementById('disc-history-list');
    if (!listEl) return;
    while (listEl.firstChild) listEl.removeChild(listEl.firstChild);

    if (!reports.length) {
      var empty = document.createElement('div');
      empty.className = 'disc-history-empty';
      empty.textContent = '\uC544\uC9C1 \uC800\uC7A5\uB41C \uD1A0\uB860 \uB9AC\uD3EC\uD2B8\uAC00 \uC5C6\uC2B5\uB2C8\uB2E4. \uC0C8 \uD1A0\uB860\uC744 \uC2DC\uC791\uD574 \uBCF4\uC138\uC694.';
      listEl.appendChild(empty);
      return;
    }

    var self = this;
    reports.forEach(function(r) {
      var row = document.createElement('div');
      row.className = 'disc-history-row';

      var info = document.createElement('div');
      info.className = 'disc-history-info';
      var topic = document.createElement('div');
      topic.className = 'disc-history-topic';
      topic.textContent = r.topic;
      info.appendChild(topic);
      var meta = document.createElement('div');
      meta.className = 'disc-history-meta';
      var participantsStr = (r.participants || []).join(' \u00B7 ');
      var when = self._formatRelativeTime(r.created_at);
      var styleLabels = { free: '\uC790\uC720', debate: '\uCC2C\uBC18', brainstorm: '\uBE0C\uB808\uC778\uC2A4\uD1A0\uBC0D' };
      var styleStr = styleLabels[r.style] || r.style;
      meta.textContent = when + ' \u2022 ' + styleStr + ' \u2022 ' + participantsStr;
      info.appendChild(meta);
      row.appendChild(info);

      var actions = document.createElement('div');
      actions.className = 'disc-history-actions';

      var openBtn = document.createElement('button');
      openBtn.className = 'disc-history-btn';
      openBtn.textContent = '\uC5F4\uAE30';
      openBtn.title = '\uC0C8 \uD0ED\uC5D0\uC11C \uC5F4\uAE30';
      openBtn.addEventListener('click', function() {
        if (r.file_path) window.open(r.file_path, '_blank', 'noopener');
      });
      actions.appendChild(openBtn);

      var dlBtn = document.createElement('button');
      dlBtn.className = 'disc-history-btn';
      dlBtn.textContent = '\uB2E4\uC6B4\uB85C\uB4DC';
      dlBtn.addEventListener('click', function() {
        if (!r.file_path) return;
        var a = document.createElement('a');
        a.href = r.file_path;
        var date = (r.created_at || '').slice(0, 10);
        a.download = '\uD1A0\uB860\uB9AC\uD3EC\uD2B8_' + (r.topic || 'discussion').slice(0, 30) + '_' + date + '.html';
        document.body.appendChild(a);
        a.click();
        a.remove();
      });
      actions.appendChild(dlBtn);

      var delBtn = document.createElement('button');
      delBtn.className = 'disc-history-btn disc-history-del';
      delBtn.textContent = '\u00D7';
      delBtn.title = '\uC0AD\uC81C';
      delBtn.addEventListener('click', async function() {
        if (!confirm('\uC774 \uB9AC\uD3EC\uD2B8\uB97C \uC0AD\uC81C\uD560\uAE4C\uC694?')) return;
        try {
          var resp = await fetch('/api/reports/discussion/' + encodeURIComponent(r.id), {
            method: 'DELETE',
            credentials: 'same-origin',
          });
          if (resp.ok) self._loadHistory();
        } catch (_) { /* ignore */ }
      });
      actions.appendChild(delBtn);

      row.appendChild(actions);
      listEl.appendChild(row);
    });
  }

  _formatRelativeTime(isoStr) {
    if (!isoStr) return '';
    var t = new Date(isoStr).getTime();
    if (isNaN(t)) return isoStr;
    var diffSec = Math.floor((Date.now() - t) / 1000);
    if (diffSec < 60)   return '\uBC29\uAE08';
    if (diffSec < 3600) return Math.floor(diffSec / 60) + '\uBD84 \uC804';
    if (diffSec < 86400) return Math.floor(diffSec / 3600) + '\uC2DC\uAC04 \uC804';
    if (diffSec < 7 * 86400) return Math.floor(diffSec / 86400) + '\uC77C \uC804';
    return new Date(isoStr).toLocaleDateString('ko-KR');
  }

  _getSelectedStyle() {
    var active = this._container.querySelector('.disc-style-btn.active');
    return active ? active.dataset.style : 'free';
  }

  _renderDefaultParticipants() {
    var cards = document.getElementById('disc-participant-cards');
    if (!cards) return;
    while (cards.firstChild) cards.removeChild(cards.firstChild);
    if (this._discMode === 'participate') {
      this._addHumanParticipantCard();
    }
    var defaults = [
      { name: '\uCC38\uAC00\uC790 A', persona: '\uC8FC\uC81C\uC5D0 \uB300\uD55C \uC804\uBB38\uC801 \uAD00\uC810' },
      { name: '\uCC38\uAC00\uC790 B', persona: '\uB2E4\uB978 \uC2DC\uAC01\uC5D0\uC11C\uC758 \uBD84\uC11D' },
    ];
    var self = this;
    defaults.forEach(function(p) { self._addParticipantCard(p); });
  }

  _addParticipantCard(data) {
    var cards = document.getElementById('disc-participant-cards');
    if (!cards) return;
    var aiCount = cards.querySelectorAll('.disc-participant-card:not(.is-human)').length;
    if (aiCount >= 4) return;

    var p = data || { name: '', persona: '' };
    var idx = aiCount + 1;
    var card = document.createElement('div');
    card.className = 'disc-participant-card';

    var topRow = document.createElement('div');
    topRow.className = 'disc-pc-row';
    var numBadge = document.createElement('div');
    numBadge.className = 'disc-pc-number';
    numBadge.textContent = idx;
    topRow.appendChild(numBadge);

    var nameInput = document.createElement('input');
    nameInput.placeholder = '\uC774\uB984';
    nameInput.value = p.name || '';
    topRow.appendChild(nameInput);
    card.appendChild(topRow);

    var personaArea = document.createElement('textarea');
    personaArea.placeholder = '\uD398\uB974\uC18C\uB098 \uC124\uBA85 (\uC5ED\uD560, \uC804\uBB38\uC131, \uC131\uACA9 \uB4F1)';
    personaArea.rows = 2;
    personaArea.value = p.persona || '';

    var removeBtn = document.createElement('button');
    removeBtn.className = 'disc-participant-remove';
    removeBtn.textContent = '\u00D7';
    removeBtn.addEventListener('click', function() {
      var aiCountNow = cards.querySelectorAll('.disc-participant-card:not(.is-human)').length;
      if (aiCountNow <= 2) return;
      card.remove();
    });

    card.appendChild(personaArea);
    card.appendChild(removeBtn);
    cards.appendChild(card);
  }

  _addHumanParticipantCard() {
    var cards = document.getElementById('disc-participant-cards');
    if (!cards) return;
    if (cards.querySelector('.disc-participant-card.is-human')) return;

    var card = document.createElement('div');
    card.className = 'disc-participant-card is-human';

    var topRow = document.createElement('div');
    topRow.className = 'disc-pc-row';
    var badge = document.createElement('div');
    badge.className = 'disc-pc-number disc-pc-human-badge';
    badge.textContent = '\uB098';  /* "\uB098" = \uB098 */
    topRow.appendChild(badge);

    var nameInput = document.createElement('input');
    nameInput.className = 'disc-human-name';
    nameInput.placeholder = '\uB098\uC758 \uC774\uB984 (\uC608: \uAE40\uC900\uC601)';
    topRow.appendChild(nameInput);
    card.appendChild(topRow);

    var personaArea = document.createElement('textarea');
    personaArea.className = 'disc-human-persona';
    personaArea.rows = 2;
    personaArea.placeholder = '\uB098\uC758 \uC785\uC7A5\u00B7\uBC30\uACBD\uC744 \uC790\uC720\uB86D\uAC8C (\uC608: \uB2E8\uC21C\uD568\uC744 \uCD94\uAD6C\uD558\uB294 10\uB144\uCC28 \uAC1C\uBC1C\uC790)';
    card.appendChild(personaArea);

    cards.appendChild(card);
  }

  _getHumanParticipantData() {
    var card = this._container.querySelector('#disc-participant-cards .disc-participant-card.is-human');
    if (!card) return null;
    var nameInput = card.querySelector('.disc-human-name');
    var personaArea = card.querySelector('.disc-human-persona');
    var rawName = nameInput ? nameInput.value.trim() : '';
    var rawPersona = personaArea ? personaArea.value.trim() : '';
    /* \uBE48\uCE78 \uCC98\uB9AC \uADDC\uCE59:
       - \uC774\uB984: \uBE44\uBA74 "\uC0AC\uC6A9\uC790"\uB85C \uBCF4\uCDA9
       - \uD398\uB974\uC18C\uB098: \uBE48 \uBB38\uC790\uC5F4 \uADF8\uB300\uB85C \uC804\uC1A1 (\uBC31\uC5D4\uB4DC moderator.py:33\uC5D0\uC11C
         "\uC2E4\uC81C \uC0AC\uC6A9\uC790 (AI\uAC00 \uC544\uB2D8)" \uD78C\uD2B8\uB97C \uC790\uB3D9 \uC0BD\uC785\uD558\uBBC0\uB85C \uB36E\uC5B4\uC4F0\uBA74 \uC548 \uB428)
       \uB354 \uC5C4\uACA9\uD558\uAC8C \uD558\uACE0 \uC2F6\uC73C\uBA74 null \uBC18\uD658 \u2014 \uD638\uCD9C\uBD80(_startDiscussion)\uAC00
       \uC774\uBBF8 null\uC744 \uAC10\uC9C0\uD574 \uC2DC\uC791\uC744 \uCC28\uB2E8\uD558\uB3C4\uB85D \uC5F0\uACB0\uB418\uC5B4 \uC788\uC74C. */
    var name = rawName || '\uC0AC\uC6A9\uC790';
    var persona = rawPersona;  /* \uBE48 \uBB38\uC790\uC5F4 \uADF8\uB300\uB85C \u2014 \uBC31\uC5D4\uB4DC\uAC00 "\uC2E4\uC81C \uC0AC\uC6A9\uC790" \uD78C\uD2B8\uB85C \uB300\uCCB4 */
    return { name: name, persona: persona };
  }

  _updateParticipantSection() {
    var section = document.getElementById('disc-participant-section');
    if (!section) return;
    var self = this;

    if (this._discMode === 'clone') {
      while (section.firstChild) section.removeChild(section.firstChild);
      var label = document.createElement('label');
      label.className = 'disc-label';
      label.textContent = '\uD398\uB974\uC18C\uB098 \uC120\uD0DD';
      section.appendChild(label);
      var hint = document.createElement('div');
      hint.style.cssText = 'font-size:11px;color:var(--cv-dim);margin-bottom:8px;';
      hint.textContent = '\uD398\uB974\uC18C\uB098 \uC6CC\uD06C\uC0F5\uC5D0\uC11C \uC0DD\uC131\uD55C \uC778\uBB3C\uC744 \uBD88\uB7EC\uC635\uB2C8\uB2E4';
      section.appendChild(hint);
      var cardsDiv = document.createElement('div');
      cardsDiv.className = 'disc-participants';
      cardsDiv.id = 'disc-clone-cards';
      section.appendChild(cardsDiv);
      var addBtnClone = document.createElement('button');
      addBtnClone.className = 'disc-mode-btn';
      addBtnClone.textContent = '+ \uD398\uB974\uC18C\uB098 \uCD94\uAC00';
      addBtnClone.addEventListener('click', function() { self._showCloneDropdown(addBtnClone, cardsDiv); });
      section.appendChild(addBtnClone);
      this._loadClonePersonas();
    } else {
      while (section.firstChild) section.removeChild(section.firstChild);
      var partHeader = document.createElement('div');
      partHeader.style.cssText = 'display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;';
      var partLabel = document.createElement('label');
      partLabel.className = 'disc-label';
      partLabel.style.marginBottom = '0';
      partLabel.textContent = '\uCC38\uAC00\uC790 (\uCD5C\uB300 4\uBA85)';
      partHeader.appendChild(partLabel);
      var addBtn = document.createElement('button');
      addBtn.className = 'disc-mode-btn';
      addBtn.textContent = '+ \uCD94\uAC00';
      addBtn.addEventListener('click', function() { self._addParticipantCard(); });
      partHeader.appendChild(addBtn);
      section.appendChild(partHeader);
      var partCards = document.createElement('div');
      partCards.className = 'disc-participants';
      partCards.id = 'disc-participant-cards';
      section.appendChild(partCards);
      this._renderDefaultParticipants();
    }
  }

  async _loadClonePersonas() {
    try {
      var resp = await fetch('/api/personas/usable');
      if (!resp.ok) return;
      var data = await resp.json();
      this._clonePersonas = data.personas || [];
      this._selectedCloneIds = [];
    } catch (e) {
      console.warn('Failed to load personas:', e);
    }
  }

  _showCloneDropdown(anchorEl, cardsContainer) {
    var existing = anchorEl.parentElement.querySelector('.disc-clone-dropdown');
    if (existing) { existing.remove(); return; }
    var self = this;

    var available = this._clonePersonas.filter(function(p) { return self._selectedCloneIds.indexOf(p.id) === -1; });
    if (available.length === 0) return;

    var dropdown = document.createElement('div');
    dropdown.className = 'disc-clone-dropdown';
    dropdown.style.cssText = 'position:absolute;z-index:10;background:var(--cv-surface);border:1px solid var(--cv-border);border-radius:8px;max-height:200px;overflow-y:auto;width:280px;';
    available.forEach(function(persona) {
      var item = document.createElement('div');
      item.style.cssText = 'padding:8px 12px;cursor:pointer;font-size:12px;color:var(--cv-text);border-bottom:1px solid var(--cv-border);';
      item.textContent = '\uD83E\uDDEC ' + persona.name + (persona.summary ? ' \u2014 ' + persona.summary.slice(0, 30) : '');
      item.addEventListener('click', function() {
        self._selectedCloneIds.push(persona.id);
        self._addCloneCard(cardsContainer, persona);
        dropdown.remove();
      });
      dropdown.appendChild(item);
    });
    anchorEl.parentElement.style.position = 'relative';
    anchorEl.parentElement.appendChild(dropdown);
    var closeHandler = function(e) {
      if (!anchorEl.parentElement.contains(e.target)) {
        dropdown.remove();
        document.removeEventListener('click', closeHandler);
      }
    };
    setTimeout(function() { document.addEventListener('click', closeHandler); }, 0);
  }

  _addCloneCard(container, persona) {
    if (container.children.length >= 4) return;
    var self = this;
    var card = document.createElement('div');
    card.className = 'disc-participant-card';
    card.dataset.personaId = persona.id;
    var nameEl = document.createElement('input');
    nameEl.value = persona.name;
    nameEl.readOnly = true;
    nameEl.style.opacity = '0.8';
    var metaEl = document.createElement('textarea');
    metaEl.rows = 1;
    metaEl.value = persona.summary || '\uD398\uB974\uC18C\uB098 \uC6CC\uD06C\uC0F5';
    metaEl.readOnly = true;
    metaEl.style.opacity = '0.7';
    var delBtn = document.createElement('button');
    delBtn.className = 'disc-participant-remove';
    delBtn.textContent = '\u00D7';
    delBtn.addEventListener('click', function() {
      self._selectedCloneIds = self._selectedCloneIds.filter(function(id) { return id !== persona.id; });
      card.remove();
    });
    card.appendChild(nameEl);
    card.appendChild(metaEl);
    card.appendChild(delBtn);
    container.appendChild(card);
  }

  async _recommendParticipants() {
    var topic = document.getElementById('disc-topic');
    if (!topic || !topic.value.trim()) {
      if (topic) {
        topic.style.borderColor = 'var(--red)';
        topic.placeholder = '\uD1A0\uB860 \uC8FC\uC81C\uB97C \uC785\uB825\uD574 \uC8FC\uC138\uC694';
        setTimeout(function() { topic.style.borderColor = ''; }, 2000);
      }
      return;
    }

    var self = this;
    var recBtn = document.getElementById('disc-recommend-btn');
    var startBtn = document.getElementById('disc-start-btn');
    var cards = document.getElementById('disc-participant-cards');

    // Snapshot original state so we can restore on error
    var prevRecText = recBtn ? recBtn.textContent : '';
    var prevStartDisabled = startBtn ? startBtn.disabled : false;

    if (recBtn) {
      recBtn.disabled = true;
      recBtn.classList.add('disc-loading');
      recBtn.textContent = '\u23F3 \uCD94\uCC9C \uC900\uBE44 \uC911...';
    }
    if (startBtn) startBtn.disabled = true;
    if (cards) cards.classList.add('disc-cards-loading');

    try {
      var resp = await fetch('/api/discussion/recommend-participants', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          topic: topic.value.trim(),
          style: this._getSelectedStyle(),
          mode: this._discMode,
          count: 3,
        }),
      });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      var data = await resp.json();
      var participants = data.participants || [];
      var humanSuggestion = data.human_suggestion || null;
      if (participants.length > 0 && cards) {
        while (cards.firstChild) cards.removeChild(cards.firstChild);
        if (self._discMode === 'participate') {
          self._addHumanParticipantCard();
          if (humanSuggestion) {
            var hCard = cards.querySelector('.disc-participant-card.is-human');
            if (hCard) {
              var hName = hCard.querySelector('.disc-human-name');
              var hPersona = hCard.querySelector('.disc-human-persona');
              if (hName && humanSuggestion.name) hName.value = humanSuggestion.name;
              if (hPersona && humanSuggestion.persona) hPersona.value = humanSuggestion.persona;
            }
          }
        }
        participants.forEach(function(p) { self._addParticipantCard(p); });
      }
    } catch (e) {
      console.warn('Recommend API failed:', e);
      if (recBtn) {
        recBtn.textContent = '\u26A0 \uCD94\uCC9C \uC2E4\uD328 \u2014 \uB2E4\uC2DC \uC2DC\uB3C4';
        setTimeout(function() {
          if (recBtn) recBtn.textContent = prevRecText || '\uCC38\uAC00\uC790 \uCD94\uCC9C';
        }, 2500);
      }
    } finally {
      if (recBtn) {
        recBtn.disabled = false;
        recBtn.classList.remove('disc-loading');
        // Only restore label if we didn't already set an error label above
        if (recBtn.textContent.indexOf('\u23F3') === 0) {
          recBtn.textContent = prevRecText || '\uCC38\uAC00\uC790 \uCD94\uCC9C';
        }
      }
      if (startBtn) startBtn.disabled = prevStartDisabled;
      if (cards) cards.classList.remove('disc-cards-loading');
    }
  }

  /* ── Presets ── */
  _loadPresets() {
    try {
      var raw = localStorage.getItem('disc_presets');
      return raw ? JSON.parse(raw) : [];
    } catch (e) { return []; }
  }

  _savePresets() {
    localStorage.setItem('disc_presets', JSON.stringify(this._presets));
  }

  _getAllPresets() {
    return [].concat(DiscussionManager.BUILTIN_PRESETS, this._presets);
  }

  _renderPresetsInSetup() {
    var area = document.getElementById('disc-presets-area');
    if (!area) return;
    while (area.firstChild) area.removeChild(area.firstChild);
    var self = this;

    var builtin = DiscussionManager.BUILTIN_PRESETS || [];
    var user = this._presets || [];

    /* ── 빠른 시작 (빌트인) ── */
    if (builtin.length > 0) {
      var qsLabel = document.createElement('label');
      qsLabel.className = 'disc-label';
      qsLabel.textContent = '\uBE60\uB978 \uC2DC\uC791';
      area.appendChild(qsLabel);

      var qsRow = document.createElement('div');
      qsRow.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;';
      builtin.forEach(function(preset) {
        qsRow.appendChild(self._buildPresetCard(preset, false));
      });
      area.appendChild(qsRow);
    }

    /* ── 내 프리셋 (사용자 저장) ── */
    var myLabel = document.createElement('label');
    myLabel.className = 'disc-label';
    myLabel.textContent = '\uB0B4 \uD504\uB9AC\uC14B';
    area.appendChild(myLabel);

    if (user.length === 0) {
      var emptyMsg = document.createElement('div');
      emptyMsg.style.cssText = 'font-size:11px;color:var(--cv-dim);padding:4px 2px 0;';
      emptyMsg.textContent = '\uC800\uC7A5\uB41C \uD504\uB9AC\uC14B\uC774 \uC5C6\uC2B5\uB2C8\uB2E4. \uC6B0\uCE21 "\uD504\uB9AC\uC14B \uC800\uC7A5" \uBC84\uD2BC\uC73C\uB85C \uD604\uC7AC \uC124\uC815\uC744 \uC800\uC7A5\uD558\uC138\uC694.';
      area.appendChild(emptyMsg);
    } else {
      var myRow = document.createElement('div');
      myRow.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;';
      user.forEach(function(preset) {
        myRow.appendChild(self._buildPresetCard(preset, true));
      });
      area.appendChild(myRow);
    }
  }

  _buildPresetCard(preset, deletable) {
    var self = this;
    var wrapper = document.createElement('div');
    wrapper.style.cssText = 'position:relative;display:inline-block;';

    var card = document.createElement('button');
    card.className = 'disc-mode-btn';
    card.style.cssText = 'text-align:left;padding:8px 12px;' + (deletable ? 'padding-right:28px;' : '');
    var pCount = (preset.participants || []).length;
    var styleLabel = preset.style === 'debate' ? '\uCC2C\uBC18' : preset.style === 'brainstorm' ? '\uBE0C\uB808\uC778\uC2A4\uD1A0\uBC0D' : '\uC790\uC720';

    var nameDiv = document.createElement('div');
    nameDiv.style.cssText = 'font-size:12px;color:var(--cv-text);margin-bottom:2px;';
    nameDiv.textContent = preset.name;
    card.appendChild(nameDiv);

    var metaDiv = document.createElement('div');
    metaDiv.style.cssText = 'font-size:10px;color:var(--cv-dim);';
    metaDiv.textContent = styleLabel + ' \u00B7 ' + pCount + '\uC778 \u00B7 ' + (preset.time_limit_min || 15) + '\uBD84';
    card.appendChild(metaDiv);

    card.addEventListener('click', function() { self._loadPresetIntoForm(preset); });
    wrapper.appendChild(card);

    if (deletable) {
      var del = document.createElement('button');
      del.type = 'button';
      del.textContent = '\u00D7';
      del.title = '\uC0AD\uC81C';
      del.style.cssText = 'position:absolute;top:4px;right:4px;width:20px;height:20px;border:none;background:transparent;color:var(--cv-dim);cursor:pointer;font-size:15px;line-height:1;padding:0;border-radius:3px;';
      del.addEventListener('mouseenter', function() { del.style.background = 'var(--cv-bg)'; del.style.color = 'var(--cv-text)'; });
      del.addEventListener('mouseleave', function() { del.style.background = 'transparent'; del.style.color = 'var(--cv-dim)'; });
      del.addEventListener('click', function(ev) {
        ev.stopPropagation();
        self._deletePreset(preset.id);
      });
      wrapper.appendChild(del);
    }

    return wrapper;
  }

  _loadPresetIntoForm(preset) {
    var topicEl = document.getElementById('disc-topic');
    if (topicEl) topicEl.value = preset.topic || '';
    var timeEl = document.getElementById('disc-time');
    if (timeEl) timeEl.value = preset.time_limit_min || 15;
    this._discMode = preset.mode || 'basic';

    var styleBtns = this._container.querySelectorAll('.disc-style-btn');
    styleBtns.forEach(function(b) {
      b.classList.toggle('active', b.dataset.style === (preset.style || 'free'));
    });

    this._step3Participants = (preset.participants || []).map(function(p) {
      return { name: p.name || 'Agent', persona: p.persona || '\uC77C\uBC18 \uCC38\uAC00\uC790' };
    });

    var cards = document.getElementById('disc-participant-cards');
    var self = this;
    if (cards) {
      while (cards.firstChild) cards.removeChild(cards.firstChild);
      this._step3Participants.forEach(function(p) { self._addParticipantCard(p); });
    }
  }

  _deletePreset(id) {
    if (!confirm('\uC774 \uD504\uB9AC\uC14B\uC744 \uC0AD\uC81C\uD560\uAE4C\uC694?')) return;
    this._presets = (this._presets || []).filter(function(p) { return p.id !== id; });
    this._savePresets();
    this._renderPresetsInSetup();
  }

  _saveCurrentAsPreset() {
    var topicEl = document.getElementById('disc-topic');
    var topic = topicEl ? topicEl.value.trim() : '';
    if (!topic) { alert('\uD1A0\uB860 \uC8FC\uC81C\uB97C \uC785\uB825\uD558\uC138\uC694.'); return; }
    var name = prompt('\uD504\uB9AC\uC14B \uC774\uB984\uC744 \uC785\uB825\uD558\uC138\uC694:', topic.slice(0, 30));
    if (!name) return;
    var preset = {
      id: 'user_' + Date.now(),
      name: name,
      topic: topic,
      style: this._getSelectedStyle(),
      time_limit_min: parseInt(document.getElementById('disc-time').value) || 15,
      participants: this._collectParticipants().map(function(p) { return { name: p.name, persona: p.persona }; }),
      mode: this._discMode,
    };
    this._presets.push(preset);
    this._savePresets();
    this._renderPresetsInSetup();
    alert('\uD504\uB9AC\uC14B\uC774 \uC800\uC7A5\uB418\uC5C8\uC2B5\uB2C8\uB2E4.');
  }

  /* ── Collect participants from UI ── */
  _collectParticipants() {
    var self = this;
    if (this._discMode === 'clone') {
      var cloneCards = this._container.querySelectorAll('#disc-clone-cards .disc-participant-card');
      var cloneResult = [];
      cloneCards.forEach(function(card) {
        var personaId = card.dataset.personaId;
        var nameEl = card.querySelector('input');
        var personaData = (self._clonePersonas || []).find(function(p) { return p.id === personaId; });
        var personaText = personaData ? (personaData.persona || personaData.name) : '(\uC800\uC7A5\uB41C \uD398\uB974\uC18C\uB098)';
        cloneResult.push({
          name: nameEl ? nameEl.value : 'Clone',
          persona: personaText,
          persona_id: personaId,
        });
      });
      return cloneResult;
    }

    var normalCards = this._container.querySelectorAll('#disc-participant-cards .disc-participant-card:not(.is-human)');
    if (normalCards.length === 0) {
      return this._step3Participants || [];
    }

    var result = [];
    normalCards.forEach(function(card) {
      var nameInput = card.querySelector('input');
      var personaArea = card.querySelector('textarea');
      result.push({
        name: nameInput ? nameInput.value.trim() : 'Agent',
        persona: personaArea ? personaArea.value.trim() : '\uC77C\uBC18 \uCC38\uAC00\uC790',
      });
    });
    return result;
  }

  /* ═══════════════════════════════════════════════════
     §  START DISCUSSION — WS connect + transition to live
     ═══════════════════════════════════════════════════ */

  async _startDiscussion() {
    var topicEl = document.getElementById('disc-topic');
    var topic = topicEl ? topicEl.value.trim() : '';
    if (!topic) {
      if (topicEl) {
        topicEl.style.borderColor = 'var(--cv-red)';
        setTimeout(function() { topicEl.style.borderColor = ''; }, 2000);
      }
      return;
    }

    var startBtn = document.getElementById('disc-start-btn');
    if (startBtn) { startBtn.disabled = true; startBtn.textContent = '\uC900\uBE44 \uC911...'; }

    var COLORS = DiscussionManager.PARTICIPANT_COLORS;
    var rawParticipants = this._collectParticipants();
    var participants = [];
    this._participantNames = {};
    this._participantColors = {};
    this._participantPersonas = {};
    this._participantOrder = [];

    for (var i = 0; i < rawParticipants.length; i++) {
      var p = rawParticipants[i];
      var pname = p.name || ('Agent ' + (i + 1));
      var persona = p.persona || '\uC77C\uBC18 \uCC38\uAC00\uC790';
      var color = COLORS[i % COLORS.length];
      var pData = { name: pname, persona: persona, color: color };
      if (p.persona_id) pData.persona_id = p.persona_id;
      participants.push(pData);
      var id = 'agent_' + String.fromCharCode(97 + i);
      this._participantColors[id] = color;
      this._participantNames[id] = pname;
      this._participantPersonas[id] = persona;
      this._participantOrder.push(id);
    }

    if (startBtn) { startBtn.disabled = false; startBtn.textContent = '\uD1A0\uB860 \uC2DC\uC791'; }

    var style = this._getSelectedStyle();
    var timeLimitMin = parseInt(document.getElementById('disc-time').value) || 15;
    this._timeLimitSec = timeLimitMin * 60;

    /* Human participant in participate mode */
    var humanJoin = this._discMode === 'participate';
    var startData = { topic: topic, participants: participants, style: style, time_limit_min: timeLimitMin };
    if (humanJoin) {
      var humanData = this._getHumanParticipantData();
      if (!humanData) {
        if (startBtn) { startBtn.disabled = false; startBtn.textContent = '\uD1A0\uB860 \uC2DC\uC791'; }
        return;
      }
      startData.human_participant = humanData;
      this._participantColors['__human__'] = '#FF6B6B';
      this._participantNames['__human__'] = humanData.name;
      this._participantPersonas['__human__'] = humanData.persona || '';
      this._participantOrder.push('__human__');
    }

    this._lastSpeakerId = null;
    this._currentTopic = topic;

    /* Transition to live view */
    this._showLive(topic);

    /* Connect WebSocket */
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var self = this;
    this.ws = new WebSocket(proto + '//' + location.host + '/ws/disc');

    this.ws.onopen = function() {
      self.ws.send(JSON.stringify({ type: 'disc_start', data: startData }));
      self._startTime = Date.now();
      self._startTimer();
      _discSignalRunning(true);
      if (window._modeManager) window._modeManager.setModeRunning('discussion', true);
    };

    this.ws.onerror = function(e) { console.error('Discussion WebSocket error:', e); };
    this.ws.onmessage = function(e) {
      try { self._handle(JSON.parse(e.data)); } catch (err) { console.error(err); }
    };

    this.ws.onclose = function() {
      self._stopTimer();
      _discSignalRunning(false);
      if (window._modeManager) window._modeManager.setModeRunning('discussion', false);
      /* If no content received, show error — but NOT if the final report
         has already been rendered (which intentionally clears all panels). */
      if (self._reportShown) return;
      var panels = self._container.querySelectorAll('.disc-panel-msg');
      if (panels.length === 0) {
        var errDetail = self._lastError || (self._discConnected ? '\uC11C\uBC84 \uB0B4\uBD80 \uC624\uB958' : '\uC11C\uBC84 \uC5F0\uACB0 \uC2E4\uD328');
        self._showModeratorText('\u274C \uD1A0\uB860\uC744 \uC2DC\uC791\uD560 \uC218 \uC5C6\uC2B5\uB2C8\uB2E4: ' + errDetail);
        setTimeout(function() {
          self.ws = null;
          self._lastError = null;
          self._discConnected = false;
          self._showSetup();
        }, 3000);
      }
    };
  }

  connectToInjection(taskId) {
    this._participantColors = {};
    this._participantNames = {};
    this._participantPersonas = {};
    this._participantOrder = [];
    this._injectionMode = true;

    this._showLive('\uC5F0\uACB0 \uC911...');

    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var self = this;
    this.ws = new WebSocket(proto + '//' + location.host + '/ws/disc?inject=' + encodeURIComponent(taskId));

    this.ws.onopen = function() {
      _discSignalRunning(true);
      if (window._modeManager) window._modeManager.setModeRunning('discussion', true);
    };
    this.ws.onmessage = function(e) {
      try { self._handle(JSON.parse(e.data)); } catch (err) { console.error(err); }
    };
    this.ws.onclose = function() { self._stopTimer(); _discSignalRunning(false); };
  }

  /* ═══════════════════════════════════════════════════
     §  LIVE VIEW — 4-split grid
     ═══════════════════════════════════════════════════ */

  _showLive(topic) {
    var c = this._container;
    while (c.firstChild) c.removeChild(c.firstChild);

    var live = document.createElement('div');
    live.className = 'disc-live';

    /* ── Header bar ── */
    var header = document.createElement('div');
    header.className = 'disc-live-header';

    var titleEl = document.createElement('div');
    titleEl.className = 'disc-live-title';
    titleEl.textContent = topic || '';
    titleEl.id = 'disc-topic-label';
    header.appendChild(titleEl);

    var roundEl = document.createElement('span');
    roundEl.className = 'disc-step disc-step-pending';
    roundEl.id = 'disc-round-label';
    roundEl.textContent = 'Round 0';
    header.appendChild(roundEl);

    var timerEl = document.createElement('div');
    timerEl.className = 'disc-timer';
    timerEl.id = 'disc-timer-text';
    timerEl.textContent = '0:00';
    header.appendChild(timerEl);

    var self = this;
    var stopBtn = document.createElement('button');
    stopBtn.className = 'disc-stop-btn';
    stopBtn.textContent = '\uD1A0\uB860 \uC911\uB2E8';
    stopBtn.addEventListener('click', function() {
      if (self.ws && self.ws.readyState === WebSocket.OPEN) {
        self.ws.send(JSON.stringify({ type: 'disc_stop' }));
      }
    });
    header.appendChild(stopBtn);
    live.appendChild(header);

    /* ── Grid ── */
    var count = this._participantOrder.length;
    var gridClass = 'disc-grid disc-grid-' + Math.min(Math.max(count, 2), 4);
    var grid = document.createElement('div');
    grid.className = gridClass;
    grid.id = 'disc-grid';

    var topPanels = [];
    var bottomPanels = [];

    var self2 = this;
    this._participantOrder.forEach(function(id, idx) {
      var panel = self2._createPanel(id, idx);
      if (idx < 2) topPanels.push(panel);
      else bottomPanels.push(panel);
    });

    /* Top row */
    topPanels.forEach(function(p) { grid.appendChild(p); });

    /* Moderator bar */
    var modBar = this._createModeratorBar();
    grid.appendChild(modBar);

    /* Bottom row */
    bottomPanels.forEach(function(p) { grid.appendChild(p); });

    live.appendChild(grid);

    /* ── Human input bar (participate mode) ── */
    if (this._discMode === 'participate') {
      var humanBar = document.createElement('div');
      humanBar.className = 'disc-human-input';
      humanBar.id = 'disc-human-input';
      humanBar.style.display = 'none';

      var humanQ = document.createElement('div');
      humanQ.id = 'disc-human-question';
      humanQ.style.cssText = 'font-size:11px;color:var(--cv-dim);margin-bottom:4px;width:100%;';
      humanBar.appendChild(humanQ);

      var humanRow = document.createElement('div');
      humanRow.style.cssText = 'display:flex;gap:8px;width:100%;';
      var humanArea = document.createElement('textarea');
      humanArea.id = 'disc-human-textarea';
      humanArea.placeholder = '\uD1A0\uB860\uC5D0 \uCC38\uC5EC\uD558\uC138\uC694...';
      humanArea.rows = 2;
      humanArea.disabled = true;
      humanRow.appendChild(humanArea);

      var humanSend = document.createElement('button');
      humanSend.id = 'disc-human-send';
      humanSend.textContent = '\uC804\uC1A1';
      humanSend.disabled = true;
      humanSend.style.cssText = 'padding:8px 16px;border-radius:8px;border:none;background:var(--cv-accent);color:white;font-size:13px;cursor:pointer;white-space:nowrap;opacity:0.5;';
      humanRow.appendChild(humanSend);
      humanBar.appendChild(humanRow);

      var humanTimer = document.createElement('div');
      humanTimer.id = 'disc-human-timer';
      humanTimer.style.cssText = 'font-size:10px;color:var(--cv-dim);margin-top:4px;display:none;';
      humanBar.appendChild(humanTimer);

      live.appendChild(humanBar);
    }

    c.appendChild(live);
  }

  _createModeratorBar() {
    var modBar = document.createElement('div');
    modBar.className = 'disc-moderator-bar';
    var modIcon = document.createElement('div');
    modIcon.className = 'disc-mod-icon';
    modIcon.textContent = '\uD83C\uDF99\uFE0F';
    modBar.appendChild(modIcon);
    var modContent = document.createElement('div');
    modContent.className = 'disc-mod-content';
    var modName = document.createElement('div');
    modName.className = 'disc-mod-name';
    modName.textContent = '\uC9C4\uD589\uC790';
    modContent.appendChild(modName);
    var modText = document.createElement('div');
    modText.className = 'disc-mod-text';
    modText.id = 'disc-mod-text';
    modText.textContent = '\uD1A0\uB860\uC744 \uC2DC\uC791\uD569\uB2C8\uB2E4...';
    modContent.appendChild(modText);
    modBar.appendChild(modContent);
    return modBar;
  }

  _createPanel(id, idx) {
    var isHuman = (id === '__human__');
    var panel = document.createElement('div');
    panel.className = 'disc-panel' + (isHuman ? ' is-human' : '');
    panel.id = 'disc-panel-' + id;

    var header = document.createElement('div');
    header.className = 'disc-panel-header';

    var statusDot = document.createElement('div');
    statusDot.className = 'disc-panel-status';
    header.appendChild(statusDot);

    var name = document.createElement('div');
    name.className = 'disc-panel-name';
    name.textContent = this._participantNames[id] || id;
    header.appendChild(name);

    if (isHuman) {
      var badge = document.createElement('span');
      badge.className = 'disc-human-badge';
      badge.textContent = '나';
      header.appendChild(badge);
    }

    panel.appendChild(header);

    var role = document.createElement('div');
    role.className = 'disc-panel-role';
    var personaText = this._participantPersonas[id] || '';
    role.textContent = personaText.length > 60 ? personaText.slice(0, 60) + '...' : personaText;
    panel.appendChild(role);

    var messages = document.createElement('div');
    messages.className = 'disc-panel-messages';
    messages.id = 'disc-msgs-' + id;
    panel.appendChild(messages);

    return panel;
  }

  _rebuildGrid() {
    var grid = document.getElementById('disc-grid');
    if (!grid) return;
    while (grid.firstChild) grid.removeChild(grid.firstChild);
    var count = this._participantOrder.length;
    grid.className = 'disc-grid disc-grid-' + Math.min(Math.max(count, 2), 4);

    var topPanels = [];
    var bottomPanels = [];
    var self = this;
    this._participantOrder.forEach(function(id, idx) {
      var panel = self._createPanel(id, idx);
      if (idx < 2) topPanels.push(panel);
      else bottomPanels.push(panel);
    });

    topPanels.forEach(function(p) { grid.appendChild(p); });

    var modBar = self._createModeratorBar();
    grid.appendChild(modBar);

    bottomPanels.forEach(function(p) { grid.appendChild(p); });
  }

  /* ═══════════════════════════════════════════════════
     §  WEBSOCKET MESSAGE HANDLERS
     ═══════════════════════════════════════════════════ */

  _handle(ev) {
    if (ev.type === 'heartbeat') return;
    var self = this;
    var handlers = {
      disc_init: function() { self._discConnected = true; },
      disc_config: function(d) { self._onConfig(d); },
      disc_phase: function(d) { self._onPhase(d); },
      disc_utterance: function(d) { self._onUtterance(d); },
      disc_moderator: function(d) { self._onModerator(d); },
      disc_round: function(d) { self._onRound(d); },
      disc_report: function(d) { self._onReport(d); },
      disc_report_stage: function(d) { self._onReportStage(d); },
      disc_complete: function(d) { self._onComplete(d); },
      disc_human_turn: function(d) { self._onHumanTurn(d); },
      disc_search_start: function(d) { self._onSearchStart(d); },
      disc_persona_progress: function(d) { self._onPersonaProgress(d); },
      error: function(d) { self._onError(d); },
    };
    var fn = handlers[ev.type];
    if (fn) fn(ev.data);
  }

  _onConfig(d) {
    var topicLabel = document.getElementById('disc-topic-label');
    if (topicLabel) topicLabel.textContent = d.topic || '';
    this._timeLimitSec = (d.time_limit_min || 5) * 60;
    this._startTime = d.started_at ? d.started_at * 1000 : Date.now();
    this._startTimer();

    this._participantOrder = [];
    var self = this;
    (d.participants || []).forEach(function(p) {
      self._participantColors[p.id] = p.color || 'var(--cv-dim)';
      self._participantNames[p.id] = p.name;
      self._participantPersonas[p.id] = p.persona || '';
      self._participantOrder.push(p.id);
    });

    this._rebuildGrid();
  }

  _onPhase(d) {
    var phaseLabels = {
      opening: '\uD1A0\uB860\uC744 \uC900\uBE44\uD558\uACE0 \uC788\uC2B5\uB2C8\uB2E4...',
      opening_speak: '\uCC38\uAC00\uC790\uB4E4\uC774 \uBC1C\uC5B8\uC744 \uC900\uBE44 \uC911...',
      discussing: '\uD1A0\uB860 \uC9C4\uD589 \uC911',
      closing: '\uD1A0\uB860 \uB9C8\uBB34\uB9AC \uC911...',
      report: '\uC778\uC0AC\uC774\uD2B8 \uB9AC\uD3EC\uD2B8 \uC0DD\uC131 \uC911...',
    };
    if (phaseLabels[d.phase] && d.phase !== 'report') {
      this._showModeratorText(phaseLabels[d.phase]);
    }

    var roundLabel = document.getElementById('disc-round-label');
    if (d.phase === 'discussing' && roundLabel) {
      roundLabel.className = 'disc-step disc-step-active';
    }
    if (d.phase === 'closing') {
      this._stopTimer();
      if (roundLabel) roundLabel.className = 'disc-step disc-step-done';
      this._showModeratorText('\uD83D\uDCCB \uD1A0\uB860 \uB0B4\uC6A9\uC744 \uC815\uB9AC\uD558\uACE0 \uC788\uC2B5\uB2C8\uB2E4...');
    }
    if (d.phase === 'report') {
      /* Start the live report timer. Updates every second with elapsed time
         + rotating reassurance. The timer is stopped by _onReport() when
         the report arrives, or _onComplete() / _onError() on teardown. */
      this._reportStageText = '\uD83E\uDD16 \uB9AC\uD3EC\uD2B8 \uC791\uC131 \uC911';
      this._startReportTimer();
    }
  }

  _startReportTimer() {
    this._stopReportTimer();
    this._reportTimerStart = Date.now();
    var self = this;
    var render = function() {
      var elapsed = Math.floor((Date.now() - self._reportTimerStart) / 1000);
      var mm = Math.floor(elapsed / 60);
      var ss = elapsed % 60;
      var timeStr = (mm > 0 ? mm + '\uBD84 ' : '') + ss + '\uCD08 \uACBD\uACFC';
      self._showModeratorText(self._reportStageText + ' \u00B7 ' + timeStr);
    };
    render();
    this._reportTimerIv = setInterval(render, 1000);
  }

  _stopReportTimer() {
    if (this._reportTimerIv) {
      clearInterval(this._reportTimerIv);
      this._reportTimerIv = null;
    }
  }

  _onReportStage(d) {
    if (!d || !d.message) return;
    this._reportStageText = d.message;
    /* Force an immediate render so the user sees the stage change without
       waiting for the next 1-second tick. */
    if (this._reportTimerIv) {
      var elapsed = Math.floor((Date.now() - this._reportTimerStart) / 1000);
      var mm = Math.floor(elapsed / 60);
      var ss = elapsed % 60;
      var timeStr = (mm > 0 ? mm + '\uBD84 ' : '') + ss + '\uCD08 \uACBD\uACFC';
      this._showModeratorText(this._reportStageText + ' \u00B7 ' + timeStr);
    }
  }

  _onUtterance(d) {
    var speakerId = d.speaker_id;

    /* 사용자 메시지 중복 방지 — _sendHumanInput에서 이미 표시했으므로
       동일 내용의 백엔드 에코는 스킵. 타임아웃 메시지 등은 통과. */
    if (speakerId === '__human__' && this._lastHumanContent && d.content.trim() === this._lastHumanContent) {
      this._lastHumanContent = null;
      return;
    }

    var msgArea = document.getElementById('disc-msgs-' + speakerId);
    if (!msgArea) return;

    /* Add message to the speaker's panel */
    var msgDiv = document.createElement('div');
    msgDiv.className = 'disc-panel-msg';
    /* Safe: _renderContent() escapes HTML entities before adding formatting tags */
    var rendered = this._renderContent(d.content);
    var tempDiv = document.createElement('div');
    tempDiv.className = 'disc-panel-msg-inner';
    /* Use DOMParser for safe HTML insertion */
    var parser = new DOMParser();
    var doc = parser.parseFromString('<div>' + rendered + '</div>', 'text/html');
    while (doc.body.firstChild && doc.body.firstChild.firstChild) {
      tempDiv.appendChild(doc.body.firstChild.firstChild);
    }
    /* Apply name highlighting on the parsed DOM tree (safe — text nodes only) */
    this._highlightNamesInTree(tempDiv);

    msgDiv.appendChild(tempDiv);
    msgArea.appendChild(msgDiv);
    msgArea.scrollTop = msgArea.scrollHeight;

    /* Update speaking state */
    this._setSpeaking(speakerId);

    /* Broadcast to mode-company.js for audience canvas reactions */
    if (window.AppBus) window.AppBus.emit('disc:utterance', d);
  }

  _onModerator(d) {
    var targetName = this._participantNames[d.next_speaker_id] || d.next_speaker_id;
    var el = document.getElementById('disc-mod-text');
    if (el) {
      el.textContent = '';
      var targetSpan = document.createElement('span');
      targetSpan.className = 'disc-mod-target';
      targetSpan.textContent = targetName + '\uB2D8\uC5D0\uAC8C: ';
      el.appendChild(targetSpan);
      el.appendChild(document.createTextNode(d.instruction || ''));
    }
    this._setSpeaking(d.next_speaker_id);
  }

  _onRound(d) {
    var el = document.getElementById('disc-round-label');
    if (el) {
      el.textContent = 'Round ' + d.round;
      el.className = 'disc-step disc-step-active';
    }
  }

  _onReport(d) {
    this._stopReportTimer();
    this._showReport(d.html, d.download_url);
    _discSignalRunning(false);
  }

  _onComplete(d) {
    this._stopTimer();
    this._stopReportTimer();
    if (d && d.cancelled) {
      this._showModeratorText('\u23F9 \uD1A0\uB860\uC774 \uC911\uB2E8\uB418\uC5C8\uC2B5\uB2C8\uB2E4.');
    } else {
      this._showModeratorText('\u2705 \uD1A0\uB860\uC774 \uC644\uB8CC\uB418\uC5C8\uC2B5\uB2C8\uB2E4.');
    }
    _discSignalRunning(false);
    if (window._modeManager) window._modeManager.setModeRunning('discussion', false);

    /* Morph the stop button into a "back to setup" button whenever the live
       grid is still on-screen (i.e. no report took over). Covers both user
       cancellation and edge cases where completion fires without a report. */
    if (!this._reportShown && this._container) {
      var stopBtn = this._container.querySelector('.disc-stop-btn');
      if (stopBtn) {
        var freshBtn = stopBtn.cloneNode(false);
        freshBtn.textContent = '\u2190 \uCC98\uC74C\uC73C\uB85C';
        freshBtn.classList.add('disc-back-btn');
        var self = this;
        freshBtn.addEventListener('click', function() {
          self._resetState();
          self._showSetup();
        });
        stopBtn.parentNode.replaceChild(freshBtn, stopBtn);
      }
    }
  }

  _onHumanTurn(d) {
    var inputBar = document.getElementById('disc-human-input');
    var question = document.getElementById('disc-human-question');
    var area = document.getElementById('disc-human-textarea');
    var btn = document.getElementById('disc-human-send');
    var timerEl = document.getElementById('disc-human-timer');
    if (!inputBar || !area || !btn) return;

    if (question) question.textContent = '\uD83C\uDF99 ' + (d.instruction || '\uC758\uACAC\uC744 \uB9D0\uC500\uD574 \uC8FC\uC138\uC694.');
    inputBar.style.display = '';
    area.disabled = false;
    area.placeholder = '\uC5EC\uAE30\uC5D0 \uC785\uB825\uD558\uC138\uC694...';
    btn.disabled = false;
    btn.style.opacity = '1';

    var self = this;
    if (timerEl) {
      timerEl.style.display = '';
      var remaining = d.timeout_sec || 120;
      this._humanTimerIv = setInterval(function() {
        remaining--;
        timerEl.textContent = '\uB0A8\uC740 \uC2DC\uAC04: ' + remaining + '\uCD08';
        if (remaining <= 30) timerEl.style.color = 'var(--cv-red)';
        if (remaining <= 0) {
          clearInterval(self._humanTimerIv);
          self._disableHumanInput();
          self._showModeratorText('\u23F0 \uC785\uB825 \uC2DC\uAC04\uC774 \uCD08\uACFC\uB418\uC5C8\uC2B5\uB2C8\uB2E4.');
        }
      }, 1000);
    }
    area.focus();
    btn.onclick = function() { self._sendHumanInput(); };
    area.onkeydown = function(e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); self._sendHumanInput(); }
    };

    /* 사용자 패널 speaking 상태 + 실시간 타이핑 프리뷰 */
    this._setSpeaking('__human__');
    var msgArea = document.getElementById('disc-msgs-__human__');
    if (msgArea) {
      var preview = document.createElement('div');
      preview.className = 'disc-panel-msg disc-typing-preview';
      preview.id = 'disc-human-typing-preview';
      preview.textContent = '';
      msgArea.appendChild(preview);
      area.oninput = function() {
        preview.textContent = area.value || '';
        msgArea.scrollTop = msgArea.scrollHeight;
      };
    }

    this._stopTimer();
  }

  _sendHumanInput() {
    var area = document.getElementById('disc-human-textarea');
    var content = area ? area.value.trim() : '';
    if (!content) return;
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: 'disc_human_input', data: { content: content } }));
    }

    /* 패널에 즉시 표시 (백엔드 에코 기다리지 않음) */
    this._showHumanMessage(content);
    this._lastHumanContent = content;

    clearInterval(this._humanTimerIv);
    this._disableHumanInput();
    if (area) area.value = '';

    /* 타이핑 프리뷰 제거 */
    var preview = document.getElementById('disc-human-typing-preview');
    if (preview) preview.remove();
  }

  _showHumanMessage(content) {
    var msgArea = document.getElementById('disc-msgs-__human__');
    if (!msgArea) return;
    var msgDiv = document.createElement('div');
    msgDiv.className = 'disc-panel-msg';
    var rendered = this._renderContent(content);
    var tempDiv = document.createElement('div');
    tempDiv.className = 'disc-panel-msg-inner';
    var parser = new DOMParser();
    var doc = parser.parseFromString('<div>' + rendered + '</div>', 'text/html');
    while (doc.body.firstChild && doc.body.firstChild.firstChild) {
      tempDiv.appendChild(doc.body.firstChild.firstChild);
    }
    this._highlightNamesInTree(tempDiv);
    msgDiv.appendChild(tempDiv);
    msgArea.appendChild(msgDiv);
    msgArea.scrollTop = msgArea.scrollHeight;
    this._setSpeaking('__human__');
  }

  _disableHumanInput() {
    var area = document.getElementById('disc-human-textarea');
    var btn = document.getElementById('disc-human-send');
    var timerEl = document.getElementById('disc-human-timer');
    if (area) { area.disabled = true; area.placeholder = '\uD1A0\uB860 \uC9C4\uD589 \uC911...'; area.oninput = null; }
    if (btn) { btn.disabled = true; btn.style.opacity = '0.5'; }
    if (timerEl) timerEl.style.display = 'none';
    var preview = document.getElementById('disc-human-typing-preview');
    if (preview) preview.remove();
    this._startTimer();
  }

  _onSearchStart(d) {
    this._setSpeaking(d.speaker_id);
    var msgArea = document.getElementById('disc-msgs-' + d.speaker_id);
    if (msgArea) {
      var indicator = document.createElement('div');
      indicator.className = 'disc-panel-msg';
      indicator.style.cssText = 'color:var(--cv-dim);font-style:italic;';
      indicator.textContent = '\uD83D\uDD0D \uAC80\uC0C9 \uC911...';
      indicator.dataset.searchIndicator = 'true';
      msgArea.appendChild(indicator);
      msgArea.scrollTop = msgArea.scrollHeight;
    }
  }

  _onPersonaProgress(d) {
    var labels = { searching: '\uAC80\uC0C9 \uC911', synthesizing: '\uD569\uC131 \uC911', done: '\uC644\uB8CC' };
    var text = '\uD83E\uDDEC ' + d.participant_name + ' \uD398\uB974\uC18C\uB098 ' + (labels[d.stage] || d.stage) + '... (' + (d.index + 1) + '/' + d.total + ')';
    this._showModeratorText(text);
  }

  _onError(d) {
    this._lastError = d.message || 'Unknown';
    this._showModeratorText('\u274C \uC624\uB958: ' + this._lastError);
    _discSignalRunning(false);
  }

  /* ═══════════════════════════════════════════════════
     §  PANEL HELPERS
     ═══════════════════════════════════════════════════ */

  _setSpeaking(speakerId) {
    this._container.querySelectorAll('.disc-panel.speaking').forEach(function(el) { el.classList.remove('speaking'); });
    this._container.querySelectorAll('[data-search-indicator]').forEach(function(el) { el.remove(); });
    var panel = document.getElementById('disc-panel-' + speakerId);
    if (panel) panel.classList.add('speaking');
  }

  _showModeratorText(text) {
    var el = document.getElementById('disc-mod-text');
    if (el) el.textContent = text;
  }

  /* ── Formatted content renderer ──
     Uses marked.js (loaded globally — same pattern as
     card-chat-panel.js's `markdown: true` opt for AI Company
     clarification questions) so the model's headings, lists, ---,
     blockquotes, code, and tables render as real HTML.
     Falls back to a safe escape + bold-only path if marked is unavailable. */
  _renderContent(text) {
    if (typeof marked !== 'undefined') {
      try { return marked.parse(text || ''); } catch (_) { /* fall through */ }
    }
    return this._renderContentFallback(text);
  }

  _renderContentFallback(text) {
    var s = (text || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    s = '<p>' + s.replace(/\n{2,}/g, '</p><p>') + '</p>';
    return s.replace(/\n/g, '<br>');
  }

  /* Apply participant-name highlighting after markdown parsing.
     Walks text nodes only (not raw HTML) so we never corrupt link hrefs,
     code blocks, or attributes. Wraps each match in a colored <span>. */
  _highlightNamesInTree(rootEl) {
    var names = this._participantNames || {};
    var colors = this._participantColors || {};
    var entries = [];
    for (var id in names) {
      if (!names[id]) continue;
      var escaped = names[id].replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      entries.push({
        re: new RegExp(escaped + '(\uB2D8|\uC528|\uAD50\uC218|\uB300\uD45C|\uC120\uC0DD|\uC704\uC6D0)?', 'g'),
        color: colors[id] || 'var(--cv-dim)',
      });
    }
    if (!entries.length) return;

    var walker = document.createTreeWalker(rootEl, NodeFilter.SHOW_TEXT, {
      acceptNode: function(node) {
        // Skip text inside code/pre/links — keep them pristine
        var p = node.parentNode;
        while (p && p !== rootEl) {
          var tag = p.nodeName;
          if (tag === 'CODE' || tag === 'PRE' || tag === 'A') return NodeFilter.FILTER_REJECT;
          p = p.parentNode;
        }
        return NodeFilter.FILTER_ACCEPT;
      },
    });

    var targets = [];
    var current;
    while ((current = walker.nextNode())) targets.push(current);

    targets.forEach(function(textNode) {
      var orig = textNode.nodeValue;
      var matches = [];
      entries.forEach(function(entry) {
        var iter = orig.matchAll(entry.re);
        var item = iter.next();
        while (!item.done) {
          var m = item.value;
          matches.push({ start: m.index, end: m.index + m[0].length, text: m[0], color: entry.color });
          item = iter.next();
        }
      });
      if (!matches.length) return;
      matches.sort(function(a, b) { return a.start - b.start; });
      var clean = [];
      var lastEnd = -1;
      matches.forEach(function(m) {
        if (m.start >= lastEnd) { clean.push(m); lastEnd = m.end; }
      });
      var frag = document.createDocumentFragment();
      var cursor = 0;
      clean.forEach(function(m) {
        if (m.start > cursor) frag.appendChild(document.createTextNode(orig.slice(cursor, m.start)));
        var span = document.createElement('span');
        span.style.color = m.color;
        span.style.fontWeight = '600';
        span.textContent = m.text;
        frag.appendChild(span);
        cursor = m.end;
      });
      if (cursor < orig.length) frag.appendChild(document.createTextNode(orig.slice(cursor)));
      textNode.parentNode.replaceChild(frag, textNode);
    });
  }

  /* ═══════════════════════════════════════════════════
     §  REPORT SCREEN — inside container
     ═══════════════════════════════════════════════════ */

  _showReport(html, downloadUrl) {
    this._reportShown = true;
    var c = this._container;
    while (c.firstChild) c.removeChild(c.firstChild);
    var self = this;

    var report = document.createElement('div');
    report.className = 'disc-report';

    /* Header */
    var header = document.createElement('div');
    header.className = 'disc-report-header';
    var h2 = document.createElement('h2');
    h2.textContent = '\uD83D\uDCCA \uD1A0\uB860 \uB9AC\uD3EC\uD2B8';
    header.appendChild(h2);

    var actions = document.createElement('div');
    actions.style.cssText = 'display:flex;gap:8px;';

    if (downloadUrl) {
      var dlBtn = document.createElement('button');
      dlBtn.className = 'disc-mode-btn';
      dlBtn.textContent = '\uD83D\uDCE5 \uB2E4\uC6B4\uB85C\uB4DC';
      dlBtn.addEventListener('click', function() {
        var topicStr = self._currentTopic || 'discussion';
        var date = new Date().toISOString().slice(0, 10);
        var a = document.createElement('a');
        a.href = downloadUrl;
        a.download = '\uD1A0\uB860\uB9AC\uD3EC\uD2B8_' + topicStr.slice(0, 30) + '_' + date + '.html';
        document.body.appendChild(a);
        a.click();
        a.remove();
      });
      actions.appendChild(dlBtn);

      var openBtn = document.createElement('button');
      openBtn.className = 'disc-mode-btn';
      openBtn.textContent = '\u2197 \uC0C8 \uD0ED\uC5D0\uC11C \uC5F4\uAE30';
      openBtn.addEventListener('click', function() {
        window.open(downloadUrl, '_blank', 'noopener');
      });
      actions.appendChild(openBtn);
    }

    var newBtn = document.createElement('button');
    newBtn.className = 'disc-mode-btn active';
    newBtn.style.cssText = 'padding:6px 16px;font-weight:600;';
    newBtn.textContent = '\uC0C8 \uD1A0\uB860';
    newBtn.addEventListener('click', function() {
      self._resetState();
      self._showSetup();
    });
    actions.appendChild(newBtn);
    header.appendChild(actions);
    report.appendChild(header);

    /* Report body — sandboxed iframe hosts the LLM's self-contained HTML.
       Prefer loading via URL so CSS/JS inside the report cannot leak out
       and the parent page's styles cannot leak in. If no URL is available
       (e.g., report export disabled), fall back to srcdoc from the inline
       HTML payload so the user still sees the report. */
    var body = document.createElement('div');
    body.className = 'disc-report-body';
    body.style.cssText = 'padding:0;background:#ffffff;';

    var iframe = document.createElement('iframe');
    iframe.sandbox = 'allow-same-origin allow-popups';
    iframe.style.cssText = 'width:100%;border:none;min-height:600px;display:block;background:#ffffff;';
    iframe.setAttribute('title', '\uD1A0\uB860 \uB9AC\uD3EC\uD2B8');
    iframe.addEventListener('load', function() {
      try {
        var doc = iframe.contentDocument;
        if (!doc) return;
        var h = doc.documentElement.scrollHeight;
        if (h && h > 200) iframe.style.height = (h + 24) + 'px';
      } catch (_) { /* cross-origin — leave default height */ }
    });
    if (downloadUrl) {
      iframe.src = downloadUrl;
    } else if (html) {
      iframe.srcdoc = html;
    }
    body.appendChild(iframe);
    report.appendChild(body);

    c.appendChild(report);
  }

  _resetState() {
    this._stopTimer();
    this._stopReportTimer();
    this._participantColors = {};
    this._participantNames = {};
    this._participantPersonas = {};
    this._participantOrder = [];
    this._lastSpeakerId = null;
    this._step3Participants = null;
    this._discConnected = false;
    this._lastError = null;
    this._discMode = 'basic';
    this._reportShown = false;
    this.ws = null;
  }

  /* ═══════════════════════════════════════════════════
     §  TIMER
     ═══════════════════════════════════════════════════ */

  _startTimer() {
    var textEl = document.getElementById('disc-timer-text');
    if (!textEl) return;
    var limitSec = this._timeLimitSec;
    var self = this;
    var fmt = function(s) { return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); };
    textEl.textContent = '0:00 / ' + fmt(limitSec);

    this._timerIv = setInterval(function() {
      var elapsed = Math.floor((Date.now() - self._startTime) / 1000);
      textEl.textContent = fmt(elapsed) + ' / ' + fmt(limitSec);
      if (elapsed / limitSec > 0.8) textEl.style.color = 'var(--cv-red)';
    }, 1000);
  }

  _stopTimer() {
    if (this._timerIv) { clearInterval(this._timerIv); this._timerIv = null; }
  }
}
