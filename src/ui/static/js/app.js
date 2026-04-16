'use strict';

/* ═══════════════════════════════════════════════════
   §  MODE MANAGER — Card Shell is the only UI
   ═══════════════════════════════════════════════════ */
class ModeManager {
  constructor() {
    this.activeMode = null;
    this._engBooted = false;
    /* Track running state per mode */
    this._modeRunning = { company: false, discussion: false, secretary: false, agent: false, engineering: false };
  }

  selectMode(mode, opts = {}) {
    this.activeMode = mode;
    location.hash = mode;

    // Boot card view shell
    this._bootCardView();

    // Delegate mode switch to CardView
    CardView.switchMode(mode);
  }

  /* Return to default landing mode (플레이북) */
  returnToLanding() {
    this.selectMode('builder');
  }

  /* Mark a mode as running/stopped */
  setModeRunning(mode, running) {
    this._modeRunning[mode] = running;
  }

  _bootCardView() {
    var cardApp = document.getElementById('card-app');
    if (!this._cardViewBooted) {
      this._cardViewBooted = true;
      CardView.init();
    } else {
      requestAnimationFrame(function() { window.dispatchEvent(new Event('resize')); });
    }
  }

  _bootEngineering() {
    if (!this._engBooted) {
      this._engBooted = true;
      this._engMgr = new EngineeringManager();
    }
    document.getElementById('eng-app').classList.remove('app-hidden');
    if (this._engMgr && (!this._engMgr.ws || this._engMgr.ws.readyState !== WebSocket.OPEN)) {
      this._engMgr._reconnectAttempts = 0;
      this._engMgr.connect();
    }
  }
}

