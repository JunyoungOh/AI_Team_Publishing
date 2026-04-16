/* mode-skill.js — 스킬 탭: 만들기 + 목록 (스케줄링은 Plan 2)
 *
 * 두 서브 패널: 'create' / 'list'
 *  - create: WebSocket /ws/skill-builder 로 skill-creator와 다중 턴 대화
 *  - list:   REST /api/skill-builder/list 에서 data/skills/registry.json 조회
 *
 * 답변 입력은 textarea 사용 — skill-creator가 4개 질문을 한 번에 묶어서
 * 던지는 경우가 많아서 여러 줄 답변이 편해야 함.
 *
 * 대기 중엔 답변 입력란이 disabled 상태라 stdin 버퍼 같은 문제 없음.
 */
var SkillManager = (function () {
  'use strict';

  /* Sidebar "running" indicator signal — uses reference counting because
     skill has TWO WebSockets (_execWs for running a skill, _ws for
     creating one) that can theoretically overlap. The glow only turns
     off when BOTH have closed. See mode-chatbot.js for receiver. */
  var _signalCount = 0;
  function _signalRunning(on) {
    _signalCount += on ? 1 : -1;
    if (_signalCount < 0) _signalCount = 0;
    try {
      if (window.chatbotSignal) window.chatbotSignal('skill', _signalCount > 0);
    } catch (_) { /* noop */ }
  }

  var _container = null;
  var _activePanel = 'create';  // 'create' | 'list'
  var _ws = null;
  var _replyInput = null;
  var _replyBtn = null;
  var _replyRow = null;
  var _lastDescription = '';
  var _createWsPanel = null;

  function mountInShell(container) {
    _container = container;
    _render();
  }

  function _render() {
    if (!_container) return;
    while (_container.firstChild) _container.removeChild(_container.firstChild);

    var switcher = document.createElement('div');
    switcher.className = 'skill-panel-switcher';
    [['create', '스킬 만들기'], ['list', '내 스킬']].forEach(function (pair) {
      var btn = document.createElement('button');
      btn.textContent = pair[1];
      btn.className = (pair[0] === _activePanel) ? 'active' : '';
      btn.onclick = function () { _activePanel = pair[0]; _render(); };
      switcher.appendChild(btn);
    });
    _container.appendChild(switcher);

    var panel = document.createElement('div');
    panel.className = 'skill-panel';
    _container.appendChild(panel);

    if (_activePanel === 'create') {
      _renderCreatePanel(panel);
    } else {
      _renderListPanel(panel);
    }
  }

  function _renderCreatePanel(root) {
    var title = document.createElement('h3');
    title.textContent = '새 스킬 만들기';
    root.appendChild(title);

    var sub = document.createElement('p');
    sub.className = 'subtitle';
    sub.textContent = '재사용 가능한 워크플로우를 한 번 만들어두고 언제든 불러쓰세요.';
    root.appendChild(sub);

    var input = document.createElement('input');
    input.className = 'skill-create-input';
    input.placeholder = '예: 긴 텍스트를 3줄로 요약해줘';
    root.appendChild(input);

    _createWsPanel = WorkspacePanel.create(root, 'skill');

    var log = document.createElement('div');
    log.className = 'skill-chat-log';
    root.appendChild(log);
    _appendSystem(log, '설명을 입력하고 "만들기 시작" 버튼을 눌러주세요.');

    var startBtn = document.createElement('button');
    startBtn.className = 'skill-create-btn';
    startBtn.textContent = '만들기 시작';
    startBtn.onclick = function () {
      var desc = input.value.trim();
      if (!desc) return;
      _lastDescription = desc;
      input.disabled = true;
      startBtn.disabled = true;
      _startSession(desc, log);
    };
    root.appendChild(startBtn);

    // 답변 입력 row (처음엔 숨김)
    _replyRow = document.createElement('div');
    _replyRow.className = 'skill-reply-row';
    _replyRow.style.display = 'none';

    _replyInput = document.createElement('textarea');
    _replyInput.className = 'skill-reply-textarea';
    _replyInput.placeholder = '답변을 입력하세요... (여러 질문에 한 번에 답해도 됩니다)';
    _replyInput.disabled = true;

    var hint = document.createElement('div');
    hint.className = 'skill-reply-hint';
    hint.textContent = 'Cmd/Ctrl + Enter 로 전송, Shift + Enter 로 줄바꿈';

    _replyBtn = document.createElement('button');
    _replyBtn.className = 'skill-reply-btn';
    _replyBtn.textContent = '답변 보내기';
    _replyBtn.disabled = true;
    _replyBtn.onclick = function () { _sendReply(log); };

    _replyInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        _sendReply(log);
      }
    });

    _replyRow.appendChild(_replyInput);
    _replyRow.appendChild(hint);
    _replyRow.appendChild(_replyBtn);
    root.appendChild(_replyRow);
  }

  function _sendReply(logEl) {
    if (!_ws || _ws.readyState !== WebSocket.OPEN) return;
    var text = _replyInput.value.trim();
    if (!text) return;
    _appendUser(logEl, text);
    _ws.send(JSON.stringify({ type: 'user_message', data: { text: text } }));
    _replyInput.value = '';
    _disableReply();
    _appendSystem(logEl, '응답 대기 중...');
  }

  function _scrollLogToBottom(logEl) {
    logEl.scrollTop = logEl.scrollHeight;
  }

  function _appendSystem(logEl, text) {
    var el = document.createElement('div');
    el.className = 'skill-msg skill-msg-system';
    el.textContent = text;
    logEl.appendChild(el);
    _scrollLogToBottom(logEl);
  }

  function _appendUser(logEl, text) {
    var el = document.createElement('div');
    el.className = 'skill-msg skill-msg-user';
    el.textContent = text;
    logEl.appendChild(el);
    _scrollLogToBottom(logEl);
  }

  function _renderMarkdownSafe(el, markdownText) {
    /* marked.parse → DOMPurify.sanitize → assign.
     * Backend content comes from skill-creator subprocess (our controlled
     * handoff prompt), but we still sanitize as defense-in-depth. */
    var safe;
    try {
      var raw = marked.parse(markdownText || '');
      safe = (typeof DOMPurify !== 'undefined')
        ? DOMPurify.sanitize(raw)
        : raw;
    } catch (_) {
      el.textContent = markdownText || '';
      return;
    }
    // DOMPurify output is sanitized HTML string — safe to assign.
    el.innerHTML = safe; // eslint-disable-line no-unsanitized/property
  }

  function _appendAssistant(logEl, markdownText) {
    var wrapper = document.createElement('div');
    wrapper.className = 'skill-msg skill-msg-assistant';

    var label = document.createElement('div');
    label.className = 'skill-msg-label';
    label.textContent = 'skill-creator';
    wrapper.appendChild(label);

    var body = document.createElement('div');
    body.className = 'skill-msg-body';
    _renderMarkdownSafe(body, markdownText);
    wrapper.appendChild(body);

    logEl.appendChild(wrapper);
    _scrollLogToBottom(logEl);
  }

  function _clearLog(logEl) {
    while (logEl.firstChild) logEl.removeChild(logEl.firstChild);
  }

  function _enableReply() {
    if (!_replyRow) return;
    _replyRow.style.display = '';
    _replyInput.disabled = false;
    _replyBtn.disabled = false;
    _replyInput.focus();
  }

  function _disableReply() {
    if (!_replyInput) return;
    _replyInput.disabled = true;
    _replyBtn.disabled = true;
  }

  function _hideReply() {
    if (_replyRow) _replyRow.style.display = 'none';
  }

  function _renderListPanel(root) {
    var title = document.createElement('h3');
    title.textContent = '내 스킬';
    root.appendChild(title);

    var sub = document.createElement('p');
    sub.className = 'subtitle';
    sub.textContent = '카드를 클릭해서 실행하세요. 입력란에 자유롭게 텍스트를 적고 실행 버튼을 누르면 됩니다.';
    root.appendChild(sub);

    var grid = document.createElement('div');
    grid.className = 'skill-list-grid';
    root.appendChild(grid);

    fetch('/api/skill-builder/list')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var skills = (data && data.skills) || [];
        if (!skills.length) {
          var empty = document.createElement('p');
          empty.className = 'skill-empty';
          empty.textContent = '아직 만든 스킬이 없어요. "스킬 만들기" 탭에서 시작하세요.';
          grid.appendChild(empty);
          return;
        }
        skills.forEach(function (s) {
          var card = document.createElement('div');
          card.className = 'skill-card';
          card.setAttribute('data-slug', s.slug);

          var h = document.createElement('h4');
          h.textContent = s.name || s.slug;
          card.appendChild(h);

          var meta = document.createElement('div');
          meta.className = 'meta';
          meta.textContent =
            (s.source === 'created' ? '직접 생성' : '가져옴')
            + ' · ' + (s.created_at || '').slice(0, 10);
          card.appendChild(meta);

          if (s.required_mcps && s.required_mcps.length) {
            var mcps = document.createElement('div');
            mcps.className = 'mcps';
            mcps.textContent = 'MCP: ' + s.required_mcps.join(', ');
            card.appendChild(mcps);
          }

          var hint = document.createElement('div');
          hint.className = 'skill-card-hint';
          hint.textContent = '클릭해서 실행';
          card.appendChild(hint);

          // ── 카드 액션 버튼 (편집 + 삭제) ──
          var actions = document.createElement('div');
          actions.className = 'skill-card-actions';

          var editBtn = document.createElement('button');
          editBtn.className = 'skill-card-action-btn skill-card-edit-btn';
          editBtn.textContent = '편집';
          editBtn.title = 'SKILL.md 편집';
          editBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            _openEditModal(s);
          });
          actions.appendChild(editBtn);

          var delBtn = document.createElement('button');
          delBtn.className = 'skill-card-action-btn skill-card-del-btn';
          delBtn.textContent = '삭제';
          delBtn.title = '스킬 삭제';
          delBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            _deleteSkill(s.slug, s.name || s.slug);
          });
          actions.appendChild(delBtn);
          card.appendChild(actions);

          card.addEventListener('click', function (e) {
            if (e.target.closest('.skill-exec-panel')) return;
            if (e.target.closest('.skill-card-actions')) return;
            _toggleExecPanel(card, s);
          });

          grid.appendChild(card);
        });
      })
      .catch(function (e) {
        var err = document.createElement('p');
        err.className = 'skill-empty';
        err.textContent = '목록을 불러오지 못했어요: ' + e.message;
        grid.appendChild(err);
      });
  }

  // ── 카드 실행 (인라인 펼침) ──
  var _activeExecCard = null;
  var _execWs = null;

  function _toggleExecPanel(card, skill) {
    if (_activeExecCard && _activeExecCard !== card) {
      _closeExecPanel(_activeExecCard);
    }
    if (card.classList.contains('expanded')) {
      _closeExecPanel(card);
      return;
    }
    _openExecPanel(card, skill);
  }

  function _closeExecPanel(card) {
    if (_execWs) {
      try { _execWs.close(); } catch (e) { /* ignore */ }
      _execWs = null;
    }
    var panel = card.querySelector('.skill-exec-panel');
    if (panel) panel.parentNode.removeChild(panel);
    card.classList.remove('expanded');
    if (_activeExecCard === card) _activeExecCard = null;
  }

  function _openExecPanel(card, skill) {
    card.classList.add('expanded');
    _activeExecCard = card;

    var panel = document.createElement('div');
    panel.className = 'skill-exec-panel';

    var form = document.createElement('div');
    form.className = 'skill-exec-form';

    var textarea = document.createElement('textarea');
    textarea.className = 'skill-exec-input';
    textarea.placeholder = '이 스킬에 어떤 입력을 줄까요?\n자유롭게 적어주세요.';
    form.appendChild(textarea);

    var runBtn = document.createElement('button');
    runBtn.className = 'skill-exec-run-btn';
    runBtn.textContent = '실행';
    form.appendChild(runBtn);

    var skillWsPanel = WorkspacePanel.create(form, 'skill');

    panel.appendChild(form);

    var log = document.createElement('div');
    log.className = 'skill-exec-log';
    log.style.display = 'none';
    panel.appendChild(log);

    var result = document.createElement('div');
    result.className = 'skill-exec-result';
    result.style.display = 'none';
    panel.appendChild(result);

    // 실행 횟수 카운트 라벨 (이력 펼침은 제거 — 카운트만 충분)
    var historyLabel = document.createElement('div');
    historyLabel.className = 'skill-exec-history-count';
    historyLabel.textContent = '실행 횟수: 불러오는 중...';
    panel.appendChild(historyLabel);

    card.appendChild(panel);

    function _refreshRunCount() {
      fetch('/api/skill-builder/runs/' + encodeURIComponent(skill.slug))
        .then(function (r) { return r.json(); })
        .then(function (data) {
          var runs = (data && data.runs) || [];
          historyLabel.textContent = '실행 횟수: ' + runs.length + '회';
        })
        .catch(function () {
          historyLabel.textContent = '실행 횟수: (불러오기 실패)';
        });
    }
    _refreshRunCount();

    runBtn.onclick = function () {
      var input = textarea.value.trim();
      runBtn.disabled = true;
      textarea.disabled = true;
      while (log.firstChild) log.removeChild(log.firstChild);
      log.style.display = '';
      result.style.display = 'none';
      _startExecution(skill, input, log, result, runBtn, textarea, _refreshRunCount, skillWsPanel);
    };

    textarea.focus();
  }

  function _startExecution(skill, input, logEl, resultEl, runBtn, textarea, refreshRunCount, wsPanel) {
    var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    _execWs = new WebSocket(proto + location.host + '/ws/skill-execute');

    function appendLog(text, kind) {
      var line = document.createElement('div');
      line.className = 'exec-log-line ' + (kind || '');
      line.textContent = text;
      logEl.appendChild(line);
      logEl.scrollTop = logEl.scrollHeight;
    }

    _execWs.onopen = function () {
      appendLog('▶ 실행 시작', 'started');
      _signalRunning(true);
      _execWs.send(JSON.stringify({
        type: 'execute',
        data: { slug: skill.slug, user_input: input, workspace_files: wsPanel ? wsPanel.getSelectedFiles() : [] },
      }));
    };

    _execWs.onmessage = function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === 'started') {
        appendLog('🚀 세션 시작', 'started');
      } else if (msg.type === 'tool_use') {
        var tool = (msg.data && msg.data.tool) || '?';
        var elapsed = (msg.data && msg.data.elapsed) || 0;
        appendLog('🔧 ' + tool + ' (' + elapsed + 's)', 'tool');
      } else if (msg.type === 'text') {
        // 텍스트 청크는 결과 누적용 — 실시간 표시는 옵션
      } else if (msg.type === 'timeout') {
        appendLog('⏱️ 타임아웃', 'timeout');
      } else if (msg.type === 'completed') {
        var data = msg.data || {};
        appendLog(
          '✅ 완료 (' + data.duration_seconds + 's, 도구 ' + data.tool_count + '회)',
          'completed'
        );
        _renderMarkdownSafe(resultEl, data.result_text || '(빈 결과)');
        resultEl.style.display = '';
        runBtn.disabled = false;
        textarea.disabled = false;
        // 카운트 라벨만 갱신 (이력 펼침은 제거됨)
        if (typeof refreshRunCount === 'function') refreshRunCount();
      } else if (msg.type === 'error') {
        appendLog('❌ ' + ((msg.data && msg.data.message) || '오류'), 'error');
        runBtn.disabled = false;
        textarea.disabled = false;
      }
    };

    _execWs.onerror = function () {
      appendLog('[오류] WebSocket 연결 실패', 'error');
      runBtn.disabled = false;
      textarea.disabled = false;
    };

    _execWs.onclose = function () {
      appendLog('세션 종료', 'closed');
      _signalRunning(false);
    };
  }

  function _startSession(description, logEl) {
    _clearLog(logEl);
    _appendSystem(logEl, '연결 중...');

    if (_ws) { try { _ws.close(); } catch (e) { /* ignore */ } }
    var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    _ws = new WebSocket(proto + location.host + '/ws/skill-builder');

    _ws.onopen = function () {
      _appendSystem(logEl, '연결됨');
      _signalRunning(true);
      var wsFiles = _createWsPanel ? _createWsPanel.getSelectedFiles() : [];
      _ws.send(JSON.stringify({
        type: 'start',
        data: { description: description, workspace_files: wsFiles },
      }));
    };

    _ws.onmessage = function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === 'greeting') {
        _appendAssistant(logEl, msg.data.text);
      } else if (msg.type === 'search_results') {
        var cs = (msg.data && msg.data.candidates) || [];
        if (cs.length) {
          var lines = ['**기존 스킬 ' + cs.length + '개 발견**', ''];
          cs.forEach(function (c) {
            lines.push('- **' + c.name + '** (설치 '
              + c.unique_installs.toLocaleString() + ')');
          });
          lines.push('');
          lines.push('_새로 만들기로 진행합니다._');
          _appendAssistant(logEl, lines.join('\n'));
        } else {
          _appendSystem(logEl, '기존 스킬 없음 — 새로 만들기로 진행');
        }
        _ws.send(JSON.stringify({ type: 'choice', data: { choice: 'new' } }));
        _appendSystem(logEl, 'skill-creator 시작 중... (약 20초)');
      } else if (msg.type === 'assistant_message') {
        _appendAssistant(logEl, (msg.data && msg.data.text) || '');
        _enableReply();
      } else if (msg.type === 'created') {
        var doneText = '**✅ 스킬이 저장되었습니다**\n\n'
          + '- slug: `' + msg.data.slug + '`\n'
          + '- 경로: `' + msg.data.skill_path + '`';
        _appendAssistant(logEl, doneText);
        _hideReply();
      } else if (msg.type === 'error') {
        var errEl = document.createElement('div');
        errEl.className = 'skill-msg skill-msg-error';
        errEl.textContent = '❌ ' + ((msg.data && msg.data.message) || '');
        logEl.appendChild(errEl);
        _scrollLogToBottom(logEl);
        _hideReply();
      }
    };

    _ws.onerror = function () {
      _appendSystem(logEl, '[오류] WebSocket 연결 실패');
    };
    _ws.onclose = function () {
      _appendSystem(logEl, '세션 종료');
      _signalRunning(false);
    };
  }

  // ── 삭제 ──
  function _deleteSkill(slug, displayName) {
    if (!confirm('"' + displayName + '" 스킬을 삭제할까요?\n삭제하면 되돌릴 수 없습니다.')) return;
    fetch('/api/skill-builder/skills/' + encodeURIComponent(slug), { method: 'DELETE' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function () { _activePanel = 'list'; _render(); })
      .catch(function (e) { alert('삭제 실패: ' + e.message); });
  }

  // ── 편집 모달 ──
  function _openEditModal(skill) {
    var overlay = document.createElement('div');
    overlay.className = 'skill-edit-overlay';

    var modal = document.createElement('div');
    modal.className = 'skill-edit-modal';

    var header = document.createElement('div');
    header.className = 'skill-edit-header';
    var titleEl = document.createElement('h3');
    titleEl.textContent = '스킬 편집: ' + (skill.name || skill.slug);
    header.appendChild(titleEl);
    var closeBtn = document.createElement('button');
    closeBtn.className = 'skill-edit-close';
    closeBtn.textContent = '\u00D7';
    closeBtn.onclick = function () { overlay.remove(); };
    header.appendChild(closeBtn);
    modal.appendChild(header);

    var editor = document.createElement('textarea');
    editor.className = 'skill-edit-textarea';
    editor.value = '불러오는 중...';
    editor.disabled = true;
    modal.appendChild(editor);

    var footer = document.createElement('div');
    footer.className = 'skill-edit-footer';
    var saveBtn = document.createElement('button');
    saveBtn.className = 'skill-create-btn';
    saveBtn.textContent = '저장';
    saveBtn.disabled = true;
    var cancelBtn = document.createElement('button');
    cancelBtn.className = 'skill-card-action-btn';
    cancelBtn.textContent = '취소';
    cancelBtn.onclick = function () { overlay.remove(); };
    footer.appendChild(cancelBtn);
    footer.appendChild(saveBtn);
    modal.appendChild(footer);

    overlay.appendChild(modal);
    document.body.appendChild(overlay);

    // Load current body
    fetch('/api/skill-builder/skills/' + encodeURIComponent(skill.slug) + '/body')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.error) { editor.value = '오류: ' + data.error; return; }
        editor.value = data.body || '';
        editor.disabled = false;
        saveBtn.disabled = false;
      })
      .catch(function (e) { editor.value = '불러오기 실패: ' + e.message; });

    saveBtn.onclick = function () {
      saveBtn.disabled = true;
      saveBtn.textContent = '저장 중...';
      fetch('/api/skill-builder/skills/' + encodeURIComponent(skill.slug) + '/body', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ body: editor.value }),
      })
        .then(function (r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        })
        .then(function () {
          overlay.remove();
          _activePanel = 'list';
          _render();
        })
        .catch(function (e) {
          alert('저장 실패: ' + e.message);
          saveBtn.disabled = false;
          saveBtn.textContent = '저장';
        });
    };
  }

  return { mountInShell: mountInShell };
})();
