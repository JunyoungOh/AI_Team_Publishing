'use strict';

// ═══ Dandelion Foresight Mode ═══════════════════════

let _dlWs = null;
let _dlThemes = [];
let _dlSeeds = [];
let _dlFocusedSeed = null;
// True when themes/seeds arrived while the tab was hidden (dims=0).
// _dlRefresh() drains this on mode re-entry.
let _dlPendingRender = false;

/* Sidebar "running" indicator signal — see mode-chatbot.js for receiver. */
function _dlSignalRunning(on) {
  try {
    if (window.chatbotSignal) window.chatbotSignal('foresight', on);
  } catch (_) { /* noop */ }
}

const _DL_BASE_R = 16;
const _DL_SCALE = 6;
const _DL_WEIGHT_CAP = 5;
const _DL_STEM_PADDING = { top: 80, bottom: 80 };

// ── Init ──────────────────────────────────────────

function _initDandelion() {
  if (_dlWs && _dlWs.readyState <= 1) return;

  var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  _dlWs = new WebSocket(proto + '//' + location.host + '/ws/dandelion');

  _dlWs.onopen = function() {
    _dlSetStatus('ready');
    _dlClearCanvas();
  };

  _dlWs.onmessage = function(evt) {
    var msg = JSON.parse(evt.data);
    if (msg.type === 'clarify') _dlShowClarify(msg.questions);
    else if (msg.type === 'progress') _dlUpdateProgress(msg.step, msg.label, msg.current, msg.total);
    else if (msg.type === 'themes') _dlRenderStems(msg.themes);
    else if (msg.type === 'seed') _dlRenderSingleSeed(msg.theme_id, msg.seed);
    else if (msg.type === 'seeds') _dlRenderSeeds(msg.theme_id, msg.seeds);
    else if (msg.type === 'complete') _dlOnComplete();
    else if (msg.type === 'theme_error') _dlOnThemeError(msg.theme_id, msg.message);
    else if (msg.type === 'error') _dlOnError(msg.message);
    else if (msg.type === 'session_log') _dlShowSessionLog(msg.label);
    else if (msg.type === 'export_ready') _dlDownloadReport(msg.url);
    else if (msg.type === 'export_error') _dlOnError(msg.message);
  };

  _dlWs.onclose = function() { _dlSetStatus('disconnected'); _dlSignalRunning(false); };
}

// ── Send query ────────────────────────────────────

function _dlSend() {
  var input = document.getElementById('dandelion-input');
  var query = input.value.trim();
  if (!query || !_dlWs || _dlWs.readyState !== 1) return;

  _dlClearCanvas();
  _dlHideClarify();
  _dlThemes = [];
  _dlSeeds = [];
  _dlFocusedSeed = null;
  _dlPendingRender = false;
  _dlSetStatus('running');
  _dlShowProgressBar();

  _dlWs.send(JSON.stringify({ type: 'start', query: query, files: [] }));
  _dlSignalRunning(true);
  input.value = '';
}

// ── Progress bar (replaces input area) ────────────

var _dlStepNames = ['', '테마 결정', '데이터 수집', '상상'];

function _dlShowProgressBar() {
  var inputArea = document.getElementById('dandelion-input-area');
  var progressArea = document.getElementById('dandelion-progress-area');
  if (inputArea) inputArea.style.display = 'none';
  if (progressArea) progressArea.style.display = 'flex';

  // Reset all steps
  for (var i = 1; i <= 3; i++) {
    var step = document.getElementById('dandelion-step-' + i);
    if (step) {
      step.className = 'dandelion-step';
      var detail = step.querySelector('.dandelion-step-detail');
      if (detail) detail.textContent = '';
    }
  }
}

function _dlHideProgressBar() {
  var inputArea = document.getElementById('dandelion-input-area');
  var progressArea = document.getElementById('dandelion-progress-area');
  if (inputArea) inputArea.style.display = 'flex';
  if (progressArea) progressArea.style.display = 'none';
  document.getElementById('dandelion-send-btn').disabled = false;
}