/* ══ Auth Manager ══ */
const Auth = {
  user: null,
  membershipEnabled: false,

  _esc(s) {
    const d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
  },

  async init() {
    try {
      const h = await fetch('/health').then(r => r.json());
      this.membershipEnabled = h.membership === true;
    } catch { this.membershipEnabled = false; }

    if (!this.membershipEnabled) {
      document.getElementById('auth-screen').classList.add('hidden');
      var settingsBtn = document.getElementById('cs-settings-btn');
      if (settingsBtn) settingsBtn.style.display = 'none';
      return true;
    }

    try {
      const r = await fetch('/api/auth/me');
      if (r.ok) {
        const d = await r.json();
        if (d.ok) { this.user = d.user; this._showBadge(); this._applyCompanyName(); return true; }
      }
    } catch {}

    document.getElementById('auth-screen').classList.remove('hidden');
    this._bindEvents();
    return false;
  },

  _bindEvents() {
    const $ = id => document.getElementById(id);
    $('auth-show-register').onclick = () => { $('auth-login-form').style.display = 'none'; $('auth-register-form').style.display = 'block'; $('reg-err').textContent = ''; };
    $('auth-show-login').onclick = () => { $('auth-register-form').style.display = 'none'; $('auth-login-form').style.display = 'block'; $('auth-err').textContent = ''; };
    $('auth-login-btn').onclick = () => this._login();
    $('auth-pass').onkeydown = (e) => { if (e.key === 'Enter') this._login(); };
    $('auth-register-btn').onclick = () => this._register();
    $('reg-pass').onkeydown = (e) => { if (e.key === 'Enter') $('reg-pass-confirm').focus(); };
    $('reg-pass-confirm').onkeydown = (e) => { if (e.key === 'Enter') this._register(); };
  },

  async _login() {
    const $ = id => document.getElementById(id);
    const body = { entry_code: $('auth-entry').value, username: $('auth-user').value, password: $('auth-pass').value };
    $('auth-login-btn').disabled = true;
    $('auth-err').textContent = '';
    try {
      const r = await fetch('/api/auth/login', {
        method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body),
      });
      const d = await r.json();
      if (d.ok) {
        this.user = d.user;
        $('auth-screen').classList.add('hidden');
        this._showBadge();
        this._bootApp();
      } else {
        $('auth-err').textContent = d.error || '아이디 또는 비밀번호를 확인해 주세요';
      }
    } catch (e) { $('auth-err').textContent = '인터넷 연결을 확인하고 다시 시도해 주세요'; }
    $('auth-login-btn').disabled = false;
  },

  async _register() {
    const $ = id => document.getElementById(id);
    const email = $('reg-user').value.trim().toLowerCase();
    const body = { entry_code: $('reg-entry').value, username: email, display_name: $('reg-name').value, password: $('reg-pass').value };
    $('auth-register-btn').disabled = true;
    $('reg-err').textContent = '';
    if (!email.includes('@')) { $('reg-err').textContent = '이메일 형식으로 입력해주세요.'; $('auth-register-btn').disabled = false; return; }
    if (body.password !== $('reg-pass-confirm').value) { $('reg-err').textContent = '비밀번호가 일치하지 않습니다.'; $('auth-register-btn').disabled = false; return; }
    if (body.password.length < 6) { $('reg-err').textContent = '비밀번호는 6자 이상이어야 합니다.'; $('auth-register-btn').disabled = false; return; }
    try {
      const r = await fetch('/api/auth/register', {
        method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body),
      });
      const d = await r.json();
      if (d.ok) {
        $('reg-err').textContent = '';
        $('auth-register-form').style.display = 'none';
        $('auth-login-form').style.display = 'block';
        $('auth-err').textContent = '';
        const msg = document.createElement('div');
        msg.className = 'auth-pending';
        msg.textContent = d.auto_admin
          ? '관리자 계정으로 등록되었습니다! 바로 로그인하세요.'
          : '가입 신청이 완료되었습니다! 관리자 승인을 기다려주세요.';
        $('auth-login-form').appendChild(msg);
        setTimeout(() => msg.remove(), 8000);
      } else {
        $('reg-err').textContent = d.error || '가입 정보를 확인해 주세요';
      }
    } catch (e) { $('reg-err').textContent = '인터넷 연결을 확인하고 다시 시도해 주세요'; }
    $('auth-register-btn').disabled = false;
  },

  _showBadge() {
    if (!this.user) return;
    const u = this.user;

    // Populate settings panel
    const avatar = document.getElementById('sp-avatar');
    if (avatar) avatar.textContent = (u.display_name || u.username || '?')[0].toUpperCase();

    const dnEl = document.getElementById('sp-display-name');
    if (dnEl) dnEl.textContent = u.display_name || '';

    const unEl = document.getElementById('sp-username');
    if (unEl) unEl.textContent = u.username || '';

    const badge = document.getElementById('sp-role-badge');
    if (badge) {
      badge.textContent = u.role === 'admin' ? 'Admin' : 'Member';
      badge.className = 'sp-role-badge ' + (u.role === 'admin' ? 'role-admin' : 'role-user');
    }

    // Admin button visibility
    const adminBtn = document.getElementById('sp-admin-btn');
    if (adminBtn) adminBtn.style.display = u.role === 'admin' ? '' : 'none';

    // Bind settings panel events (only once)
    if (!this._settingsBound) {
      this._settingsBound = true;

      const settingsBtn = document.getElementById('cs-settings-btn');
      const panel = document.getElementById('settings-panel');
      const closeBtn = document.getElementById('settings-close');
      const logoutBtn = document.getElementById('sp-logout-btn');

      if (settingsBtn) settingsBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        panel.classList.add('show');
      });
      if (closeBtn) closeBtn.addEventListener('click', () => panel.classList.remove('show'));
      if (panel) panel.addEventListener('click', (e) => {
        if (e.target === panel) panel.classList.remove('show');
      });
      if (adminBtn) adminBtn.addEventListener('click', () => {
        panel.classList.remove('show');
        Auth._openAdmin();
      });
      if (logoutBtn) logoutBtn.addEventListener('click', async () => {
        try { await fetch('/api/auth/logout', { method: 'POST' }); } catch {}
        location.reload();
      });

      // Also bind admin-close button
      const adminClose = document.getElementById('admin-close');
      if (adminClose) adminClose.addEventListener('click', () => {
        document.getElementById('admin-panel').classList.remove('show');
      });
    }
  },

  async _openAdmin() {
    const panel = document.getElementById('admin-panel');
    panel.classList.add('show');

    // Load entry code
    this._loadEntryCode();
    this._bindEntryCodeEvents();

    // Load users
    const pendingList = document.getElementById('admin-pending-list');
    const activeList = document.getElementById('admin-active-list');
    const inactiveList = document.getElementById('admin-inactive-list');
    [pendingList, activeList, inactiveList].forEach(el => { el.textContent = ''; });

    try {
      const r = await fetch('/api/auth/admin/users');
      const d = await r.json();
      if (!d.ok) { pendingList.textContent = '권한이 없습니다.'; return; }

      const pending = d.users.filter(u => u.status === 'pending');
      const active = d.users.filter(u => u.status === 'approved');
      const inactive = d.users.filter(u => u.status === 'rejected' || u.status === 'disabled');

      const renderList = (container, users, emptyMsg) => {
        if (users.length === 0) {
          const p = document.createElement('div');
          p.style.cssText = 'color:var(--dim);font-size:0.82em;padding:8px 0;';
          p.textContent = emptyMsg;
          container.appendChild(p);
          return;
        }
        users.forEach(u => container.appendChild(this._renderUserRow(u)));
      };

      renderList(pendingList, pending, '대기 중인 가입 신청이 없습니다.');
      renderList(activeList, active, '활성 회원이 없습니다.');
      renderList(inactiveList, inactive, '비활성/거절 회원이 없습니다.');
    } catch (e) { pendingList.textContent = '일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요'; }
  },

  _renderUserRow(u) {
    const row = document.createElement('div');
    row.className = 'admin-user';
    const info = document.createElement('div');
    info.className = 'au-info';
    const nameEl = document.createElement('div');
    nameEl.className = 'au-name';
    const roleTag = u.role === 'admin' ? ' [관리자]' : '';
    nameEl.textContent = u.display_name + ' (' + u.username + ')' + roleTag;
    const metaEl = document.createElement('div');
    metaEl.className = 'au-meta';
    const statusLabels = {pending:'대기',approved:'활성',rejected:'거절',disabled:'비활성'};
    const lastLogin = u.last_login ? new Date(u.last_login).toLocaleDateString('ko-KR') : '-';
    metaEl.textContent = '가입: ' + new Date(u.created_at).toLocaleDateString('ko-KR') + ' · 최근: ' + lastLogin;
    info.appendChild(nameEl);
    info.appendChild(metaEl);

    /* Mode visibility toggles (only for approved users) */
    if (u.status === 'approved') {
      const modesRow = document.createElement('div');
      modesRow.className = 'au-modes';
      modesRow.style.cssText = 'display:flex;gap:4px;flex-wrap:wrap;margin-top:4px;';
      const ALL_MODES = [
        {id:'company',label:'Company',icon:'🏢'},
        {id:'discussion',label:'모의토론',icon:'💬'},
        {id:'secretary',label:'Secretary',icon:'📋'},
        // {id:'datalab',label:'DataLab',icon:'🔬'},  // 숨김 처리
        {id:'foresight',label:'미래아이디어',icon:'📡'},
        {id:'engineering',label:'Engineering',icon:'🛠️'},
        {id:'agent',label:'Agent',icon:'🎮'},
        {id:'law',label:'법령상담',icon:'⚖️'},
      ];
      const currentModes = u.visible_modes; // null = all, or array
      ALL_MODES.forEach(m => {
        const isOn = !currentModes || currentModes.includes(m.id);
        const chip = document.createElement('button');
        chip.className = 'au-mode-chip' + (isOn ? ' on' : '');
        chip.style.cssText = 'font-size:0.7em;padding:2px 6px;border-radius:10px;border:1px solid ' + (isOn ? 'var(--accent,#3b82f6)' : 'var(--dim,#555)') + ';background:' + (isOn ? 'var(--accent,#3b82f6)' : 'transparent') + ';color:' + (isOn ? '#fff' : 'var(--dim,#888)') + ';cursor:pointer;transition:all 0.2s;';
        chip.textContent = m.icon + ' ' + m.label;
        chip.title = (isOn ? '비활성화' : '활성화') + ': ' + m.label;
        chip.onclick = async () => {
          let newModes;
          if (isOn) {
            /* Turning OFF this mode */
            newModes = (currentModes || ALL_MODES.map(x=>x.id)).filter(x => x !== m.id);
          } else {
            /* Turning ON this mode */
            newModes = [...(currentModes || []), m.id];
          }
          /* If all modes selected, send null (= unrestricted) */
          if (newModes.length >= ALL_MODES.length) newModes = null;
          try {
            const r = await fetch('/api/auth/admin/visible-modes/' + encodeURIComponent(u.id), {
              method: 'PUT', headers: {'Content-Type':'application/json'},
              body: JSON.stringify({visible_modes: newModes}),
            });
            if (r.ok) this._openAdmin(); /* refresh */
          } catch {}
        };
        modesRow.appendChild(chip);
      });
      info.appendChild(modesRow);
    }
    const actions = document.createElement('div');
    actions.className = 'au-actions';
    const addBtn = (cls, label, action) => {
      const b = document.createElement('button');
      b.className = cls; b.textContent = label;
      b.onclick = () => this._adminAction(action, u.id);
      actions.appendChild(b);
    };
    if (u.status === 'pending') {
      addBtn('au-approve', '승인', 'approve');
      addBtn('au-reject', '거절', 'reject');
    } else if (u.status === 'approved') {
      if (u.role === 'admin') {
        addBtn('au-disable', '관리자해제', 'demote');
      } else {
        addBtn('au-approve', '관리자승격', 'promote');
      }
      addBtn('au-reset', 'PW초기화', 'reset-password');
      addBtn('au-disable', '비활성', 'disable');
      addBtn('au-reject', '삭제', 'delete');
    } else {
      addBtn('au-approve', '재활성', 'reactivate');
      addBtn('au-reject', '삭제', 'delete');
    }
    row.appendChild(info);
    row.appendChild(actions);
    return row;
  },

  async _loadEntryCode() {
    const display = document.getElementById('admin-entry-display');
    const revealBtn = document.getElementById('admin-entry-reveal');
    try {
      const r = await fetch('/api/auth/admin/entry-code');
      const d = await r.json();
      if (d.ok) {
        display.textContent = d.masked;
        display._fullCode = d.entry_code;
        display._revealed = false;
        revealBtn.textContent = '보기';
      }
    } catch { display.textContent = '오류'; }
  },

  _bindEntryCodeEvents() {
    const display = document.getElementById('admin-entry-display');
    const revealBtn = document.getElementById('admin-entry-reveal');
    const saveBtn = document.getElementById('admin-entry-save');
    const input = document.getElementById('admin-entry-input');
    const msg = document.getElementById('admin-entry-msg');

    revealBtn.onclick = () => {
      if (display._revealed) {
        const code = display._fullCode || '';
        display.textContent = code.length > 4 ? code.slice(0,2) + '•'.repeat(code.length-4) + code.slice(-2) : '•'.repeat(code.length);
        revealBtn.textContent = '보기';
        display._revealed = false;
      } else {
        display.textContent = display._fullCode || '(미설정)';
        revealBtn.textContent = '숨기기';
        display._revealed = true;
      }
    };

    saveBtn.onclick = async () => {
      const newCode = input.value.trim();
      msg.textContent = '';
      msg.style.color = '';
      if (!newCode || newCode.length < 4) { msg.textContent = '4자 이상 입력해주세요.'; msg.style.color = 'var(--red)'; return; }
      try {
        const r = await fetch('/api/auth/admin/entry-code', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({entry_code: newCode}),
        });
        const d = await r.json();
        if (d.ok) {
          msg.textContent = '입장코드가 변경되었습니다.';
          msg.style.color = 'var(--green)';
          input.value = '';
          this._loadEntryCode();
        } else {
          msg.textContent = d.error || '변경할 수 없습니다. 다시 시도해 주세요';
          msg.style.color = 'var(--red)';
        }
      } catch { msg.textContent = '일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요'; msg.style.color = 'var(--red)'; }
    };
  },

  async _adminAction(action, userId) {
    if (action === 'reset-password') {
      try {
        const r = await fetch('/api/auth/admin/reset-password/' + encodeURIComponent(userId), {method:'POST'});
        const d = await r.json();
        if (d.ok) {
          alert('임시 비밀번호: ' + d.temp_password + '\n\n' + d.username + ' 에게 전달해주세요.');
        } else {
          alert('초기화할 수 없습니다: ' + (d.error || ''));
        }
      } catch { alert('인터넷 연결을 확인하고 다시 시도해 주세요'); }
      return;
    }
    if (action === 'delete') {
      try { await fetch('/api/auth/admin/users/' + encodeURIComponent(userId), {method:'DELETE'}); } catch {}
    } else {
      try { await fetch('/api/auth/admin/' + action + '/' + encodeURIComponent(userId), {method:'POST'}); } catch {}
    }
    this._openAdmin();
  },

  _bootApp() {
    if (window._modeManager) return;
    const mm = new ModeManager();
    window._modeManager = mm;
    var hash = location.hash.replace('#', '');
    if (hash === 'company' || hash === 'instant') hash = 'builder'; // backward compat
    const vm = Auth.user && Auth.user.visible_modes;
    const canAccess = (mode) => !vm || vm.includes(mode);
    const validModes = ['builder','discussion','foresight','law']; // persona/secretary parked
    if (hash && validModes.includes(hash) && canAccess(hash)) {
      mm.selectMode(hash);
    } else {
      mm.selectMode('builder');
    }
  }
};

