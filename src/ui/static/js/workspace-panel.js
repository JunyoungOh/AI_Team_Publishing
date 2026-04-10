/* workspace-panel.js — 모드별 input 파일 목록 + 선택 UI
 *
 * Usage:
 *   var panel = WorkspacePanel.create(containerEl, 'instant');
 *   var selected = panel.getSelectedFiles(); // ["data.csv"]
 *   panel.destroy(); // 모드 전환 시 DOM 정리
 */
var WorkspacePanel = (function () {
  'use strict';

  function create(container, mode) {
    var files = [];
    var selected = {};

    var el = document.createElement('div');
    el.className = 'ws-panel';

    // Header
    var header = document.createElement('div');
    header.className = 'ws-panel-header';

    var title = document.createElement('span');
    title.className = 'ws-panel-title';
    title.textContent = 'input 폴더';
    header.appendChild(title);

    var btns = document.createElement('span');

    var refreshBtn = document.createElement('button');
    refreshBtn.className = 'ws-refresh-btn';
    refreshBtn.textContent = '새로고침';
    refreshBtn.onclick = function () { _loadFiles(); };
    btns.appendChild(refreshBtn);

    var openBtn = document.createElement('button');
    openBtn.className = 'ws-open-btn';
    openBtn.textContent = '폴더 열기';
    openBtn.onclick = function () {
      fetch('/api/workspace/' + mode + '/open', { method: 'POST' })
        .catch(function (err) { console.warn('폴더 열기 실패:', err); });
    };
    btns.appendChild(openBtn);

    header.appendChild(btns);
    el.appendChild(header);

    // File list
    var listEl = document.createElement('div');
    listEl.className = 'ws-file-list';
    el.appendChild(listEl);

    container.appendChild(el);

    function _loadFiles() {
      fetch('/api/workspace/' + mode + '/files')
        .then(function (r) { return r.json(); })
        .then(function (data) {
          files = data.files || [];
          _renderFiles();
        })
        .catch(function (err) { console.warn('파일 목록 로드 실패:', err); });
    }

    function _renderFiles() {
      while (listEl.firstChild) listEl.removeChild(listEl.firstChild);

      if (files.length === 0) {
        var empty = document.createElement('span');
        empty.className = 'ws-empty';
        empty.textContent = '파일 없음 — "폴더 열기"로 파일을 넣어주세요';
        listEl.appendChild(empty);
        return;
      }

      files.forEach(function (f) {
        var chip = document.createElement('span');
        chip.className = 'ws-file-chip';
        if (selected[f.name]) chip.classList.add('selected');
        chip.textContent = f.name;
        chip.onclick = function () {
          if (selected[f.name]) {
            delete selected[f.name];
            chip.classList.remove('selected');
          } else {
            selected[f.name] = true;
            chip.classList.add('selected');
          }
        };
        listEl.appendChild(chip);
      });
    }

    // 초기 로드
    _loadFiles();

    return {
      getSelectedFiles: function () {
        return Object.keys(selected);
      },
      getAllFiles: function () {
        return files.map(function (f) { return f.name; });
      },
      refresh: function () { _loadFiles(); },
      clear: function () {
        selected = {};
        _renderFiles();
      },
      destroy: function () {
        if (el.parentNode) el.parentNode.removeChild(el);
      },
      getElement: function () { return el; },
    };
  }

  return { create: create };
})();
