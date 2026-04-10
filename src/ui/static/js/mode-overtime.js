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

  var _otMode = 'research';   // 'research' | 'dev'
  var _devTask = '';
  var _devSessionId = '';
  var _devQuestions = '';

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

    // 탭 스위처
    var tabSwitcher = document.createElement('div');
    tabSwitcher.className = 'ot-tab-switcher';

    var researchBtn = document.createElement('button');
    researchBtn.textContent = '리서치';
    researchBtn.className = 'ot-tab-btn' + (_otMode === 'research' ? ' active' : '');
    researchBtn.onclick = function () { _otMode = 'research'; _render(); };
    tabSwitcher.appendChild(researchBtn);

    var devBtn = document.createElement('button');
    devBtn.textContent = '개발';
    devBtn.className = 'ot-tab-btn' + (_otMode === 'dev' ? ' active' : '');
    devBtn.onclick = function () { _otMode = 'dev'; _render(); };
    tabSwitcher.appendChild(devBtn);

    _container.appendChild(tabSwitcher);

    if (_otMode === 'research') {

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

    } else {
      _renderDevForm();
    }
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

    _ws.onclose = function () { _ws = null; };
  }

  function _handleMessage(msg) {
    var type = msg.type;
    var data = msg.data || {};

    if (type === 'dev_clarify_questions') {
      _showDevQuestions(data.questions, data.session_id);
    } else if (type === 'dev_started') {
      _addDevLog('개발이 시작되었습니다', 'session_start');
    } else if (type === 'dev_progress') {
      _handleDevProgress(data);
    } else if (type === 'overtime_detail_prompt' || type === 'overtime_questions') {
      _showOtDetailForm();
    } else if (type === 'overtime_iteration') {
      _handleIteration(data);
    } else if (type === 'overtime_activity') {
      // 도구 사용 활동 — 현재 iteration 카드에 카운터 업데이트
      _updateActivityCount(data);
    } else if (type === 'overtime_started') {
      // started
    } else if (type === 'overtime_stopped') {
      _running = false;
      _addLogEntry('⏹️ 야근이 중단되었습니다.');
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

  function _renderDevForm() {
    var form = document.createElement('div');
    form.className = 'ot-form';

    var title = document.createElement('h2');
    title.className = 'ot-title';
    title.textContent = '앱 개발';
    form.appendChild(title);

    var subtitle = document.createElement('p');
    subtitle.className = 'ot-subtitle';
    subtitle.textContent = '만들고 싶은 앱을 설명하면 AI가 자동으로 개발합니다. 로컬에서 바로 실행 가능한 앱이 만들어집니다.';
    form.appendChild(subtitle);

    // 앱 설명 입력
    var taskLabel = document.createElement('label');
    taskLabel.textContent = '만들고 싶은 앱';
    taskLabel.className = 'ot-label';
    form.appendChild(taskLabel);

    var taskInput = document.createElement('textarea');
    taskInput.className = 'dev-task-input';
    taskInput.placeholder = '예: 할일 관리 앱을 만들어줘. 할일을 추가하고 완료 체크하고, 날짜별로 정리할 수 있었으면 좋겠어.';
    taskInput.rows = 5;
    form.appendChild(taskInput);

    // WorkspacePanel
    var wsSection = document.createElement('div');
    wsSection.className = 'ot-field';
    _otWsPanel = WorkspacePanel.create(wsSection, 'overtime');
    form.appendChild(wsSection);

    // 시작 버튼
    var startBtn = document.createElement('button');
    startBtn.className = 'ot-start-btn';
    startBtn.textContent = '개발 시작';
    startBtn.onclick = function () {
      var task = taskInput.value.trim();
      if (!task) {
        alert('만들고 싶은 앱을 설명해주세요.');
        return;
      }
      _devTask = task;
      startBtn.disabled = true;
      startBtn.textContent = '질문 생성 중...';
      _connect();
      var retries = 0;
      var sendClarify = function () {
        if (_ws && _ws.readyState === WebSocket.OPEN) {
          _ws.send(JSON.stringify({
            type: 'start_dev_clarify',
            data: { task: task },
          }));
        } else if (retries < 50) {
          retries++;
          setTimeout(sendClarify, 100);
        }
      };
      sendClarify();
    };
    form.appendChild(startBtn);

    _container.appendChild(form);
  }

  function _showDevQuestions(questions, sessionId) {
    _devSessionId = sessionId;
    _devQuestions = questions;

    // Clear container and show questions
    while (_container.firstChild) _container.removeChild(_container.firstChild);

    var panel = document.createElement('div');
    panel.className = 'ot-form';

    var title = document.createElement('h2');
    title.className = 'ot-title';
    title.textContent = '몇 가지만 확인할게요';
    panel.appendChild(title);

    // 질문 표시 (AI가 생성한 텍스트를 줄바꿈으로 분리)
    var qText = document.createElement('div');
    qText.className = 'dev-questions-text';
    qText.style.cssText = 'white-space:pre-wrap;font-size:14px;color:#E6EDF3;line-height:1.8;margin-bottom:16px;padding:16px;background:var(--surface,#161B22);border-radius:8px;border:1px solid var(--border,rgba(255,255,255,0.08));';
    qText.textContent = questions;
    panel.appendChild(qText);

    // 답변 textarea
    var ansLabel = document.createElement('label');
    ansLabel.textContent = '답변';
    ansLabel.className = 'ot-label';
    panel.appendChild(ansLabel);

    var ansInput = document.createElement('textarea');
    ansInput.className = 'dev-task-input';
    ansInput.placeholder = '위 질문에 자유롭게 답변해주세요. 모든 질문에 답하지 않아도 됩니다.';
    ansInput.rows = 6;
    panel.appendChild(ansInput);

    // 개발 시작 버튼
    var startBtn = document.createElement('button');
    startBtn.className = 'ot-start-btn';
    startBtn.textContent = '개발 시작';
    startBtn.onclick = function () {
      var answers = ansInput.value.trim();
      _startDev(_devTask, answers, _devSessionId);
    };
    panel.appendChild(startBtn);

    // 건너뛰기 (답변 없이 진행)
    var skipBtn = document.createElement('button');
    skipBtn.className = 'ot-skip-btn';
    skipBtn.style.cssText = 'margin-top:8px;background:none;border:1px solid var(--border,rgba(255,255,255,0.08));color:var(--dim,#8b949e);padding:8px 16px;border-radius:8px;cursor:pointer;width:100%;font-size:14px;';
    skipBtn.textContent = '건너뛰고 바로 개발 시작';
    skipBtn.onclick = function () {
      _startDev(_devTask, '', _devSessionId);
    };
    panel.appendChild(skipBtn);

    _container.appendChild(panel);
  }

  function _startDev(task, answers, sessionId) {
    _running = true;

    // Progress UI
    while (_container.firstChild) _container.removeChild(_container.firstChild);
    _renderDevProgress();

    var wsFiles = _otWsPanel ? _otWsPanel.getSelectedFiles() : [];

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

  function _renderDevProgress() {
    var wrap = document.createElement('div');
    wrap.id = 'dev-progress';
    wrap.style.padding = '20px';

    // Phase bar
    var phaseBar = document.createElement('div');
    phaseBar.className = 'dev-phase-bar';
    phaseBar.id = 'dev-phase-bar';

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
        phaseBar.appendChild(conn);
      }
      var item = document.createElement('div');
      item.className = 'dev-phase-item';
      item.id = 'dev-phase-' + p.id;
      item.textContent = p.label;
      // clarify is already done at this point
      if (p.id === 'clarify') {
        item.classList.add('done');
        item.textContent = '✓ ' + p.label;
      }
      phaseBar.appendChild(item);
    });

    wrap.appendChild(phaseBar);

    // Log area
    var logArea = document.createElement('div');
    logArea.id = 'dev-log';
    logArea.style.cssText = 'margin-top:16px;max-height:400px;overflow-y:auto;';
    wrap.appendChild(logArea);

    _container.appendChild(wrap);
  }

  function _handleDevProgress(data) {
    var phase = data.phase;
    var action = data.action;

    // Update phase bar
    var phaseEl = document.getElementById('dev-phase-' + phase);
    if (phaseEl) {
      // Clear previous active states for this phase
      phaseEl.classList.remove('done');
      if (action === 'complete' || action === 'generating') {
        if (action === 'complete') {
          phaseEl.classList.add('done');
          phaseEl.classList.remove('active');
          var label = phaseEl.textContent.replace(/^[●○✓]\s*/, '');
          phaseEl.textContent = '✓ ' + label;
          // Also mark connector as done
          var conn = document.getElementById('dev-conn-' + phase);
          if (conn) conn.classList.add('done');
        }
      } else {
        phaseEl.classList.add('active');
        var label2 = phaseEl.textContent.replace(/^[●○✓]\s*/, '');
        phaseEl.textContent = '● ' + label2;
      }
    }

    // Add log entry
    if (data.message) {
      _addDevLog(data.message, action);
    }

    // If report complete, show link
    if (phase === 'report' && action === 'complete' && data.report_path) {
      _addDevReportLink(data.report_path, data.app_dir);
    }
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
    var logArea = document.getElementById('dev-log');
    if (!logArea) return;

    var linkWrap = document.createElement('div');
    linkWrap.style.cssText = 'margin-top:16px;display:flex;gap:8px;';

    var reportBtn = document.createElement('button');
    reportBtn.className = 'ot-start-btn';
    reportBtn.textContent = '📄 리포트 + 실행 가이드 보기';
    reportBtn.onclick = function () { window.open(reportPath + '/results.html', '_blank'); };
    linkWrap.appendChild(reportBtn);

    if (appDir) {
      var folderBtn = document.createElement('button');
      folderBtn.style.cssText = 'padding:10px 16px;background:var(--surface);border:1px solid var(--border);border-radius:8px;color:var(--text);cursor:pointer;font-size:14px;';
      folderBtn.textContent = '📁 앱 폴더 열기';
      folderBtn.onclick = function () {
        fetch('/api/workspace/overtime/open', { method: 'POST' }).catch(function () {});
      };
      linkWrap.appendChild(folderBtn);
    }

    logArea.appendChild(linkWrap);
    _running = false;
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