/* Capability ticker + advisory comments removed — landing page no longer exists */

/* ══ Theme Controller ══
   Initial theme is set by inline script in <head> (avoids flash).
   Default on first visit is 'light'; toggle persists to localStorage. */
const ThemeController = {
  KEY: 'ai-company-theme',

  init() {
    // Sync color-scheme for browser UA widgets (scrollbars, form controls)
    const current = document.documentElement.getAttribute('data-theme') || 'light';
    document.documentElement.style.colorScheme = current;

    const btn = document.getElementById('theme-toggle');
    if (btn) btn.addEventListener('click', () => this.toggle());
  },

  apply(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    document.documentElement.style.colorScheme = theme;
  },

  toggle() {
    const next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
    this.apply(next);
    try { localStorage.setItem(this.KEY, next); } catch {}
  }
};

document.addEventListener('DOMContentLoaded', async () => {
  ThemeController.init();
  const authed = await Auth.init();
  if (authed) {
    const mm = new ModeManager();
    window._modeManager = mm;
    var hash = location.hash.replace('#', '');
    if (hash === 'company' || hash === 'instant') hash = 'builder'; // backward compat
    const vm = Auth.user && Auth.user.visible_modes;
    const canAccess = (mode) => !vm || vm.includes(mode);
    const validModes = ['builder','discussion','foresight','law']; // persona/secretary parked
    if (hash && validModes.includes(hash) && canAccess(hash)) {
      mm.selectMode(hash);
    } else {
      mm.selectMode('builder');
    }
  }
});
