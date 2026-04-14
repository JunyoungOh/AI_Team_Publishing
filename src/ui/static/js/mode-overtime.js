/* mode-overtime.js — 야근팀 UI: 목표 도달까지 반복 실행
 *
 * WebSocket /ws/overtime 연결
 * 설정 폼 → iteration 진행 카드 → 최종 보고서 링크
 */
var OvertimeManager = (function () {
  'use strict';

  var _ws = null;
  var _container = null;
  var _running = false;
  var _strategies = [];
  var _selectedStrategy = null;
  var _otWsPanel = null;

  var _overtimeHistory = [];
  // NOTE: 개발(0→1) 모드는 '자동개발' 탭(mode-upgrade.js)으로 이동됨 — 야근팀은 리서치 전용

  /* Sidebar "running" indicator signal — see mode-chatbot.js for receiver.
     Defensive try/catch: signaling must NEVER break the mode itself. */
  function _signalRunning(on) {
    try {
      if (window.chatbotSignal) window.chatbotSignal('overtime', on);
    } catch (_) { /* noop */ }
  }

  function mountInShell(container) {
    _container = container;
    _render();
  }

  function _loadStrategies() {
    // CardBuilder에서 전략 목록을 참조
    if (typeof CardBuilder !== 'undefined' && CardBuilder.getStrategies) {
      _strategies = CardBuilder.getStrategies();
    }
  }

  function _render() {
    if (!_container) return;
    _loadStrategies();
    while (_container.firstChild) _container.removeChild(_container.firstChild);

    // 설정 폼
    var form = document.createElement('div');
    form.className = 'ot-form';

    var title = document.createElement('h2');
    title.className = 'ot-title';
    title.textContent = '야근팀';
    form.appendChild(title);

    var subtitle = document.createElement('p');
    subtitle.className = 'ot-subtitle';
    subtitle.textContent = '목표를 달성할 때까지 AI가 반복적으로 리서치하고 분석합니다.';
    form.appendChild(subtitle);

    // 전략 선택 영역
    var picker = _buildOtStrategyPicker();
    form.appendChild(picker);

    // 작업 입력
    var taskLabel = document.createElement('label');
    taskLabel.className = 'ot-label';
    taskLabel.textContent = '작업';
    var taskInput = document.createElement('input');
    taskInput.className = 'ot-input';
    taskInput.id = 'ot-task';
    taskInput.placeholder = '예: 쿠팡의 마케팅 전략을 심층 분석해줘';
    form.appendChild(taskLabel);
    form.appendChild(taskInput);

    // 목표 입력
    var goalLabel = document.createElement('label');
    goalLabel.className = 'ot-label';
    goalLabel.textContent = '달성 목표';
    var goalInput = document.createElement('input');
    goalInput.className = 'ot-input';
    goalInput.id = 'ot-goal';
    goalInput.placeholder = '예: 5개 관점에서 각각 데이터 포인트 10개 이상 확보';
    form.appendChild(goalLabel);
    form.appendChild(goalInput);

    // 최대 반복 횟수
    var iterLabel = document.createElement('label');
    iterLabel.className = 'ot-label';
    iterLabel.textContent = '최대 반복 횟수';
    var iterSelect = document.createElement('select');
    iterSelect.className = 'ot-select';
    iterSelect.id = 'ot-max-iter';
    [3, 5, 7, 10].forEach(function (n) {
      var opt = document.createElement('option');
      opt.value = n;
      opt.textContent = n + '회';
      if (n === 5) opt.selected = true;
      iterSelect.appendChild(opt);
    });
    form.appendChild(iterLabel);
    form.appendChild(iterSelect);

    // 출력 형식
    var fmtLabel = document.createElement('label');
    fmtLabel.className = 'ot-label';
    fmtLabel.textContent = '출력 형식';
    var fmtSelect = document.createElement('select');
    fmtSelect.className = 'ot-select';
    fmtSelect.id = 'ot-format';
    [
      { v: 'html', l: '📄 HTML' },
      { v: 'pdf', l: '📑 PDF' },
      { v: 'markdown', l: '📝 Markdown' },
      { v: 'csv', l: '📊 CSV' },
      { v: 'json', l: '{} JSON' },
    ].forEach(function (o) {
      var opt = document.createElement('option');
      opt.value = o.v;
      opt.textContent = o.l;
      fmtSelect.appendChild(opt);
    });
    form.appendChild(fmtLabel);
    form.appendChild(fmtSelect);

    // 워크스페이스 파일
    var wsSection = document.createElement('div');
    wsSection.className = 'ot-field';
    _otWsPanel = WorkspacePanel.create(wsSection, 'overtime');
    form.appendChild(wsSection);

    // 시작 버튼
    var startBtn = document.createElement('button');
    startBtn.className = 'ot-start-btn';
    startBtn.textContent = '야근 시작';
    startBtn.addEventListener('click', _startOvertime);
    form.appendChild(startBtn);

    _container.appendChild(form);

    // 진행 영역 (초기에는 숨김)
    var progress = document.createElement('div');
    progress.className = 'ot-progress';
    progress.id = 'ot-progress';
    progress.style.display = 'none';
    _container.appendChild(progress);
  }

  var _pendingOtTask = '';
  var _pendingOtGoal = '';
  var _pendingOtMaxIter = 5;

  function _startOvertime() {
    var task = document.getElementById('ot-task').value.trim();
    var goal = document.getElementById('ot-goal').value.trim();
    var maxIter = parseInt(document.getElementById('ot-max-iter').value, 10);

    if (!_selectedStrategy) {
      alert('야근팀은 방식 선택이 필수입니다.\n\n"나만의 방식" 탭에서 🌙 야근 타입으로 방식을 먼저 만든 뒤, 여기서 선택해주세요.');
      return;
    }
    if (!task) { alert('작업을 입력하세요.'); return; }
    if (!goal) { alert('달성 목표를 입력하세요.'); return; }

    _pendingOtTask = task;
    _pendingOtGoal = goal;
    _pendingOtMaxIter = maxIter;

    // 질문 생성 요청
    _connect();
    var startBtn = _container.querySelector('.ot-start-btn');
    if (startBtn) { startBtn.disabled = true; startBtn.textContent = '질문 생성 중...'; }

    var retries = 0;
    var sendQ = function () {
      if (_ws && _ws.readyState === WebSocket.OPEN) {
        _ws.send(JSON.stringify({ type: 'generate_overtime_questions', data: { task: task } }));
      } else if (retries < 50) {
        retries++;
        setTimeout(sendQ, 100);
      }
    };
    sendQ();
  }

  function _doStartOvertime(answers, detailDesc) {
    _running = true;
    _signalRunning(true);

    var task = _pendingOtTask;
    var goal = _pendingOtGoal;
    var maxIter = _pendingOtMaxIter;
    _connect();

    var retries = 0;
    var sendStart = function () {
      if (_ws && _ws.readyState === WebSocket.OPEN) {
        var payload = { task: task, goal: goal, max_iterations: maxIter };
        if (_selectedStrategy) {
          payload.strategy = _selectedStrategy;
          payload.strategy_id = _selectedStrategy.id;
        }
        if (detailDesc) {
          payload.detail_description = detailDesc;
        } else if (answers && answers.length > 0) {
          payload.clarify_answers = answers;
        }
        payload.workspace_files = _otWsPanel ? _otWsPanel.getSelectedFiles() : [];
        _ws.send(JSON.stringify({
          type: 'start_overtime',
          data: payload,
        }));
        // 폼 + 질문 폼 숨기기, 진행 영역 표시
        var form = _container.querySelector('.ot-form');
        if (form) form.style.display = 'none';
        var qaForm = document.getElementById('ot-qa-form');
        if (qaForm) qaForm.remove();
        var progress = document.getElementById('ot-progress');
        if (progress) {
          progress.style.display = '';
          _renderProgressHeader(task, goal, maxIter);
        }
      } else if (retries < 50) {
        retries++;
        setTimeout(sendStart, 100);
      }
    };
    sendStart();
  }

  function _connect() {
    if (_ws && (_ws.readyState === WebSocket.OPEN || _ws.readyState === WebSocket.CONNECTING)) return;
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var url = proto + '//' + location.host + '/ws/overtime';
    _ws = new WebSocket(url);

    _ws.onmessage = function (e) {
      try {
        var msg = JSON.parse(e.data);
        _handleMessage(msg);
      } catch (err) {}
    };

    _ws.onclose = function () { _ws = null; _signalRunning(false); };
  }

  function _handleMessage(msg) {
    var type = msg.type;
    var data = msg.data || {};

    if (type === 'overtime_detail_prompt' || type === 'overtime_questions') {
      _showOtDetailForm();
    } else if (type === 'overtime_iteration') {
      _handleIteration(data);
    } else if (type === 'overtime_activity') {
      _updateActivityCount(data);
    } else if (type === 'overtime_started') {
      // started
    } else if (type === 'overtime_stopped') {
      _running = false;
      _signalRunning(false);
      _addLogEntry('⏹️ 야근이 중단되었습니다.');
    } else if (type === 'overtime_list') {
      _overtimeHistory = (data.overtimes || []);
    } else if (type === 'error') {
      _addLogEntry('❌ ' + (data.message || '오류'));
    }
  }

  function _renderProgressHeader(task, goal, maxIter) {
    var progress = document.getElementById('ot-progress');
    if (!progress) return;

    var header = document.createElement('div');
    header.className = 'ot-progress-header';

    var h = document.createElement('h3');
    h.textContent = '🌙 야근 진행 중';
    header.appendChild(h);

    var info = document.createElement('div');
    info.className = 'ot-info';
    info.textContent = task;
    header.appendChild(info);

    var goalEl = document.createElement('div');
    goalEl.className = 'ot-goal-text';
    goalEl.textContent = '🎯 ' + goal;
    header.appendChild(goalEl);

    var stopBtn = document.createElement('button');
    stopBtn.className = 'ot-stop-btn';
    stopBtn.textContent = '중단';
    stopBtn.addEventListener('click', function () {
      if (_ws && _ws.readyState === WebSocket.OPEN) {
        _ws.send(JSON.stringify({ type: 'stop_overtime' }));
      }
    });
    header.appendChild(stopBtn);

    progress.appendChild(header);

    // iteration 카드 컨테이너
    var cards = document.createElement('div');
    cards.className = 'ot-iterations';
    cards.id = 'ot-iterations';
    progress.appendChild(cards);
  }

  function _handleIteration(data) {
    var action = data.action;

    if (action === 'start') {
      _addIterationCard(data.iteration, data.max_iterations);
    } else if (action === 'scored') {
      _updateIterationCard(data.iteration, data.score, data.summary, data.gaps, data.elapsed);
    } else if (action === 'rate_limited') {
      _addLogEntry(data.message || '⏸️ 사용량 한도 도달 — 대기 중...');
    } else if (action === 'finalizing') {
      _addLogEntry('📝 최종 보고서 생성 중...');
    } else if (action === 'completed') {
      _running = false;
      _signalRunning(false);
      _addLogEntry('✅ 야근 완료! (' + data.total_iterations + '회 반복)');
      if (data.report_path) {
        _addReportLink(data.report_path);
      }
    }
  }

  function _addIterationCard(iteration, maxIter) {
    var cards = document.getElementById('ot-iterations');
    if (!cards) return;

    var card = document.createElement('div');
    card.className = 'ot-iter-card ot-iter-active';
    card.id = 'ot-iter-' + iteration;

    var header = document.createElement('div');
    header.className = 'ot-iter-header';

    var num = document.createElement('span');
    num.className = 'ot-iter-num';
    num.textContent = 'Iteration ' + iteration + '/' + maxIter;
    header.appendChild(num);

    var score = document.createElement('span');
    score.className = 'ot-iter-score';
    score.id = 'ot-iter-score-' + iteration;
    score.textContent = '수집 중...';
    header.appendChild(score);

    card.appendChild(header);

    var body = document.createElement('div');
    body.className = 'ot-iter-body';
    body.id = 'ot-iter-body-' + iteration;
    card.appendChild(body);

    cards.appendChild(card);
    cards.scrollTop = cards.scrollHeight;
  }

  function _updateIterationCard(iteration, score, summary, gaps, elapsed) {
    var card = document.getElementById('ot-iter-' + iteration);
    if (card) card.classList.remove('ot-iter-active');

    var scoreEl = document.getElementById('ot-iter-score-' + iteration);
    if (scoreEl) {
      scoreEl.textContent = score + '% (' + elapsed + 's)';
      scoreEl.className = 'ot-iter-score ' + (score >= 90 ? 'ot-score-pass' : 'ot-score-fail');
    }

    var body = document.getElementById('ot-iter-body-' + iteration);
    if (body) {
      var sumEl = document.createElement('div');
      sumEl.className = 'ot-iter-summary';
      sumEl.textContent = summary || '';
      body.appendChild(sumEl);

      if (gaps && gaps.length > 0) {
        var gapEl = document.createElement('div');
        gapEl.className = 'ot-iter-gaps';
        gapEl.textContent = '보완 필요: ' + gaps.join(', ');
        body.appendChild(gapEl);
      }
    }
  }

  function _updateActivityCount(data) {
    // 현재 활성 iteration 카드에 도구 카운터 표시
    var activeCard = document.querySelector('.ot-iter-active .ot-iter-body');
    if (!activeCard) return;
    var counter = activeCard.querySelector('.ot-tool-counter');
    if (!counter) {
      counter = document.createElement('div');
      counter.className = 'ot-tool-counter';
      activeCard.appendChild(counter);
    }
    counter.textContent = data.label + ' ×' + data.count;
  }

  function _addLogEntry(text) {
    var cards = document.getElementById('ot-iterations');
    if (!cards) return;
    var entry = document.createElement('div');
    entry.className = 'ot-log-entry';
    entry.textContent = text;
    cards.appendChild(entry);
    cards.scrollTop = cards.scrollHeight;
  }

  function _addReportLink(path) {
    var cards = document.getElementById('ot-iterations');
    if (!cards) return;
    var row = document.createElement('div');
    row.className = 'ot-report-row';

    var link = document.createElement('a');
    link.className = 'ot-report-link';
    link.href = path;
    link.target = '_blank';
    link.textContent = '📄 보고서 보기';
    link.addEventListener('click', function (e) {
      e.preventDefault();
      var url = this.href;
      fetch(url, { method: 'HEAD' }).then(function (res) {
        if (res.ok) {
          window.open(url, '_blank');
        } else {
          alert('보고서 파일이 삭제되었습니다.');
        }
      });
    });
    row.appendChild(link);

    var folderBtn = document.createElement('button');
    folderBtn.className = 'ot-folder-btn';
    folderBtn.textContent = '📁 폴더 열기';
    folderBtn.addEventListener('click', function () {
      var localPath = 'data' + path;
      fetch('/api/open-folder', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: localPath }),
      }).then(function (res) { return res.json(); }).then(function (data) {
        if (!data.ok) alert('폴더를 찾을 수 없습니다.');
      });
    });
    row.appendChild(folderBtn);

    cards.appendChild(row);
  }

  function _showOtDetailForm() {
    var startBtn = _container.querySelector('.ot-start-btn');
    if (startBtn) { startBtn.disabled = false; startBtn.textContent = '야근 시작'; }

    var old = document.getElementById('ot-qa-form');
    if (old) old.remove();

    var qa = document.createElement('div');
    qa.id = 'ot-qa-form';
    qa.className = 'st-qa-form';

    var qaTitle = document.createElement('div');
    qaTitle.className = 'st-qa-title';
    qaTitle.textContent = '📝 작업 상세 설명 (선택사항)';
    qa.appendChild(qaTitle);

    var hint = document.createElement('div');
    hint.className = 'st-cron-help';
    hint.textContent = '범위, 관점, 주의사항 등을 자유롭게 적으면 더 정확한 결과를 얻을 수 있습니다.';
    qa.appendChild(hint);

    var textarea = document.createElement('textarea');
    textarea.className = 'st-input st-detail-textarea';
    textarea.id = 'ot-detail-desc';
    textarea.rows = 4;
    textarea.placeholder = '예: 최근 3개월간의 데이터를 중심으로, 경쟁사와의 비교 분석을 포함해주세요. 특히 가격 변동 추이가 중요합니다.';
    qa.appendChild(textarea);

    var btnRow = document.createElement('div');
    btnRow.className = 'st-qa-actions';

    var confirmBtn = document.createElement('button');
    confirmBtn.className = 'ot-start-btn';
    confirmBtn.style.width = 'auto';
    confirmBtn.style.marginTop = '0';
    confirmBtn.textContent = '야근 시작';
    confirmBtn.addEventListener('click', function () {
      var desc = textarea.value.trim();
      qa.remove();
      _doStartOvertime([], desc);
    });

    var skipBtn = document.createElement('button');
    skipBtn.className = 'st-delete-btn';
    skipBtn.textContent = '건너뛰기';
    skipBtn.addEventListener('click', function () { _doStartOvertime([]); });

    btnRow.appendChild(confirmBtn);
    btnRow.appendChild(skipBtn);
    qa.appendChild(btnRow);

    var form = _container.querySelector('.ot-form');
    if (form) form.after(qa);
    else _container.appendChild(qa);
  }

  function _buildOtStrategyPicker() {
    var wrap = document.createElement('div');
    wrap.className = 'sp-wrapper';

    var title = document.createElement('div');
    title.className = 'sp-title';
    title.textContent = '🌙 분석 방식 선택 (필수)';
    wrap.appendChild(title);

    // overtime 타입 전략만 필터 (general은 나만의 방식 탭 전용)
    var otStrategies = _strategies.filter(function (s) {
      return (s.type || 'general') === 'overtime';
    });

    if (otStrategies.length > 0) {
      var grid = document.createElement('div');
      grid.className = 'sp-grid';
      for (var i = 0; i < otStrategies.length; i++) {
        (function (s) {
          var card = document.createElement('div');
          card.className = 'sp-card' + (_selectedStrategy && _selectedStrategy.id === s.id ? ' sp-card-active' : '');

          var name = document.createElement('div');
          name.className = 'sp-card-name';
          name.textContent = '🌙 ' + (s.name || '방식');
          card.appendChild(name);

          var desc = document.createElement('div');
          desc.className = 'sp-card-desc';
          desc.textContent = s.description || '';
          card.appendChild(desc);

          var meta = document.createElement('div');
          meta.className = 'sp-card-meta';
          var depthTag = document.createElement('span');
          depthTag.className = 'sp-card-tag';
          depthTag.textContent = s.depth || 'standard';
          meta.appendChild(depthTag);
          var typeTag = document.createElement('span');
          typeTag.className = 'sp-card-tag';
          typeTag.textContent = '야근';
          meta.appendChild(typeTag);
          card.appendChild(meta);

          card.addEventListener('click', function () {
            _selectedStrategy = (_selectedStrategy && _selectedStrategy.id === s.id) ? null : s;
            _render();
          });
          grid.appendChild(card);
        })(otStrategies[i]);
      }
      wrap.appendChild(grid);
    } else {
      var empty = document.createElement('div');
      empty.className = 'sp-empty';
      empty.textContent = '저장된 야근 방식이 없습니다. "나만의 방식" 탭에서 🌙 야근 타입을 먼저 만들어주세요.';
      wrap.appendChild(empty);
    }

    return wrap;
  }

  return {
    mountInShell: mountInShell,
  };
})();