function _dlUpdateProgress(step, label, current, total) {
  // Mark previous steps as done
  for (var i = 1; i < step; i++) {
    var prev = document.getElementById('dandelion-step-' + i);
    if (prev) {
      prev.className = 'dandelion-step done';
      var prevDetail = prev.querySelector('.dandelion-step-detail');
      if (prevDetail) prevDetail.textContent = '✓';
    }
  }

  // Mark current step as active
  var cur = document.getElementById('dandelion-step-' + step);
  if (cur) {
    cur.className = 'dandelion-step active';
    // Remove pulsing from all details first
    document.querySelectorAll('.dandelion-step-detail').forEach(function(d) { d.classList.remove('pulsing'); });
    var curDetail = cur.querySelector('.dandelion-step-detail');
    if (curDetail) {
      if (total > 0) {
        curDetail.textContent = current + '/' + total;
        curDetail.classList.remove('pulsing');
      } else {
        curDetail.textContent = '진행 중...';
        curDetail.classList.add('pulsing');
      }
    }
  }
}

function _dlStopPipeline() {
  if (_dlWs && _dlWs.readyState === 1) {
    _dlWs.close();
    _dlWs = null;
  }
  _dlHideProgressBar();
  _dlHideLoading();
  _dlSetStatus('disconnected');
  // Reconnect after a brief pause
  setTimeout(function() { _initDandelion(); }, 500);
}

// ── Status ────────────────────────────────────────

function _dlSetStatus(status) {
  var el = document.getElementById('dandelion-status');
  if (!el) return;
  el.className = 'dandelion-status dandelion-status-' + status;
  var labels = { ready: '준비', running: '실행 중...', disconnected: '연결 끊김' };
  el.textContent = labels[status] || status;
}

function _dlShowLoading(text) {
  var el = document.getElementById('dandelion-loading');
  if (el) { el.textContent = text; el.classList.add('show'); }
}

function _dlHideLoading() {
  var el = document.getElementById('dandelion-loading');
  if (el) el.classList.remove('show');
}

function _dlShowSessionLog(label) {
  var el = document.getElementById('dandelion-loading');
  if (el) { el.textContent = label; el.classList.add('show'); }
}

// ── Clarification ─────────────────────────────────

function _dlShowClarify(questions) {
  _dlHideLoading();
  _dlSetStatus('ready');

  var container = document.getElementById('dandelion-clarify');
  if (!container) return;

  var html = '<div class="dandelion-clarify-header">더 구체적인 상상을 위해 몇 가지 질문이 있습니다</div>';
  html += '<div class="dandelion-clarify-sub">답변하고 싶은 질문만 골라서 답변해주세요.</div>';
  html += '<div class="dandelion-clarify-questions">';

  for (var i = 0; i < questions.length; i++) {
    html += '<div class="dandelion-clarify-q" data-idx="' + i + '">';
    html += '<label class="dandelion-clarify-label">' + questions[i] + '</label>';
    html += '<textarea class="dandelion-clarify-input" data-idx="' + i + '" rows="2" placeholder="답변 입력 (선택사항)"></textarea>';
    html += '</div>';
  }

  html += '</div>';
  html += '<div class="dandelion-clarify-actions">';
  html += '<button class="dandelion-clarify-submit" onclick="_dlSubmitClarify()">상상 시작</button>';
  html += '<button class="dandelion-clarify-skip" onclick="_dlSkipClarify()">건너뛰고 바로 시작</button>';
  html += '</div>';

  container.innerHTML = html;
  container.classList.add('show');
}

function _dlHideClarify() {
  var container = document.getElementById('dandelion-clarify');
  if (container) {
    container.classList.remove('show');
    container.innerHTML = '';
  }
}

function _dlSubmitClarify() {
  var answers = {};
  var inputs = document.querySelectorAll('.dandelion-clarify-input');
  inputs.forEach(function(input) {
    var val = input.value.trim();
    if (val) answers[input.getAttribute('data-idx')] = val;
  });

  _dlHideClarify();
  _dlSetStatus('running');
  _dlShowProgressBar();

  _dlWs.send(JSON.stringify({ type: 'clarify_response', answers: answers }));
}

