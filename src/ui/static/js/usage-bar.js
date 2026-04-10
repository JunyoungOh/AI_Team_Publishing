/* usage-bar.js — Claude Code 구독 사용량 실시간 표시
 *
 * 탑바 (#card-header .ch-right)에 5시간/7일 사용량 바를 렌더링.
 * 30초마다 GET /api/usage 를 폴링하여 갱신.
 *
 * Usage: UsageBar.mount()  — 앱 로드 시 1회 호출
 */
var UsageBar = (function () {
  'use strict';

  var POLL_INTERVAL = 30000; // 30초
  var _el = null;
  var _timer = null;

  function mount() {
    var target = document.getElementById('ch-usage-area');
    if (!target) return;

    _el = document.createElement('div');
    _el.className = 'usage-bar-wrap';
    target.appendChild(_el);

    _fetch();
    _timer = setInterval(_fetch, POLL_INTERVAL);
  }

  function _fetch() {
    fetch('/api/usage')
      .then(function (r) { return r.json(); })
      .then(function (data) { _render(data); })
      .catch(function () {});
  }

  function _level(pct) {
    if (pct >= 80) return 'danger';
    if (pct >= 50) return 'warn';
    return 'ok';
  }

  function _formatReset(ts, type) {
    if (!ts) return '';
    var d = new Date(ts * 1000);
    var now = new Date();
    if (type === '5h') {
      // 남은 시간 표시
      var diffMs = d - now;
      if (diffMs <= 0) return 'soon';
      var diffMin = Math.floor(diffMs / 60000);
      if (diffMin < 60) return diffMin + 'm';
      var h = Math.floor(diffMin / 60);
      var m = diffMin % 60;
      return h + 'h' + (m > 0 ? m + 'm' : '');
    } else {
      // 날짜 표시
      var mon = d.getMonth() + 1;
      var day = d.getDate();
      return mon + '/' + day;
    }
  }

  function _buildBar(label, pct, resetTs, type) {
    var item = document.createElement('div');
    item.className = 'usage-bar-item';

    var lbl = document.createElement('span');
    lbl.className = 'usage-bar-label';
    lbl.textContent = label;
    item.appendChild(lbl);

    var track = document.createElement('div');
    track.className = 'usage-bar-track';

    var fill = document.createElement('div');
    fill.className = 'usage-bar-fill level-' + _level(pct);
    fill.style.width = Math.min(pct, 100) + '%';
    track.appendChild(fill);
    item.appendChild(track);

    var pctEl = document.createElement('span');
    pctEl.className = 'usage-bar-pct level-' + _level(pct);
    pctEl.textContent = Math.round(pct) + '%';
    item.appendChild(pctEl);

    var reset = _formatReset(resetTs, type);
    if (reset) {
      var resetEl = document.createElement('span');
      resetEl.className = 'usage-bar-reset';
      resetEl.textContent = reset;
      item.appendChild(resetEl);
    }

    return item;
  }

  function _render(data) {
    if (!_el) return;
    while (_el.firstChild) _el.removeChild(_el.firstChild);

    if (!data || !data.available) {
      var na = document.createElement('span');
      na.className = 'usage-bar-unavailable';
      na.textContent = 'usage: --';
      _el.appendChild(na);
      return;
    }

    if (data.five_hour && data.five_hour.used_percentage != null) {
      _el.appendChild(_buildBar(
        '5h', data.five_hour.used_percentage,
        data.five_hour.resets_at, '5h'
      ));
    }

    if (data.seven_day && data.seven_day.used_percentage != null) {
      _el.appendChild(_buildBar(
        '7d', data.seven_day.used_percentage,
        data.seven_day.resets_at, '7d'
      ));
    }
  }

  return { mount: mount };
})();
