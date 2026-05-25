(function () {
  'use strict';

  function logout() {
    return fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' })
      .catch(() => {})
      .then(() => { location.href = '/login.html'; });
  }

  function me() {
    return fetch('/api/auth/me', { cache: 'no-store', credentials: 'same-origin' })
      .then(r => r.ok ? r.json() : null)
      .then(j => j && j.user ? j.user : null)
      .catch(() => null);
  }

  function injectStyle() {
    if (document.getElementById('auth-css')) return;
    const s = document.createElement('style');
    s.id = 'auth-css';
    s.textContent = `
.user-pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;background:#f0f4ff;border:1px solid #c7d2fe;color:#3730a3;padding:3px 9px;border-radius:16px;margin-left:auto;font-family:'Noto Sans KR',sans-serif}
.user-pill b{font-weight:700}
.user-pill button{background:transparent;border:none;color:#4338ca;cursor:pointer;font-size:12px;padding:0;text-decoration:underline;font-family:inherit}
.user-pill button:hover{color:#1e1b4b}
@media (max-width:480px){.user-pill{font-size:11px;padding:2px 7px}}
    `;
    document.head.appendChild(s);
  }

  function render(user) {
    if (!user) return;
    injectStyle();
    let slot = document.getElementById('userInfo');
    if (!slot) {
      // Fall back to appending into the nav so older pages without an explicit
      // slot still get a logout control.
      const nav = document.querySelector('nav');
      if (!nav) return;
      slot = document.createElement('span');
      slot.id = 'userInfo';
      nav.appendChild(slot);
    }
    slot.className = 'user-pill';
    slot.innerHTML = '';
    const name = document.createElement('b');
    name.textContent = user.displayName || user.username;
    slot.appendChild(name);
    const out = document.createElement('button');
    out.type = 'button';
    out.textContent = '로그아웃';
    out.addEventListener('click', logout);
    slot.appendChild(out);
  }

  function init() {
    me().then(render);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }

  window.Auth = { me, logout };
})();