function _dlSkipClarify() {
  _dlHideClarify();
  _dlSetStatus('running');
  _dlShowProgressBar();

  _dlWs.send(JSON.stringify({ type: 'skip_clarify' }));
}

// ── Clear ─────────────────────────────────────────

function _dlClearCanvas() {
  var svg = document.getElementById('dandelion-svg');
  if (svg) while (svg.firstChild) svg.removeChild(svg.firstChild);
  _dlHideTooltip();
  _dlClosePanel();
}

// ── Render stems ──────────────────────────────────

function _dlRenderStems(themes) {
  _dlThemes = themes;
  _dlShowLoading('데이터를 수집하고 있습니다...');

  var svg = document.getElementById('dandelion-svg');
  if (!svg) return;
  var w = svg.clientWidth || svg.parentElement.clientWidth;
  var h = svg.clientHeight || svg.parentElement.clientHeight;

  // Container hidden (tab switched mid-load) → dims=0 collapses all stems to (0,-80).
  // Buffer and let _dlRefresh() replay when the tab becomes visible again.
  if (!w || !h) {
    _dlPendingRender = true;
    return;
  }

  var originX = w / 2;
  var originY = h - _DL_STEM_PADDING.bottom;

  var origin = _svgEl('circle', {
    cx: originX, cy: originY, r: 6,
    class: 'dandelion-origin',
  });
  svg.appendChild(origin);

  var originLabel = _svgEl('text', {
    x: originX, y: originY + 20,
    class: 'dandelion-origin-label',
  });
  originLabel.textContent = '현재';
  svg.appendChild(originLabel);

  var stemSpacing = w / 5;
  for (var i = 0; i < 4; i++) {
    var topX = stemSpacing * (i + 1);
    var topY = _DL_STEM_PADDING.top;

    var cp1x = originX + (topX - originX) * 0.3;
    var cp1y = originY - (originY - topY) * 0.5;
    var cp2x = topX - (topX - originX) * 0.1;
    var cp2y = topY + (originY - topY) * 0.2;

    var d = 'M ' + originX + ' ' + originY +
            ' C ' + cp1x + ' ' + cp1y + ', ' + cp2x + ' ' + cp2y + ', ' + topX + ' ' + topY;

    var path = _svgEl('path', {
      d: d,
      class: 'dandelion-stem dandelion-stem-animate',
      stroke: themes[i].color,
      'stroke-opacity': '0.5',
      'data-theme-id': themes[i].id,
    });

    svg.appendChild(path);
    var len = path.getTotalLength();
    path.style.strokeDasharray = len;
    path.style.strokeDashoffset = len;
    path.style.setProperty('--stem-length', len);

    var label = _svgEl('text', {
      x: topX, y: topY - 12,
      fill: themes[i].color,
      'text-anchor': 'middle',
      class: 'dandelion-theme-label',
    });
    label.textContent = themes[i].name;
    svg.appendChild(label);

    themes[i]._stemPath = path;
    themes[i]._topX = topX;
    themes[i]._topY = topY;
    themes[i]._originX = originX;
    themes[i]._originY = originY;
  }
}

// ── Render single seed (real-time streaming) ──────

function _dlRenderSingleSeed(themeId, seed) {
  var theme = _dlThemes.find(function(t) { return t.id === themeId; });
  if (!theme) return;

  // Defer when stems weren't drawn yet or tab is hidden — keep the seed so _dlRefresh() can replay it.
  if (!theme._stemPath || !_dlHasDims()) {
    _dlSeeds.push(seed);
    _dlPendingRender = true;
    return;
  }

  var svg = document.getElementById('dandelion-svg');
  if (!svg) return;

  // Count existing seeds for this theme to determine position
  var themeSeedCount = _dlSeeds.filter(function(s) { return s.theme_id === themeId; }).length;

  _dlRenderOneSeed(svg, theme, seed, themeSeedCount);
  _dlSeeds.push(seed);
}

