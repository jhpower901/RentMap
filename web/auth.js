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
.admin-link{background:#111827!important;color:#fff!important}
@media (max-width:480px){.user-pill{font-size:11px;padding:2px 7px}}
    `;
    document.head.appendChild(s);
  }

  function ensureAdminLink(user) {
    if (!user || !user.isAdmin) return;
    const nav = document.querySelector('nav');
    if (!nav || nav.querySelector('a[href="admin.html"]')) return;
    const link = document.createElement('a');
    link.href = 'admin.html';
    link.textContent = '관리';
    link.className = location.pathname.endsWith('/admin.html') ? 'active-nav admin-link' : 'admin-link';
    const slot = document.getElementById('userInfo');
    nav.insertBefore(link, slot || null);
  }

  function ensureRegionLink(user) {
    // "지역 신청" link is shown to every logged-in user so they can propose a
    // crawl area. The admin reviews + approves under /admin.html.
    if (!user) return;
    const nav = document.querySelector('nav');
    if (!nav || nav.querySelector('a[href="region-request.html"]')) return;
    const link = document.createElement('a');
    link.href = 'region-request.html';
    link.textContent = '지역 신청';
    if (location.pathname.endsWith('/region-request.html')) {
      link.className = 'active-nav';
    }
    const slot = document.getElementById('userInfo');
    nav.insertBefore(link, slot || null);
  }

  function ensureRegionSelector() {
    // Region selector lives in its own file (region.js) and self-installs
    // into the nav. Dynamically inject the script once per page so every
    // page that loads auth.js automatically gets the selector without each
    // page needing a hand-written <script src="region.js"> tag.
    if (document.getElementById('region-script')) return;
    var s = document.createElement('script');
    s.id = 'region-script';
    s.src = 'region.js?v=20260526-region1';
    s.async = true;
    document.head.appendChild(s);
  }

  function render(user) {
    if (!user) return;
    injectStyle();
    ensureAdminLink(user);
    ensureRegionLink(user);
    ensureRegionSelector();
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
