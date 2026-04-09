/* card-builder.js — Company builder mode UI logic.
 *
 * Manages:
 *  - WebSocket /ws/company-builder connection
 *  - Action buttons (new team, add agent, load team)
 *  - builder_stream → chat panel token streaming
 *  - builder_team → CardView.layoutTree()
 *  - save/load/delete company operations
 */
var CardBuilder = (function () {
  'use strict';

  var _ws = null;
  var _wsReady = false;
  var _chatPanel = null;
  var _streamBuffer = '';  // accumulates streamed tokens
  var _streamEl = null;    // current streaming message DOM element
  var _companies = [];     // list of saved companies
  var _schedules = [];     // list of saved schedules
  var _strategies = [];    // list of saved strategies
  var _currentCompanyId = null;
  var _currentStrategyId = null;
  var _currentStrategy = null; // loaded strategy data
  var _useStrategyMode = true; // 싱글 세션 모드에서는 항상 전략 설계
  var _pendingEditMode = false; // 수정 요청 대기 플래그
  var _initialStrategyBtnAdded = false; // 초기 저장된 방식 버튼 추가 여부
  var _strategyType = 'general'; // 현재 선택된 전략 타입: general | schedule | overtime
  var _typeSelected = false; // 타입 선택 완료 여부

  /* ── WebSocket ── */

  function _connect(chatPanel) {
    _chatPanel = chatPanel;
    if (_ws && (_ws.readyState === WebSocket.OPEN || _ws.readyState === WebSocket.CONNECTING)) return;

    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var url = proto + '//' + location.host + '/ws/company-builder';
    _ws = new WebSocket(url);
    _wsReady = false;

    _ws.onopen = function () { _wsReady = true; };

    _ws.onmessage = function (e) {
      try {
        var msg = JSON.parse(e.data);
        _handleMessage(msg);
      } catch (err) { /* ignore */ }
    };

    _ws.onclose = function () {
      _wsReady = false;
      _ws = null;
      // Only reconnect if builder mode is still active
      if (CardView.getActiveMode() === 'builder') {
        setTimeout(function () { _connect(_chatPanel); }, 3000);
      }
    };

    _ws.onerror = function () { _wsReady = false; };
  }

  function _send(obj) {
    if (_ws && _wsReady) _ws.send(JSON.stringify(obj));
  }

  function disconnect() {
    if (_ws) {
      // Remove onclose to prevent reconnect after intentional disconnect
      _ws.onclose = null;
      _ws.onerror = null;
      try { _ws.close(); } catch (_) {}
      _ws = null;
      _wsReady = false;
    }
    _streamEl = null;
    _streamBuffer = '';
  }

  /* ── Message Handlers ── */

  function _handleMessage(msg) {
    var type = msg.type;
    var data = msg.data || {};

    if (type === 'builder_stream') {
      _handleStream(data);
    } else if (type === 'builder_team') {
      _handleTeam(data);
    } else if (type === 'builder_companies') {
      _companies = data.companies || [];
      _renderSidebarTeamList();
    } else if (type === 'company_saved') {
      _currentCompanyId = data.id || null;
      if (_chatPanel) _chatPanel.addMessage('💾 팀이 저장되었습니다: ' + (data.name || data.id), 'system');
    } else if (type === 'company_loaded') {
      _loadCompanyToCanvas(data);
    } else if (type === 'company_deleted') {
      if (_chatPanel) _chatPanel.addMessage('🗑️ 팀이 삭제되었습니다.', 'system');
    } else if (type === 'schedule_saved') {
      if (_chatPanel) _chatPanel.addMessage('⏰ 스케줄이 저장되었습니다: ' + (data.name || data.id), 'system');
    } else if (type === 'schedule_list') {
      _schedules = data.schedules || [];
    } else if (type === 'schedule_toggled') {
      var state = data.enabled ? '활성화' : '비활성화';
      if (_chatPanel) _chatPanel.addMessage('⏰ 스케줄 ' + state + ': ' + (data.name || data.id), 'system');
    } else if (type === 'schedule_deleted') {
      if (_chatPanel) _chatPanel.addMessage('🗑️ 스케줄이 삭제되었습니다.', 'system');
    } else if (type === 'task_validation') {
      if (data.fit) {
        if (_onTaskValidated) _onTaskValidated(data);
        _onTaskValidated = null;
      } else {
        _onTaskValidated = null;
        if (_chatPanel) {
          _chatPanel.addMessage('⚠️ ' + (data.suggestion || '이 업무는 현재 팀과 맞지 않습니다.'), 'system');
          if (data.matching_team_id) {
            _chatPanel.addMessage('💡 다른 저장된 팀이 더 적합할 수 있습니다. 불러오시겠습니까?', 'system');
          }
        }
      }
    } else if (type === 'builder_strategy') {
      _handleStrategy(data);
    } else if (type === 'builder_strategies') {
      _strategies = data.strategies || [];
      _renderSidebarStrategyList();
    } else if (type === 'strategy_saved') {
      _currentStrategyId = data.id || null;
      if (_chatPanel) _chatPanel.addMessage('💾 방식이 저장되었습니다: ' + (data.name || data.id), 'system');
    } else if (type === 'strategy_loaded') {
      _displayStrategyCards(data);
    } else if (type === 'strategy_deleted') {
      if (_chatPanel) _chatPanel.addMessage('🗑️ 방식이 삭제되었습니다.', 'system');
    } else if (type === 'error') {
      if (_chatPanel) _chatPanel.addMessage('❌ ' + (data.message || '오류'), 'system');
    }
  }

  function _handleStream(data) {
    if (!_chatPanel) return;

    if (!_streamEl) {
      // Start new streaming message — thinking 제거
      _chatPanel.hideThinking();
      _streamBuffer = '';
      _streamEl = document.createElement('div');
      _streamEl.className = 'cc-message cc-message-system';
      _streamEl.id = 'builder-stream-msg';
      _chatPanel.messagesEl.appendChild(_streamEl);
    }

    _streamBuffer += data.token || '';

    // Strip ```team_json ... ``` block from display (raw JSON is not user-facing)
    var displayText = _streamBuffer.replace(/```team_json[\s\S]*?```/g, '').trim();
    // Also strip trailing ``` if team_json block is still being streamed
    displayText = displayText.replace(/```team_json[\s\S]*/g, '').trim();
    // 마크다운 렌더링 — marked.js (Secretary/Discussion과 동일 패턴, 백엔드 생성 콘텐츠)
    if (typeof marked !== 'undefined') {
      try { _streamEl.innerHTML = marked.parse(displayText); } catch (_) { _streamEl.textContent = displayText; } // eslint-disable-line no-unsanitized/property
    } else {
      _streamEl.textContent = displayText;
    }
    _chatPanel.messagesEl.scrollTop = _chatPanel.messagesEl.scrollHeight;

    if (data.done) {
      // Final cleanup — remove team/strategy JSON blocks
      displayText = _streamBuffer.replace(/```(?:team_json|strategy_json)[\s\S]*?```/g, '').trim();
      if (typeof marked !== 'undefined') {
        try { _streamEl.innerHTML = marked.parse(displayText); } catch (_) { _streamEl.textContent = displayText; } // eslint-disable-line no-unsanitized/property
      } else {
        _streamEl.textContent = displayText;
      }
      _streamEl = null;
      _streamBuffer = '';
      // thinking indicator 제거 + placeholder 복원
      if (_chatPanel) {
        _chatPanel.hideThinking();
        _chatPanel.setInputPlaceholder('이 전략으로 업무를 지시하세요...');
      }
    }
  }

  function _handleTeam(data) {
    var agents = data.agents || [];
    var edges = data.edges || [];

    if (agents.length === 0) return;

    // Hide empty state
    var emptyEl = document.getElementById('card-empty-state');
    if (emptyEl) emptyEl.style.display = 'none';

    CardView.layoutTree(agents, edges);

    if (_chatPanel) {
      _chatPanel.addMessage('🏗️ 팀 구조가 캔버스에 배치되었습니다. (' + agents.length + '명)', 'system');
      _chatPanel.setInputPlaceholder('이 팀에게 업무를 지시하세요...');
    }
  }

  /* ── Strategy Handling ── */

  function _handleStrategy(data) {
    _currentStrategy = data;
    _displayStrategyCards(data);
    // 스트리밍 텍스트는 사용자 안내를 포함하므로 유지 (숨기지 않음)
    if (_chatPanel) {
      // 방식 저장 안내 + 버튼 (카드 바로 아래)
      _chatPanel.addMessage('이 방식을 저장하면 다음에 바로 불러와 사용할 수 있습니다.', 'system');
      _chatPanel.addActionButtons([
        {
          label: '이 방식 저장하기',
          icon: '💾',
          action: function () {
            _send({ type: 'save_strategy', data: data });
          },
        },
        {
          label: '방식 수정 요청',
          icon: '✏️',
          action: function () {
            _chatPanel.setInputPlaceholder('수정 요청을 입력하세요...');
            _chatPanel.addMessage('어떤 부분을 수정할까요? (예: "경쟁사 분석 관점 추가해줘")', 'system');
            _pendingEditMode = true;
          },
        },
      ]);
      // 출력 형식 선택 — 실행 지시 시 선택
      _chatPanel.showFormatSelector([
        { id: 'html', label: 'HTML', icon: '📄', default: true },
        { id: 'pdf', label: 'PDF', icon: '📑' },
        { id: 'markdown', label: 'Markdown', icon: '📝' },
        { id: 'csv', label: 'CSV', icon: '📊' },
        { id: 'json', label: 'JSON', icon: '{}' },
      ]);
      _chatPanel.setInputPlaceholder('이 방식으로 업무를 지시하세요...');
    }
  }

  function _displayStrategyCards(strategy) {
    if (!strategy) { _currentStrategy = null; _currentStrategyId = null; return; }
    _currentStrategy = strategy;
    _currentStrategyId = strategy.id || null;

    // 풀와이드 모드: 채팅 메시지 영역에 인라인 렌더링
    // 분할 모드: 캔버스에 오버레이 렌더링
    var app = document.getElementById('card-app');
    var isInline = app && app.classList.contains('chat-fullwidth');
    var container = isInline
      ? (_chatPanel ? _chatPanel.messagesEl : document.querySelector('.cc-messages'))
      : document.getElementById('card-canvas');
    if (!container) return;

    if (!isInline) {
      // 분할 모드: 빈 상태 숨기기 + Drawflow 숨기기
      var emptyEl = document.getElementById('card-empty-state');
      if (emptyEl) emptyEl.style.display = 'none';
      var drawflow = container.querySelector('.drawflow');
      if (drawflow) drawflow.style.display = 'none';
    }

    // 기존 전략 뷰 제거
    var old = document.getElementById('strategy-view');
    if (old) old.remove();

    var perspectives = strategy.perspectives || [];
    var depthLabels = { light: '간략', standard: '표준', deep: '심층' };
    var formatLabels = { summary: '요약', executive_report: '보고서', data_table: '데이터', presentation: '발표' };

    // 전략 뷰 생성 (DOM only, no innerHTML with user data)
    var view = document.createElement('div');
    view.id = 'strategy-view';

    // 헤더
    var header = document.createElement('div');
    header.className = 'sv-header';
    var title = document.createElement('div');
    title.className = 'sv-title';
    title.textContent = strategy.name || '분석 전략';
    var desc = document.createElement('div');
    desc.className = 'sv-desc';
    desc.textContent = strategy.description || '';
    header.appendChild(title);
    header.appendChild(desc);
    view.appendChild(header);

    // 메타 태그
    var meta = document.createElement('div');
    meta.className = 'sv-meta';
    var depthTag = document.createElement('span');
    depthTag.className = 'sv-tag';
    depthTag.textContent = '깊이: ' + (depthLabels[strategy.depth] || '표준');
    var fmtTag = document.createElement('span');
    fmtTag.className = 'sv-tag';
    fmtTag.textContent = '형식: ' + (formatLabels[strategy.output_format] || '보고서');
    meta.appendChild(depthTag);
    meta.appendChild(fmtTag);
    view.appendChild(meta);

    // 관점 카드 그리드
    var grid = document.createElement('div');
    grid.className = 'sv-grid';
    for (var i = 0; i < perspectives.length; i++) {
      var p = perspectives[i];
      var card = document.createElement('div');
      card.className = 'sv-card';
      var cardIcon = document.createElement('div');
      cardIcon.className = 'sv-card-icon';
      cardIcon.textContent = p.icon || '📌';
      var cardName = document.createElement('div');
      cardName.className = 'sv-card-name';
      cardName.textContent = p.name || '관점';
      var cardInst = document.createElement('div');
      cardInst.className = 'sv-card-inst';
      cardInst.textContent = p.instruction || '';
      card.appendChild(cardIcon);
      card.appendChild(cardName);
      card.appendChild(cardInst);
      grid.appendChild(card);
    }
    view.appendChild(grid);

    // 특별 지시
    if (strategy.special_instructions) {
      var special = document.createElement('div');
      special.className = 'sv-special';
      special.textContent = '💡 ' + strategy.special_instructions;
      view.appendChild(special);
    }

    if (isInline) view.classList.add('sv-inline');
    container.appendChild(view);
    if (isInline) container.scrollTop = container.scrollHeight;
  }

  function _renderSidebarStrategyList() {
    // WebSocket 초기 수신 시만 호출 (초기 버튼 추가는 _handleMessage에서 처리)
  }

  var _showStrategyListRequested = false;
  var _strategyListEl = null;

  function showStrategyList() {
    if (!_chatPanel) return;

    // 이미 열려있으면 토글로 닫기
    if (_strategyListEl && _strategyListEl.parentNode) {
      _strategyListEl.remove();
      _strategyListEl = null;
      return;
    }

    if (_strategies.length === 0) {
      _chatPanel.addMessage('저장된 방식이 없습니다. 새로 만들어보세요.', 'system');
      return;
    }

    // 트리형 리스트 생성
    var list = document.createElement('div');
    list.className = 'cc-strategy-tree';
    for (var i = 0; i < _strategies.length; i++) {
      (function (s) {
        var item = document.createElement('div');
        item.className = 'cc-strategy-item';
        item.textContent = '📊 ' + (s.name || '방식');
        item.addEventListener('click', function () {
          _send({ type: 'load_strategy', data: { strategy_id: s.id } });
          _displayStrategyCards(s);
          if (_chatPanel) {
            _chatPanel.addMessage('✅ "' + (s.name || '방식') + '" 방식이 로드되었습니다. 업무를 지시하세요.', 'system');
            _chatPanel.setInputPlaceholder('이 방식으로 업무를 지시하세요...');
            // 출력 형식 선택 표시
            _chatPanel.showFormatSelector([
              { id: 'html', label: 'HTML', icon: '📄', default: true },
              { id: 'pdf', label: 'PDF', icon: '📑' },
              { id: 'markdown', label: 'Markdown', icon: '📝' },
              { id: 'csv', label: 'CSV', icon: '📊' },
              { id: 'json', label: 'JSON', icon: '{}' },
            ]);
          }
          // 리스트 닫기
          if (list.parentNode) list.remove();
          _strategyListEl = null;
        });
        list.appendChild(item);
      })(_strategies[i]);
    }
    _chatPanel.messagesEl.appendChild(list);
    _chatPanel.messagesEl.scrollTop = _chatPanel.messagesEl.scrollHeight;
    _strategyListEl = list;
  }

  function _loadCompanyToCanvas(company) {
    _currentCompanyId = company.id || null;
    var agents = company.agents || [];
    var edges = company.edges || [];

    // Reconstruct from flow if available
    if (company.flow && !agents.length) {
      // flow is Drawflow export — import directly
      var editor = CardView.getEditor();
      if (editor && company.flow.drawflow) {
        editor.import(company.flow);
        return;
      }
    }

    if (agents.length > 0) {
      var emptyEl = document.getElementById('card-empty-state');
      if (emptyEl) emptyEl.style.display = 'none';
      CardView.layoutTree(agents, edges);
    }

    if (_chatPanel) {
      _chatPanel.addMessage('📂 "' + (company.name || '팀') + '" 을 불러왔습니다.', 'system');
      if (agents.length > 0) {
        _chatPanel.setInputPlaceholder('이 팀에게 업무를 지시하세요...');
      }
    }
  }

  /* ── Sidebar Team List ── */

  function _renderSidebarTeamList() {
    var listEl = document.getElementById('cs-team-list');
    if (!listEl) return;
    while (listEl.firstChild) listEl.removeChild(listEl.firstChild);

    if (_companies.length === 0) return;

    var sep = document.createElement('div');
    sep.className = 'cs-team-sep';
    listEl.appendChild(sep);

    var header = document.createElement('div');
    header.className = 'cs-team-header';
    header.textContent = '저장된 팀';
    listEl.appendChild(header);

    _companies.forEach(function (co) {
      var item = document.createElement('button');
      item.className = 'cs-team-item';
      item.title = co.name || co.id;
      item.textContent = '📁';

      var nameSpan = document.createElement('span');
      nameSpan.className = 'cs-team-name';
      nameSpan.textContent = co.name || co.id;
      item.appendChild(nameSpan);

      item.addEventListener('click', function () {
        loadCompany(co.id);
      });
      listEl.appendChild(item);
    });
  }

  /* ── Strategy Type Selector ── */

  var _TYPE_INFO = {
    general: { icon: '🔍', label: 'General', desc: '범용 분석 방식' },
    schedule: { icon: '📅', label: '스케줄', desc: '정기 반복 모니터링용' },
    overtime: { icon: '🌙', label: '야근', desc: '심층 반복 탐색용' },
  };

  function showTypeSelector() {
    if (!_chatPanel) return;
    _typeSelected = false;

    var container = document.createElement('div');
    container.className = 'cb-type-selector';
    container.id = 'cb-type-selector';

    var title = document.createElement('div');
    title.className = 'cb-type-title';
    title.textContent = '어떤 용도의 방식을 만드시겠어요?';
    container.appendChild(title);

    var grid = document.createElement('div');
    grid.className = 'cb-type-grid';

    var types = ['general', 'schedule', 'overtime'];
    for (var i = 0; i < types.length; i++) {
      (function (t) {
        var info = _TYPE_INFO[t];
        var btn = document.createElement('button');
        btn.className = 'cb-type-btn';
        btn.dataset.type = t;

        var icon = document.createElement('div');
        icon.className = 'cb-type-icon';
        icon.textContent = info.icon;
        btn.appendChild(icon);

        var label = document.createElement('div');
        label.className = 'cb-type-label';
        label.textContent = info.label;
        btn.appendChild(label);

        var desc = document.createElement('div');
        desc.className = 'cb-type-desc';
        desc.textContent = info.desc;
        btn.appendChild(desc);

        btn.addEventListener('click', function () {
          _selectType(t);
        });
        grid.appendChild(btn);
      })(types[i]);
    }

    container.appendChild(grid);
    _chatPanel.messagesEl.appendChild(container);
    _chatPanel.messagesEl.scrollTop = _chatPanel.messagesEl.scrollHeight;
  }

  function _selectType(type) {
    _strategyType = type;
    _typeSelected = true;

    // 서버에 타입 설정 전송
    _send({ type: 'set_strategy_type', data: { strategy_type: type } });

    // 선택 UI 업데이트 — 선택된 버튼 강조, 나머지 비활성
    var selector = document.getElementById('cb-type-selector');
    if (selector) {
      var btns = selector.querySelectorAll('.cb-type-btn');
      for (var i = 0; i < btns.length; i++) {
        if (btns[i].dataset.type === type) {
          btns[i].classList.add('cb-type-selected');
        } else {
          btns[i].classList.add('cb-type-dimmed');
          btns[i].disabled = true;
        }
      }
    }

    // 안내 메시지
    var info = _TYPE_INFO[type];
    if (_chatPanel) {
      _chatPanel.addMessage(
        info.icon + ' **' + info.label + '** 방식을 설계합니다. 어떤 분석이 필요한지 알려주세요.',
        'system'
      );
      _chatPanel.setInputPlaceholder(
        type === 'schedule' ? '정기 모니터링할 내용을 알려주세요...' :
        type === 'overtime' ? '심층 분석할 내용을 알려주세요...' :
        '분석 방식을 설계해보세요...'
      );
    }
  }

  /**
   * 외부에서 특정 타입으로 빌더를 시작할 때 사용.
   * 스케줄팀/야근팀에서 "새로 만들기" 시 호출.
   */
  function startWithType(type) {
    // builder 모드로 전환 (create 서브탭 강제)
    if (typeof CardView !== 'undefined') {
      CardView.switchMode('builder');
    }

    // 메시지 영역 초기화 + 타입 선택 상태 리셋
    _typeSelected = false;
    _strategyType = 'general';
    if (_chatPanel && _chatPanel.messagesEl) {
      while (_chatPanel.messagesEl.firstChild) {
        _chatPanel.messagesEl.removeChild(_chatPanel.messagesEl.firstChild);
      }
    }

    // WS 연결 보장
    if (_chatPanel) _connect(_chatPanel);

    // 타입 바로 선택 (selector 없이 즉시 설정)
    var retries = 0;
    var trySet = function () {
      if (_ws && _wsReady) {
        _selectType(type);
      } else if (retries < 30) {
        retries++;
        setTimeout(trySet, 200);
      }
    };
    trySet();
  }

  /* ── Helpers ── */

  function _summarizeStrategyForEdit(s) {
    if (!s) return '(없음)';
    var lines = [];
    lines.push('- 이름: ' + (s.name || ''));
    if (s.description) lines.push('- 설명: ' + s.description);
    lines.push('- 깊이: ' + (s.depth || 'standard'));
    lines.push('- 출력 형식: ' + (s.output_format || 'executive_report'));
    var perspectives = s.perspectives || [];
    lines.push('- 관점 (' + perspectives.length + '개):');
    for (var i = 0; i < perspectives.length; i++) {
      var p = perspectives[i] || {};
      lines.push(
        '  ' + (i + 1) + '. ' + (p.icon || '📌') + ' ' + (p.name || '관점') +
        (p.instruction ? ' — ' + p.instruction : '')
      );
    }
    if (s.special_instructions) {
      lines.push('- 특별 지시: ' + s.special_instructions);
    }
    return lines.join('\n');
  }

  /* ── Public API ── */

  function sendMessage(text) {
    // 타입 미선택 시 안내
    if (_useStrategyMode && !_typeSelected && !_currentStrategy) {
      if (_chatPanel) _chatPanel.addMessage('먼저 방식 유형을 선택해주세요.', 'system');
      return;
    }
    // 수정 요청 모드: 현재 전략 전체 컨텍스트를 포함하여 재설계 요청 전달
    // (세션이 유실되었거나 CLI resume이 실패해도 모델이 전체 맥락을 복원할 수 있도록)
    if (_pendingEditMode && _currentStrategy) {
      _pendingEditMode = false;
      var strategyCtx = _summarizeStrategyForEdit(_currentStrategy);
      var editPrompt =
        '[방식 수정 요청 — 명확화 질문 없이 바로 재설계]\n\n' +
        '## 현재 방식\n' + strategyCtx + '\n\n' +
        '## 사용자 수정 요청\n' + text + '\n\n' +
        '위 수정 요청을 반영하여 **전체 방식을 재설계**해주세요. ' +
        '반드시 ```strategy_json 블록을 포함하여 응답하세요. ' +
        '명확화 질문은 생략하고 바로 수정된 방식을 출력하세요.';
      _send({ type: 'strategy_message', data: { content: editPrompt } });
      return;
    }
    _pendingEditMode = false;
    // 싱글 세션 모드에서는 전략 설계 에이전트로 전달
    var msgType = _useStrategyMode ? 'strategy_message' : 'builder_message';
    _send({ type: msgType, data: { content: text } });
  }

  function saveCurrentTeam(name, description) {
    var editor = CardView.getEditor();
    var flow = editor ? editor.export() : {};

    // Extract agents and edges from Drawflow nodes
    var agents = [];
    var edges = [];
    if (editor && flow.drawflow && flow.drawflow.Home && flow.drawflow.Home.data) {
      var nodes = flow.drawflow.Home.data;
      var nodeKeys = Object.keys(nodes);
      for (var i = 0; i < nodeKeys.length; i++) {
        var node = nodes[nodeKeys[i]];
        var d = node.data || {};
        agents.push({
          id: d.agentId || d.id || ('agent_node_' + nodeKeys[i]),
          name: d.name || 'Agent',
          role: d.role || '',
          tool_category: d.toolCategory || d.tool_category || 'research',
          emoji: d.emoji || '⚙️',
          system_prompt: d.system_prompt || '',
        });
        // Extract edges from output connections
        var outputs = node.outputs || {};
        var outKeys = Object.keys(outputs);
        for (var j = 0; j < outKeys.length; j++) {
          var conns = outputs[outKeys[j]].connections || [];
          for (var k = 0; k < conns.length; k++) {
            var targetNodeId = conns[k].node;
            var targetNode = nodes[targetNodeId];
            if (targetNode && targetNode.data) {
              edges.push({
                from: d.agentId || d.id || ('agent_node_' + nodeKeys[i]),
                to: targetNode.data.agentId || targetNode.data.id || ('agent_node_' + targetNodeId),
              });
            }
          }
        }
      }
    }

    var company = {
      name: name || '새 팀',
      description: description || '',
      agents: agents,
      edges: edges,
      flow: flow,
    };

    if (_currentCompanyId) {
      company.id = _currentCompanyId;
    }

    _send({ type: 'save_company', data: company });
  }

  function loadCompany(companyId) {
    _send({ type: 'load_company', data: { company_id: companyId } });
  }

  function deleteCompany(companyId) {
    _send({ type: 'delete_company', data: { company_id: companyId } });
  }

  function listCompanies() {
    _send({ type: 'list_companies' });
  }

  function listStrategies() {
    _send({ type: 'list_strategies' });
  }

  function getCompanies() {
    return _companies;
  }

  function saveSchedule(companyId, taskDescription, cronExpression, name) {
    _send({
      type: 'save_schedule',
      data: {
        company_id: companyId || _currentCompanyId || '',
        task_description: taskDescription,
        cron_expression: cronExpression,
        name: name || taskDescription.substring(0, 50),
        enabled: true,
      },
    });
  }

  function listSchedules() {
    _send({ type: 'list_schedules' });
  }

  function toggleSchedule(scheduleId, enabled) {
    _send({ type: 'toggle_schedule', data: { schedule_id: scheduleId, enabled: enabled } });
  }

  function deleteSchedule(scheduleId) {
    _send({ type: 'delete_schedule', data: { schedule_id: scheduleId } });
  }

  function getSchedules() {
    return _schedules;
  }

  function isConnected() {
    return _wsReady;
  }

  /* ── Team execution helpers ── */

  var _onTaskValidated = null;

  function getCanvasAgents() {
    var editor = CardView.getEditor();
    if (!editor) return [];
    var flow = editor.export();
    if (!flow.drawflow || !flow.drawflow.Home || !flow.drawflow.Home.data) return [];
    var nodes = flow.drawflow.Home.data;
    var agents = [];
    var nodeKeys = Object.keys(nodes);
    for (var i = 0; i < nodeKeys.length; i++) {
      var d = nodes[nodeKeys[i]].data || {};
      if (d.agentId || d.name) agents.push(d);
    }
    return agents;
  }

  function getCurrentCompanyId() {
    return _currentCompanyId || '';
  }

  function validateTask(task, teamId, callback) {
    _onTaskValidated = callback;
    _send({ type: 'validate_task', data: { task: task, team_id: teamId } });
  }

  return {
    connect: _connect,
    disconnect: disconnect,
    sendMessage: sendMessage,
    saveCurrentTeam: saveCurrentTeam,
    loadCompany: loadCompany,
    deleteCompany: deleteCompany,
    listCompanies: listCompanies,
    listStrategies: listStrategies,
    showStrategyList: showStrategyList,
    getCompanies: getCompanies,
    saveSchedule: saveSchedule,
    listSchedules: listSchedules,
    toggleSchedule: toggleSchedule,
    deleteSchedule: deleteSchedule,
    getSchedules: getSchedules,
    isConnected: isConnected,
    getCanvasAgents: getCanvasAgents,
    getCurrentCompanyId: getCurrentCompanyId,
    getCurrentStrategyId: function () { return _currentStrategyId; },
    getCurrentStrategy: function () { return _currentStrategy; },
    getStrategies: function () { return _strategies; },
    getStrategiesByType: function (type) {
      return _strategies.filter(function (s) { return (s.type || 'general') === type; });
    },
    loadAndDisplayStrategy: function (s) { _displayStrategyCards(s); },
    deleteStrategy: function (id) { _send({ type: 'delete_strategy', data: { strategy_id: id } }); },
    setPendingEditMode: function (v) { _pendingEditMode = !!v; },
    isPendingEditMode: function () { return _pendingEditMode; },
    validateTask: validateTask,
    showTypeSelector: showTypeSelector,
    startWithType: startWithType,
    getStrategyType: function () { return _strategyType; },
    isTypeSelected: function () { return _typeSelected; },
    resetTypeSelection: function () { _typeSelected = false; _strategyType = 'general'; },
  };
})();