function _dlRenderOneSeed(svg, theme, seed, posIndex) {
  var stemPath = theme._stemPath;
  var pathLen = stemPath.getTotalLength();

  // Distribute along stem: spread across 0.15~0.85 range, max 10 per theme
  var spreadT = 0.15 + (posIndex / 9) * 0.7;
  var point = stemPath.getPointAtLength(pathLen * spreadT);

  // Wider horizontal offset, alternating sides
  var offset = (posIndex % 2 === 0 ? 1 : -1) * (25 + Math.random() * 20);

  var r = _DL_BASE_R + (Math.min(seed.weight, _DL_WEIGHT_CAP) - 1) * _DL_SCALE;

  // Ensure defs exist
  var defs = svg.querySelector('defs') || svg.insertBefore(_svgEl('defs', {}), svg.firstChild);
  if (!svg.querySelector('#dandelion-blur')) {
    var blur = _svgEl('filter', { id: 'dandelion-blur', x: '-50%', y: '-50%', width: '200%', height: '200%' });
    blur.appendChild(_svgEl('feGaussianBlur', { in: 'SourceGraphic', stdDeviation: '3' }));
    defs.appendChild(blur);
  }

  // Gradient
  var gradId = 'grad-' + seed.id;
  var grad = _svgEl('radialGradient', { id: gradId, cx: '45%', cy: '45%', r: '55%' });
  grad.appendChild(_svgEl('stop', { offset: '0%', 'stop-color': theme.color, 'stop-opacity': '0.95' }));
  grad.appendChild(_svgEl('stop', { offset: '50%', 'stop-color': theme.color, 'stop-opacity': '0.5' }));
  grad.appendChild(_svgEl('stop', { offset: '100%', 'stop-color': theme.color, 'stop-opacity': '0.08' }));
  defs.appendChild(grad);

  var cx = point.x + offset;
  var cy = point.y;

  // Glow
  var glow = _svgEl('circle', {
    cx: cx, cy: cy, r: r * 1.6,
    fill: 'url(#' + gradId + ')',
    filter: 'url(#dandelion-blur)',
    class: 'dandelion-seed-glow dandelion-seed-pop',
    style: 'transform: scale(0); opacity: 0; pointer-events: none;',
  });
  svg.appendChild(glow);

  // Core seed
  var circle = _svgEl('circle', {
    cx: cx, cy: cy, r: r,
    fill: 'url(#' + gradId + ')',
    class: 'dandelion-seed dandelion-seed-pop',
    'data-seed-id': seed.id,
    style: 'transform: scale(0); opacity: 0;',
  });

  circle.addEventListener('click', function(e) {
    e.stopPropagation();
    _dlOnSeedClick(seed, circle, theme);
  });

  svg.appendChild(circle);

  seed._el = circle;
  seed._glow = glow;
  seed._theme = theme;
}

// ── Render seeds (batch, for resize redraw) ───────

