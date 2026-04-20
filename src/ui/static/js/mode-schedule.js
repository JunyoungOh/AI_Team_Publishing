/* mode-schedule.js — 자동실행 UI: 정기 자동 실행 관리
 *
 * 기존 company-builder WS를 통해 스케줄 CRUD.
 * 스케줄 목록 + 생성 폼 + 실행 이력 대시보드.
 */
var ScheduleTeamManager = (function () {
  'use strict';

  /* Sidebar "running" indicator signal — tracks when a SCHEDULED JOB is
     actually executing in the backend (not when the management WS is
     merely open). Driven by schedule_running / schedule_run_complete
     messages from the server. See mode-chatbot.js for receiver. */
  function _signalRunning(on) {
    try {
      if (window.chatbotSignal) window.chatbotSignal('schedule', on);
    } catch (_) { /* noop */ }
  }

  var _ws = null;
  var _container = null;
  var _schedules = [];
  var _strategies = [];
  var _pendingTask = '';
  var _pendingCron = '';
  var _selectedStrategy = null;

  function mountInShell(container) {
    _container = container;
    _connect();
  }

  function _connect() {
    // 이미 열린 WS가 있어도, onmessage가 이 탭의 핸들러인지 확인
    if (_ws && _ws.readyState === WebSocket.OPEN && _ws._scheduleMode) return;
    // 기존 WS 닫기 (다른 탭에서 열었을 수 있음)
    if (_ws && _ws.readyState === WebSocket.OPEN) {
      try { _ws.close(); } catch(e) {}
    }
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var url = proto + '//' + location.host + '/ws/company-builder';
    _ws = new WebSocket(url);

    _ws._scheduleMode = true;
    _ws.onopen = function () {
      _send({ type: 'list_schedules' });
      _send({ type: 'list_strategies' });
    };

    _ws.onmessage = function (e) {
      try {
        var msg = JSON.parse(e.data);
        if (msg.type === 'schedule_list') {
          _schedules = (msg.data && msg.data.schedules) || [];
          _render();
        } else if (msg.type === 'builder_strategies') {
          _strategies = (msg.data && msg.data.strategies) || [];
          _render();
        } else if (msg.type === 'schedule_saved') {
          _showSaveResult(msg.data || {});
          _send({ type: 'list_schedules' });
        } else if (msg.type === 'schedule_deleted' || msg.type === 'schedule_toggled') {
          _send({ type: 'list_schedules' });
        } else if (msg.type === 'schedule_detail_prompt') {
          _showDetailForm();
        } else if (msg.type === 'schedule_questions') {
          // 레거시 호환
          _showDetailForm();
        } else if (msg.type === 'schedule_running') {
          _signalRunning(true);
          _showRunProgress(msg.data && msg.data.schedule_id);
        } else if (msg.type === 'schedule_run_complete') {
          _signalRunning(false);
          _hideRunProgress(msg.data);
          _send({ type: 'list_schedules' });
        } else if (msg.type === 'error') {
          _signalRunning(false);
          _hideRunProgress({ status: 'error' });
        }
      } catch (err) {}
    };

    _ws.onclose = function () { _ws = null; _signalRunning(false); };
  }

  function _send(obj) {
    if (_ws && _ws.readyState === WebSocket.OPEN) {
      _ws.send(JSON.stringify(obj));
    }
  }

  function _render() {
    if (!_container) return;
    while (_container.firstChild) _container.removeChild(_container.firstChild);

    var wrapper = document.createElement('div');
    wrapper.className = 'st-wrapper';

    // 헤더
    var header = document.createElement('div');
    header.className = 'st-header';
    var title = document.createElement('h2');
    title.className = 'st-title';
    title.textContent = '자동실행';
    var subtitle = document.createElement('p');
    subtitle.className = 'st-subtitle';
    subtitle.textContent = '저장된 플레이북을 정해진 시간에 자동으로 실행합니다.';
    header.appendChild(title);
    header.appendChild(subtitle);
    wrapper.appendChild(header);

    // 전략 선택 영역
    var picker = _buildStrategyPicker();
    wrapper.appendChild(picker);

    // 새 스케줄 생성 폼
    var form = document.createElement('div');
    form.className = 'st-form';

    // 시간 선택 UI (시/분/요일)
    var timeRow = document.createElement('div');
    timeRow.className = 'st-time-row';

    var hourSelect = document.createElement('select');
    hourSelect.className = 'st-input st-time-select';
    hourSelect.id = 'st-hour';
    for (var h = 0; h < 24; h++) {
      var opt = document.createElement('option');
      opt.value = h;
      opt.textContent = (h < 10 ? '0' : '') + h + '시';
      if (h === 9) opt.selected = true;
      hourSelect.appendChild(opt);
    }

    var minSelect = document.createElement('select');
    minSelect.className = 'st-input st-time-select';
    minSelect.id = 'st-min';
    for (var m = 0; m < 60; m += 5) {
      var opt2 = document.createElement('option');
      opt2.value = m;
      opt2.textContent = (m < 10 ? '0' : '') + m + '분';
      if (m === 0) opt2.selected = true;
      minSelect.appendChild(opt2);
    }

    var timeSep = document.createElement('span');
    timeSep.className = 'st-time-sep';
    timeSep.textContent = ':';

    timeRow.appendChild(hourSelect);
    timeRow.appendChild(timeSep);
    timeRow.appendChild(minSelect);

    // 요일 체크박스
    var dowRow = document.createElement('div');
    dowRow.className = 'st-dow-row';
    var dowLabel = document.createElement('span');
    dowLabel.className = 'st-dow-label';
    dowLabel.textContent = '반복 요일';
    dowRow.appendChild(dowLabel);

    var dayNames = ['월', '화', '수', '목', '금', '토', '일'];
    var dayCronVals = ['1', '2', '3', '4', '5', '6', '0'];
    for (var di = 0; di < dayNames.length; di++) {
      var dayBtn = document.createElement('button');
      dayBtn.type = 'button';
      dayBtn.className = 'st-dow-btn';
      dayBtn.textContent = dayNames[di];
      dayBtn.dataset.cronVal = dayCronVals[di];
      dayBtn.addEventListener('click', function () {
        this.classList.toggle('st-dow-active');
      });
      dowRow.appendChild(dayBtn);
    }

    var dowHint = document.createElement('div');
    dowHint.className = 'st-cron-help';
    dowHint.textContent = '요일을 선택하지 않으면 매일 실행됩니다';

    // 숨겨진 cron 값 (호환용)
    var cronInput = document.createElement('input');
    cronInput.type = 'hidden';
    cronInput.id = 'st-cron';

    var fmtSelect = document.createElement('select');
    fmtSelect.className = 'st-input st-input-sm';
    fmtSelect.id = 'st-format';
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

    var addBtn = document.createElement('button');
    addBtn.className = 'st-add-btn';
    addBtn.id = 'st-add-btn';
    if (_selectedStrategy) {
      addBtn.textContent = '+ 자동실행 추가';
    } else {
      addBtn.textContent = '먼저 플레이북을 선택하세요';
      addBtn.disabled = true;
    }
    addBtn.addEventListener('click', function () {
      if (!_selectedStrategy) return;
      var task = _selectedStrategy.name || '자동실행';
      addBtn.disabled = true;
      addBtn.textContent = '질문 생성 중...';
      _pendingTask = task;
      // 시/분/요일 UI → cron 변환
      var hVal = document.getElementById('st-hour').value;
      var mVal = document.getElementById('st-min').value;
      var activeDays = document.querySelectorAll('.st-dow-btn.st-dow-active');
      var dowPart = '*';
      if (activeDays.length > 0) {
        var days = [];
        for (var di2 = 0; di2 < activeDays.length; di2++) {
          days.push(activeDays[di2].dataset.cronVal);
        }
        dowPart = days.join(',');
      }
      _pendingCron = mVal + ' ' + hVal + ' * * ' + dowPart;
      _connect();
      // WS 연결 보장 후 전송
      var retries = 0;
      var trySend = function () {
        if (_ws && _ws.readyState === WebSocket.OPEN) {
          _send({ type: 'generate_schedule_questions', data: { task: task } });
        } else if (retries < 30) {
          retries++;
          setTimeout(trySend, 200);
        } else {
          // 연결 실패 시 질문 없이 바로 저장
          addBtn.disabled = false;
          addBtn.textContent = '+ 자동실행 추가';
          _saveScheduleWithAnswers([]);
        }
      };
      trySend();
    });

    form.appendChild(timeRow);
    form.appendChild(dowRow);
    form.appendChild(dowHint);
    form.appendChild(cronInput);
    form.appendChild(fmtSelect);
    form.appendChild(addBtn);
    wrapper.appendChild(form);

    // 스케줄 목록
    var listHeader = document.createElement('div');
    listHeader.className = 'st-list-header';

    var listTitle = document.createElement('h3');
    listTitle.className = 'st-list-title';
    listTitle.textContent = '등록된 자동실행 (' + _schedules.length + ')';
    listHeader.appendChild(listTitle);

    var pathBtn = document.createElement('button');
    pathBtn.className = 'st-result-path';
    pathBtn.textContent = '📂 data/reports/';
    pathBtn.title = '결과물 저장 폴더 열기';
    pathBtn.addEventListener('click', function () {
      fetch('/api/open-folder', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: 'data/reports' }),
      }).then(function (res) { return res.json(); }).then(function (data) {
        if (!data.ok) alert('폴더를 찾을 수 없습니다.');
      });
    });
    listHeader.appendChild(pathBtn);

    wrapper.appendChild(listHeader);

    if (_schedules.length === 0) {
      var empty = document.createElement('div');
      empty.className = 'st-empty';
      empty.textContent = '등록된 자동실행이 없습니다.';
      wrapper.appendChild(empty);
    } else {
      var list = document.createElement('div');
      list.className = 'st-list';
      for (var i = 0; i < _schedules.length; i++) {
        list.appendChild(_renderScheduleCard(_schedules[i]));
      }
      wrapper.appendChild(list);
    }

    _container.appendChild(wrapper);
  }

  function _renderScheduleCard(sched) {
    var card = document.createElement('div');
    card.className = 'st-card' + (sched.enabled ? '' : ' st-card-disabled');

    // 상단: 이름 + 토글
    var top = document.createElement('div');
    top.className = 'st-card-top';

    var name = document.createElement('div');
    name.className = 'st-card-name';
    name.textContent = sched.name || sched.task_description || '(무제)';
    top.appendChild(name);

    var runCount = sched.run_count || 0;
    if (runCount > 0) {
      var badge = document.createElement('span');
      badge.className = 'st-run-count';
      badge.textContent = runCount + '회 실행';
      badge.title = '총 ' + runCount + '회 자동실행됨';
      top.appendChild(badge);
    }

    var toggle = document.createElement('button');
    toggle.className = 'st-toggle' + (sched.enabled ? ' st-toggle-on' : '');
    toggle.textContent = sched.enabled ? 'ON' : 'OFF';
    toggle.addEventListener('click', function () {
      _send({ type: 'toggle_schedule', data: { schedule_id: sched.id, enabled: !sched.enabled } });
    });
    top.appendChild(toggle);

    card.appendChild(top);

    // 크론 + 작업 설명
    var cron = document.createElement('div');
    cron.className = 'st-card-cron';
    cron.textContent = _cronToKorean(sched.cron_expression || '');
    card.appendChild(cron);

    var task = document.createElement('div');
    task.className = 'st-card-task';
    task.textContent = sched.task_description || '';
    card.appendChild(task);

    // 실행 이력
    var history = sched.run_history || [];
    if (history.length > 0) {
      var lastRun = history[history.length - 1];
      var histEl = document.createElement('div');
      histEl.className = 'st-card-history';

      var statusLabel = lastRun.status === 'completed' ? '✅ 완료' :
                        lastRun.status === 'failed' ? '❌ 실패' :
                        lastRun.status || '';
      var duration = lastRun.duration_s ? ' (' + Math.round(lastRun.duration_s) + '초)' : '';
      histEl.textContent = '마지막: ' + (lastRun.started_at || '').substring(0, 16) +
        ' — ' + statusLabel + duration;
      card.appendChild(histEl);

      // 폴더 열기
      if (lastRun.report_path) {
        var reportRow = document.createElement('div');
        reportRow.className = 'st-report-row';

        var folderBtn = document.createElement('button');
        folderBtn.className = 'st-folder-btn';
        folderBtn.textContent = '📁 폴더 열기';
        folderBtn.addEventListener('click', function () {
          var localPath = 'data' + lastRun.report_path;
          fetch('/api/open-folder', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: localPath }),
          }).then(function (res) { return res.json(); }).then(function (data) {
            if (!data.ok) alert('폴더를 찾을 수 없습니다.');
          });
        });
        reportRow.appendChild(folderBtn);

        card.appendChild(reportRow);
      }
    }

    // 버튼 영역
    var actions = document.createElement('div');
    actions.className = 'st-card-actions';

    var runBtn = document.createElement('button');
    runBtn.className = 'st-run-btn';
    runBtn.textContent = '지금 실행';
    runBtn.addEventListener('click', function () {
      runBtn.disabled = true;
      runBtn.textContent = '실행 중...';
      _send({ type: 'run_schedule_now', data: { schedule_id: sched.id } });
    });
    actions.appendChild(runBtn);

    var del = document.createElement('button');
    del.className = 'st-delete-btn';
    del.textContent = '삭제';
    del.addEventListener('click', function () {
      _send({ type: 'delete_schedule', data: { schedule_id: sched.id } });
    });
    actions.appendChild(del);

    card.appendChild(actions);

    return card;
  }

  function _cronToKorean(cron) {
    if (!cron) return '';
    var parts = cron.split(' ');
    if (parts.length !== 5) return cron;
    var min = parts[0], hour = parts[1], dom = parts[2], mon = parts[3], dow = parts[4];
    var dayNames = { '0': '일', '1': '월', '2': '화', '3': '수', '4': '목', '5': '금', '6': '토', '7': '일' };

    if (dom === '*' && mon === '*' && dow === '*') {
      return '매일 ' + hour + ':' + (min.length === 1 ? '0' + min : min);
    }
    if (dom === '*' && mon === '*' && dow !== '*') {
      var dayLabel = dayNames[dow] || dow;
      return '매주 ' + dayLabel + '요일 ' + hour + ':' + (min.length === 1 ? '0' + min : min);
    }
    return cron;
  }

  function _showDetailForm() {
    // 버튼 복원
    var addBtn = document.getElementById('st-add-btn');
    if (addBtn) { addBtn.disabled = false; addBtn.textContent = '+ 자동실행 추가'; }

    // 기존 폼 제거
    var old = document.getElementById('st-qa-form');
    if (old) old.remove();

    var qa = document.createElement('div');
    qa.id = 'st-qa-form';
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
    textarea.id = 'st-detail-desc';
    textarea.rows = 4;
    textarea.placeholder = '예: 국내외 AI 뉴스 중 LLM과 규제 관련 내용을 중심으로, 핵심만 요약해주세요. 기술적 세부사항보다는 비즈니스 영향 위주로.';
    qa.appendChild(textarea);

    var btnRow = document.createElement('div');
    btnRow.className = 'st-qa-actions';

    var confirmBtn = document.createElement('button');
    confirmBtn.className = 'st-add-btn';
    confirmBtn.textContent = '등록';
    confirmBtn.addEventListener('click', function () {
      var desc = textarea.value.trim();
      qa.remove();
      _saveScheduleWithDetail(desc);
    });

    var skipBtn = document.createElement('button');
    skipBtn.className = 'st-delete-btn';
    skipBtn.textContent = '건너뛰기';
    skipBtn.addEventListener('click', function () {
      qa.remove();
      _saveScheduleWithDetail('');
    });

    btnRow.appendChild(confirmBtn);
    btnRow.appendChild(skipBtn);
    qa.appendChild(btnRow);

    // 폼 아래에 삽입
    var form = _container.querySelector('.st-form');
    if (form) form.after(qa);
    else _container.appendChild(qa);
  }

  function _saveScheduleWithAnswers(answers) {
    _saveScheduleWithDetail('', answers);
  }

  function _saveScheduleWithDetail(detail, legacyAnswers) {
    var fmt = document.getElementById('st-format');
    var data = {
      task_description: _pendingTask,
      cron_expression: _pendingCron,
      name: _pendingTask.substring(0, 30),
      enabled: true,
      output_format: fmt ? fmt.value : 'html',
    };
    if (_selectedStrategy) {
      data.strategy = _selectedStrategy;
      data.strategy_id = _selectedStrategy.id;
    }
    if (detail) {
      data.detail_description = detail;
    } else if (legacyAnswers && legacyAnswers.length > 0) {
      data.clarify_answers = legacyAnswers;
    }
    _send({ type: 'save_schedule', data: data });
    _pendingTask = '';
    _pendingCron = '';
    _selectedStrategy = null;
  }

  var _progressTimer = null;
  var _progressStart = 0;

  function _showSaveResult(saved) {
    // 저장 자체는 성공했더라도 APScheduler 등록이 실패했다면 사용자에게 알림.
    // 이 경우 cron 시간이 되어도 자동 실행이 안 되므로 수동 "지금 실행"만 가능.
    if (saved.enabled === false) return;  // 비활성 저장은 등록 안 되는 게 정상
    if (saved.registered === false) {
      var reason = saved.register_error || 'unknown';
      var msg = '⚠️ 자동실행은 저장됐지만 등록에 실패했습니다.\n';
      if (reason === 'scheduler_service_unavailable') {
        msg += '스케줄러가 시작되지 않았습니다. 서버를 재시작해주세요.';
      } else if (reason === 'registration_failed') {
        msg += '크론 표현식이나 저장 파일을 확인해주세요.';
      } else {
        msg += '사유: ' + reason;
      }
      msg += '\n\n"지금 실행" 버튼은 정상 동작합니다.';
      alert(msg);
    }
  }

  function _showRunProgress(scheduleId) {
    // 카드 내에 프로그레스 바 삽입
    var cards = document.querySelectorAll('.st-card');
    _progressStart = Date.now();

    // 모든 카드 위에 프로그레스 오버레이
    var existing = document.getElementById('st-run-overlay');
    if (existing) existing.remove();

    var overlay = document.createElement('div');
    overlay.id = 'st-run-overlay';
    overlay.className = 'st-run-overlay';

    var spinner = document.createElement('div');
    spinner.className = 'st-spinner';

    var msg = document.createElement('div');
    msg.className = 'st-run-msg';
    msg.textContent = '자동실행 진행 중...';

    var timer = document.createElement('div');
    timer.className = 'st-run-timer';
    timer.id = 'st-run-timer';
    timer.textContent = '0:00';

    overlay.appendChild(spinner);
    overlay.appendChild(msg);
    overlay.appendChild(timer);

    if (_container) _container.appendChild(overlay);

    _progressTimer = setInterval(function () {
      var el = document.getElementById('st-run-timer');
      if (!el) return;
      var sec = Math.round((Date.now() - _progressStart) / 1000);
      var m = Math.floor(sec / 60);
      var s = sec % 60;
      el.textContent = m + ':' + (s < 10 ? '0' : '') + s;
    }, 1000);
  }

  function _hideRunProgress(data) {
    if (_progressTimer) { clearInterval(_progressTimer); _progressTimer = null; }
    var overlay = document.getElementById('st-run-overlay');
    if (overlay) {
      var status = (data && data.status) || 'unknown';
      var duration = (data && data.duration_s) || 0;
      var msg = overlay.querySelector('.st-run-msg');
      if (msg) {
        if (status === 'completed') {
          msg.textContent = '✅ 완료 (' + Math.round(duration) + '초)';
        } else {
          msg.textContent = '❌ ' + status + ' (' + Math.round(duration) + '초)';
        }
      }
      // 3초 후 오버레이 제거
      setTimeout(function () {
        if (overlay.parentNode) overlay.remove();
      }, 3000);
    }
  }

  function _buildStrategyPicker() {
    var wrap = document.createElement('div');
    wrap.className = 'sp-wrapper';

    var title = document.createElement('div');
    title.className = 'sp-title';
    title.textContent = '📅 플레이북 선택';
    wrap.appendChild(title);

    // 모든 저장된 플레이북 표시 (타입 필터 없음)
    if (_strategies.length > 0) {
      var grid = document.createElement('div');
      grid.className = 'sp-grid';
      for (var i = 0; i < _strategies.length; i++) {
        (function (s) {
          var card = document.createElement('div');
          card.className = 'sp-card' + (_selectedStrategy && _selectedStrategy.id === s.id ? ' sp-card-active' : '');

          var name = document.createElement('div');
          name.className = 'sp-card-name';
          name.textContent = '📒 ' + (s.name || '플레이북');
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
          card.appendChild(meta);

          card.addEventListener('click', function () {
            _selectedStrategy = (_selectedStrategy && _selectedStrategy.id === s.id) ? null : s;
            _render();
          });
          grid.appendChild(card);
        })(_strategies[i]);
      }
      wrap.appendChild(grid);
    } else {
      var empty = document.createElement('div');
      empty.className = 'sp-empty';
      empty.textContent = '저장된 플레이북이 없습니다. "플레이북" 탭에서 먼저 플레이북을 만들어주세요.';
      wrap.appendChild(empty);
    }

    return wrap;
  }

  return {
    mountInShell: mountInShell,
  };
})();
