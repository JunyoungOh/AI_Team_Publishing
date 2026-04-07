/* card-event-handler.js — EventBridge → CardView mapping
 *
 * Translates backend WebSocket events into CardView API calls.
 * Stateless per event; all mutable state lives in CardView.
 */
var CardEventHandler = (function () {
  'use strict';

  /* ── Agent tracking ── */
  var _agents = {};       // { agentId: agentObj }
  var _edges = [];        // [{ from, to }]
  var _stepLabel = '';     // current pipeline step label

  /* ── Callbacks (set by init) ── */
  var _onChatMessage = null;   // (text, type, opts) => void
  var _onInterrupt = null;     // (data) => void
  var _onComplete = null;      // (data) => void
  var _onError = null;         // (message) => void
  var _onSceneChange = null;   // (label) => void
  var _onHierarchy = null;     // () => void — called when agent hierarchy arrives

  /* ── Helpers ── */

  function _statusFromProgress(p) {
    if (p == null) return 'idle';
    var s = p.status || '';
    if (s === 'done' || s === 'completed') return 'done';
    if (s === 'failed' || s === 'error') return 'error';
    if (s === 'running' || s === 'tier2') return 'running';
    if (s === 'pending' || s === 'waiting') return 'idle';
    return 'running';
  }

  function _pctFromProgress(p) {
    var v = p.progress;
    if (v == null) return 0;
    // Backend sends 0.0~1.0 float; values > 1.0 are already percentages
    if (v > 1) return Math.min(100, Math.round(v));
    return Math.min(100, Math.round(v * 100));
  }

  function _findAgentByWorkerId(workerId) {
    if (!workerId) return null;
    var keys = Object.keys(_agents);
    for (var i = 0; i < keys.length; i++) {
      var a = _agents[keys[i]];
      if (a.worker_id === workerId || a.id === workerId) return a;
    }
    return null;
  }

  function _findAgentByDomain(domain) {
    if (!domain) return null;
    var keys = Object.keys(_agents);
    for (var i = 0; i < keys.length; i++) {
      var a = _agents[keys[i]];
      if (a.domain === domain || a.id === domain) return a;
    }
    return null;
  }

  /* ── Event Handlers ── */

  function _onInitEvent(data) {
    reset();
    // Don't render CEO/You cards on init — they appear when workers are assigned
    // Welcome message is already shown by _showCompanyMode, no need to duplicate
  }

  function _onHierarchyEvent(data) {
    if (_onHierarchy) _onHierarchy();

    var leaders = (data && data.leaders) || [];

    for (var i = 0; i < leaders.length; i++) {
      var leader = leaders[i];
      var domain = leader.domain || 'unknown';
      var workers = leader.workers || [];

      for (var j = 0; j < workers.length; j++) {
        var w = workers[j];
        var wId = w.worker_id || (w.domain + '_' + j);

        if (!_agents[wId]) {
          _agents[wId] = {
            id: wId,
            name: w.name || wId,
            role: w.role || domain + ' 워커',
            emoji: w.emoji || '⚙️',
            domain: w.domain || domain,
            worker_id: wId,
            toolCategory: w.tool_category || w.domain || domain,
            status: 'idle',
            progress: 0,
          };
        }

      }
    }

    // Build task_title → worker_id map for dependency resolution
    var allWorkers = [];
    for (var li = 0; li < leaders.length; li++) {
      var lw = leaders[li].workers || [];
      for (var wi = 0; wi < lw.length; wi++) {
        allWorkers.push(lw[wi]);
      }
    }

    var titleToId = {};
    allWorkers.forEach(function(w) {
      var wId = w.worker_id || w.domain;
      if (w.task_title) titleToId[w.task_title] = wId;
    });

    // Create edges: inter-worker from dependencies, CEO→worker only for root nodes
    var ceoId = _findCeoId();
    allWorkers.forEach(function(w) {
      var wId = w.worker_id || w.domain;
      var deps = w.dependencies || [];
      if (deps.length > 0) {
        deps.forEach(function(depTitle) {
          var depId = titleToId[depTitle];
          if (depId) _addEdge(depId, wId);
        });
      } else {
        // No dependencies = root worker, connect to CEO
        if (ceoId) _addEdge(ceoId, wId);
      }
    });

    _rebuildCanvas();
  }

  function _onCharSpawnEvent(data) {
    var c = (data && data.character) || {};
    var id = c.id || c.domain || ('spawn_' + Date.now());

    if (!_agents[id]) {
      _agents[id] = {
        id: id,
        name: c.name || id,
        role: c.title || c.role || '',
        emoji: c.emoji || '⚙️',
        domain: c.domain || '',
        worker_id: id,
        toolCategory: c.domain || 'general',
        status: 'idle',
        progress: 0,
      };

      var ceoId = _findCeoId();
      if (ceoId && id !== ceoId) {
        _addEdge(ceoId, id);
      }

      _rebuildCanvas();
    }
  }

  function _onProgressEvent(data) {
    // Match by worker_id first, then by character/domain
    var agent = _findAgentByWorkerId(data.worker_id)
             || _findAgentByDomain(data.character);

    if (!agent) return;

    var status = _statusFromProgress(data);
    var pct = _pctFromProgress(data);

    agent.status = status;
    agent.progress = pct;

    // Update card name if worker_name is available and better than domain code
    var updates = { status: status, progress: pct };
    if (data.worker_name && data.worker_name !== agent.name) {
      agent.name = data.worker_name;
      updates.name = data.worker_name;
    }

    CardView.updateAgentCard(agent.id, updates);

    if (data.summary && _onChatMessage) {
      _onChatMessage(data.summary, 'system', { silent: true });
    }
  }

  function _onStepProgressEvent(data) {
    _stepLabel = data.label || data.node || '';
    var pct = data.progress != null ? Math.round(data.progress * 100) : -1;
    var displayLabel = pct >= 0 && pct < 100 ? _stepLabel + ' (' + pct + '%)' : _stepLabel;
    if (_onSceneChange) _onSceneChange(displayLabel);
  }

  function _onSceneChangeEvent(data) {
    _stepLabel = data.label || data.node || '';

    // Update step bar (always)
    if (_onSceneChange) _onSceneChange(_stepLabel);

    // Only show user-important scene changes in chat, skip internal steps
    var node = data.node || '';
    var _SILENT_NODES = [
      'intake', 'ceo_route', 'ceo_question', 'await_user_answers',
      'ceo_task_decomposition', '__interrupt__',
    ];
    var isSilent = false;
    for (var i = 0; i < _SILENT_NODES.length; i++) {
      if (node === _SILENT_NODES[i] || node.indexOf(_SILENT_NODES[i]) !== -1) {
        isSilent = true; break;
      }
    }
    if (!isSilent && _onChatMessage && _stepLabel) {
      _onChatMessage('📋 ' + _stepLabel, 'system', { step: true });
    }
  }

  function _onMessageEvent(data) {
    if (!_onChatMessage) return;
    var content = data.content || '';
    var style = data.style || '';

    // Filter out verbose internal logs — only show user-facing messages
    if (content.startsWith('[CEO]') || content.startsWith('SYSTEM:')) return;
    if (content.startsWith('[CEO ->')) return;
    if (content.startsWith('[Analyst]')) return;
    if (content.startsWith('[Blackboard')) return;
    if (content.startsWith('[Report Review]')) return;
    if (content.startsWith('[CEO Revise]')) return;
    if (content.startsWith('[Generated Files]')) return;
    if (content.indexOf('Rationale:') !== -1) return;
    if (content.indexOf('Generated ') !== -1 && content.indexOf(' questions') !== -1) return;

    // Show only meaningful messages: reports, user-facing announcements
    if (style === 'report') {
      _onChatMessage(content, 'system', { style: style });
    } else {
      _onChatMessage(content, 'system', { style: style });
    }
  }

  function _onInterruptEvent(data) {
    if (_onInterrupt) _onInterrupt(data);
  }

  function _onCompleteEvent(data) {
    // 타이머 정지
    if (_activityTimer) { clearInterval(_activityTimer); _activityTimer = null; }
    Object.keys(_toolStats).forEach(function(k) { _toolStats[k].active = false; });
    _rebuildDashboard();

    // Mark all agents as done
    var keys = Object.keys(_agents);
    for (var i = 0; i < keys.length; i++) {
      _agents[keys[i]].status = 'done';
      _agents[keys[i]].progress = 100;
      CardView.updateAgentCard(keys[i], { status: 'done', progress: 100 });
    }

    if (_onComplete) _onComplete(data);
    if (_onChatMessage) {
      var reportPath = data.report_path || '';
      var localPath = data.local_path || '';
      if (reportPath) {
        _onChatMessage('✅ 작업이 완료되었습니다! 보고서를 확인하세요.', 'system', {
          report: reportPath,
          localPath: localPath,
        });
      } else {
        _onChatMessage('✅ 작업이 완료되었습니다!', 'system', {});
      }
    }
  }

  function _onErrorEvent(data) {
    var msg = (data && data.message) || '알 수 없는 오류';
    if (_onError) _onError(msg);
    if (_onChatMessage) {
      _onChatMessage('❌ 오류: ' + msg, 'system', { error: true });
    }
  }

  /* ── Activity Dashboard (canvas area) ── */

  var _toolStats = {};    // { toolName: { count, icon, label, active } }
  var _activityFeed = []; // recent detail texts (max 4)
  var _activityTimer = null;
  var _activityStart = 0;

  var _TOOL_CARDS = {
    'WebSearch':  { icon: '\uD83D\uDD0D', label: '\uC6F9 \uAC80\uC0C9' },
    'WebFetch':   { icon: '\uD83C\uDF10', label: '\uD398\uC774\uC9C0 \uC218\uC9D1' },
    'Agent':      { icon: '\uD83E\uDD16', label: '\uC11C\uBE0C\uC5D0\uC774\uC804\uD2B8' },
    'Write':      { icon: '\uD83D\uDCDD', label: '\uD30C\uC77C \uC791\uC131' },
    'Read':       { icon: '\uD83D\uDCC4', label: '\uD30C\uC77C \uC77D\uAE30' },
    'Bash':       { icon: '\u2699\uFE0F', label: '\uBA85\uB839 \uC2E4\uD589' },
    'Glob':       { icon: '\uD83D\uDCC2', label: '\uD30C\uC77C \uAC80\uC0C9' },
    'Grep':       { icon: '\uD83D\uDD0E', label: '\uCF54\uB4DC \uAC80\uC0C9' },
    'mcp__firecrawl__firecrawl_scrape': { icon: '\uD83D\uDD77\uFE0F', label: '\uC2A4\uD06C\uB798\uD551' },
    'mcp__firecrawl__firecrawl_crawl':  { icon: '\uD83D\uDD77\uFE0F', label: '\uC2A4\uD06C\uB798\uD551' },
    'mcp__firecrawl__firecrawl_extract':{ icon: '\uD83D\uDD77\uFE0F', label: '\uC2A4\uD06C\uB798\uD551' },
    'mcp__firecrawl__firecrawl_search': { icon: '\uD83D\uDD77\uFE0F', label: '\uC2A4\uD06C\uB798\uD551' },
  };

  // UI에 노출하지 않을 내부 도구
  var _HIDDEN_TOOLS = { 'ToolSearch': true, 'TodoRead': true, 'TodoWrite': true };

  function _renderActivityToCanvas(d) {
    // 풀와이드 모드: chat messages 안에 인라인 렌더링
    // 분할 모드: canvas 위에 오버레이 렌더링
    var app = document.getElementById('card-app');
    var isInline = app && app.classList.contains('chat-fullwidth');
    var container;
    if (isInline) {
      // 모드별 컨테이너가 여러 개일 수 있으므로 보이는(display !== none) 것을 찾음
      var candidates = document.querySelectorAll('.cc-messages');
      for (var ci = 0; ci < candidates.length; ci++) {
        if (candidates[ci].style.display !== 'none') { container = candidates[ci]; break; }
      }
    } else {
      container = document.getElementById('card-canvas');
    }
    if (!container) return;

    var dash = document.getElementById('activity-dash');
    if (!dash) {
      if (!isInline) {
        var empty = document.getElementById('card-empty-state');
        if (empty) empty.style.display = 'none';
      }
      dash = document.createElement('div');
      dash.id = 'activity-dash';
      if (isInline) dash.classList.add('ad-inline');
      // Build structure with DOM (no innerHTML for untrusted data)
      var header = document.createElement('div');
      header.className = 'ad-header';
      var title = document.createElement('span');
      title.className = 'ad-title';
      title.textContent = 'AI \uC791\uC5C5 \uD604\uD669';
      var timer = document.createElement('span');
      timer.className = 'ad-timer';
      timer.id = 'ad-timer';
      timer.textContent = '0:00';
      header.appendChild(title);
      header.appendChild(timer);
      var cards = document.createElement('div');
      cards.className = 'ad-cards';
      cards.id = 'ad-cards';
      var feed = document.createElement('div');
      feed.className = 'ad-feed';
      feed.id = 'ad-feed';
      dash.appendChild(header);
      dash.appendChild(cards);
      dash.appendChild(feed);
      container.appendChild(dash);
      if (isInline) container.scrollTop = container.scrollHeight;
      _activityStart = Date.now();
      _activityTimer = setInterval(_updateTimer, 1000);
    }

    if (d.action === 'tool_use') {
      var tool = d.tool || '';
      if (_HIDDEN_TOOLS[tool]) return;
      if (!_toolStats[tool]) {
        var meta = _TOOL_CARDS[tool] || { icon: '\uD83D\uDD27', label: tool.replace('mcp__','').substring(0,12) };
        _toolStats[tool] = { count: 0, icon: meta.icon, label: meta.label, active: true };
      }
      _toolStats[tool].count++;
      _toolStats[tool].active = true;
      if (d.detail) {
        _activityFeed.push(d.detail);
        if (_activityFeed.length > 4) _activityFeed.shift();
      }
    }

    if (d.action === 'completed' || d.action === 'timeout') {
      Object.keys(_toolStats).forEach(function(k) { _toolStats[k].active = false; });
      if (_activityTimer) { clearInterval(_activityTimer); _activityTimer = null; }
    }

    _rebuildDashboard();
  }

  function _rebuildDashboard() {
    var cardsEl = document.getElementById('ad-cards');
    var feedEl = document.getElementById('ad-feed');
    if (!cardsEl) return;

    // 카드 렌더링 (DOM으로 구성)
    while (cardsEl.firstChild) cardsEl.removeChild(cardsEl.firstChild);
    var tools = Object.keys(_toolStats);
    for (var i = 0; i < tools.length; i++) {
      var t = _toolStats[tools[i]];
      var card = document.createElement('div');
      card.className = 'ad-card' + (t.active ? ' ad-card-active' : '');
      var iconEl = document.createElement('div');
      iconEl.className = 'ad-card-icon';
      iconEl.textContent = t.icon;
      var labelEl = document.createElement('div');
      labelEl.className = 'ad-card-label';
      labelEl.textContent = t.label;
      var countEl = document.createElement('div');
      countEl.className = 'ad-card-count';
      countEl.textContent = t.count;
      card.appendChild(iconEl);
      card.appendChild(labelEl);
      card.appendChild(countEl);
      cardsEl.appendChild(card);
    }

    // 피드 렌더링 (DOM으로 구성)
    if (feedEl) {
      while (feedEl.firstChild) feedEl.removeChild(feedEl.firstChild);
      for (var j = 0; j < _activityFeed.length; j++) {
        var item = document.createElement('div');
        item.className = 'ad-feed-item';
        item.textContent = _activityFeed[j];
        feedEl.appendChild(item);
      }
    }
  }

  function _updateTimer() {
    var el = document.getElementById('ad-timer');
    if (!el) return;
    var sec = Math.round((Date.now() - _activityStart) / 1000);
    var m = Math.floor(sec / 60);
    var s = sec % 60;
    el.textContent = m + ':' + (s < 10 ? '0' : '') + s;
  }

  /* ── Internal helpers ── */

  function _findCeoId() {
    var keys = Object.keys(_agents);
    for (var i = 0; i < keys.length; i++) {
      var a = _agents[keys[i]];
      if (a.id === 'ceo' || a.domain === '' || a.role === 'ceo') return a.id;
    }
    return keys.length > 0 ? keys[0] : null;
  }

  function _addEdge(from, to) {
    for (var i = 0; i < _edges.length; i++) {
      if (_edges[i].from === from && _edges[i].to === to) return;
    }
    _edges.push({ from: from, to: to });
  }

  function _rebuildCanvas() {
    var agentList = [];
    var keys = Object.keys(_agents);
    for (var i = 0; i < keys.length; i++) {
      agentList.push(_agents[keys[i]]);
    }

    // Hide empty state
    var emptyEl = document.getElementById('card-empty-state');
    if (emptyEl) emptyEl.style.display = agentList.length > 0 ? 'none' : '';

    if (agentList.length > 0) {
      CardView.layoutTree(agentList, _edges);
    }
  }

  /* ── Dispatch table ── */
  var _handlers = {
    init:             function (d) { _onInitEvent(d); },
    scene_change:     function (d) { _onSceneChangeEvent(d); },
    char_spawn:       function (d) { _onCharSpawnEvent(d); },
    hierarchy:        function (d) { _onHierarchyEvent(d); },
    progress:         function (d) { _onProgressEvent(d); },
    step_progress:    function (d) { _onStepProgressEvent(d); },
    message:          function (d) { _onMessageEvent(d); },
    interrupt:        function (d) { _onInterruptEvent(d); },
    complete:         function (d) { _onCompleteEvent(d); },
    error:            function (d) { _onErrorEvent(d); },
    heartbeat:        function ()  { /* noop */ },
    space_transition: function ()  { /* card view ignores space transitions */ },
    research_finding: function (d) {
      if (_onChatMessage && d.finding) {
        _onChatMessage('🔍 ' + (d.sub_query || '') + ': ' + d.finding, 'system', { finding: true });
      }
    },
    activity: function (d) {
      // 캔버스/인라인 영역에 활동 대시보드 렌더링
      _renderActivityToCanvas(d);
      // 풀와이드 모드: 도구 사용도 채팅 타임라인에 표시
      var app = document.getElementById('card-app');
      var isFullwidth = app && app.classList.contains('chat-fullwidth');
      if (d.action === 'started' || d.action === 'completed' || d.action === 'timeout') {
        if (_onChatMessage) _onChatMessage(d.message || '', 'system', { activity: true });
        if (d.action === 'completed' && _onHierarchy) _onHierarchy();
      }
    },
  };

  /* ── Public API ── */

  function init(opts) {
    _onChatMessage = opts.onChatMessage || null;
    _onInterrupt = opts.onInterrupt || null;
    _onComplete = opts.onComplete || null;
    _onError = opts.onError || null;
    _onSceneChange = opts.onSceneChange || null;
    _onHierarchy = opts.onHierarchy || null;
  }

  function handle(ev) {
    if (!ev || !ev.type) return;
    if (ev.type !== 'heartbeat') {
      console.log('[CEH]', ev.type, JSON.stringify(ev.data || {}).slice(0, 200));
    }
    var fn = _handlers[ev.type];
    if (fn) fn(ev.data || {});
    else console.warn('[CEH] no handler for:', ev.type);
  }

  function reset() {
    _agents = {};
    _edges = [];
    _stepLabel = '';
    _toolStats = {};
    _activityFeed = [];
    if (_activityTimer) { clearInterval(_activityTimer); _activityTimer = null; }
    var dash = document.getElementById('activity-dash');
    if (dash) dash.remove();
  }

  function getAgents() { return _agents; }
  function getStepLabel() { return _stepLabel; }

  return {
    init: init,
    handle: handle,
    reset: reset,
    getAgents: getAgents,
    getStepLabel: getStepLabel,
  };
})();