function _dlRenderSeeds(themeId, seeds) {
  var theme = _dlThemes.find(function(t) { return t.id === themeId; });
  if (!theme) return;

  _dlShowLoading('상상을 정리하고 있습니다... (' + (_dlSeeds.length > 0 ? Math.round(_dlSeeds.length / 28 * 100) : 0) + '%)');

  var svg = document.getElementById('dandelion-svg');
  if (!svg) return;

  seeds.sort(function(a, b) { return a.time_months - b.time_months; });

  var stemPath = theme._stemPath;
  if (!stemPath) return;

  // Ensure defs + blur filter exist
  var defs = svg.querySelector('defs') || svg.insertBefore(_svgEl('defs', {}), svg.firstChild);
  if (!svg.querySelector('#dandelion-blur')) {
    var blur = _svgEl('filter', { id: 'dandelion-blur', x: '-50%', y: '-50%', width: '200%', height: '200%' });
    var feBlur = _svgEl('feGaussianBlur', { in: 'SourceGraphic', stdDeviation: '3' });
    blur.appendChild(feBlur);
    defs.appendChild(blur);
  }

  // Spread seeds evenly along stem (not clustered by time_months)
  var seedCount = seeds.length;
  seeds.forEach(function(seed, idx) {
    // Distribute evenly along stem length (0.15 ~ 0.85) for visual spread
    var spreadT = 0.15 + (idx / Math.max(seedCount - 1, 1)) * 0.7;
    var pathLen = stemPath.getTotalLength();
    var point = stemPath.getPointAtLength(pathLen * spreadT);

    // Wider horizontal offset, alternating sides with randomness
    var offset = (idx % 2 === 0 ? 1 : -1) * (25 + Math.random() * 20);

    var r = _DL_BASE_R + (Math.min(seed.weight, _DL_WEIGHT_CAP) - 1) * _DL_SCALE;

    // Gradient for fluffy dandelion seed look
    var gradId = 'grad-' + seed.id;
    var grad = _svgEl('radialGradient', { id: gradId, cx: '45%', cy: '45%', r: '55%' });
    var stop1 = _svgEl('stop', { offset: '0%', 'stop-color': theme.color, 'stop-opacity': '0.95' });
    var stop2 = _svgEl('stop', { offset: '50%', 'stop-color': theme.color, 'stop-opacity': '0.5' });
    var stop3 = _svgEl('stop', { offset: '100%', 'stop-color': theme.color, 'stop-opacity': '0.08' });
    grad.appendChild(stop1);
    grad.appendChild(stop2);
    grad.appendChild(stop3);
    defs.appendChild(grad);

    var cx = point.x + offset;
    var cy = point.y;

    // Outer glow layer (soft, bigger, blurred)
    var glow = _svgEl('circle', {
      cx: cx, cy: cy,
      r: r * 1.6,
      fill: 'url(#' + gradId + ')',
      filter: 'url(#dandelion-blur)',
      class: 'dandelion-seed-glow dandelion-seed-pop',
      style: 'animation-delay: ' + (idx * 0.15) + 's; transform: scale(0); opacity: 0; pointer-events: none;',
    });
    svg.appendChild(glow);

    // Core seed (clickable)
    var circle = _svgEl('circle', {
      cx: cx, cy: cy,
      r: r,
      fill: 'url(#' + gradId + ')',
      class: 'dandelion-seed dandelion-seed-pop',
      'data-seed-id': seed.id,
      style: 'animation-delay: ' + (idx * 0.15) + 's; transform: scale(0); opacity: 0;',
    });

    circle.addEventListener('click', function(e) {
      e.stopPropagation();
      _dlOnSeedClick(seed, circle, theme);
    });

    svg.appendChild(circle);

    seed._el = circle;
    seed._glow = glow;
    seed._theme = theme;
    _dlSeeds.push(seed);
  });
}

// ── Seed click (hybrid C→B) ──────────────────────

function _dlOnSeedClick(seed, el, theme) {
  if (_dlFocusedSeed) {
    document.querySelectorAll('.dandelion-seed').forEach(function(s) {
      s.classList.remove('dimmed', 'focused');
    });
  }

  if (_dlFocusedSeed === seed) {
    _dlFocusedSeed = null;
    _dlHideTooltip();
    return;
  }

  _dlFocusedSeed = seed;

  document.querySelectorAll('.dandelion-seed').forEach(function(s) {
    if (s.getAttribute('data-seed-id') !== seed.id) s.classList.add('dimmed');
  });
  el.classList.add('focused');

  var origR = parseFloat(el.getAttribute('r'));
  el.setAttribute('r', origR * 1.5);

  _dlShowTooltip(seed, el, theme);
}

// ── Tooltip ───────────────────────────────────────

