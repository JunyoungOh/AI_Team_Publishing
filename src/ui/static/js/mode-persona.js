'use strict';

/* Sidebar "running" indicator signal — see mode-chatbot.js for receiver. */
function _personaSignalRunning(on) {
  try {
    if (window.chatbotSignal) window.chatbotSignal('persona', on);
  } catch (_) { /* noop */ }
}

class PersonaManager {
  constructor() {
    this._view = 'gallery'; // 'gallery' | 'create' | 'interview' | 'preview'
    this._ws = null;
    this._previewToken = null;
    this._uploadedFileIds = [];
    this._container = null; // set by mountInShell
  }

  /* ═══════════════════════════════════════════════════
     Shell integration — called by CardView._bootMode()
     ═══════════════════════════════════════════════════ */

  static mountInShell(container) {
    if (!PersonaManager._instance) {
      PersonaManager._instance = new PersonaManager();
    }
    PersonaManager._instance._mount(container);
  }

  _mount(container) {
    this._container = container;
    // Clear container safely
    while (container.firstChild) container.removeChild(container.firstChild);

    // Build shell structure
    const shell = document.createElement('div');
    shell.className = 'persona-shell';

    // View containers
    const gallery = document.createElement('div');
    gallery.id = 'persona-gallery';
    shell.appendChild(gallery);

    const create = document.createElement('div');
    create.id = 'persona-create';
    create.style.display = 'none';
    shell.appendChild(create);

    const interview = document.createElement('div');
    interview.id = 'persona-interview-view';
    interview.style.display = 'none';
    shell.appendChild(interview);

    const preview = document.createElement('div');
    preview.id = 'persona-preview';
    preview.style.display = 'none';
    shell.appendChild(preview);

    container.appendChild(shell);
    this.loadGallery();
  }

  _showView(view) {
    this._view = view;
    const viewMap = {
      'gallery': 'persona-gallery',
      'create': 'persona-create',
      'interview': 'persona-interview-view',
      'preview': 'persona-preview',
    };
    const activeId = viewMap[view];
    Object.values(viewMap).forEach(id => {
      const el = document.getElementById(id);
      if (el) el.style.display = (id === activeId) ? '' : 'none';
    });
  }

  /* ═══════════════════════════════════════════════════
     Gallery
     ═══════════════════════════════════════════════════ */

  async loadGallery() {
    this._showView('gallery');
    // Clear other views to prevent stale content
    const createEl = document.getElementById('persona-create');
    const previewEl = document.getElementById('persona-preview');
    const interviewEl = document.getElementById('persona-interview-view');
    if (createEl) createEl.textContent = '';
    if (previewEl) previewEl.textContent = '';
    if (interviewEl) interviewEl.textContent = '';

    const container = document.getElementById('persona-gallery');
    if (!container) return;
    container.textContent = '';

    // Toolbar
    const toolbar = document.createElement('div');
    toolbar.className = 'persona-toolbar';
    container.appendChild(toolbar);

    const grid = document.createElement('div');
    grid.className = 'persona-gallery-grid';

    // Add "+" card
    const addCard = document.createElement('div');
    addCard.className = 'agent-card persona-add-card';
    addCard.textContent = '+ \uc0c8 \ud398\ub974\uc18c\ub098';
    addCard.addEventListener('click', () => this.showCreate());
    grid.appendChild(addCard);

    // Load personas from API
    try {
      const resp = await fetch('/api/personas');
      if (resp.ok) {
        const data = await resp.json();
        (data.personas || []).forEach(p => {
          grid.appendChild(this._createPersonaCard(p));
        });
      }
    } catch (e) {
      console.warn('Failed to load personas:', e);
    }

    container.appendChild(grid);

    // === Shared section ===
    const sharedSection = document.createElement('div');
    sharedSection.style.cssText = 'margin-top:24px;';

    const sharedTitle = document.createElement('div');
    sharedTitle.style.cssText = 'font-size:13px;font-weight:600;color:var(--cv-dim);margin-bottom:12px;';
    sharedTitle.textContent = '\uacf5\uc720 \ud398\ub974\uc18c\ub098';
    sharedSection.appendChild(sharedTitle);

    const sharedGrid = document.createElement('div');
    sharedGrid.className = 'persona-gallery-grid';

    try {
      const sharedResp = await fetch('/api/personas/shared');
      if (sharedResp.ok) {
        const sharedData = await sharedResp.json();
        (sharedData.personas || []).forEach(p => {
          sharedGrid.appendChild(this._createSharedCard(p));
        });
      }
    } catch (e) { console.warn('Failed to load shared personas:', e); }

    if (sharedGrid.children.length === 0) {
      const emptyMsg = document.createElement('div');
      emptyMsg.style.cssText = 'font-size:11px;color:var(--cv-dim);padding:8px 0;';
      emptyMsg.textContent = '\uc544\uc9c1 \uacf5\uc720\ub41c \ud398\ub974\uc18c\ub098\uac00 \uc5c6\uc2b5\ub2c8\ub2e4.';
      sharedSection.appendChild(emptyMsg);
    }

    sharedSection.appendChild(sharedGrid);
    container.appendChild(sharedSection);
  }

