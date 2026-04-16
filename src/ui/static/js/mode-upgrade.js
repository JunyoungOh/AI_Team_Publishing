/* mode-upgrade.js — 개발의뢰 탭 UI
 *
 * 서브탭 2개:
 *   1) 최초개발 (0→1): 새 앱을 처음부터 자율 개발
 *   2) 강화소: 기존 앱 폴더를 업그레이드
 *
 * WebSocket /ws/upgrade 연결 (두 서브탭 공유)
 */
var UpgradeManager = (function () {
  'use strict';

  var _ws = null;
  var _container = null;
  var _tabSwitcher = null;
  var _initialWrap = null;   // 최초개발 영구 컨테이너
  var _upgradeWrap = null;   // 강화소 영구 컨테이너
  var _running = false;

  /* Sidebar "running" indicator signal — see mode-chatbot.js for receiver.
     Defensive try/catch: signaling must NEVER break the mode itself. */
  function _signalRunning(on) {
    try {
      if (window.chatbotSignal) window.chatbotSignal('upgrade', on);
    } catch (_) { /* noop */ }
  }

  // 서브탭 상태
  var _subMode = 'initial';  // 'initial' | 'upgrade'

  // 강화소(업그레이드) 상태
  var _state = 'form';   // 'form' | 'analyzing' | 'questions' | 'developing' | 'done'
  var _folderPath = '';
  var _task = '';
  var _sessionId = '';
  var _backupPath = '';
  var _analysis = null;
  var _lastToolLabel = '';

  // 최초개발 상태
  var _devTask = '';
  var _devSessionId = '';
  var _devQuestions = '';
  var _devPaused = false;
  var _initialWsPanel = null;  // WorkspacePanel (파일 참고용)

  // Rate limit 대기 패널 상태 (자동 재개 카운트다운)
  var _rateLimitPanel = null;       // DOM node or null
  var _rateLimitInterval = null;    // setInterval handle
  var _manualRetryDebounceAt = 0;   // ms timestamp — 30초 debounce 기준
  var _activeSessionsChecked = false;  // 탭 첫 마운트 시 한 번만 체크

  function _clear(el) {
    while (el && el.firstChild) el.removeChild(el.firstChild);
  }

  function mountInShell(container) {
    // 이미 마운트됐으면 기존 구조 재사용 (상태 보존)
    if (_container === container && _initialWrap && _upgradeWrap) {
      _switchSubMode(_subMode);
      return;
    }

    _container = container;
    _clear(_container);

    // 서브탭 스위처 (영구)
    _tabSwitcher = document.createElement('div');
    _tabSwitcher.className = 'ot-tab-switcher';

    var initialBtn = document.createElement('button');
    initialBtn.textContent = '최초개발';
    initialBtn.className = 'ot-tab-btn';
    initialBtn.onclick = function () { _switchSubMode('initial'); };
    _tabSwitcher.appendChild(initialBtn);

    var upgradeBtn = document.createElement('button');
    upgradeBtn.textContent = '강화소';
    upgradeBtn.className = 'ot-tab-btn';
    upgradeBtn.onclick = function () { _switchSubMode('upgrade'); };
    _tabSwitcher.appendChild(upgradeBtn);

    _container.appendChild(_tabSwitcher);

    // 두 서브탭 영구 wrap
    _initialWrap = document.createElement('div');
    _initialWrap.className = 'au-subtab-wrap';
    _container.appendChild(_initialWrap);

    _upgradeWrap = document.createElement('div');
    _upgradeWrap.className = 'au-subtab-wrap';
    _container.appendChild(_upgradeWrap);

    // 초기 폼 렌더 (각 wrap에 한 번씩)
    _renderInitialForm();
    _renderUpgradeForm();

    _switchSubMode(_subMode);

    // 첫 마운트에서 진행 중인 개발 세션 있는지 확인 — 있으면 재개 카드 표시
    if (!_activeSessionsChecked) {
      _activeSessionsChecked = true;
      _checkActiveDevSessions();
    }
  }

  // ──────────────────────────────────────────────
  // 활성 세션 체크 + 재개 카드 (최초개발 한정)
  // ──────────────────────────────────────────────
  function _checkActiveDevSessions() {
    fetch('/api/dev-sessions/active', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : { sessions: [] }; })
      .then(function (body) {
        var sessions = (body && body.sessions) || [];
        if (sessions.length === 0) return;
        _renderResumeCard(sessions);
      })
      .catch(function () {});
  }

  function _renderResumeCard(sessions) {
    // 최초개발 wrap의 맨 위에 카드 삽입 (폼 유지 — 사용자가 "새로 시작" 선택 가능)
    if (!_initialWrap) return;

    var existing = document.getElementById('dev-resume-card');
    if (existing) existing.parentNode.removeChild(existing);

    var card = document.createElement('div');
    card.id = 'dev-resume-card';
    card.style.cssText = 'margin:0 0 16px 0;padding:14px 16px;background:rgba(56,139,253,0.08);border-left:3px solid var(--blue,#60a5fa);border-radius:6px;';

    var heading = document.createElement('div');
    heading.style.cssText = 'font-size:14px;font-weight:600;color:var(--text,#e6edf3);margin-bottom:8px;';
    heading.textContent = '⏸️ 진행 중인 개발 ' + sessions.length + '건';
    card.appendChild(heading);

    sessions.forEach(function (s) {
      var row = document.createElement('div');
      row.style.cssText = 'margin:8px 0;padding:10px;background:rgba(255,255,255,0.04);border-radius:4px;';

      var preview = document.createElement('div');
      preview.style.cssText = 'font-size:13px;color:var(--text,#e6edf3);margin-bottom:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
      preview.textContent = (s.task_preview || '(제목 없음)').slice(0, 120);
      row.appendChild(preview);

      var meta = document.createElement('div');
      meta.style.cssText = 'font-size:11px;color:var(--dim,#8b949e);margin-bottom:8px;';
      var stateLabel = s.state === 'waiting' ? '⏳ 대기 중' :
                       s.state === 'running' ? '🏃 실행 중' :
                       s.state === 'pending' ? '대기' : s.state;
      var metaText = stateLabel + ' · Phase: ' + (s.phase || '-') +
                     ' · 세션 #' + (s.session_number || 0);
      if (s.next_retry_at) {
        var mins = Math.max(0, Math.round((s.next_retry_at - Date.now() / 1000) / 60));
        metaText += ' · ' + (mins > 0 ? '약 ' + mins + '분 후 재개' : '재시도 예정');
      }
      meta.textContent = metaText;
      row.appendChild(meta);

      var btnRow = document.createElement('div');
      btnRow.style.cssText = 'display:flex;gap:8px;';

      var resumeBtn = document.createElement('button');
      resumeBtn.textContent = '▶ 이어보기';
      resumeBtn.style.cssText = 'padding:6px 12px;background:var(--blue,#388bfd);border:none;border-radius:4px;color:#fff;cursor:pointer;font-size:12px;';
      resumeBtn.onclick = function () { _resumeDevSession(s.session_id); };
      btnRow.appendChild(resumeBtn);

      var abandonBtn = document.createElement('button');
      abandonBtn.textContent = '✕ 포기';
      abandonBtn.style.cssText = 'padding:6px 12px;background:none;border:1px solid var(--border,rgba(255,255,255,0.1));border-radius:4px;color:var(--dim,#8b949e);cursor:pointer;font-size:12px;';
      abandonBtn.onclick = function () { _abandonDevSession(s.session_id, row); };
      btnRow.appendChild(abandonBtn);

      row.appendChild(btnRow);
      card.appendChild(row);
    });

    // 맨 위에 삽입
    if (_initialWrap.firstChild) {
      _initialWrap.insertBefore(card, _initialWrap.firstChild);
    } else {
      _initialWrap.appendChild(card);
    }
  }

  function _resumeDevSession(sessionId) {
    _devSessionId = sessionId;
    _running = true;
    _signalRunning(true);
    _connect();

    _clear(_initialWrap);
    _renderInitialDevProgress();

    var retries = 0;
    var send = function () {
      if (_ws && _ws.readyState === WebSocket.OPEN) {
        _ws.send(JSON.stringify({
          type: 'observe_dev',
          data: { session_id: sessionId },
        }));
      } else if (retries < 50) {
        retries++;
        setTimeout(send, 100);
      }
    };
    send();
  }

  function _abandonDevSession(sessionId, rowEl) {
    if (!confirm('이 세션을 포기합니다. 진행 중이었다면 중지되고, 상태 파일은 보존됩니다. 계속할까요?')) {
      return;
    }
    _connect();
    var retries = 0;
    var send = function () {
      if (_ws && _ws.readyState === WebSocket.OPEN) {
        _ws.send(JSON.stringify({
          type: 'observe_dev',
          data: { session_id: sessionId },
        }));
        setTimeout(function () {
          if (_ws && _ws.readyState === WebSocket.OPEN) {
            _ws.send(JSON.stringify({ type: 'stop_dev' }));
          }
        }, 300);
      } else if (retries < 50) {
        retries++;
        setTimeout(send, 100);
      }
    };
    send();
    if (rowEl && rowEl.parentNode) rowEl.parentNode.removeChild(rowEl);
  }

  function _applyDevSessionRestore(data) {
    _devSessionId = data.session_id;
    var phase = data.phase || 'dev';

    // Phase bar 복원: clarify는 항상 done, 현재 phase 활성
    var phases = ['clarify', 'dev', 'report'];
    phases.forEach(function (p) {
      var el = document.getElementById('dev-phase-' + p);
      if (!el) return;
      var baseLabel = el.textContent.replace(/^[●○✓]\s*/, '');
      var order = phases.indexOf(p);
      var currentOrder = phases.indexOf(phase);
      if (order < currentOrder || (order === currentOrder && data.dev_complete && p !== 'report')) {
        el.classList.add('done');
        el.textContent = '✓ ' + baseLabel;
      } else if (order === currentOrder) {
        el.classList.add('active');
        el.textContent = '● ' + baseLabel;
      }
    });

    _addDevLog('진행 중이던 세션을 이어서 봅니다 (세션 #' + (data.session_number || 0) + ')',
               'session_start');

    // waiting 상태이면 카운트다운 패널 즉시 띄움
    if (data.state === 'waiting' && data.next_retry_at) {
      _showRateLimitPanel({
        next_retry_at: data.next_retry_at,
        retry_count: data.backoff_index || 0,
        guard_remaining: data.guard_remaining,
      });
    }
  }

  function _switchSubMode(mode) {
    _subMode = mode;
    if (_initialWrap) _initialWrap.style.display = (mode === 'initial') ? '' : 'none';
    if (_upgradeWrap) _upgradeWrap.style.display = (mode === 'upgrade') ? '' : 'none';
    if (_tabSwitcher) {
      var btns = _tabSwitcher.querySelectorAll('.ot-tab-btn');
      for (var i = 0; i < btns.length; i++) {
        var btnMode = (i === 0) ? 'initial' : 'upgrade';
        btns[i].classList.toggle('active', btnMode === mode);
      }
    }
  }

  // 레거시 _render 호출 대응 (내부 흐름에서 'form으로 돌아가기' 등)
  function _render() {
    _switchSubMode(_subMode);
  }

  // ──────────────────────────────────────────────
  // 강화소 (기존 앱 업그레이드)
  // ──────────────────────────────────────────────
  function _renderUpgradeForm() {

    var form = document.createElement('div');
    form.className = 'ot-form';

    var title = document.createElement('h2');
    title.className = 'ot-title';
    title.textContent = '강화소';
    form.appendChild(title);

    var subtitle = document.createElement('p');
    subtitle.className = 'ot-subtitle';
    subtitle.textContent = '이미 있는 앱을 업그레이드합니다. 폴더와 지시사항을 입력하면, AI가 먼저 앱을 파악한 뒤 필요한 질문을 되묻고 자동으로 개발을 진행합니다.';
    form.appendChild(subtitle);

    // ── 폴더 경로 ──
    var folderLabel = document.createElement('label');
    folderLabel.className = 'ot-label';
    folderLabel.textContent = '대상 앱 폴더';
    form.appendChild(folderLabel);

    var folderHint = document.createElement('div');
    folderHint.className = 'st-cron-help';
    folderHint.textContent = '💡 "폴더 선택" 버튼이 가장 확실합니다. 드래그앤드롭은 브라우저 보안상 폴더 이름만 들어올 수 있어서 보정이 필요할 수 있어요.';
    form.appendChild(folderHint);

    var folderRow = document.createElement('div');
    folderRow.style.cssText = 'display:flex;gap:8px;align-items:stretch;';

    var folderInput = document.createElement('input');
    folderInput.type = 'text';
    folderInput.className = 'ot-input';
    folderInput.id = 'upgrade-folder-path';
    folderInput.placeholder = '예: /Users/me/projects/my-todo-app';
    folderInput.value = _folderPath;
    folderInput.style.flex = '1';
    folderRow.appendChild(folderInput);

    var pickBtn = document.createElement('button');
    pickBtn.type = 'button';
    pickBtn.className = 'ot-pick-btn';
    pickBtn.textContent = '📁 폴더 선택';
    pickBtn.style.cssText = 'padding:0 14px;background:var(--surface);border:1px solid var(--border);border-radius:8px;color:var(--text);cursor:pointer;font-size:13px;white-space:nowrap;';
    pickBtn.onclick = function () {
      pickBtn.disabled = true;
      var prevText = pickBtn.textContent;
      pickBtn.textContent = '여는 중…';
      fetch('/api/pick-folder', { method: 'POST' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data && data.ok && data.path) {
            folderInput.value = data.path;
          } else if (data && !data.cancelled && data.error) {
            alert('폴더 선택 다이얼로그를 열 수 없어요: ' + data.error);
          }
        })
        .catch(function (err) {
          alert('폴더 선택 요청 실패: ' + (err && err.message ? err.message : err));
        })
        .finally(function () {
          pickBtn.disabled = false;
          pickBtn.textContent = prevText;
        });
    };
    folderRow.appendChild(pickBtn);

    form.appendChild(folderRow);

    folderInput.addEventListener('dragover', function (e) {
      e.preventDefault();
      folderInput.classList.add('upgrade-drag-over');
    });
    folderInput.addEventListener('dragleave', function () {
      folderInput.classList.remove('upgrade-drag-over');
    });
    folderInput.addEventListener('drop', function (e) {
      e.preventDefault();
      folderInput.classList.remove('upgrade-drag-over');
      var dt = e.dataTransfer;
      if (!dt) return;

      // 베스트에포트 1: 모든 dataTransfer 타입을 훑어보며 file:// URI를 찾는다.
      // macOS Finder가 어떤 타입에 URI를 넣을지는 브라우저/버전에 따라 다르다.
      var types = [];
      try { types = Array.prototype.slice.call(dt.types || []); } catch (_) {}
      var candidates = [];
      types.forEach(function (t) {
        try {
          var v = dt.getData(t);
          if (v) candidates.push(v);
        } catch (_) {}
      });
      for (var i = 0; i < candidates.length; i++) {
        var lines = candidates[i].split(/\r?\n/);
        for (var j = 0; j < lines.length; j++) {
          var line = lines[j];
          if (!line || line.indexOf('#') === 0) continue;
          if (line.indexOf('file://') === 0) {
            try {
              var decoded = decodeURIComponent(line.replace(/^file:\/\/(localhost)?/, ''));
              if (decoded) { folderInput.value = decoded.replace(/\/$/, ''); return; }
            } catch (_) {}
          }
          // 경로처럼 보이는 문자열도 받아준다 (사용자가 텍스트로 경로를 드래그한 경우).
          if (line.charAt(0) === '/' || /^[A-Za-z]:\\/.test(line)) {
            folderInput.value = line.replace(/\/$/, '');
            return;
          }
        }
      }

      // Fallback: 절대경로를 못 얻으면 폴더 이름이라도 입력해서 사용자가 보정할 수 있게 한다.
      // (브라우저 보안상 드래그된 폴더의 절대경로는 종종 노출되지 않는다 — 정확한 경로가 필요하면
      // 옆의 "📁 폴더 선택" 버튼을 사용한다.)
      var files = dt.files;
      if (files && files.length > 0 && files[0].name) {
        folderInput.value = files[0].name;
      }
    });

    // ── 지시사항 ──
    var taskLabel = document.createElement('label');
    taskLabel.className = 'ot-label';
    taskLabel.textContent = '업그레이드 지시사항';
    form.appendChild(taskLabel);

    var taskInput = document.createElement('textarea');
    taskInput.className = 'dev-task-input';
    taskInput.id = 'upgrade-task';
    taskInput.placeholder = '예: 메인 화면에 다크모드 토글 버튼 추가. 설정은 localStorage에 저장해서 다음 실행 시에도 유지되게.';
    taskInput.rows = 5;
    taskInput.value = _task;
    form.appendChild(taskInput);

    // ── 시작 버튼 ──
    var startBtn = document.createElement('button');
    startBtn.className = 'ot-start-btn';
    startBtn.textContent = '분석 시작';
    startBtn.onclick = function () {
      var folder = folderInput.value.trim();
      var task = taskInput.value.trim();
      if (!folder) { alert('대상 앱 폴더 경로를 입력해주세요.'); return; }
      if (!task) { alert('업그레이드 지시사항을 입력해주세요.'); return; }
      _folderPath = folder;
      _task = task;
      _startAnalyze();
    };
    form.appendChild(startBtn);

    _clear(_upgradeWrap);
    _upgradeWrap.appendChild(form);

    var progress = document.createElement('div');
    progress.id = 'upgrade-analyze-panel';
    progress.style.display = 'none';
    _upgradeWrap.appendChild(progress);
  }

  function _startAnalyze() {
    _state = 'analyzing';
    _running = true;
    _signalRunning(true);
    _connect();

    var startBtn = _upgradeWrap.querySelector('.ot-start-btn');
    if (startBtn) { startBtn.disabled = true; startBtn.textContent = '분석 중...'; }

    var panel = document.getElementById('upgrade-analyze-panel');
    panel.style.display = '';
    _clear(panel);

    var statusText = document.createElement('div');
    statusText.id = 'upgrade-status';
    statusText.className = 'upgrade-status';
    statusText.textContent = '백업 생성 + 앱 분석 중...';
    panel.appendChild(statusText);

    var activityLine = document.createElement('div');
    activityLine.id = 'upgrade-activity';
    activityLine.className = 'upgrade-activity';
    panel.appendChild(activityLine);

    var retries = 0;
    var send = function () {
      if (_ws && _ws.readyState === WebSocket.OPEN) {
        _ws.send(JSON.stringify({
          type: 'start_upgrade_analyze',
          data: { folder_path: _folderPath, task: _task },
        }));
      } else if (retries < 50) {
        retries++;
        setTimeout(send, 100);
      }
    };
    send();
  }

  function _connect() {
    if (_ws && (_ws.readyState === WebSocket.OPEN || _ws.readyState === WebSocket.CONNECTING)) return;
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var url = proto + '//' + location.host + '/ws/upgrade';
    _ws = new WebSocket(url);

    _ws.onmessage = function (e) {
      try { _handleMessage(JSON.parse(e.data)); } catch (err) {}
    };
    _ws.onclose = function () { _ws = null; _signalRunning(false); };
  }

  function _handleMessage(msg) {
    var type = msg.type;
    var data = msg.data || {};

    // ── 강화소 흐름 ──
    if (type === 'upgrade_progress') {
      _handleProgress(data);
    } else if (type === 'upgrade_activity') {
      _updateActivity(data);
    } else if (type === 'upgrade_analyze_result') {
      _sessionId = data.session_id || '';
      _backupPath = data.backup_path || '';
      _analysis = data.analysis || {};
      _folderPath = data.folder_path || _folderPath;
      _showAnalysisAndQuestions();
    } else if (type === 'upgrade_dev_started') {
      _addLog('업그레이드 시작');
    } else if (type === 'upgrade_stopped') {
      _running = false;
      _signalRunning(false);
      _addLog('중단되었습니다.');
    }
    // ── 최초개발 흐름 ──
    else if (type === 'dev_clarify_questions') {
      _showDevQuestions(data.questions, data.session_id);
    } else if (type === 'dev_started') {
      _addDevLog('개발이 시작되었습니다', 'session_start');
    } else if (type === 'dev_session_restore') {
      // observe_dev 응답 — 재접속한 세션의 상태를 UI로 복원
      _applyDevSessionRestore(data);
    } else if (type === 'dev_progress') {
      _handleDevProgress(data);
    } else if (type === 'overtime_activity') {
      // 최초개발 세션 중일 때만 dev 로그에 도구 상태 반영
      if (document.getElementById('dev-log')) {
        var label = data.label || data.tool || '';
        var count = data.count || 0;
        var detail = data.detail || '';
        _updateDevToolStatus(label, count, detail);
      }
    } else if (type === 'overtime_stopped') {
      _running = false;
      _signalRunning(false);
      _addDevLog('중단되었습니다', 'rate_limited');
    }
    // ── 공통 ──
    else if (type === 'error') {
      _running = false;
      _signalRunning(false);
      alert('오류: ' + (data.message || '알 수 없는 오류'));
      if (_subMode === 'initial') {
        _devPaused = false;
        _renderInitialForm();
      } else {
        _state = 'form';
        _renderUpgradeForm();
      }
    }
  }

  function _handleProgress(data) {
    var phase = data.phase;
    var action = data.action;
    var msg = data.message || '';

    if (_state === 'analyzing' || _state === 'questions') {
      // 분석 단계: 기존 간단 패널 갱신 (사용자 요청대로 그대로 유지)
      var statusEl = document.getElementById('upgrade-status');
      if (statusEl && msg) statusEl.textContent = msg;
      return;
    }

    // developing / done: Phase bar + 로그 방식 (야근팀 dev 스타일)
    _updatePhaseBar(phase, action, data);

    if (msg) _addLog(msg, action);

    if (phase === 'report' && action === 'complete') {
      _state = 'done';
      _running = false;
      _signalRunning(false);
      _showCompletion(data);
    }
  }

  function _updatePhaseBar(phase, action, data) {
    var phaseEl = document.getElementById('upgrade-phase-' + phase);
    if (!phaseEl) return;

    var baseLabel = phaseEl.dataset.baseLabel || phaseEl.textContent;
    phaseEl.dataset.baseLabel = baseLabel;

    if (action === 'complete') {
      phaseEl.classList.add('done');
      phaseEl.classList.remove('active');
      phaseEl.textContent = '✓ ' + baseLabel;
      var conn = document.getElementById('upgrade-conn-' + phase);
      if (conn) conn.classList.add('done');
      return;
    }

    if (action === 'session_start' && data.session_number) {
      phaseEl.classList.add('active');
      phaseEl.textContent = '● ' + baseLabel + ' (세션 #' + data.session_number + ')';
      return;
    }

    if (action === 'handoff') {
      // 세션 전환 — active 유지, Phase bar 텍스트는 session_start 에서 갱신됨
      return;
    }

    // 일반 진행 상태 — active 상태 표시
    phaseEl.classList.add('active');
    if (!phaseEl.textContent.startsWith('●') && !phaseEl.classList.contains('done')) {
      phaseEl.textContent = '● ' + baseLabel;
    }
  }

  function _formatToolLine(label, count, detail) {
    // 기본: "🔧 검색 (3회)" — detail 있으면 "🔧 검색 — '생태계 동향' (3회)"
    var text = '🔧 ' + label;
    if (detail) text += ' — ' + detail;
    text += ' (' + count + '회)';
    return text;
  }

  function _updateActivity(data) {
    var label = data.label || data.tool || '';
    var count = data.count || 0;
    var detail = data.detail || '';

    // 분석 단계: 간단한 한 줄 인디케이터 (detail이 있으면 함께 표시)
    if (_state === 'analyzing' || _state === 'questions') {
      var el = document.getElementById('upgrade-activity');
      if (el) {
        el.textContent = detail
          ? '🔧 ' + label + ' — ' + detail + ' × ' + count
          : '🔧 ' + label + ' × ' + count;
      }
      return;
    }

    // 개발 단계: 야근팀 dev 스타일 — 같은 도구 연속이면 마지막 줄 갱신
    if (_state !== 'developing') return;
    var logArea = document.getElementById('upgrade-log');
    if (!logArea) return;

    if (label === _lastToolLabel) {
      var lastEl = document.getElementById('upgrade-tool-last');
      if (lastEl) {
        // 같은 도구라도 detail이 바뀌면 최신 detail로 갱신 (예: 다른 파일 읽기)
        lastEl.textContent = _formatToolLine(label, count, detail);
        logArea.scrollTop = logArea.scrollHeight;
        return;
      }
    }

    var prev = document.getElementById('upgrade-tool-last');
    if (prev) prev.removeAttribute('id');

    var newEl = document.createElement('div');
    newEl.id = 'upgrade-tool-last';
    newEl.style.cssText = 'padding:4px 0;font-size:12px;color:var(--blue,#60a5fa);border-bottom:1px solid var(--border,rgba(255,255,255,0.08));';
    newEl.textContent = _formatToolLine(label, count, detail);
    logArea.appendChild(newEl);
    logArea.scrollTop = logArea.scrollHeight;
    _lastToolLabel = label;
  }

  function _showAnalysisAndQuestions() {
    _state = 'questions';
    _clear(_upgradeWrap);

    var panel = document.createElement('div');
    panel.className = 'ot-form';

    var title = document.createElement('h2');
    title.className = 'ot-title';
    title.textContent = '앱 분석 결과';
    panel.appendChild(title);

    var summaryCard = document.createElement('div');
    summaryCard.className = 'upgrade-summary-card';

    var summary = document.createElement('div');
    summary.className = 'upgrade-summary-text';
    summary.textContent = _analysis.summary || '앱 요약을 파악할 수 없었습니다.';
    summaryCard.appendChild(summary);

    if (_analysis.stack && _analysis.stack.length > 0) {
      var stackRow = document.createElement('div');
      stackRow.className = 'upgrade-chip-row';
      _analysis.stack.forEach(function (s) {
        var chip = document.createElement('span');
        chip.className = 'upgrade-chip';
        chip.textContent = s;
        stackRow.appendChild(chip);
      });
      summaryCard.appendChild(stackRow);
    }

    if (_backupPath) {
      var backupNote = document.createElement('div');
      backupNote.className = 'upgrade-backup-note';
      backupNote.textContent = '🛟 백업 완료: ' + _backupPath;
      summaryCard.appendChild(backupNote);
    }

    panel.appendChild(summaryCard);

    var qTitle = document.createElement('h3');
    qTitle.className = 'upgrade-q-title';
    qTitle.textContent = '몇 가지만 확인할게요';
    panel.appendChild(qTitle);

    var qList = document.createElement('ol');
    qList.className = 'upgrade-q-list';
    (_analysis.questions || []).forEach(function (q) {
      var li = document.createElement('li');
      li.textContent = q;
      qList.appendChild(li);
    });
    panel.appendChild(qList);

    if (_analysis.concerns && _analysis.concerns.length > 0) {
      var concernTitle = document.createElement('div');
      concernTitle.className = 'upgrade-concern-title';
      concernTitle.textContent = '⚠️ 주의할 점';
      panel.appendChild(concernTitle);

      var concernList = document.createElement('ul');
      concernList.className = 'upgrade-concern-list';
      _analysis.concerns.forEach(function (c) {
        var li = document.createElement('li');
        li.textContent = c;
        concernList.appendChild(li);
      });
      panel.appendChild(concernList);
    }

    var ansLabel = document.createElement('label');
    ansLabel.className = 'ot-label';
    ansLabel.textContent = '답변';
    panel.appendChild(ansLabel);

    var ansInput = document.createElement('textarea');
    ansInput.className = 'dev-task-input';
    ansInput.id = 'upgrade-answers';
    ansInput.placeholder = '위 질문에 자유롭게 답변하세요. 모든 질문에 답하지 않아도 됩니다.';
    ansInput.rows = 6;
    panel.appendChild(ansInput);

    var startBtn = document.createElement('button');
    startBtn.className = 'ot-start-btn';
    startBtn.textContent = '업그레이드 시작';
    startBtn.onclick = function () {
      _startDev(ansInput.value.trim());
    };
    panel.appendChild(startBtn);

    var skipBtn = document.createElement('button');
    skipBtn.className = 'ot-skip-btn';
    skipBtn.style.cssText = 'margin-top:8px;background:none;border:1px solid var(--border,rgba(255,255,255,0.08));color:var(--dim,#8b949e);padding:8px 16px;border-radius:8px;cursor:pointer;width:100%;font-size:14px;';
    skipBtn.textContent = '건너뛰고 바로 업그레이드';
    skipBtn.onclick = function () { _startDev(''); };
    panel.appendChild(skipBtn);

    _upgradeWrap.appendChild(panel);
  }

  function _startDev(answers) {
    _state = 'developing';
    _running = true;
    _signalRunning(true);

    _clear(_upgradeWrap);
    _renderUpgradeProgress();

    var retries = 0;
    var send = function () {
      if (_ws && _ws.readyState === WebSocket.OPEN) {
        _ws.send(JSON.stringify({
          type: 'start_upgrade_dev',
          data: {
            folder_path: _folderPath,
            task: _task,
            answers: answers,
            backup_path: _backupPath,
            analysis: _analysis,
            session_id: _sessionId,
          },
        }));
      } else if (retries < 50) {
        retries++;
        setTimeout(send, 100);
      }
    };
    send();
  }

  function _renderUpgradeProgress() {
    _lastToolLabel = '';
    var wrap = document.createElement('div');
    wrap.id = 'upgrade-progress';
    wrap.style.padding = '20px';

    // Phase bar — 5단계 (백업/분석/질문 이미 완료, 개발/리포트 남음)
    var phaseBar = document.createElement('div');
    phaseBar.className = 'dev-phase-bar';
    phaseBar.id = 'upgrade-phase-bar';

    var phases = [
      { id: 'backup', label: '백업', done: true },
      { id: 'analyze', label: '분석', done: true },
      { id: 'clarify', label: '질문', done: true },
      { id: 'dev', label: '개발', done: false },
      { id: 'report', label: '리포트', done: false },
    ];

    phases.forEach(function (p, i) {
      if (i > 0) {
        var conn = document.createElement('div');
        conn.className = 'dev-phase-connector';
        conn.id = 'upgrade-conn-' + p.id;
        // 이전 단계가 완료된 상태면 커넥터도 completed
        if (phases[i - 1].done) conn.classList.add('done');
        phaseBar.appendChild(conn);
      }
      var item = document.createElement('div');
      item.className = 'dev-phase-item' + (p.done ? ' done' : '');
      item.id = 'upgrade-phase-' + p.id;
      item.dataset.baseLabel = p.label;
      item.textContent = p.done ? '✓ ' + p.label : p.label;
      phaseBar.appendChild(item);
    });

    wrap.appendChild(phaseBar);

    // 중지 버튼
    var stopBtn = document.createElement('button');
    stopBtn.id = 'upgrade-stop-btn';
    stopBtn.className = 'dev-stop-btn';
    stopBtn.textContent = '■ 중지';
    stopBtn.onclick = function () {
      if (_ws && _ws.readyState === WebSocket.OPEN) {
        _ws.send(JSON.stringify({ type: 'stop_upgrade' }));
      }
    };
    wrap.appendChild(stopBtn);

    // 로그 영역 (야근팀 dev 스타일)
    var logArea = document.createElement('div');
    logArea.id = 'upgrade-log';
    logArea.style.cssText = 'margin-top:16px;max-height:400px;overflow-y:auto;';
    wrap.appendChild(logArea);

    _upgradeWrap.appendChild(wrap);
  }

  function _addLog(text, type) {
    if (!text) return;
    var log = document.getElementById('upgrade-log');
    if (!log) return;
    var entry = document.createElement('div');
    entry.style.cssText = 'padding:6px 0;font-size:13px;color:var(--dim,#8b949e);border-bottom:1px solid var(--border,rgba(255,255,255,0.08));';

    var time = new Date().toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' });
    var icon = '📋';
    if (type === 'rate_limited') icon = '⏸️';
    else if (type === 'complete') icon = '✅';
    else if (type === 'error') icon = '❌';
    else if (type === 'session_start') icon = '🚀';
    else if (type === 'handoff') icon = '🔄';
    else if (type === 'generating') icon = '📝';
    else if (type === 'max_sessions') icon = '⚠️';

    entry.textContent = time + ' ' + icon + ' ' + text;
    log.appendChild(entry);
    log.scrollTop = log.scrollHeight;
  }

  function _showCompletion(data) {
    // 중지 버튼 숨김 (야근팀 dev 패턴)
    var stopBtn = document.getElementById('upgrade-stop-btn');
    if (stopBtn) stopBtn.style.display = 'none';

    var logArea = document.getElementById('upgrade-log');
    if (!logArea) return;

    var linkWrap = document.createElement('div');
    linkWrap.style.cssText = 'margin-top:16px;display:flex;gap:8px;flex-wrap:wrap;';

    if (data.report_path) {
      var reportBtn = document.createElement('button');
      reportBtn.className = 'ot-start-btn';
      reportBtn.textContent = '📄 업그레이드 리포트 보기';
      reportBtn.onclick = function () { window.open(data.report_path + '/results.html', '_blank'); };
      linkWrap.appendChild(reportBtn);
    }

    if (data.folder_path) {
      var folderBtn = document.createElement('button');
      folderBtn.style.cssText = 'padding:10px 16px;background:var(--surface);border:1px solid var(--border);border-radius:8px;color:var(--text);cursor:pointer;font-size:14px;';
      folderBtn.textContent = '📁 앱 폴더 열기';
      folderBtn.onclick = function () {
        fetch('/api/open-folder', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: data.folder_path }),
        }).catch(function () {});
      };
      linkWrap.appendChild(folderBtn);
    }

    logArea.appendChild(linkWrap);

    if (data.backup_path) {
      var backupInfo = document.createElement('div');
      backupInfo.style.cssText = 'margin-top:12px;padding:8px 12px;background:rgba(63,185,80,0.06);border-left:3px solid var(--green,#3FB950);font-size:12px;color:var(--dim,#8b949e);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-all;';
      backupInfo.textContent = '🛟 복원 지점: ' + data.backup_path;
      logArea.appendChild(backupInfo);
    }

    // 처음으로 돌아가기 버튼 (야근팀 스타일)
    var homeWrap = document.createElement('div');
    homeWrap.style.cssText = 'margin-top:12px;';
    var homeBtn = document.createElement('button');
    homeBtn.style.cssText = 'padding:10px 16px;background:none;border:1px solid var(--border,rgba(255,255,255,0.08));border-radius:8px;color:var(--dim,#8b949e);cursor:pointer;font-size:14px;width:100%;';
    homeBtn.textContent = '← 처음으로';
    homeBtn.onclick = function () {
      _state = 'form';
      _folderPath = '';
      _task = '';
      _sessionId = '';
      _backupPath = '';
      _analysis = null;
      _running = false;
      _signalRunning(false);
      _renderUpgradeForm();  // 강화소 wrap만 초기화
    };
    homeWrap.appendChild(homeBtn);
    logArea.appendChild(homeWrap);
  }

  // ──────────────────────────────────────────────
  // 최초개발 (0→1 새 앱 생성)
  // ──────────────────────────────────────────────
  function _renderInitialForm() {
    _connect();

    var form = document.createElement('div');
    form.className = 'ot-form';

    var title = document.createElement('h2');
    title.className = 'ot-title';
    title.textContent = '최초개발';
    form.appendChild(title);

    var subtitle = document.createElement('p');
    subtitle.className = 'ot-subtitle';
    subtitle.textContent = '만들고 싶은 앱을 설명하면 AI가 자동으로 개발합니다. 로컬에서 바로 실행 가능한 앱이 만들어집니다.';
    form.appendChild(subtitle);

    var taskLabel = document.createElement('label');
    taskLabel.textContent = '만들고 싶은 앱';
    taskLabel.className = 'ot-label';
    form.appendChild(taskLabel);

    var taskInput = document.createElement('textarea');
    taskInput.className = 'dev-task-input';
    taskInput.placeholder = '예: 할일 관리 앱을 만들어줘. 할일을 추가하고 완료 체크하고, 날짜별로 정리할 수 있었으면 좋겠어.';
    taskInput.rows = 5;
    form.appendChild(taskInput);

    // WorkspacePanel — 참고할 파일 선택 (야근팀 dev와 동일 패턴)
    var wsSection = document.createElement('div');
    wsSection.className = 'ot-field';
    if (typeof WorkspacePanel !== 'undefined' && WorkspacePanel.create) {
      _initialWsPanel = WorkspacePanel.create(wsSection, 'overtime');
    }
    form.appendChild(wsSection);

    var startBtn = document.createElement('button');
    startBtn.className = 'ot-start-btn';
    startBtn.textContent = '개발 시작';
    startBtn.onclick = function () {
      var task = taskInput.value.trim();
      if (!task) { alert('만들고 싶은 앱을 설명해주세요.'); return; }
      _devTask = task;
      startBtn.disabled = true;
      startBtn.textContent = '질문 생성 중...';
      _connect();
      // 사용자가 선택한 파일명 목록을 clarify 단계부터 같이 전송한다.
      // 서버는 data/workspace/overtime/input/{name} 절대경로로 변환해
      // clarify LLM에게 "이 파일은 이미 선택됨"을 알려준다.
      var wsFiles = _initialWsPanel ? _initialWsPanel.getSelectedFiles() : [];
      var retries = 0;
      var sendClarify = function () {
        if (_ws && _ws.readyState === WebSocket.OPEN) {
          _ws.send(JSON.stringify({
            type: 'start_dev_clarify',
            data: { task: task, workspace_files: wsFiles },
          }));
        } else if (retries < 50) {
          retries++;
          setTimeout(sendClarify, 100);
        }
      };
      sendClarify();
    };
    form.appendChild(startBtn);

    _clear(_initialWrap);
    _initialWrap.appendChild(form);
  }

  function _showDevQuestions(questions, sessionId) {
    _devSessionId = sessionId;
    _devQuestions = questions;

    _clear(_initialWrap);

    var panel = document.createElement('div');
    panel.className = 'ot-form';

    var title = document.createElement('h2');
    title.className = 'ot-title';
    title.textContent = '몇 가지만 확인할게요';
    panel.appendChild(title);

    // 질문 텍스트 (marked 안전하게 fallback)
    var qText = document.createElement('div');
    qText.className = 'dev-questions-text';
    if (typeof marked !== 'undefined' && marked.parse) {
      try {
        qText.innerHTML = marked.parse(questions); // eslint-disable-line no-unsanitized/property
      } catch (_) {
        qText.textContent = questions;
      }
    } else {
      qText.textContent = questions;
    }
    panel.appendChild(qText);

    var ansLabel = document.createElement('label');
    ansLabel.textContent = '답변';
    ansLabel.className = 'ot-label';
    panel.appendChild(ansLabel);

    var ansInput = document.createElement('textarea');
    ansInput.className = 'dev-task-input';
    ansInput.placeholder = '위 질문에 자유롭게 답변해주세요. 모든 질문에 답하지 않아도 됩니다.';
    ansInput.rows = 6;
    panel.appendChild(ansInput);

    var startBtn = document.createElement('button');
    startBtn.className = 'ot-start-btn';
    startBtn.textContent = '개발 시작';
    startBtn.onclick = function () {
      _startInitialDev(_devTask, ansInput.value.trim(), _devSessionId);
    };
    panel.appendChild(startBtn);

    var skipBtn = document.createElement('button');
    skipBtn.className = 'ot-skip-btn';
    skipBtn.style.cssText = 'margin-top:8px;background:none;border:1px solid var(--border,rgba(255,255,255,0.08));color:var(--dim,#8b949e);padding:8px 16px;border-radius:8px;cursor:pointer;width:100%;font-size:14px;';
    skipBtn.textContent = '건너뛰고 바로 개발 시작';
    skipBtn.onclick = function () { _startInitialDev(_devTask, '', _devSessionId); };
    panel.appendChild(skipBtn);

    _initialWrap.appendChild(panel);
  }

  function _startInitialDev(task, answers, sessionId) {
    _running = true;
    _signalRunning(true);
    var wsFiles = _initialWsPanel ? _initialWsPanel.getSelectedFiles() : [];

    _clear(_initialWrap);
    _renderInitialDevProgress();

    var retries = 0;
    var sendStart = function () {
      if (_ws && _ws.readyState === WebSocket.OPEN) {
        _ws.send(JSON.stringify({
          type: 'start_dev',
          data: {
            task: task,
            answers: answers,
            session_id: sessionId,
            workspace_files: wsFiles,
          },
        }));
      } else if (retries < 50) {
        retries++;
        setTimeout(sendStart, 100);
      }
    };
    sendStart();
  }

  function _renderInitialDevProgress() {
    _lastToolLabel = '';

    var wrap = document.createElement('div');
    wrap.id = 'dev-progress';
    wrap.style.padding = '20px';

    // Phase bar
    var phaseBar = document.createElement('div');
    phaseBar.className = 'dev-phase-bar';

    var phases = [
      { id: 'clarify', label: '질문' },
      { id: 'dev', label: '개발' },
      { id: 'report', label: '리포트' },
    ];

    phases.forEach(function (p, i) {
      if (i > 0) {
        var conn = document.createElement('div');
        conn.className = 'dev-phase-connector';
        conn.id = 'dev-conn-' + p.id;
        if (p.id === 'dev') conn.classList.add('done');
        phaseBar.appendChild(conn);
      }
      var item = document.createElement('div');
      item.className = 'dev-phase-item';
      item.id = 'dev-phase-' + p.id;
      item.textContent = p.label;
      if (p.id === 'clarify') {
        item.classList.add('done');
        item.textContent = '✓ ' + p.label;
      }
      phaseBar.appendChild(item);
    });

    wrap.appendChild(phaseBar);

    var stopBtn = document.createElement('button');
    stopBtn.id = 'dev-stop-btn';
    stopBtn.className = 'dev-stop-btn';
    stopBtn.textContent = '■ 중지';
    stopBtn.onclick = function () { _handleDevStop(); };
    wrap.appendChild(stopBtn);

    var logArea = document.createElement('div');
    logArea.id = 'dev-log';
    logArea.style.cssText = 'margin-top:16px;max-height:400px;overflow-y:auto;';
    wrap.appendChild(logArea);

    _initialWrap.appendChild(wrap);
  }

  function _handleDevProgress(data) {
    var phase = data.phase;
    var action = data.action;

    // ── Rate limit 자동 재개 흐름 ──
    if (action === 'rate_limited') {
      _showRateLimitPanel(data);
      if (data.message) _addDevLog(data.message, 'rate_limited');
      return;
    }
    if (action === 'retrying') {
      _hideRateLimitPanel();
      var trigger = data.trigger === 'manual' ? '수동 재시도' : '자동 재시도';
      _addDevLog(data.message || trigger, 'retrying');
      return;
    }
    if (action === 'guard_triggered') {
      _hideRateLimitPanel();
      _addDevLog(data.message || '자동 재개 중지', 'error');
      var stopBtn = document.getElementById('dev-stop-btn');
      if (stopBtn) stopBtn.style.display = 'none';
      return;
    }

    // ── 일반 Phase bar 업데이트 ──
    var phaseEl = document.getElementById('dev-phase-' + phase);
    if (phaseEl) {
      if (action === 'complete') {
        phaseEl.classList.add('done');
        phaseEl.classList.remove('active');
        var label = phaseEl.textContent.replace(/^[●○✓]\s*/, '');
        phaseEl.textContent = '✓ ' + label;
        var conn = document.getElementById('dev-conn-' + phase);
        if (conn) conn.classList.add('done');
      } else if (action !== 'generating') {
        phaseEl.classList.add('active');
        var label2 = phaseEl.textContent.replace(/^[●○✓]\s*/, '');
        if (!phaseEl.textContent.startsWith('●')) {
          phaseEl.textContent = '● ' + label2;
        }
      }
    }

    if (data.message) _addDevLog(data.message, action);

    if (phase === 'report' && action === 'complete' && data.report_path) {
      _addDevReportLink(data.report_path, data.app_dir);
    }
  }

  // ──────────────────────────────────────────────
  // Rate limit 카운트다운 패널 + 수동 재시도 버튼
  // ──────────────────────────────────────────────
  function _showRateLimitPanel(data) {
    _hideRateLimitPanel();  // 기존 패널 있으면 교체

    var wrap = document.getElementById('dev-progress');
    if (!wrap) return;

    var panel = document.createElement('div');
    panel.id = 'dev-rate-limit-panel';
    panel.style.cssText = 'margin:12px 0;padding:14px 16px;background:rgba(240,136,62,0.08);border-left:3px solid var(--orange,#f0883e);border-radius:6px;';

    var heading = document.createElement('div');
    heading.style.cssText = 'font-size:13px;font-weight:600;color:var(--text,#e6edf3);margin-bottom:6px;';
    heading.textContent = '⏳ 사용량 한도 도달 — 자동 재개 대기 중';
    panel.appendChild(heading);

    var countdownEl = document.createElement('div');
    countdownEl.id = 'dev-rate-limit-countdown';
    countdownEl.style.cssText = 'font-size:20px;font-weight:700;color:var(--orange,#f0883e);font-variant-numeric:tabular-nums;margin:6px 0;';
    panel.appendChild(countdownEl);

    var meta = document.createElement('div');
    meta.style.cssText = 'font-size:12px;color:var(--dim,#8b949e);';
    var retryCount = data.retry_count || 0;
    var guardRemaining = typeof data.guard_remaining === 'number' ? data.guard_remaining : '-';
    meta.textContent = '재시도 ' + retryCount + '회 누적 · 6시간 내 ' + guardRemaining + '회 더 가능';
    panel.appendChild(meta);

    // 지금 시도 버튼
    var btnRow = document.createElement('div');
    btnRow.style.cssText = 'margin-top:10px;';

    var manualBtn = document.createElement('button');
    manualBtn.id = 'dev-manual-retry-btn';
    manualBtn.textContent = '🔄 지금 시도';
    manualBtn.style.cssText = 'padding:8px 16px;background:var(--orange,#f0883e);border:none;border-radius:6px;color:#fff;cursor:pointer;font-size:13px;font-weight:600;';
    manualBtn.onclick = function () { _handleManualRetry(manualBtn); };
    btnRow.appendChild(manualBtn);

    panel.appendChild(btnRow);

    // Phase bar와 Stop 버튼 다음, Log 영역 위에 삽입
    var logArea = document.getElementById('dev-log');
    if (logArea) {
      wrap.insertBefore(panel, logArea);
    } else {
      wrap.appendChild(panel);
    }

    _rateLimitPanel = panel;

    // 카운트다운 시작
    var nextAt = data.next_retry_at || 0;  // unix seconds
    _updateRateLimitCountdown(nextAt);
    _rateLimitInterval = setInterval(function () {
      _updateRateLimitCountdown(nextAt);
    }, 1000);
  }

  function _updateRateLimitCountdown(nextAt) {
    var el = document.getElementById('dev-rate-limit-countdown');
    if (!el) return;
    var remaining = Math.max(0, Math.round(nextAt - Date.now() / 1000));
    var mm = Math.floor(remaining / 60);
    var ss = remaining % 60;
    var pad = function (n) { return n < 10 ? '0' + n : '' + n; };
    el.textContent = pad(mm) + ':' + pad(ss) + (remaining === 0 ? '  (재시도 예정)' : '');
  }

  function _hideRateLimitPanel() {
    if (_rateLimitInterval) {
      clearInterval(_rateLimitInterval);
      _rateLimitInterval = null;
    }
    if (_rateLimitPanel && _rateLimitPanel.parentNode) {
      _rateLimitPanel.parentNode.removeChild(_rateLimitPanel);
    }
    _rateLimitPanel = null;
  }

  function _handleManualRetry(btn) {
    var now = Date.now();
    if (now - _manualRetryDebounceAt < 30_000) {
      // 30초 debounce — 전송 안 함
      return;
    }
    _manualRetryDebounceAt = now;
    if (_ws && _ws.readyState === WebSocket.OPEN) {
      _ws.send(JSON.stringify({
        type: 'manual_retry',
        data: { session_id: _devSessionId },
      }));
    }
    // UI: 버튼 비활성 + 30초 후 재활성
    btn.disabled = true;
    var origText = btn.textContent;
    btn.textContent = '⏱ 방금 시도했어요 (30초)';
    setTimeout(function () {
      if (btn && btn.parentNode) {
        btn.disabled = false;
        btn.textContent = origText;
      }
    }, 30_000);
  }

  function _updateDevToolStatus(label, count, detail) {
    var logArea = document.getElementById('dev-log');
    if (!logArea) return;
    detail = detail || '';

    if (label === _lastToolLabel) {
      var lastEl = document.getElementById('dev-tool-last');
      if (lastEl) {
        lastEl.textContent = _formatToolLine(label, count, detail);
        logArea.scrollTop = logArea.scrollHeight;
        return;
      }
    }

    var prev = document.getElementById('dev-tool-last');
    if (prev) prev.removeAttribute('id');

    var el = document.createElement('div');
    el.id = 'dev-tool-last';
    el.style.cssText = 'padding:4px 0;font-size:12px;color:var(--blue,#60a5fa);border-bottom:1px solid var(--border,rgba(255,255,255,0.08));';
    el.textContent = _formatToolLine(label, count, detail);
    logArea.appendChild(el);
    logArea.scrollTop = logArea.scrollHeight;
    _lastToolLabel = label;
  }

  function _addDevLog(message, type) {
    var logArea = document.getElementById('dev-log');
    if (!logArea) return;
    var entry = document.createElement('div');
    entry.style.cssText = 'padding:6px 0;font-size:13px;color:var(--dim,#8b949e);border-bottom:1px solid var(--border,rgba(255,255,255,0.08));';

    var time = new Date().toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' });
    var icon = '📋';
    if (type === 'rate_limited') icon = '⏸️';
    else if (type === 'complete') icon = '✅';
    else if (type === 'error') icon = '❌';
    else if (type === 'session_start') icon = '🚀';
    else if (type === 'handoff') icon = '🔄';
    else if (type === 'generating') icon = '📝';

    entry.textContent = time + ' ' + icon + ' ' + message;
    logArea.appendChild(entry);
    logArea.scrollTop = logArea.scrollHeight;
  }

  function _addDevReportLink(reportPath, appDir) {
    _hideRateLimitPanel();  // 완료 시 대기 패널 정리
    var logArea = document.getElementById('dev-log');
    if (!logArea) return;

    var linkWrap = document.createElement('div');
    linkWrap.style.cssText = 'margin-top:16px;display:flex;gap:8px;';

    var reportBtn = document.createElement('button');
    reportBtn.className = 'ot-start-btn';
    reportBtn.style.width = 'auto';
    reportBtn.textContent = '📄 리포트 + 실행 가이드 보기';
    reportBtn.onclick = function () { window.open(reportPath, '_blank'); };
    linkWrap.appendChild(reportBtn);

    if (appDir) {
      var folderBtn = document.createElement('button');
      folderBtn.style.cssText = 'padding:10px 16px;background:var(--surface);border:1px solid var(--border);border-radius:8px;color:var(--text);cursor:pointer;font-size:14px;';
      folderBtn.textContent = '📁 앱 폴더 열기';
      folderBtn.onclick = function () {
        fetch('/api/open-folder', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: appDir }),
        }).catch(function () {});
      };
      linkWrap.appendChild(folderBtn);
    }

    logArea.appendChild(linkWrap);
    _running = false;
    _signalRunning(false);
    var stopBtn = document.getElementById('dev-stop-btn');
    if (stopBtn) stopBtn.style.display = 'none';

    var homeWrap = document.createElement('div');
    homeWrap.style.cssText = 'margin-top:12px;';
    var homeBtn = document.createElement('button');
    homeBtn.style.cssText = 'padding:10px 16px;background:none;border:1px solid var(--border,rgba(255,255,255,0.08));border-radius:8px;color:var(--dim,#8b949e);cursor:pointer;font-size:14px;width:100%;';
    homeBtn.textContent = '← 처음으로';
    homeBtn.onclick = function () {
      _devTask = '';
      _devSessionId = '';
      _devPaused = false;
      _renderInitialForm();
    };
    homeWrap.appendChild(homeBtn);
    logArea.appendChild(homeWrap);
  }

  function _handleDevStop() {
    if (_devPaused) return;
    _devPaused = true;
    if (_ws && _ws.readyState === WebSocket.OPEN) {
      _ws.send(JSON.stringify({ type: 'stop_dev' }));
    }
    _hideRateLimitPanel();  // 대기 중이었다면 카운트다운 정리
    _addDevLog('중단 요청됨', 'rate_limited');
    var stopBtn = document.getElementById('dev-stop-btn');
    if (stopBtn) stopBtn.style.display = 'none';
  }

  return {
    mountInShell: mountInShell,
  };
})();