function _dlShowTooltip(seed, el, theme) {
  var tooltip = document.getElementById('dandelion-tooltip');
  if (!tooltip) return;

  document.getElementById('dandelion-tooltip-title').textContent = seed.title;
  document.getElementById('dandelion-tooltip-title').style.color = theme.color;
  document.getElementById('dandelion-tooltip-summary').textContent = seed.summary;

  var weightEl = document.getElementById('dandelion-tooltip-weight');
  if (seed.source_count > 1) {
    weightEl.textContent = 'AI ' + seed.source_count + '명이 유사하게 상상';
    weightEl.style.display = 'block';
  } else {
    weightEl.style.display = 'none';
  }

  var svg = document.getElementById('dandelion-svg');
  var rect = svg.getBoundingClientRect();
  var cx = parseFloat(el.getAttribute('cx'));
  var cy = parseFloat(el.getAttribute('cy'));

  // Position tooltip, then clamp to viewport
  var left = rect.left + cx + 20;
  var top = rect.top + cy - 40;

  tooltip.style.left = '0px';
  tooltip.style.top = '0px';
  tooltip.classList.add('show');

  var tw = tooltip.offsetWidth;
  var th = tooltip.offsetHeight;
  var vw = window.innerWidth;
  var vh = window.innerHeight;

  // Clamp: don't go off right/bottom/top edges
  if (left + tw > vw - 10) left = rect.left + cx - tw - 20;
  if (top + th > vh - 10) top = vh - th - 10;
  if (top < 10) top = 10;
  if (left < 10) left = 10;

  tooltip.style.left = left + 'px';
  tooltip.style.top = top + 'px';

  document.getElementById('dandelion-tooltip-more').onclick = function() {
    _dlOpenPanel(seed, theme);
  };
}

function _dlHideTooltip() {
  var tooltip = document.getElementById('dandelion-tooltip');
  if (tooltip) tooltip.classList.remove('show');

  document.querySelectorAll('.dandelion-seed').forEach(function(s) {
    var seedId = s.getAttribute('data-seed-id');
    var seed = _dlSeeds.find(function(sd) { return sd.id === seedId; });
    if (seed) {
      var r = _DL_BASE_R + (Math.min(seed.weight, _DL_WEIGHT_CAP) - 1) * _DL_SCALE;
      s.setAttribute('r', r);
    }
  });
}

// ── Side panel ────────────────────────────────────

function _dlOpenPanel(seed, theme) {
  var panel = document.getElementById('dandelion-panel');
  if (!panel) return;

  document.getElementById('dandelion-panel-title').textContent = seed.title;
  document.getElementById('dandelion-panel-title').style.color = theme.color;

  var detailEl = document.getElementById('dandelion-panel-detail');
  if (typeof marked !== 'undefined') {
    detailEl.innerHTML = marked.parse(seed.detail);
  } else {
    detailEl.textContent = seed.detail;
  }

  var meta = '시점: ' + seed.time_months + '개월 후';
  if (seed.source_count > 1) meta += ' · AI ' + seed.source_count + '명 동의';
  document.getElementById('dandelion-panel-meta').textContent = meta;

  panel.classList.add('open');
}

function _dlClosePanel() {
  var panel = document.getElementById('dandelion-panel');
  if (panel) panel.classList.remove('open');
}

// ── Complete / Error ──────────────────────────────

function _dlOnComplete() {
  _dlSetStatus('ready');
  _dlHideLoading();
  _dlHideProgressBar();
  _dlShowExportBtn();
  _dlSignalRunning(false);
}

function _dlShowExportBtn() {
  var existing = document.getElementById('dandelion-export-btn');
  if (existing) existing.remove();

  var btn = document.createElement('button');
  btn.id = 'dandelion-export-btn';
  btn.className = 'dandelion-export-btn';
  btn.textContent = '리포트 내보내기';
  btn.onclick = function() {
    btn.disabled = true;
    btn.textContent = '생성 중...';
    if (_dlWs && _dlWs.readyState === 1) {
      _dlWs.send(JSON.stringify({ type: 'export_report' }));
    }
  };

  var header = document.querySelector('.dandelion-header-right');
  if (header) header.appendChild(btn);
}