  _createPersonaCard(p) {
    const card = document.createElement('div');
    card.className = 'agent-card persona-card';
    card.dataset.id = p.id;

    // Header: name + badge
    const header = document.createElement('div');
    header.className = 'ac-header';
    const name = document.createElement('span');
    name.className = 'ac-name';
    name.textContent = p.name;
    header.appendChild(name);

    const badgeLabels = { web: 'web', interview: 'interview', mixed: 'mixed' };
    const badge = document.createElement('span');
    badge.className = 'ac-badge' + (p.source === 'interview' ? ' ac-badge-interview' : '');
    badge.textContent = badgeLabels[p.source] || p.source || 'web';
    header.appendChild(badge);
    card.appendChild(header);

    // Role / summary
    const role = document.createElement('div');
    role.className = 'ac-role';
    role.textContent = p.summary || '';
    card.appendChild(role);

    // Skills
    const tools = document.createElement('div');
    tools.className = 'ac-tools';
    const skills = this._extractSkills(p);
    skills.forEach(s => {
      const tag = document.createElement('span');
      tag.className = 'ac-tool';
      tag.textContent = s;
      tools.appendChild(tag);
    });
    card.appendChild(tools);

    // Status row with share toggle
    const statusRow = document.createElement('div');
    statusRow.className = 'ac-status';
    const dot = document.createElement('div');
    dot.className = 'ac-dot';
    dot.style.background = p.shared ? 'var(--cv-green)' : 'var(--cv-muted)';
    statusRow.appendChild(dot);
    const statusLabel = document.createTextNode(p.shared ? '\uacf5\uc720\ub428' : '\ube44\uacf5\uac1c');
    statusRow.appendChild(statusLabel);

    // Share toggle
    const shareToggle = document.createElement('label');
    shareToggle.style.cssText = 'margin-left:auto;display:flex;align-items:center;gap:4px;font-size:10px;color:var(--cv-dim);cursor:pointer;';
    shareToggle.addEventListener('click', (e) => e.stopPropagation());
    const shareCheck = document.createElement('input');
    shareCheck.type = 'checkbox';
    shareCheck.checked = !!p.shared;
    shareCheck.addEventListener('change', async (e) => {
      e.stopPropagation();
      await fetch('/api/personas/' + p.id + '/share', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ shared: e.target.checked }),
      });
      dot.style.background = e.target.checked ? 'var(--cv-green)' : 'var(--cv-muted)';
      statusRow.childNodes[1].textContent = e.target.checked ? '\uacf5\uc720\ub428' : '\ube44\uacf5\uac1c';
    });
    shareToggle.appendChild(shareCheck);
    shareToggle.appendChild(document.createTextNode('\uacf5\uc720'));
    statusRow.appendChild(shareToggle);
    card.appendChild(statusRow);

    // Click -> preview
    card.addEventListener('click', () => this._loadAndShowPreview(p.id));

    return card;
  }

  _extractSkills(p) {
    const skills = [];
    if (p.source) skills.push(p.source);
    if (p.summary) {
      const words = p.summary.split(/[\s,]+/).filter(w => w.length > 1 && w.length < 10);
      skills.push(...words.slice(0, 3));
    }
    return skills.slice(0, 4);
  }

  async _loadAndShowPreview(personaId) {
    try {
      const resp = await fetch('/api/personas/' + personaId);
      if (!resp.ok) return;
      const data = await resp.json();
      this.showPreview(data.persona || data);
    } catch (e) {
      console.warn('Failed to load persona:', e);
    }
  }

  _createSharedCard(p) {
    const card = document.createElement('div');
    card.className = 'agent-card persona-card';
    card.dataset.id = p.id;

    // Header
    const header = document.createElement('div');
    header.className = 'ac-header';
    const name = document.createElement('span');
    name.className = 'ac-name';
    name.textContent = p.name;
    header.appendChild(name);

    const badgeLabels = { web: 'web', interview: 'interview', mixed: 'mixed' };
    const badge = document.createElement('span');
    badge.className = 'ac-badge' + (p.source === 'interview' ? ' ac-badge-interview' : '');
    badge.textContent = badgeLabels[p.source] || p.source || 'web';
    header.appendChild(badge);
    card.appendChild(header);

    // Role / summary
    const role = document.createElement('div');
    role.className = 'ac-role';
    role.textContent = p.summary || '';
    card.appendChild(role);

    // Owner info as tool tag
    const tools = document.createElement('div');
    tools.className = 'ac-tools';
    const ownerTag = document.createElement('span');
    ownerTag.className = 'ac-tool';
    ownerTag.textContent = 'by ' + (p.owner_name || p.user_id || '').slice(0, 20);
    tools.appendChild(ownerTag);
    card.appendChild(tools);

    // Status
    const statusRow = document.createElement('div');
    statusRow.className = 'ac-status';
    const dot = document.createElement('div');
    dot.className = 'ac-dot';
    dot.style.background = 'var(--cv-green)';
    statusRow.appendChild(dot);
    statusRow.appendChild(document.createTextNode('\uacf5\uc720\ub428'));

    // Use toggle
    const useToggle = document.createElement('label');
    useToggle.style.cssText = 'margin-left:auto;display:flex;align-items:center;gap:4px;font-size:10px;color:var(--cv-dim);cursor:pointer;';
    useToggle.addEventListener('click', (e) => e.stopPropagation());
    const useCheck = document.createElement('input');
    useCheck.type = 'checkbox';
    useCheck.checked = !!p.used_by_me;
    useCheck.addEventListener('change', async (e) => {
      e.stopPropagation();
      await fetch('/api/personas/' + p.id + '/use', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: e.target.checked }),
      });
    });
    useToggle.appendChild(useCheck);
    useToggle.appendChild(document.createTextNode('\uc0ac\uc6a9'));
    statusRow.appendChild(useToggle);
    card.appendChild(statusRow);

    // Click -> read-only preview
    card.addEventListener('click', () => this.showPreview(p, true));
    return card;
  }

  /* ═══════════════════════════════════════════════════
     Create Flow
     ═══════════════════════════════════════════════════ */

  showCreate() {
    this._showView('create');
    this._previewToken = null;
    this._uploadedFileIds = [];
    const container = document.getElementById('persona-create');
    container.textContent = '';
    container.className = 'persona-create-panel';

    // Title
    const title = document.createElement('h3');
    title.textContent = '\uc0c8 \ud398\ub974\uc18c\ub098 \ub9cc\ub4e4\uae30';
    container.appendChild(title);

    // Back button
    const backBtn = document.createElement('button');
    backBtn.textContent = '\u2190 \uac24\ub7ec\ub9ac\ub85c';
    backBtn.style.cssText = 'padding:6px 12px;border:1px solid var(--cv-border);border-radius:8px;background:transparent;color:var(--cv-dim);font-size:0.78em;cursor:pointer;margin-bottom:14px;font-family:inherit;';
    backBtn.addEventListener('click', () => this.loadGallery());
    container.appendChild(backBtn);

    // Name input
    const nameLabel = document.createElement('label');
    nameLabel.textContent = '\uc778\ubb3c \uc774\ub984';
    nameLabel.style.cssText = 'display:block;font-size:0.82em;font-weight:600;margin-bottom:4px;color:var(--cv-text);';
    container.appendChild(nameLabel);

    const nameInput = document.createElement('input');
    nameInput.type = 'text';
    nameInput.placeholder = '\uc608: \uc77c\ub860 \uba38\uc2a4\ud06c, \uc2a4\ud2f0\ube0c \uc7a1\uc2a4';
    nameInput.style.cssText = 'width:100%;padding:10px 12px;border:1px solid var(--cv-border);border-radius:8px;font-size:0.88em;margin-bottom:12px;font-family:inherit;background:var(--cv-glass);color:var(--cv-text);';
    container.appendChild(nameInput);

    // Keywords input
    const kwLabel = document.createElement('label');
    kwLabel.textContent = '\uC5F0\uAD00 \uD0A4\uC6CC\uB4DC (\uAD8C\uC7A5)';
    kwLabel.style.cssText = 'display:block;font-size:0.82em;font-weight:600;margin-bottom:4px;color:var(--cv-text);';
    container.appendChild(kwLabel);
    const kwHint = document.createElement('div');
    kwHint.style.cssText = 'font-size:0.72em;color:var(--cv-dim);margin-bottom:6px;line-height:1.4;';
    kwHint.textContent = '\uC18C\uC18D \uD68C\uC0AC\uBA85, \uC9C1\uCC45, \uC804\uBB38 \uBD84\uC57C \uB4F1\uC744 \uD568\uAED8 \uC785\uB825\uD558\uBA74 \uB3D9\uBA85\uC774\uC778\uC744 \uD53C\uD558\uACE0 \uC815\uD655\uD55C \uC778\uBB3C\uC744 \uCC3E\uC744 \uC218 \uC788\uC2B5\uB2C8\uB2E4.';
    container.appendChild(kwHint);

    const kwInput = document.createElement('input');
    kwInput.type = 'text';
    kwInput.placeholder = '\uC608: \uC0BC\uC131\uC804\uC790 CEO, \uCE74\uCE74\uC624 \uACF5\uB3D9\uCC3D\uC5C5\uC790, AI \uC5F0\uAD6C\uC6D0 \uAD50\uC218';
    kwInput.style.cssText = 'width:100%;padding:10px 12px;border:1px solid var(--cv-border);border-radius:8px;font-size:0.88em;margin-bottom:16px;font-family:inherit;background:var(--cv-glass);color:var(--cv-text);';
    container.appendChild(kwInput);

    // Search button
    const searchBtn = document.createElement('button');
    searchBtn.textContent = '\uc6f9\uc5d0\uc11c \uac80\uc0c9';
    searchBtn.style.cssText = 'padding:10px 20px;border:none;border-radius:8px;background:var(--cv-blue);color:white;font-size:0.88em;font-weight:600;cursor:pointer;font-family:inherit;';
    container.appendChild(searchBtn);

    // Result area
    const resultArea = document.createElement('div');
    resultArea.style.cssText = 'margin-top:16px;';
    container.appendChild(resultArea);

    searchBtn.addEventListener('click', () => {
      const name = nameInput.value.trim();
      if (!name) {
        this._showInlineMsg(resultArea, '\uc778\ubb3c \uc774\ub984\uc744 \uc785\ub825\ud574\uc8fc\uc138\uc694.', true);
        return;
      }
      this._doSearch(name, kwInput.value.trim(), resultArea);
    });

    // Enter key triggers search
    nameInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') searchBtn.click();
    });
    kwInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') searchBtn.click();
    });
  }

  async _doSearch(name, keywords, resultArea) {
    resultArea.textContent = '';

    // Progress UI
    const progressBox = document.createElement('div');
    progressBox.style.cssText = 'padding:14px;border:1px solid var(--cv-border);border-radius:10px;background:var(--cv-glass);';

    const statusText = document.createElement('div');
    statusText.style.cssText = 'font-size:0.82em;color:var(--cv-dim);margin-bottom:8px;';
    statusText.textContent = '\uc6f9\uc5d0\uc11c \uc815\ubcf4\ub97c \uac80\uc0c9\ud558\uace0 \uc788\uc2b5\ub2c8\ub2e4...';
    progressBox.appendChild(statusText);

    // Progress bar
    const barBg = document.createElement('div');
    barBg.style.cssText = 'height:4px;border-radius:2px;background:rgba(255,255,255,0.06);overflow:hidden;margin-bottom:6px;';
    const barFill = document.createElement('div');
    barFill.style.cssText = 'height:100%;border-radius:2px;background:var(--cv-blue);width:0%;transition:width 0.5s ease;';
    barBg.appendChild(barFill);
    progressBox.appendChild(barBg);

    const timerText = document.createElement('div');
    timerText.style.cssText = 'font-size:0.7em;color:var(--cv-dim);';
    timerText.textContent = '0\uCD08 \uACBD\uACFC';
    progressBox.appendChild(timerText);

    resultArea.appendChild(progressBox);

    // Animate progress
    let elapsed = 0;
    const steps = [
      { at: 3, pct: 10, msg: '\uC6F9 \uAC80\uC0C9 \uC911...' },
      { at: 15, pct: 25, msg: '\uACBD\uB825 \uBC0F \uBC1C\uC5B8 \uC218\uC9D1 \uC911...' },
      { at: 40, pct: 45, msg: '\uC8FC\uC694 \uD398\uC774\uC9C0 \uC77D\uB294 \uC911...' },
      { at: 80, pct: 65, msg: '\uC778\uC0DD \uC804\uD658\uC810 \uBD84\uC11D \uC911...' },
      { at: 120, pct: 80, msg: '\uCDA9\uBD84\uB3C4 \uD310\uC815 \uC911...' },
      { at: 180, pct: 90, msg: '\uB370\uC774\uD130\uAC00 \uB9CE\uC544 \uC870\uAE08 \uB354 \uAC78\uB9BD\uB2C8\uB2E4...' },
    ];
    const progressIv = setInterval(() => {
      elapsed++;
      timerText.textContent = elapsed + '\uCD08 \uACBD\uACFC';
      for (const s of steps) {
        if (elapsed === s.at) {
          barFill.style.width = s.pct + '%';
          statusText.textContent = s.msg;
        }
      }
    }, 1000);

    try {
      const resp = await fetch('/api/personas/search-preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, keywords }),
      });
      const data = await resp.json();

      clearInterval(progressIv);
      resultArea.textContent = '';

      if (!data.ok) {
        this._showInlineMsg(resultArea, data.error || '\uac80\uc0c9\uc5d0 \uc2e4\ud328\ud588\uc2b5\ub2c8\ub2e4.', true);
        return;
      }

      this._previewToken = data.preview_token;

      // Show search results
      const box = document.createElement('div');
      box.style.cssText = 'padding:14px;border:1px solid var(--cv-border);border-radius:10px;background:var(--cv-glass);';

      // Sufficiency badge
      const badgeRow = document.createElement('div');
      badgeRow.style.cssText = 'display:flex;align-items:center;gap:8px;margin-bottom:10px;';
      const badge = document.createElement('span');
      badge.style.cssText = 'font-size:0.78em;padding:3px 10px;border-radius:12px;font-weight:600;';
      if (data.sufficient) {
        badge.style.background = 'rgba(34,197,94,0.12)';
        badge.style.color = 'var(--cv-green)';
        badge.textContent = '\u2705 \ucda9\ubd84\ud55c \uc815\ubcf4';
      } else {
        badge.style.background = 'rgba(230,126,34,0.12)';
        badge.style.color = '#e67e22';
        badge.textContent = '\u26a0\ufe0f \ucd94\uac00 \uc815\ubcf4 \uad8c\uc7a5';
      }
      badgeRow.appendChild(badge);

      if (data.source_count) {
        const srcCount = document.createElement('span');
        srcCount.style.cssText = 'font-size:0.72em;color:var(--cv-dim);';
        srcCount.textContent = '\ucd9c\ucc98 ' + data.source_count + '\uac74';
        badgeRow.appendChild(srcCount);
      }
      box.appendChild(badgeRow);

      // Summary
      if (data.summary) {
        const summaryEl = document.createElement('div');
        summaryEl.style.cssText = 'font-size:0.82em;line-height:1.5;color:var(--cv-text);margin-bottom:14px;';
        summaryEl.textContent = data.summary;
        box.appendChild(summaryEl);
      }

      // Action buttons
      const btnRow = document.createElement('div');
      btnRow.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;';

      if (data.search_failed) {
        const hint = document.createElement('div');
        hint.style.cssText = 'font-size:0.78em;color:var(--cv-dim);margin-bottom:10px;line-height:1.5;';
        hint.textContent = '\uD0A4\uC6CC\uB4DC\uC5D0 \uC18C\uC18D \uD68C\uC0AC\uBA85, \uC9C1\uCC45, \uB300\uD45C \uACBD\uB825 \uB4F1\uC744 \uCD94\uAC00\uD558\uBA74 \uC778\uBB3C \uD2B9\uC815\uC774 \uC27D\uC2B5\uB2C8\uB2E4.';
        box.appendChild(hint);

        const retryBtn = document.createElement('button');
        retryBtn.textContent = '\uD0A4\uC6CC\uB4DC \uCD94\uAC00 \uD6C4 \uC7AC\uAC80\uC0C9';
        retryBtn.style.cssText = 'padding:8px 16px;border:none;border-radius:8px;background:var(--cv-blue);color:white;font-size:0.82em;font-weight:600;cursor:pointer;font-family:inherit;';
        retryBtn.addEventListener('click', () => this.showCreate());
        btnRow.appendChild(retryBtn);

        const moreBtn = document.createElement('button');
        moreBtn.textContent = '\uC9C1\uC811 \uC815\uBCF4 \uC785\uB825';
        moreBtn.style.cssText = 'padding:8px 16px;border:1px solid var(--cv-border);border-radius:8px;background:transparent;color:var(--cv-text);font-size:0.82em;font-weight:600;cursor:pointer;font-family:inherit;';
        moreBtn.addEventListener('click', () => this._showAdditionalInput(name, resultArea));
        btnRow.appendChild(moreBtn);
      } else {
        const createBtn = document.createElement('button');
        createBtn.textContent = '\uc774\ub300\ub85c \uc0dd\uc131';
        createBtn.style.cssText = 'padding:8px 16px;border:none;border-radius:8px;background:var(--cv-green);color:white;font-size:0.82em;font-weight:600;cursor:pointer;font-family:inherit;';
        createBtn.addEventListener('click', () => this._doAutoCreate(this._previewToken, resultArea));
        btnRow.appendChild(createBtn);

        const moreBtn = document.createElement('button');
        moreBtn.textContent = '\ucd94\uac00 \uc815\ubcf4 \uc785\ub825';
        moreBtn.style.cssText = 'padding:8px 16px;border:1px solid var(--cv-border);border-radius:8px;background:transparent;color:var(--cv-text);font-size:0.82em;font-weight:600;cursor:pointer;font-family:inherit;';
        moreBtn.addEventListener('click', () => this._showAdditionalInput(name, resultArea));
        btnRow.appendChild(moreBtn);
      }

      box.appendChild(btnRow);
      resultArea.appendChild(box);

    } catch (e) {
      clearInterval(progressIv);
      resultArea.textContent = '';
      const errBox = document.createElement('div');
      errBox.style.cssText = 'padding:14px;border:1px solid rgba(233,69,96,0.3);border-radius:10px;background:rgba(233,69,96,0.05);';
      const errMsg = document.createElement('div');
      errMsg.style.cssText = 'font-size:0.82em;color:var(--cv-red);margin-bottom:10px;';
      errMsg.textContent = '\uAC80\uC0C9 \uC911 \uC624\uB958\uAC00 \uBC1C\uC0DD\uD588\uC2B5\uB2C8\uB2E4. \uB124\uD2B8\uC6CC\uD06C \uC0C1\uD0DC\uB97C \uD655\uC778\uD558\uACE0 \uB2E4\uC2DC \uC2DC\uB3C4\uD574\uC8FC\uC138\uC694.';
      errBox.appendChild(errMsg);
      const retryBtn = document.createElement('button');
      retryBtn.textContent = '\uB2E4\uC2DC \uC2DC\uB3C4';
      retryBtn.style.cssText = 'padding:8px 16px;border:1px solid var(--cv-border);border-radius:8px;background:transparent;color:var(--cv-text);font-size:0.82em;font-weight:600;cursor:pointer;font-family:inherit;';
      retryBtn.addEventListener('click', () => this._doSearch(name, keywords, resultArea));
      errBox.appendChild(retryBtn);
      resultArea.appendChild(errBox);
      console.warn('Search error:', e);
    }
  }

  _showAdditionalInput(name, resultArea) {
    resultArea.textContent = '';

    const box = document.createElement('div');
    box.style.cssText = 'padding:14px;border:1px solid var(--cv-border);border-radius:10px;background:var(--cv-glass);';

    const subtitle = document.createElement('div');
    subtitle.style.cssText = 'font-size:0.88em;font-weight:600;margin-bottom:10px;color:var(--cv-text);';
    subtitle.textContent = '\ucd94\uac00 \uc790\ub8cc \uc5c5\ub85c\ub4dc';
    box.appendChild(subtitle);

    const hint = document.createElement('div');
    hint.style.cssText = 'font-size:0.75em;color:var(--cv-dim);margin-bottom:10px;';
    hint.textContent = '.txt, .md, .pdf \ud30c\uc77c\uc744 \uc5c5\ub85c\ub4dc\ud560 \uc218 \uc788\uc2b5\ub2c8\ub2e4 (\uc120\ud0dd).';
    box.appendChild(hint);

    const fileInput = document.createElement('input');
    fileInput.type = 'file';
    fileInput.accept = '.txt,.md,.pdf';
    fileInput.multiple = true;
    fileInput.style.cssText = 'display:block;margin-bottom:12px;font-size:0.8em;color:var(--cv-dim);';
    box.appendChild(fileInput);

    const uploadStatus = document.createElement('div');
    uploadStatus.style.cssText = 'font-size:0.75em;color:var(--cv-dim);margin-bottom:10px;';
    box.appendChild(uploadStatus);

    // Upload handler
    fileInput.addEventListener('change', async () => {
      if (!fileInput.files.length) return;
      uploadStatus.textContent = '\uc5c5\ub85c\ub4dc \uc911...';
      const formData = new FormData();
      for (const f of fileInput.files) {
        formData.append('files', f);
      }
      try {
        const resp = await fetch('/api/personas/upload', { method: 'POST', body: formData });
        const data = await resp.json();
        if (data.ok && data.files) {
          this._uploadedFileIds = data.files.map(f => f.file_id);
          uploadStatus.textContent = data.files.length + '\uac1c \ud30c\uc77c \uc5c5\ub85c\ub4dc \uc644\ub8cc';
          uploadStatus.style.color = 'var(--cv-green)';
        } else {
          uploadStatus.textContent = '\uc5c5\ub85c\ub4dc \uc2e4\ud328';
          uploadStatus.style.color = 'var(--cv-red)';
        }
      } catch (e) {
        uploadStatus.textContent = '\uc5c5\ub85c\ub4dc \uc624\ub958';
        uploadStatus.style.color = 'var(--cv-red)';
      }
    });

    // Start interview button
    const interviewBtn = document.createElement('button');
    interviewBtn.textContent = '\uc778\ud130\ubdf0 \uc2dc\uc791';
    interviewBtn.style.cssText = 'padding:10px 20px;border:none;border-radius:8px;background:var(--cv-accent);color:white;font-size:0.88em;font-weight:600;cursor:pointer;font-family:inherit;';
    interviewBtn.addEventListener('click', () => this._startInterview(name));
    box.appendChild(interviewBtn);

    resultArea.appendChild(box);
  }

  async _doAutoCreate(previewToken, resultArea) {
    if (!previewToken) return;

    resultArea.textContent = '';
    const loading = document.createElement('div');
    loading.style.cssText = 'font-size:0.82em;color:var(--cv-dim);padding:12px 0;';
    loading.textContent = '\ud569\uc131 \uc911... \ud398\ub974\uc18c\ub098\ub97c \uc0dd\uc131\ud558\uace0 \uc788\uc2b5\ub2c8\ub2e4.';
    resultArea.appendChild(loading);

    try {
      const resp = await fetch('/api/personas', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          preview_token: previewToken,
          file_ids: this._uploadedFileIds,
        }),
      });
      const data = await resp.json();

      if (data.ok && data.persona) {
        resultArea.textContent = '';
        const doneMsg = document.createElement('div');
        doneMsg.style.cssText = 'font-size:0.85em;color:var(--cv-green);padding:10px 0;font-weight:600;';
        doneMsg.textContent = '\ud398\ub974\uc18c\ub098 \uc0dd\uc131 \uc644\ub8cc!';
        resultArea.appendChild(doneMsg);
        await new Promise(r => setTimeout(r, 800));
        this.showPreview(data.persona);
      } else {
        resultArea.textContent = '';
        this._showInlineMsg(resultArea, data.error || '\ud398\ub974\uc18c\ub098 \uc0dd\uc131\uc5d0 \uc2e4\ud328\ud588\uc2b5\ub2c8\ub2e4.', true);
      }
    } catch (e) {
      resultArea.textContent = '';
      this._showInlineMsg(resultArea, '\ud398\ub974\uc18c\ub098 \uc0dd\uc131 \uc911 \uc624\ub958\uac00 \ubc1c\uc0dd\ud588\uc2b5\ub2c8\ub2e4.', true);
      console.warn('Create error:', e);
    }
  }

  /* ═══════════════════════════════════════════════════
     Interview Chat
     ═══════════════════════════════════════════════════ */

  _startInterview(name) {
    this._showView('interview');
    const container = document.getElementById('persona-interview-view');
    container.textContent = '';
    container.className = 'persona-interview-panel';

    // Header
    const header = document.createElement('div');
    header.style.cssText = 'display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;';

    const titleEl = document.createElement('h3');
    titleEl.textContent = name + ' \uc778\ud130\ubdf0';
    header.appendChild(titleEl);

    const backBtn = document.createElement('button');
    backBtn.textContent = '\u2190 \ub3cc\uc544\uac00\uae30';
    backBtn.style.cssText = 'padding:5px 12px;border:1px solid var(--cv-border);border-radius:8px;background:transparent;color:var(--cv-dim);font-size:0.75em;cursor:pointer;font-family:inherit;';
    backBtn.addEventListener('click', () => {
      this._closeWs();
      this.showCreate();
    });
    header.appendChild(backBtn);
    container.appendChild(header);

    // Chat area
    const chatArea = document.createElement('div');
    chatArea.style.cssText = 'flex:1;overflow-y:auto;border:1px solid var(--cv-border);border-radius:10px;background:var(--cv-glass);padding:12px;margin-bottom:12px;min-height:200px;max-height:400px;';
    container.appendChild(chatArea);

    // Status / turn indicator
    const statusBar = document.createElement('div');
    statusBar.style.cssText = 'font-size:0.72em;color:var(--cv-dim);margin-bottom:8px;';
    statusBar.textContent = '\uc5f0\uacb0 \uc911...';
    container.appendChild(statusBar);

    // Input area
    const inputRow = document.createElement('div');
    inputRow.style.cssText = 'display:flex;gap:8px;align-items:flex-end;';

    const textarea = document.createElement('textarea');
    textarea.rows = 2;
    textarea.placeholder = '\ub2f5\ubcc0\uc744 \uc785\ub825\ud558\uc138\uc694...';
    textarea.style.cssText = 'flex:1;padding:10px 12px;border:1px solid var(--cv-border);border-radius:8px;font-size:0.85em;font-family:inherit;resize:none;background:var(--cv-glass);color:var(--cv-text);';
    textarea.disabled = true;
    inputRow.appendChild(textarea);

    const sendBtn = document.createElement('button');
    sendBtn.textContent = '\uc804\uc1a1';
    sendBtn.style.cssText = 'padding:10px 16px;border:none;border-radius:8px;background:var(--cv-blue);color:white;font-size:0.82em;font-weight:600;cursor:pointer;font-family:inherit;white-space:nowrap;';
    sendBtn.disabled = true;
    inputRow.appendChild(sendBtn);

    container.appendChild(inputRow);

    // Finish button
    const finishRow = document.createElement('div');
    finishRow.style.cssText = 'margin-top:10px;text-align:right;';

    const finishBtn = document.createElement('button');
    finishBtn.textContent = '\uc785\ub825 \uc644\ub8cc';
    finishBtn.style.cssText = 'padding:8px 16px;border:1px solid var(--cv-border);border-radius:8px;background:transparent;color:var(--cv-dim);font-size:0.78em;font-weight:600;cursor:pointer;font-family:inherit;';
    finishBtn.disabled = true;
    finishRow.appendChild(finishBtn);
    container.appendChild(finishRow);

    // Connect WebSocket
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = proto + '//' + location.host + '/ws/persona';
    this._closeWs();
    const ws = new WebSocket(wsUrl);
    this._ws = ws;

    const addMessage = (text, isSystem) => {
      const msg = document.createElement('div');
      msg.style.cssText = 'margin-bottom:10px;padding:8px 12px;border-radius:8px;font-size:0.82em;line-height:1.5;max-width:85%;' +
        (isSystem
          ? 'background:var(--cv-accent-bg);color:var(--cv-text);'
          : 'background:var(--cv-glass-hi);color:var(--cv-text);margin-left:auto;text-align:right;');
      msg.textContent = text;
      chatArea.appendChild(msg);
      chatArea.scrollTop = chatArea.scrollHeight;
    };

    ws.onopen = () => {
      statusBar.textContent = '\uc5f0\uacb0\ub428 \u2014 \uc778\ud130\ubdf0\ub97c \uc2dc\uc791\ud569\ub2c8\ub2e4...';
      _personaSignalRunning(true);
      ws.send(JSON.stringify({
        type: 'interview_start',
        data: {
          name: name,
          preview_token: this._previewToken || '',
          file_ids: this._uploadedFileIds,
        },
      }));
    };

    ws.onmessage = (event) => {
      let msg;
      try { msg = JSON.parse(event.data); } catch { return; }

      switch (msg.type) {
        case 'interview_ready':
          statusBar.textContent = '\uc9c8\ubb38\uc744 \uc900\ube44\ud558\uace0 \uc788\uc2b5\ub2c8\ub2e4...';
          break;

        case 'interview_question': {
          const d = msg.data || {};
          addMessage(d.question, true);
          statusBar.textContent = '\uc9c8\ubb38 ' + d.turn + '/' + d.max_turns;
          textarea.disabled = false;
          sendBtn.disabled = false;
          finishBtn.disabled = false;
          textarea.focus();
          break;
        }

        case 'interview_max_reached':
          addMessage(msg.data?.message || '\ucd5c\ub300 \uc9c8\ubb38 \ud69f\uc218\uc5d0 \ub3c4\ub2ec\ud588\uc2b5\ub2c8\ub2e4.', true);
          textarea.disabled = true;
          sendBtn.disabled = true;
          finishBtn.disabled = true;
          statusBar.textContent = '\ud569\uc131 \ub300\uae30 \uc911...';
          break;

        case 'interview_synthesizing':
          addMessage(msg.data?.message || '\ud398\ub974\uc18c\ub098\ub97c \ud569\uc131\ud558\uace0 \uc788\uc2b5\ub2c8\ub2e4...', true);
          textarea.disabled = true;
          sendBtn.disabled = true;
          finishBtn.disabled = true;
          statusBar.textContent = '\ud569\uc131 \uc911...';
          break;

        case 'persona_ready':
          statusBar.textContent = '\uc644\ub8cc!';
          this._closeWs();
          if (msg.data?.persona) {
            this.showPreview(msg.data.persona);
          }
          break;

        case 'error':
          addMessage('\uc624\ub958: ' + (msg.data?.message || '\uc624\ub958\uac00 \ubc1c\uc0dd\ud588\uc2b5\ub2c8\ub2e4.'), true);
          statusBar.textContent = '\uc624\ub958 \ubc1c\uc0dd';
          break;
      }
    };

    ws.onclose = () => {
      textarea.disabled = true;
      sendBtn.disabled = true;
      finishBtn.disabled = true;
      _personaSignalRunning(false);
      if (statusBar.textContent !== '\uc644\ub8cc!') {
        statusBar.textContent = '\uc5f0\uacb0 \uc885\ub8cc';
      }
    };

    ws.onerror = () => {
      statusBar.textContent = '\uc5f0\uacb0 \uc624\ub958';
    };

    // Send answer
    const sendAnswer = () => {
      const answer = textarea.value.trim();
      if (!answer || !ws || ws.readyState !== WebSocket.OPEN) return;
      addMessage(answer, false);
      ws.send(JSON.stringify({ type: 'interview_answer', data: { answer } }));
      textarea.value = '';
      textarea.disabled = true;
      sendBtn.disabled = true;
      statusBar.textContent = '\ub2e4\uc74c \uc9c8\ubb38 \uc900\ube44 \uc911...';
    };

    sendBtn.addEventListener('click', sendAnswer);
    textarea.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendAnswer();
      }
    });

    // Finish interview
    finishBtn.addEventListener('click', () => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'interview_finish' }));
        textarea.disabled = true;
        sendBtn.disabled = true;
        finishBtn.disabled = true;
        statusBar.textContent = '\ud569\uc131 \ub300\uae30 \uc911...';
        addMessage('\uc785\ub825\uc744 \uc644\ub8cc\ud588\uc2b5\ub2c8\ub2e4. \ud398\ub974\uc18c\ub098\ub97c \ud569\uc131\ud569\ub2c8\ub2e4...', true);
      }
    });
  }

  _closeWs() {
    if (this._ws) {
      try { this._ws.close(); } catch (e) { /* ignore */ }
      this._ws = null;
    }
  }

  /* ═══════════════════════════════════════════════════
     Preview / Edit
     ═══════════════════════════════════════════════════ */

  showPreview(p, readOnly = false) {
    this._showView('preview');
    const container = document.getElementById('persona-preview');
    container.textContent = '';

    // Header row
    const headerRow = document.createElement('div');
    headerRow.style.cssText = 'display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;';

    const title = document.createElement('h3');
    title.style.cssText = 'color:var(--cv-text);margin:0;';
    title.textContent = p.name;
    headerRow.appendChild(title);

    const backBtn = document.createElement('button');
    backBtn.textContent = '\u2190 \uac24\ub7ec\ub9ac\ub85c';
    backBtn.style.cssText = 'padding:6px 12px;border:1px solid var(--cv-border);border-radius:8px;background:transparent;color:var(--cv-dim);font-size:0.78em;cursor:pointer;font-family:inherit;';
    backBtn.addEventListener('click', () => this.loadGallery());
    headerRow.appendChild(backBtn);
    container.appendChild(headerRow);

    // Meta info
    const meta = document.createElement('div');
    meta.style.cssText = 'font-size:0.72em;color:var(--cv-dim);margin-bottom:14px;display:flex;gap:12px;flex-wrap:wrap;';

    const srcSpan = document.createElement('span');
    srcSpan.textContent = '\uc18c\uc2a4: ' + (p.source || '-');
    meta.appendChild(srcSpan);

    if (p.created_at) {
      const dateSpan = document.createElement('span');
      dateSpan.textContent = '\uc0dd\uc131: ' + p.created_at.slice(0, 10);
      meta.appendChild(dateSpan);
    }
    container.appendChild(meta);

    // Editable sections
    const sections = [
      { key: 'name', label: '\uc774\ub984', value: p.name, multiline: false },
      { key: 'summary', label: '\uc694\uc57d', value: p.summary || '', multiline: false },
      { key: 'persona_text', label: '\ud398\ub974\uc18c\ub098 \ud14d\uc2a4\ud2b8', value: p.persona_text || '', multiline: true },
    ];

    sections.forEach(sec => {
      const section = document.createElement('div');
      section.style.cssText = 'margin-bottom:16px;';

      const labelRow = document.createElement('div');
      labelRow.style.cssText = 'display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;';

      const label = document.createElement('div');
      label.style.cssText = 'font-size:0.78em;font-weight:600;color:var(--cv-dim);text-transform:uppercase;letter-spacing:0.5px;';
      label.textContent = sec.label;
      labelRow.appendChild(label);

      const editToggle = document.createElement('button');
      editToggle.textContent = '\ud3b8\uc9d1';
      editToggle.style.cssText = 'padding:2px 8px;border:1px solid var(--cv-border);border-radius:6px;background:transparent;color:var(--cv-dim);font-size:0.68em;cursor:pointer;font-family:inherit;';
      if (!readOnly) labelRow.appendChild(editToggle);

      section.appendChild(labelRow);

      // Display element
      const display = document.createElement('div');
      display.style.cssText = 'font-size:0.82em;line-height:1.6;background:var(--cv-glass);padding:12px;border-radius:10px;color:var(--cv-text);';
      if (sec.multiline && sec.value) {
        this._renderPersonaText(display, sec.value);
      } else {
        display.textContent = sec.value || '(\ub0b4\uc6a9 \uc5c6\uc74c)';
      }
      section.appendChild(display);

      // Edit element (hidden)
      const editBox = document.createElement('div');
      editBox.style.cssText = 'display:none;';

      let input;
      if (sec.multiline) {
        input = document.createElement('textarea');
        input.rows = 12;
        input.style.cssText = 'width:100%;padding:10px 12px;border:1px solid var(--cv-border);border-radius:8px;font-size:0.82em;font-family:inherit;resize:vertical;line-height:1.6;background:var(--cv-glass);color:var(--cv-text);';
      } else {
        input = document.createElement('input');
        input.type = 'text';
        input.style.cssText = 'width:100%;padding:10px 12px;border:1px solid var(--cv-border);border-radius:8px;font-size:0.82em;font-family:inherit;background:var(--cv-glass);color:var(--cv-text);';
      }
      input.value = sec.value;
      editBox.appendChild(input);

      const saveBtn = document.createElement('button');
      saveBtn.textContent = '\uc800\uc7a5';
      saveBtn.style.cssText = 'margin-top:6px;padding:6px 14px;border:none;border-radius:6px;background:var(--cv-green);color:white;font-size:0.78em;font-weight:600;cursor:pointer;font-family:inherit;';
      editBox.appendChild(saveBtn);

      section.appendChild(editBox);

      let editing = false;
      editToggle.addEventListener('click', () => {
        editing = !editing;
        display.style.display = editing ? 'none' : '';
        editBox.style.display = editing ? '' : 'none';
        editToggle.textContent = editing ? '\ucde8\uc18c' : '\ud3b8\uc9d1';
        if (editing) input.focus();
      });

      saveBtn.addEventListener('click', async () => {
        const newVal = input.value.trim();
        saveBtn.textContent = '\uc800\uc7a5 \uc911...';
        saveBtn.disabled = true;
        try {
          const resp = await fetch('/api/personas/' + p.id, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ [sec.key]: newVal }),
          });
          const data = await resp.json();
          if (data.ok) {
            display.textContent = newVal || '(\ub0b4\uc6a9 \uc5c6\uc74c)';
            p[sec.key] = newVal;
            if (sec.key === 'name') {
              title.textContent = newVal;
            }
            editing = false;
            display.style.display = '';
            editBox.style.display = 'none';
            editToggle.textContent = '\ud3b8\uc9d1';
          } else {
            this._showInlineMsg(editBox, data.error || '\uc800\uc7a5 \uc2e4\ud328', true);
          }
        } catch (e) {
          this._showInlineMsg(editBox, '\uc800\uc7a5 \uc911 \uc624\ub958\uac00 \ubc1c\uc0dd\ud588\uc2b5\ub2c8\ub2e4.', true);
        }
        saveBtn.textContent = '\uc800\uc7a5';
        saveBtn.disabled = false;
      });

      container.appendChild(section);
    });

    // Delete button (hidden for read-only shared personas)
    if (readOnly) return;
    const deleteRow = document.createElement('div');
    deleteRow.style.cssText = 'margin-top:24px;padding-top:16px;border-top:1px solid var(--cv-border);';

    const deleteBtn = document.createElement('button');
    deleteBtn.textContent = '\uc0ad\uc81c';
    deleteBtn.style.cssText = 'padding:8px 16px;border:1px solid var(--cv-red);border-radius:8px;background:transparent;color:var(--cv-red);font-size:0.78em;font-weight:600;cursor:pointer;font-family:inherit;';
    deleteBtn.addEventListener('click', async () => {
      if (!confirm(p.name + ' \ud398\ub974\uc18c\ub098\ub97c \uc0ad\uc81c\ud558\uc2dc\uaca0\uc2b5\ub2c8\uae4c?')) return;
      deleteBtn.textContent = '\uc0ad\uc81c \uc911...';
      deleteBtn.disabled = true;
      try {
        const resp = await fetch('/api/personas/' + p.id, { method: 'DELETE' });
        const data = await resp.json();
        if (data.ok) {
          this.loadGallery();
        } else {
          deleteBtn.textContent = '\uc0ad\uc81c';
          deleteBtn.disabled = false;
          this._showInlineMsg(deleteRow, data.error || '\uc0ad\uc81c \uc2e4\ud328', true);
        }
      } catch (e) {
        deleteBtn.textContent = '\uc0ad\uc81c';
        deleteBtn.disabled = false;
        this._showInlineMsg(deleteRow, '\uc0ad\uc81c \uc911 \uc624\ub958\uac00 \ubc1c\uc0dd\ud588\uc2b5\ub2c8\ub2e4.', true);
      }
    });
    deleteRow.appendChild(deleteBtn);
    container.appendChild(deleteRow);
  }

  /* ═══════════════════════════════════════════════════
     Helpers
     ═══════════════════════════════════════════════════ */

  _renderPersonaText(container, text) {
    /* Render persona text with ## headers as visual section dividers.
       Uses safe DOM methods only. */
    const lines = text.split('\n');
    let currentSection = null;

    for (const line of lines) {
      const trimmed = line.trim();

      // ## Header -> new section card
      if (trimmed.startsWith('## ')) {
        const heading = document.createElement('div');
        heading.style.cssText = 'font-size:0.82em;font-weight:700;color:var(--cv-accent);margin-top:14px;margin-bottom:6px;padding-bottom:4px;border-bottom:1px solid var(--cv-border);';
        heading.textContent = trimmed.replace(/^##\s*/, '');
        container.appendChild(heading);
        currentSection = document.createElement('div');
        currentSection.style.cssText = 'margin-bottom:10px;';
        container.appendChild(currentSection);
        continue;
      }

      // Bold: **text** -> styled span
      if (trimmed.startsWith('- **') || trimmed.startsWith('**')) {
        const item = document.createElement('div');
        item.style.cssText = 'margin-bottom:3px;font-size:0.82em;';
        const match = trimmed.match(/\*\*(.+?)\*\*(.*)/);
        if (match) {
          const bold = document.createElement('strong');
          bold.textContent = (trimmed.startsWith('- ') ? '  ' : '') + match[1];
          item.appendChild(bold);
          if (match[2]) {
            item.appendChild(document.createTextNode(match[2]));
          }
        } else {
          item.textContent = trimmed;
        }
        (currentSection || container).appendChild(item);
        continue;
      }

      // List items
      if (trimmed.startsWith('- ')) {
        const item = document.createElement('div');
        item.style.cssText = 'margin-bottom:2px;padding-left:8px;font-size:0.82em;';
        item.textContent = '\u2022 ' + trimmed.slice(2);
        (currentSection || container).appendChild(item);
        continue;
      }

      // Empty line -> spacing
      if (!trimmed) {
        continue;
      }

      // Regular text
      const par = document.createElement('div');
      par.style.cssText = 'margin-bottom:4px;font-size:0.82em;';
      par.textContent = trimmed;
      (currentSection || container).appendChild(par);
    }
  }

  _showInlineMsg(parent, text, isError) {
    const msg = document.createElement('div');
    msg.style.cssText = 'font-size:0.78em;padding:8px 12px;border-radius:8px;margin-top:8px;' +
      (isError ? 'background:rgba(233,69,96,0.08);color:var(--cv-red);' : 'background:rgba(34,197,94,0.08);color:var(--cv-green);');
    msg.textContent = text;
    parent.appendChild(msg);
    setTimeout(() => { if (msg.parentNode) msg.remove(); }, 5000);
  }
}
