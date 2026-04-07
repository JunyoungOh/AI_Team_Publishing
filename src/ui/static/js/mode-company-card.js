/* mode-company-card.js — Card view orchestrator (Drawflow + sidebar + chat) */
var CardView = (function () {
  'use strict';

  /* ── Internal state ── */
  var _editor = null;       // Drawflow instance
  var _chatPanel = null;    // CardChatPanel instance
  var _activeMode = 'instant'; // 'instant' | 'builder' | 'discussion' | 'foresight' | 'persona' | 'secretary'
  var _nodeMap = {};         // { agentId: drawflowNodeId }
  var _ws = null;            // WebSocket instance
  var _wsReady = false;      // WebSocket open state
  var _running = false;      // pipeline running state
  var _runningMode = null;   // which mode started the running task

  /* ── Welcome messages per mode ── */
  var WELCOME = {
    instant: '업무를 지시해주세요. CEO가 팀을 구성하고 실행합니다.',
    builder: '나만의 팀 모드입니다. \'새 팀 만들기\', \'에이전트 추가\', \'저장된 팀 불러오기\' 중 선택하세요.',
  };

  /* ── Mode titles for header ── */
  var _modeTitles = {
    instant: 'AI Company',
    builder: 'AI Company',
    discussion: 'AI 토론',
    foresight: 'Foresight',
    persona: '페르소나 워크숍',
    secretary: 'AI 비서'
  };

  /* ── Boot tracking for lazy-init modes ── */
  var _modeBooted = {};

  /* ── Private helpers ── */

  function _selectSidebarMode(mode) {
    var prevMode = _activeMode;
    _activeMode = mode;

    // 사이드바 활성 상태 업데이트
    document.querySelectorAll('#card-sidebar .cs-item').forEach(function (item) {
      item.classList.toggle('active', item.dataset.cardMode === mode);
    });

    // URL 해시
    location.hash = mode;

    // Company 모드 (instant/builder)
    if (mode === 'instant' || mode === 'builder') {
      _showCompanyMode(mode, prevMode);
      return;
    }

    // 비-Company 모드 (실행 중이면 UI만 숨기고 작업은 계속)
    _hideCompanyUI();
    document.querySelectorAll('.card-mode-content').forEach(function (el) {
      el.style.display = 'none';
    });
    var container = document.getElementById('card-mode-' + mode);
    if (container) container.style.display = '';
    _bootMode(mode);
    _updateHeaderForMode(mode);
  }

  function _showCompanyMode(mode, prevMode) {
    // Hide non-company mode content
    document.querySelectorAll('.card-mode-content').forEach(function (el) {
      el.style.display = 'none';
    });

    var app = document.getElementById('card-app');

    // 인스턴트 + 빌더 모두 풀와이드 채팅 (canvas 숨김)
    if (app) app.classList.add('chat-fullwidth');

    // 모드별 메시지 컨테이너 전환
    if (_chatPanel) {
      _chatPanel.switchMode(mode);
      _chatPanel.toggle(true);
    }

    // 실행 중인 모드로 복귀하면 UI 복원
    if (_running && _runningMode === mode) {
      if (mode === 'builder') {
        CardBuilder.connect(_chatPanel);
      }
      _updateHeaderForMode(mode);
      document.getElementById('card-stop-btn').style.display = '';
      var stepBar = document.getElementById('card-step-bar');
      if (stepBar && CardEventHandler.getStepLabel()) stepBar.style.display = '';
      return;
    }

    // 다른 모드로 전환: 중지 버튼/스텝바는 현재 모드 것만 표시
    if (!_running || _runningMode !== mode) {
      document.getElementById('card-stop-btn').style.display = 'none';
      document.getElementById('card-step-bar').style.display = 'none';
    }

    // 이미 초기화된 모드면 컨텐츠 유지 (재초기화 하지 않음)
    if (_chatPanel && _chatPanel.messagesEl.childNodes.length > 0) {
      // Connect builder WS if needed
      if (mode === 'builder') {
        CardBuilder.connect(_chatPanel);
      }
      _updateHeaderForMode(mode);
      return;
    }

    // 첫 진입: 초기화
    CardEventHandler.reset();

    if (_editor) {
      _editor.clear();
    }
    _nodeMap = {};

    // Connect/disconnect builder WS based on mode
    if (mode === 'builder') {
      CardBuilder.connect(_chatPanel);
    } else {
      CardBuilder.disconnect();
    }

    CardEditor.close();

    // Show welcome for this mode
    if (_chatPanel) {
      _chatPanel.addMessage(WELCOME[mode] || '', 'system', { welcome: true });
      _chatPanel.setInputPlaceholder(
        mode === 'builder' ? '팀 설계를 요청하세요...' : '업무를 지시하세요...'
      );

      // 출력 형식 선택 (인스턴트 + 빌더 공통)
      if (mode === 'instant' || mode === 'builder') {
        _chatPanel.showFormatSelector([
          { id: 'html', label: 'HTML', icon: '📄', default: true },
          { id: 'pdf', label: 'PDF', icon: '📑' },
          { id: 'markdown', label: 'Markdown', icon: '📝' },
          { id: 'csv', label: 'CSV', icon: '📊' },
          { id: 'json', label: 'JSON', icon: '{}' },
        ]);
      }

      // Add action buttons for builder mode
      if (mode === 'builder') {
        _chatPanel.addActionButtons([
          { label: '새 팀 만들기', icon: '🏗️', action: function () {
            _chatPanel.addMessage('어떤 팀을 만들까요? 목적과 분야를 알려주세요.', 'system');
            _chatPanel.setInputPlaceholder('예: 이커머스 마케팅 팀을 만들어줘');
          }},
          { label: '저장된 팀 불러오기', icon: '📂', action: function () {
            CardBuilder.listCompanies();
            _chatPanel.addMessage('저장된 팀 목록을 조회 중입니다...', 'system');
          }},
          { label: '에이전트 추가', icon: '➕', action: function () {
            _chatPanel.addMessage('어떤 역할의 에이전트를 추가할까요?', 'system');
            _chatPanel.setInputPlaceholder('예: SNS 마케팅 분석가를 추가해줘');
          }},
          { label: '현재 팀 저장', icon: '💾', action: function () {
            var name = prompt('팀 이름을 입력하세요:');
            if (name) {
              CardBuilder.saveCurrentTeam(name, '');
            }
          }},
        ]);
      }
    }

    // Hide stop button
    var stopBtn = document.getElementById('card-stop-btn');
    if (stopBtn) stopBtn.style.display = 'none';

    _updateHeaderForMode(mode);
  }

  function _hideCompanyUI() {
    var drawflow = document.querySelector('#card-canvas .drawflow');
    if (drawflow) drawflow.style.display = 'none';
    var actDash = document.getElementById('activity-dash');
    if (actDash) actDash.style.display = 'none';
    document.getElementById('card-empty-state').style.display = 'none';
    document.getElementById('card-stop-btn').style.display = 'none';
    document.getElementById('card-step-bar').style.display = 'none';
    var chatToggle = document.getElementById('card-chat-toggle');
    if (chatToggle) chatToggle.style.display = 'none';
    if (_chatPanel) _chatPanel.toggle(false);
    // Ensure fullwidth and chat-open grid columns collapse
    var cardApp = document.getElementById('card-app');
    if (cardApp) cardApp.classList.remove('chat-fullwidth');
    if (cardApp) cardApp.classList.remove('chat-open');
    CardEditor.close();
  }

  function _bootMode(mode) {
    if (_modeBooted[mode]) return;
    var container = document.getElementById('card-mode-' + mode);
    switch (mode) {
      case 'discussion':
        if (typeof DiscussionManager !== 'undefined' && DiscussionManager.mountInShell) {
          DiscussionManager.mountInShell(container); _modeBooted[mode] = true;
        }
        break;
      case 'foresight':
        if (typeof _mountDandelion === 'function') {
          _mountDandelion(container); _modeBooted[mode] = true;
        }
        break;
      case 'persona':
        if (typeof PersonaManager !== 'undefined' && PersonaManager.mountInShell) {
          PersonaManager.mountInShell(container); _modeBooted[mode] = true;
        }
        break;
      case 'secretary':
        if (typeof SecretaryManager !== 'undefined' && SecretaryManager.mountInShell) {
          SecretaryManager.mountInShell(container); _modeBooted[mode] = true;
        }
        break;
      case 'schedule':
        if (typeof ScheduleTeamManager !== 'undefined' && ScheduleTeamManager.mountInShell) {
          ScheduleTeamManager.mountInShell(container); _modeBooted[mode] = true;
        }
        break;
      case 'overtime':
        if (typeof OvertimeManager !== 'undefined' && OvertimeManager.mountInShell) {
          OvertimeManager.mountInShell(container); _modeBooted[mode] = true;
        }
        break;
    }
  }

  function _updateHeaderForMode(mode) {
    var title = document.querySelector('.ch-title');
    if (!title) return;
    var accentMap = {
      instant: ['AI ', 'Company'],
      builder: ['나만의 ', '팀'],
      discussion: ['AI ', 'Discussion'],
      foresight: ['', 'Foresight'],
      persona: ['Persona ', 'Workshop'],
      secretary: ['AI ', 'Secretary'],
      schedule: ['', '스케줄팀'],
      overtime: ['', '야근팀']
    };
    var parts = accentMap[mode] || accentMap.instant;
    title.textContent = '';
    if (parts[0]) title.appendChild(document.createTextNode(parts[0]));
    var accent = document.createElement('span');
    accent.className = 'ch-title-accent';
    accent.textContent = parts[1];
    title.appendChild(accent);
  }

  /* ── WebSocket ── */

  function _connectWS() {
    if (_ws && (_ws.readyState === WebSocket.OPEN || _ws.readyState === WebSocket.CONNECTING)) return;

    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var url = proto + '//' + location.host + '/ws';
    _ws = new WebSocket(url);
    _wsReady = false;

    _ws.onopen = function () {
      _wsReady = true;
    };

    _ws.onmessage = function (e) {
      try {
        var ev = JSON.parse(e.data);
        CardEventHandler.handle(ev);
      } catch (err) { /* ignore parse errors */ }
    };

    _ws.onclose = function () {
      _wsReady = false;
      _running = false;
      _runningMode = null;
      _ws = null;
      // Reconnect only if instant mode is active
      if (_activeMode === 'instant') {
        setTimeout(_connectWS, 3000);
      }
    };

    _ws.onerror = function () {
      _wsReady = false;
    };
  }

  function _sendWS(obj) {
    if (_ws && _wsReady) {
      _ws.send(JSON.stringify(obj));
    }
  }

  function _disconnectWS() {
    if (_ws) {
      _ws.onclose = null;
      _ws.onerror = null;
      try { _ws.close(); } catch (_) {}
      _ws = null;
      _wsReady = false;
    }
  }

  function _handleChatMessage(text) {
    if (_activeMode === 'instant') {
      if (!_running) {
        // Start a new pipeline run
        _running = true;
        _runningMode = _activeMode;
        CardEventHandler.reset();
        document.getElementById('card-stop-btn').style.display = '';
        _connectWS();
        // Wait for WS to open, then send (max 5s timeout)
        var retries = 0;
        var sendStart = function () {
          if (_wsReady) {
            var fmt = (_chatPanel && _chatPanel.getSelectedFormat) ? _chatPanel.getSelectedFormat() : 'html';
            if (fmt !== 'html' && _chatPanel) {
              var fmtLabels = { markdown: 'Markdown', csv: 'CSV', json: 'JSON' };
              _chatPanel.addMessage('📎 출력 형식: ' + (fmtLabels[fmt] || fmt), 'system');
            }
            _sendWS({ type: 'start', task: text, output_format: fmt });
          } else if (retries < 50) {
            retries++;
            setTimeout(sendStart, 100);
          } else {
            _running = false;
            document.getElementById('card-stop-btn').style.display = 'none';
            if (_chatPanel) {
              _chatPanel.addMessage('❌ 서버에 연결할 수 없습니다. 인터넷 연결을 확인하고 잠시 후 다시 시도해 주세요.', 'system');
            }
          }
        };
        sendStart();
      } else {
        // If running and there's an interrupt pending, send interrupt response
        // Send as plain string — server passes data directly to graph resume
        _sendWS({ type: 'interrupt_response', data: text });
        if (_chatPanel) {
          _chatPanel.addMessage('🔄 답변을 확인했습니다. 정보를 수집하고 보고서를 작성 중입니다...', 'system');
          _chatPanel.setInputPlaceholder('작업 진행 중...');
          _chatPanel.showThinking();
        }
      }
    } else if (_activeMode === 'builder') {
      var currentStrategy = CardBuilder.getCurrentStrategy();
      var canvasAgents = CardBuilder.getCanvasAgents();

      if (currentStrategy && !_running) {
        // 전략이 로드됨 → 이 전략으로 싱글 세션 실행
        _startStrategyExecution(text, currentStrategy);
      } else if (canvasAgents && canvasAgents.length > 0 && !_running) {
        // Team is loaded — validate and execute task with this team
        var teamId = CardBuilder.getCurrentCompanyId();
        CardBuilder.validateTask(text, teamId, function (result) {
          if (result.fit) {
            _startTeamExecution(text, teamId);
          }
        });
      } else if (_running) {
        // Running — interrupt response
        _sendWS({ type: 'interrupt_response', data: text });
        if (_chatPanel) {
          _chatPanel.addMessage('🔄 답변을 확인했습니다. 정보를 수집하고 보고서를 작성 중입니다...', 'system');
          _chatPanel.setInputPlaceholder('작업 진행 중...');
          _chatPanel.showThinking();
        }
      } else {
        // No strategy loaded — 기존 전략 매칭 시도 or 전략 생성 안내
        var strategies = CardBuilder.getStrategies ? CardBuilder.getStrategies() : [];
        if (strategies.length > 0) {
          // 저장된 전략이 있으면 목록을 보여주고 선택하도록 안내
          if (_chatPanel) {
            _chatPanel.addMessage('📋 저장된 전략이 ' + strategies.length + '개 있습니다. 전략을 선택하거나 새로 만들어주세요:', 'system');
            var btns = strategies.map(function (s) {
              return {
                label: s.name || '전략',
                icon: '📊',
                action: function () {
                  CardBuilder.loadAndDisplayStrategy(s);
                  if (_chatPanel) _chatPanel.addMessage('✅ "' + s.name + '" 전략이 로드되었습니다. 업무를 지시하세요.', 'system');
                },
              };
            });
            btns.push({
              label: '새 전략 만들기',
              icon: '🏗️',
              action: function () {
                if (_chatPanel) {
                  _chatPanel.addMessage('🏗️ 새 전략을 생성 중입니다...', 'system');
                  _chatPanel.showThinking();
                  _chatPanel.setInputPlaceholder('전략 생성 중...');
                }
                CardBuilder.sendMessage('다음 작업을 위한 분석 전략을 설계해줘: ' + text);
              },
            });
            _chatPanel.addActionButtons(btns);
          }
        } else {
          // 전략이 하나도 없으면 전략 생성부터 안내
          if (_chatPanel) {
            _chatPanel.addMessage('🏗️ 등록된 분석 전략이 없습니다. 이 작업을 위한 전략을 생성 중입니다...', 'system');
            _chatPanel.showThinking();
            _chatPanel.setInputPlaceholder('전략 생성 중...');
          }
          CardBuilder.sendMessage('다음 작업을 수행하기 위한 분석 전략을 설계해줘: ' + text);
        }
      }
    }
  }

  function _startStrategyExecution(text, strategy) {
    _running = true;
    CardEventHandler.reset();
    document.getElementById('card-stop-btn').style.display = '';
    if (_chatPanel) {
      _chatPanel.addMessage('🚀 "' + (strategy.name || '전략') + '" 프레임워크로 분석을 시작합니다...', 'system');
      _chatPanel.showThinking();
      _chatPanel.setInputPlaceholder('작업 진행 중...');
    }
    _connectWS();
    var retries = 0;
    var sendStart = function () {
      if (_wsReady) {
        _sendWS({ type: 'start', task: text, strategy: strategy });
      } else if (retries < 50) {
        retries++;
        setTimeout(sendStart, 100);
      } else {
        _running = false;
        _runningMode = null;
        document.getElementById('card-stop-btn').style.display = 'none';
        if (_chatPanel) _chatPanel.addMessage('❌ 서버 연결 실패', 'system');
      }
    };
    sendStart();
  }

  function _startTeamExecution(text, teamId) {
    _running = true;
    CardEventHandler.reset();
    document.getElementById('card-stop-btn').style.display = '';
    _connectWS();
    var retries = 0;
    var sendStart = function () {
      if (_wsReady) {
        _sendWS({ type: 'start', task: text, team_id: teamId });
      } else if (retries < 50) {
        retries++;
        setTimeout(sendStart, 100);
      } else {
        _running = false;
        _runningMode = null;
        document.getElementById('card-stop-btn').style.display = 'none';
        if (_chatPanel) _chatPanel.addMessage('❌ 서버 연결 실패', 'system');
      }
    };
    sendStart();
  }

  /* ── Public API ── */

  /**
   * init — Initialize Drawflow + chat + sidebar events. Call once.
   */
  function init() {
    var canvas = document.getElementById('card-canvas');
    var chatEl = document.getElementById('card-chat');

    // Create Drawflow editor on card-canvas element
    _editor = new Drawflow(canvas);
    _editor.reroute = true;
    _editor.curvature = 0.5;
    _editor.reroute_curvature = 0.5;
    _editor.reroute_curvature_start_end = 0.5;

    // Override path calculation for top-down tree layout
    // Default Drawflow draws left→right bezier; we need top→bottom
    _editor.createCurvature = function (start_pos_x, start_pos_y, end_pos_x, end_pos_y, curvature_value) {
      var midY = (start_pos_y + end_pos_y) / 2;
      // Vertical S-curve: straight down from output, curve to input
      return ' M ' + start_pos_x + ' ' + start_pos_y +
             ' C ' + start_pos_x + ' ' + midY +
             ' ' + end_pos_x + ' ' + midY +
             ' ' + end_pos_x + ' ' + end_pos_y;
    };

    _editor.start();

    // Create chat panel
    _chatPanel = new CardChatPanel(chatEl, {
      onSend: function (text) { _handleChatMessage(text); },
    });

    // Initialize card editor
    var editorEl = document.getElementById('card-editor');
    CardEditor.init(editorEl, {
      onSave: function (updated) {
        if (_chatPanel) {
          _chatPanel.addMessage('✏️ "' + updated.name + '" 에이전트가 수정되었습니다.', 'system');
        }
      },
      onDelete: function (agentId) {
        // Remove node from Drawflow
        if (_nodeMap[agentId] != null) {
          _editor.removeNodeId('node-' + _nodeMap[agentId]);
          delete _nodeMap[agentId];
        }
        if (_chatPanel) {
          _chatPanel.addMessage('🗑️ 에이전트가 삭제되었습니다.', 'system');
        }
      },
    });

    // Drawflow node click → open editor (builder mode only)
    _editor.on('nodeSelected', function (nodeId) {
      if (_activeMode !== 'builder') return;
      var nodeData = _editor.getNodeFromId(nodeId);
      if (nodeData && nodeData.data) {
        CardEditor.open(nodeData.data);
      }
    });

    // Close editor when clicking canvas background
    _editor.on('click', function () {
      if (CardEditor.isOpen()) CardEditor.close();
    });

    // Sidebar mode selection
    document.querySelectorAll('#card-sidebar .cs-item[data-card-mode]').forEach(function (item) {
      item.addEventListener('click', function () {
        _selectSidebarMode(item.dataset.cardMode);
      });
    });

    // Chat toggle button
    document.getElementById('card-chat-toggle').addEventListener('click', function () {
      _chatPanel.toggle(true);
    });

    // Stop button
    document.getElementById('card-stop-btn').addEventListener('click', function () {
      stop();
      document.getElementById('card-stop-btn').style.display = 'none';
      document.getElementById('card-step-bar').style.display = 'none';
      if (_chatPanel) _chatPanel.addMessage('⏹️ 실행이 중지되었습니다.', 'system');
    });

    // Initialize event handler with callbacks
    CardEventHandler.init({
      onChatMessage: function (text, type, opts) {
        if (_chatPanel && !(opts && opts.silent)) {
          _chatPanel.addMessage(text, type, opts);
        }
      },
      onInterrupt: function (data) {
        if (_chatPanel) {
          var q = (data && data.questions) || (data && data.content) || '질문이 있습니다. 응답해 주세요.';
          if (Array.isArray(q)) q = q.join('\n\n');
          _chatPanel.addMessage('❓ ' + q + '\n\n아래에 답변을 입력해주세요.', 'system', { interrupt: true, preserveNewlines: true });
          _chatPanel.setInputPlaceholder('응답을 입력하세요...');
        }
      },
      onComplete: function (data) {
        _running = false;
        _runningMode = null;
        if (_chatPanel) _chatPanel.hideThinking();
        document.getElementById('card-stop-btn').style.display = 'none';
        document.getElementById('card-step-bar').style.display = 'none';
        if (data && data.report_path && _chatPanel) {
          _chatPanel.addReportLink(data.report_path, data.local_path || '');
        }
        // Restore builder mode placeholder after execution
        if (_activeMode === 'builder' && _chatPanel) {
          _chatPanel.setInputPlaceholder('이 팀에게 업무를 지시하세요...');
        }
      },
      onError: function (msg) {
        _running = false;
        _runningMode = null;
        document.getElementById('card-stop-btn').style.display = 'none';
        document.getElementById('card-step-bar').style.display = 'none';
      },
      onHierarchy: function () {
        if (_chatPanel) {
          _chatPanel.hideThinking();
          _chatPanel.setInputPlaceholder('메시지 입력...');
        }
      },
      onSceneChange: function (label) {
        // Update step indicator bar
        var stepBar = document.getElementById('card-step-bar');
        var stepLabel = document.getElementById('card-step-label');
        if (stepBar && stepLabel && label) {
          stepBar.style.display = '';
          stepLabel.textContent = label;
        }
      },
    });

    _selectSidebarMode('instant');
    _chatPanel.toggle(true);
    _connectWS();
    _applyRBAC();
  }

  /**
   * addAgentCard — Add an agent card to the canvas.
   * @param {Object} agent  { id, name, role, toolCategory, emoji, status, progress }
   * @param {number} x
   * @param {number} y
   * @returns {number} nodeId
   */
  function addAgentCard(agent, x, y) {
    var html = CardRenderer.createCard(agent);
    var data = { agentId: agent.id };
    Object.keys(agent).forEach(function (k) { data[k] = agent[k]; });

    var nodeId = _editor.addNode(
      'agent',   // name
      1,         // inputs
      1,         // outputs
      x,
      y,
      'agent-card-node', // class
      data,
      html
    );

    _nodeMap[agent.id] = nodeId;
    return nodeId;
  }

  /**
   * connectAgents — Draw an edge between two agent cards.
   * @param {string} fromAgentId
   * @param {string} toAgentId
   */
  function connectAgents(fromAgentId, toAgentId) {
    var fromNode = _nodeMap[fromAgentId];
    var toNode = _nodeMap[toAgentId];
    if (fromNode == null || toNode == null) return;
    _editor.addConnection(fromNode, toNode, 'output_1', 'input_1');
  }

  /**
   * updateAgentCard — Update card status/progress.
   * @param {string} agentId
   * @param {Object} updates  { status?, progress? }
   */
  function updateAgentCard(agentId, updates) {
    var nodeId = _nodeMap[agentId];
    if (nodeId == null) return;
    var cardEl = CardRenderer.getCardElement(_editor, nodeId);
    CardRenderer.updateCard(cardEl, updates);
  }

  /**
   * layoutTree — Clear canvas, auto-position agents, and connect edges.
   * @param {Object[]} agents  Array of agent objects
   * @param {Object[]} edges   Array of { from, to }
   */
  function layoutTree(agents, edges) {
    _editor.clear();
    _nodeMap = {};

    var positions = CardLayout.computePositions(agents, edges);

    agents.forEach(function (agent) {
      var pos = positions[agent.id];
      if (pos) {
        addAgentCard(agent, pos.x, pos.y);
      }
    });

    edges.forEach(function (e) {
      connectAgents(e.from, e.to);
    });
  }

  /**
   * getEditor — Returns the Drawflow instance.
   * @returns {Object}
   */
  function getEditor() {
    return _editor;
  }

  /**
   * getChatPanel — Returns the CardChatPanel instance.
   * @returns {CardChatPanel}
   */
  function getChatPanel() {
    return _chatPanel;
  }

  /**
   * getActiveMode — Returns the current sidebar mode.
   * @returns {'instant'|'builder'}
   */
  function getActiveMode() {
    return _activeMode;
  }

  /**
   * stop — Stop the current pipeline run.
   */
  function stop() {
    if (_running) {
      _sendWS({ type: 'stop' });
      _running = false;
      _runningMode = null;
    }
  }

  /**
   * isRunning — Check if a pipeline run is active.
   */
  function isRunning() {
    return _running;
  }

  /* ── Mount Dandelion Foresight into card-mode container ── */
  function _mountDandelion(container) {
    container.innerHTML = [
      '<div id="dandelion-app" class="dandelion-shell">',
      '  <header class="dandelion-header">',
      '    <div><h1>Dandelion Foresight</h1></div>',
      '    <div class="dandelion-header-right">',
      '      <span id="dandelion-status" class="dandelion-status">준비</span>',
      '    </div>',
      '  </header>',
      '  <div id="dandelion-input-area" class="dandelion-input-area">',
      '    <input id="dandelion-input" class="dandelion-input" type="text"',
      '           placeholder="미래를 상상할 질문을 입력하세요..."',
      '           onkeydown="if(event.key===\'Enter\')_dlSend()">',
      '    <button id="dandelion-send-btn" class="dandelion-send-btn" onclick="_dlSend()">상상 시작</button>',
      '  </div>',
      '  <div id="dandelion-progress-area" class="dandelion-progress-area" style="display:none">',
      '    <div class="dandelion-steps">',
      '      <div id="dandelion-step-1" class="dandelion-step">',
      '        <div class="dandelion-step-num">1</div>',
      '        <div class="dandelion-step-name">테마 결정</div>',
      '        <div class="dandelion-step-detail"></div>',
      '      </div>',
      '      <div class="dandelion-step-arrow">→</div>',
      '      <div id="dandelion-step-2" class="dandelion-step">',
      '        <div class="dandelion-step-num">2</div>',
      '        <div class="dandelion-step-name">데이터 수집</div>',
      '        <div class="dandelion-step-detail"></div>',
      '      </div>',
      '      <div class="dandelion-step-arrow">→</div>',
      '      <div id="dandelion-step-3" class="dandelion-step">',
      '        <div class="dandelion-step-num">3</div>',
      '        <div class="dandelion-step-name">상상</div>',
      '        <div class="dandelion-step-detail"></div>',
      '      </div>',
      '    </div>',
      '    <button class="dandelion-stop-btn" onclick="_dlStopPipeline()">중지</button>',
      '  </div>',
      '  <div id="dandelion-clarify" class="dandelion-clarify"></div>',
      '  <div class="dandelion-canvas">',
      '    <svg id="dandelion-svg"></svg>',
      '    <div id="dandelion-loading" class="dandelion-loading"></div>',
      '    <div id="dandelion-tooltip" class="dandelion-tooltip">',
      '      <div id="dandelion-tooltip-title" class="dandelion-tooltip-title"></div>',
      '      <div id="dandelion-tooltip-summary" class="dandelion-tooltip-summary"></div>',
      '      <button id="dandelion-tooltip-more" class="dandelion-tooltip-more">더 보기 →</button>',
      '      <div id="dandelion-tooltip-weight" class="dandelion-tooltip-weight"></div>',
      '    </div>',
      '    <div id="dandelion-panel" class="dandelion-panel">',
      '      <button class="dandelion-panel-close" onclick="_dlClosePanel()">✕</button>',
      '      <div id="dandelion-panel-title" class="dandelion-panel-title"></div>',
      '      <div id="dandelion-panel-detail" class="dandelion-panel-detail"></div>',
      '      <div id="dandelion-panel-meta" class="dandelion-panel-meta"></div>',
      '    </div>',
      '  </div>',
      '</div>'
    ].join('\n');

    // Initialize dandelion WebSocket + event listeners
    if (typeof _initDandelion === 'function') {
      _initDandelion();
    } else if (typeof window._initDandelion === 'function') {
      window._initDandelion();
    }
  }

  function _applyRBAC() {
    var vm = window.Auth && Auth.user && Auth.user.visible_modes;
    if (!vm) return; // null = 전체 허용

    document.querySelectorAll('#card-sidebar .cs-item[data-card-mode]').forEach(function(item) {
      var mode = item.dataset.cardMode;
      if (mode !== 'instant' && !vm.includes(mode)) {
        item.style.display = 'none';
      }
    });
  }

  return {
    init: init,
    switchMode: _selectSidebarMode,
    addAgentCard: addAgentCard,
    connectAgents: connectAgents,
    updateAgentCard: updateAgentCard,
    layoutTree: layoutTree,
    getEditor: getEditor,
    getChatPanel: getChatPanel,
    getActiveMode: function () { return _activeMode; },
    stop: stop,
    isRunning: isRunning,
  };
})();