function _dlDownloadReport(url) {
  var btn = document.getElementById('dandelion-export-btn');
  if (btn) { btn.disabled = false; btn.textContent = '리포트 내보내기'; }

  var a = document.createElement('a');
  a.href = url;
  a.download = '';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

function _dlOnThemeError(themeId, message) {
  console.warn('[Dandelion] theme error:', themeId, message);
  var theme = _dlThemes.find(function(t) { return t.id === themeId; });
  if (!theme) return;

  var svg = document.getElementById('dandelion-svg');
  if (svg && theme._topX) {
    var label = _svgEl('text', {
      x: theme._topX, y: theme._topY + 20,
      fill: '#dc3545', 'text-anchor': 'middle',
      'font-size': '10px',
    });
    label.textContent = '상상 실패';
    svg.appendChild(label);
  }
}

function _dlOnError(message) {
  _dlSetStatus('ready');
  _dlHideLoading();
  _dlHideProgressBar();
  _dlSignalRunning(false);
  alert('오류: ' + message);
}

// ── SVG helper ────────────────────────────────────

function _svgEl(tag, attrs) {
  var el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (var k in attrs) {
    if (attrs.hasOwnProperty(k)) el.setAttribute(k, attrs[k]);
  }
  return el;
}

// ── Refresh / redraw ──────────────────────────────

function _dlHasDims() {
  var svg = document.getElementById('dandelion-svg');
  if (!svg) return false;
  var w = svg.clientWidth || (svg.parentElement && svg.parentElement.clientWidth) || 0;
  var h = svg.clientHeight || (svg.parentElement && svg.parentElement.clientHeight) || 0;
  return w > 0 && h > 0;
}

function _dlRedrawAll() {
  if (!_dlThemes || _dlThemes.length === 0) return;
  if (!_dlHasDims()) return;

  var savedThemes = _dlThemes.slice();
  var savedSeedsByTheme = {};
  _dlSeeds.forEach(function(s) {
    if (!savedSeedsByTheme[s.theme_id]) savedSeedsByTheme[s.theme_id] = [];
    savedSeedsByTheme[s.theme_id].push(s);
  });

  _dlClearCanvas();
  _dlSeeds = [];
  _dlRenderStems(savedThemes);

  Object.keys(savedSeedsByTheme).forEach(function(themeId) {
    var seeds = savedSeedsByTheme[themeId].map(function(s) {
      return { id: s.id, title: s.title, summary: s.summary, detail: s.detail,
               reasoning: s.reasoning, time_months: s.time_months,
               weight: s.weight, source_count: s.source_count, theme_id: s.theme_id };
    });
    _dlRenderSeeds(themeId, seeds);
  });
}

function _dlRefresh() {
  if (!_dlPendingRender) return;
  if (!_dlThemes || _dlThemes.length === 0) return;

  if (_dlHasDims()) {
    _dlRedrawAll();
    _dlPendingRender = false;
    return;
  }
  // Layout not settled yet; retry once next frame.
  requestAnimationFrame(function() {
    if (_dlPendingRender && _dlHasDims()) {
      _dlRedrawAll();
      _dlPendingRender = false;
    }
  });
}

// ── Click outside to unfocus ──────────────────────

document.addEventListener('click', function(e) {
  if (!e.target.closest('.dandelion-seed') && !e.target.closest('.dandelion-tooltip') && !e.target.closest('.dandelion-panel')) {
    _dlFocusedSeed = null;
    _dlHideTooltip();
    document.querySelectorAll('.dandelion-seed').forEach(function(s) {
      s.classList.remove('dimmed', 'focused');
    });
  }
});

// ── Responsive resize ─────────────────────────────

var _dlResizeTimer = null;
window.addEventListener('resize', function() {
  if (_dlResizeTimer) clearTimeout(_dlResizeTimer);
  _dlResizeTimer = setTimeout(function() {
    if (typeof CardView !== 'undefined' && CardView.getActiveMode() === 'foresight') {
      _dlRedrawAll();
    }
  }, 300);
});
