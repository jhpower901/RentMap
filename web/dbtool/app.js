/* RentMap DB-tool SPA.
 * No build step — plain ES modules-as-script. Each tab is a small
 * object {load, …} that owns its own state and DOM bindings.
 *
 * Cross-tab rules:
 *   - Every mutate goes through ``api(path, opts)`` which surfaces the
 *     server's HTTPException ``detail`` as the toast/error message.
 *   - Every destructive action calls ``confirmModal({title, html,
 *     dangerLabel})`` BEFORE the actual POST/DELETE. The modal preview is
 *     populated from a /preview endpoint when one exists, otherwise from
 *     the row counts visible in the table.
 */

const $ = (id) => document.getElementById(id);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

// ── Helpers ────────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  const headers = Object.assign(
    { 'Content-Type': 'application/json' },
    opts.headers || {},
  );
  const r = await fetch(path, Object.assign(
    { credentials: 'same-origin', cache: 'no-store', headers },
    opts,
  ));
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    if (r.status === 401) {
      location.href = '/login.html';
      throw new Error('redirecting to login');
    }
    throw new Error(body.detail || body.error || r.statusText);
  }
  return body;
}

function fmtTime(v) {
  if (!v) return '-';
  try { return new Date(v).toLocaleString('ko-KR'); } catch { return v; }
}

function fmtNum(v) {
  if (v == null) return '-';
  return Number(v).toLocaleString('ko-KR');
}

function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function toast(msg, kind = 'ok') {
  const wrap = $('toastRoot');
  const el = document.createElement('div');
  el.className = `toast ${kind === 'err' ? 'err' : kind === 'ok' ? 'ok' : ''}`;
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

function setNotice(id, msg, isError = false) {
  const el = $(id);
  if (!el) return;
  el.textContent = msg || '';
  el.style.color = isError ? '#b91c1c' : '#047857';
}

// ── Modal ──────────────────────────────────────────────────────────────
function confirmModal({ title, html, confirmLabel = '실행', danger = false, extraInputs = [] }) {
  return new Promise((resolve) => {
    const root = $('modalRoot');
    root.innerHTML = `
      <div class="modal-back">
        <div class="modal">
          <div class="modal-head"><h3>${esc(title)}</h3>
            <button class="btn xs" data-act="x">닫기</button></div>
          <div class="modal-body">${html}</div>
          <div class="modal-foot">
            <button class="btn" data-act="cancel">취소</button>
            <button class="btn ${danger ? 'danger' : 'primary'}" data-act="ok">${esc(confirmLabel)}</button>
          </div>
        </div>
      </div>`;
    function close(result) {
      root.innerHTML = '';
      resolve(result);
    }
    root.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-act]');
      if (!btn) return;
      if (btn.dataset.act === 'ok') {
        const vals = {};
        for (const k of extraInputs) {
          const input = root.querySelector(`[data-input="${k}"]`);
          vals[k] = input ? input.value : '';
        }
        close({ ok: true, values: vals });
      } else {
        close({ ok: false });
      }
    }, { once: false });
  });
}

// ── Auth boot ──────────────────────────────────────────────────────────
let CURRENT_USER = null;

async function boot() {
  const me = await api('/api/tool/auth/me');
  if (!me.user) {
    location.href = '/login.html';
    return;
  }
  if (!me.user.isAdmin) {
    document.body.innerHTML = '<div class="empty">관리자 권한이 필요합니다.</div>';
    return;
  }
  CURRENT_USER = me.user;
  $('meLabel').textContent = `${me.user.username} (#${me.user.id})`;
  $('logoutBtn').addEventListener('click', async () => {
    try { await api('/api/tool/auth/logout', { method: 'POST' }); } catch (e) {}
    location.href = '/login.html';
  });
  bindTabs();
  tabs.users.load().catch((e) => setNotice('noticeUsers', e.message, true));
}

// ── Tab switching ──────────────────────────────────────────────────────
function bindTabs() {
  $$('.app-nav .tab').forEach((btn) => {
    btn.addEventListener('click', () => {
      const name = btn.dataset.tab;
      $$('.app-nav .tab').forEach((b) => b.classList.toggle('active', b === btn));
      $$('.pane').forEach((p) => p.classList.toggle('active', p.id === `pane-${name}`));
      const t = tabs[name];
      if (t && !t.loaded) t.load().then(() => { t.loaded = true; })
        .catch((e) => toast(e.message, 'err'));
      else if (t && t.refresh) t.refresh();
    });
  });
}

// ── Tabs ───────────────────────────────────────────────────────────────
const tabs = {};

// ── Users ──────────────────────────────────────────────────────────────
tabs.users = {
  state: { users: [], selectedId: null, detail: null },

  async load() {
    await this.reload();
    $('refreshUsers').addEventListener('click', () => this.reload().catch((e) => toast(e.message, 'err')));
    $('userSearch').addEventListener('input', () => this.render());
    $('userCreateForm').addEventListener('submit', (e) => this.createUser(e));
    $('userRows').addEventListener('click', (e) => {
      const tr = e.target.closest('tr[data-id]');
      if (tr) this.select(Number(tr.dataset.id)).catch((e) => toast(e.message, 'err'));
    });
    this.loaded = true;
  },

  async reload() {
    const res = await api('/api/tool/users');
    this.state.users = res.users || [];
    this.render();
    // Keep selection if possible.
    if (this.state.selectedId && this.state.users.some((u) => u.id === this.state.selectedId)) {
      await this.select(this.state.selectedId);
    }
  },

  render() {
    const q = ($('userSearch').value || '').toLowerCase().trim();
    const users = this.state.users.filter((u) =>
      !q || u.username.toLowerCase().includes(q) ||
      (u.displayName || '').toLowerCase().includes(q));
    $('userCount').textContent = `${users.length}명`;
    $('userRows').innerHTML = users.map((u) => `
      <tr data-id="${u.id}" class="${u.id === this.state.selectedId ? 'row-selected' : ''}">
        <td class="mono">#${u.id}</td>
        <td><div><b>${esc(u.username)}</b></div>
            <div class="muted">${esc(u.displayName || u.username)}</div></td>
        <td class="num">${fmtNum(u.favorites)}</td>
        <td class="num">${fmtNum(u.sessions)}</td>
        <td>${u.isAdmin ? '<span class="chip indigo">admin</span> ' : ''}
            ${u.isActive ? '<span class="chip green">active</span>'
                          : '<span class="chip red">inactive</span>'}</td>
      </tr>`).join('') || '<tr><td colspan="5" class="empty">계정 없음</td></tr>';
  },

  async select(id) {
    this.state.selectedId = id;
    this.render();
    const me = CURRENT_USER;
    $('userDetailTitle').textContent = '불러오는 중...';
    $('userDetailSub').textContent = '';
    const u = this.state.users.find((x) => x.id === id);
    if (!u) return;
    const [sessRes] = await Promise.all([
      api(`/api/tool/users/${id}/sessions`),
    ]);
    this.state.detail = { user: u, sessions: sessRes.sessions || [] };
    $('userDetailTitle').textContent = u.username;
    $('userDetailSub').textContent = `생성 ${fmtTime(u.createdAt)} · 마지막 로그인 ${fmtTime(u.lastLoginAt)}`;
    $('userDetail').innerHTML = this.renderDetail(u, sessRes.sessions);
    $('userDetail').addEventListener('click', (e) => this.handleDetailClick(e, id));
    $('userDetail').addEventListener('submit', (e) => this.handleDetailSubmit(e, id));
  },

  renderDetail(u, sessions) {
    const isSelf = CURRENT_USER && CURRENT_USER.id === u.id;
    return `
      <form id="userPatchForm" class="grid-2">
        <label class="field">표시 이름<input name="display_name" value="${esc(u.displayName || '')}"></label>
        <div class="field" style="justify-content:flex-end">
          <label><input name="is_admin" type="checkbox" ${u.isAdmin ? 'checked' : ''} ${isSelf ? 'disabled' : ''}> 관리자</label>
          <label><input name="is_active" type="checkbox" ${u.isActive ? 'checked' : ''} ${isSelf ? 'disabled' : ''}> 활성</label>
        </div>
        <div style="grid-column:1/-1;display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn primary" type="submit">변경 저장</button>
          <button class="btn" type="button" data-act="reset-pw">비밀번호 재설정</button>
          <button class="btn" type="button" data-act="kill-sessions">세션 전부 종료</button>
          <button class="btn danger" type="button" data-act="delete-user" ${isSelf ? 'disabled' : ''}>계정 삭제</button>
        </div>
      </form>
      <h3 style="margin:18px 0 8px;font-size:13px">활성 세션 (${sessions.length})</h3>
      <div class="table-wrap" style="max-height:240px">
        <table class="table">
          <thead><tr><th>토큰</th><th>마지막 활동</th><th>만료</th><th>IP</th><th>UA</th></tr></thead>
          <tbody>${sessions.map((s) => `
            <tr>
              <td class="mono">${esc(s.id)}${s.isDbtool ? ' <span class="chip indigo">tool</span>' : ''}</td>
              <td>${fmtTime(s.lastSeenAt)}</td>
              <td>${fmtTime(s.expiresAt)}</td>
              <td>${esc(s.ip || '-')}</td>
              <td class="ellipsis">${esc(s.userAgent || '-')}</td>
            </tr>`).join('') || '<tr><td colspan="5" class="empty">활성 세션 없음</td></tr>'}
          </tbody>
        </table>
      </div>`;
  },

  async handleDetailClick(e, id) {
    const btn = e.target.closest('button[data-act]');
    if (!btn) return;
    const act = btn.dataset.act;
    try {
      if (act === 'reset-pw') {
        const m = await confirmModal({
          title: '비밀번호 재설정',
          html: `<div class="warn">이 사용자의 모든 세션이 즉시 종료됩니다.</div>
                 <label class="field">새 비밀번호 (6자 이상)
                   <input type="password" data-input="password" required minlength="6">
                 </label>`,
          confirmLabel: '재설정',
          extraInputs: ['password'],
        });
        if (!m.ok || !m.values.password) return;
        if (m.values.password.length < 6) return toast('6자 이상이어야 합니다', 'err');
        const r = await api(`/api/tool/users/${id}/reset-password`, {
          method: 'POST',
          body: JSON.stringify({ password: m.values.password }),
        });
        toast(`재설정 완료 — ${r.sessionsKilled}개 세션 종료`);
      } else if (act === 'kill-sessions') {
        const m = await confirmModal({
          title: '활성 세션 전부 종료',
          html: '<div class="warn">사용자가 다시 로그인해야 합니다.</div>',
          confirmLabel: '종료',
        });
        if (!m.ok) return;
        const r = await api(`/api/tool/users/${id}/kill-sessions`, { method: 'POST' });
        toast(`${r.sessionsKilled}개 세션 종료`);
      } else if (act === 'delete-user') {
        const previewRes = await api(`/api/tool/users/${id}/delete-preview`);
        const u = this.state.users.find((x) => x.id === id);
        const m = await confirmModal({
          title: `${u.username} 계정 삭제`,
          html: `
            <div class="danger">이 작업은 되돌릴 수 없습니다.</div>
            <div class="preview-counts">
              <div class="box"><b>${fmtNum(previewRes.favorites)}</b><span>찜</span></div>
              <div class="box"><b>${fmtNum(previewRes.favoriteDeleted)}</b><span>찜 삭제 tombstone</span></div>
              <div class="box"><b>${fmtNum(previewRes.sessions)}</b><span>세션</span></div>
              <div class="box"><b>${fmtNum(previewRes.userWebhooks)}</b><span>웹훅</span></div>
              <div class="box"><b>${fmtNum(previewRes.filterPreferences)}</b><span>UI 필터</span></div>
              <div class="box"><b>${fmtNum(previewRes.photoFiles)}</b><span>사진 파일</span></div>
            </div>
            <p class="muted">사진 디렉터리 <code>${esc(previewRes.photoDir)}</code> 도 함께 삭제됩니다.</p>
            <label class="field" style="margin-top:10px">확인을 위해 아이디 <b>${esc(u.username)}</b>를 입력
              <input type="text" data-input="confirm_username" placeholder="${esc(u.username)}" required>
            </label>`,
          confirmLabel: '영구 삭제',
          danger: true,
          extraInputs: ['confirm_username'],
        });
        if (!m.ok) return;
        await api(`/api/tool/users/${id}`, {
          method: 'DELETE',
          body: JSON.stringify({ confirm_username: m.values.confirm_username }),
        });
        toast('계정 삭제 완료');
        this.state.selectedId = null;
        $('userDetail').innerHTML = '<div class="empty">왼쪽 목록에서 계정을 선택하세요.</div>';
        await this.reload();
      }
      await this.reload();
    } catch (ex) { toast(ex.message, 'err'); }
  },

  async handleDetailSubmit(e, id) {
    if (e.target.id !== 'userPatchForm') return;
    e.preventDefault();
    try {
      const fd = new FormData(e.target);
      await api(`/api/tool/users/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({
          display_name: fd.get('display_name') || null,
          is_admin: fd.has('is_admin'),
          is_active: fd.has('is_active'),
        }),
      });
      toast('저장 완료');
      await this.reload();
    } catch (ex) { toast(ex.message, 'err'); }
  },

  async createUser(e) {
    e.preventDefault();
    const form = e.target;
    const fd = new FormData(form);
    try {
      await api('/api/tool/users', {
        method: 'POST',
        body: JSON.stringify({
          username: fd.get('username'),
          password: fd.get('password'),
          display_name: fd.get('display_name') || null,
          is_admin: fd.has('is_admin'),
        }),
      });
      form.reset();
      toast('계정 생성 완료');
      await this.reload();
    } catch (ex) { toast(ex.message, 'err'); }
  },

  refresh() { /* called on tab switch */ },
};

// ── Favorites / Dislikes ──────────────────────────────────────────────
// Likes and dislikes share the same favorites table — entry_json.kind
// disambiguates them. The tab keeps a single grid but adds a kind filter
// so an operator can move "just dislikes" between accounts without
// touching the likes set, and vice versa.
tabs.favorites = {
  state: { favs: [], selected: new Set(), counts: { likes: 0, dislikes: 0 } },

  async load() {
    await this.refreshUsers();
    $('favReload').addEventListener('click', () => this.reload().catch((e) => toast(e.message, 'err')));
    $('favUserSelect').addEventListener('change', () => this.reload().catch((e) => toast(e.message, 'err')));
    $('favKindFilter').addEventListener('change', () => this.reload().catch((e) => toast(e.message, 'err')));
    $('favSelectAll').addEventListener('change', (e) => this.toggleAll(e.target.checked));
    $('favRows').addEventListener('change', (e) => {
      const cb = e.target.closest('input[type=checkbox][data-key]');
      if (!cb) return;
      if (cb.checked) this.state.selected.add(cb.dataset.key);
      else this.state.selected.delete(cb.dataset.key);
    });
    $('favTransferBtn').addEventListener('click', () => this.transferSelected().catch((e) => toast(e.message, 'err')));
    $('favTransferAllBtn').addEventListener('click', () => this.transferAll().catch((e) => toast(e.message, 'err')));
    $('favBulkDeleteBtn').addEventListener('click', () => this.bulkDelete(false).catch((e) => toast(e.message, 'err')));
    $('favDeleteAllBtn').addEventListener('click', () => this.bulkDelete(true).catch((e) => toast(e.message, 'err')));
    this.loaded = true;
  },

  refresh() { this.refreshUsers().catch((e) => toast(e.message, 'err')); },

  async refreshUsers() {
    const res = await api('/api/tool/users');
    const users = res.users || [];
    const sel = $('favUserSelect');
    sel.innerHTML = users.map((u) =>
      `<option value="${u.id}">#${u.id} ${esc(u.username)} (찜 ${u.favorites})</option>`,
    ).join('');
    if (users.length) await this.reload();
  },

  async reload() {
    const userId = Number($('favUserSelect').value);
    if (!userId) return;
    const params = new URLSearchParams({
      user_id: String(userId),
      limit: '500',
      kind: $('favKindFilter').value || 'all',
    });
    const src = $('favSourceFilter').value;
    if (src) params.set('source', src);
    const q = $('favSearch').value.trim();
    if (q) params.set('q', q);
    $('favStatus').textContent = '불러오는 중...';
    const res = await api(`/api/tool/favorites?${params}`);
    this.state.favs = res.favorites || [];
    this.state.counts = res.counts || { likes: 0, dislikes: 0 };
    this.state.selected.clear();
    $('favSelectAll').checked = false;
    $('favRows').innerHTML = this.state.favs.map((f) => `
      <tr>
        <td><input type="checkbox" data-key="${esc(f.key)}"></td>
        <td>${kindChip(f.kind)}</td>
        <td class="mono ellipsis">${esc(f.key)}</td>
        <td><span class="chip">${esc(f.source || '-')}</span></td>
        <td class="mono">${esc(f.listingNo || '-')}</td>
        <td class="ellipsis">${esc((f.entry && f.entry.title) || '-')}</td>
        <td class="muted">${fmtTime(f.savedAt)}</td>
      </tr>`).join('') || '<tr><td colspan="7" class="empty">표시할 항목이 없습니다.</td></tr>';
    $('favStatus').textContent =
      `${this.state.favs.length}건 표시 · 사용자 합계 좋아요 ${this.state.counts.likes} / 싫어요 ${this.state.counts.dislikes}`;
  },

  toggleAll(check) {
    this.state.selected.clear();
    $$('input[type=checkbox][data-key]', $('favRows')).forEach((cb) => {
      cb.checked = check;
      if (check) this.state.selected.add(cb.dataset.key);
    });
  },

  // Common dialog for both selected-rows and filter-scope transfers. The
  // ``keys`` param is null for "everything matching the current filter"
  // and an array for selected rows. ``kind`` is passed through so the
  // server-side scope matches what the operator sees in the table.
  async _runTransfer(keys, kind, scopeLabel) {
    const userId = Number($('favUserSelect').value);
    const users = (await api('/api/tool/users')).users || [];
    const opts = users.filter((u) => u.id !== userId)
      .map((u) => `<option value="${u.id}">#${u.id} ${esc(u.username)}</option>`).join('');
    if (!opts) return toast('이전할 다른 사용자가 없습니다', 'err');
    const m = await confirmModal({
      title: scopeLabel,
      html: `
        <label class="field">대상 사용자
          <select data-input="to">${opts}</select>
        </label>
        <label class="field">충돌 시
          <select data-input="conflict">
            <option value="skip">skip (대상에 있으면 그대로)</option>
            <option value="overwrite">overwrite (덮어쓰기)</option>
          </select>
        </label>
        <label class="field">방식
          <select data-input="mode">
            <option value="copy">copy (원본 유지)</option>
            <option value="move">move (원본 삭제)</option>
          </select>
        </label>
        <div class="warn">미리보기 후 실행 확인 모달이 한 번 더 뜹니다.</div>`,
      confirmLabel: '미리보기',
      extraInputs: ['to', 'conflict', 'mode'],
    });
    if (!m.ok) return;
    const body = {
      from_user_id: userId,
      to_user_id: Number(m.values.to),
      keys: keys || null,
      mode: m.values.mode,
      on_conflict: m.values.conflict,
      kind,
    };
    const plan = await api('/api/tool/favorites/transfer/preview', {
      method: 'POST', body: JSON.stringify(body),
    });
    const c = await confirmModal({
      title: '이전 실행 확인',
      html: `
        <p class="muted">종류: <b>${esc(kind)}</b></p>
        <div class="preview-counts">
          <div class="box"><b>${fmtNum(plan.source)}</b><span>원본</span></div>
          <div class="box"><b>${fmtNum(plan.kindBreakdown.likes)}</b><span>좋아요</span></div>
          <div class="box"><b>${fmtNum(plan.kindBreakdown.dislikes)}</b><span>싫어요</span></div>
          <div class="box"><b>${fmtNum(plan.conflictsOnTarget)}</b><span>충돌</span></div>
          <div class="box"><b>${fmtNum(plan.wouldCopy)}</b><span>복사 예정</span></div>
          <div class="box"><b>${fmtNum(plan.wouldOverwrite)}</b><span>덮어쓰기</span></div>
          <div class="box"><b>${fmtNum(plan.wouldDeleteSource)}</b><span>원본 삭제</span></div>
        </div>`,
      confirmLabel: '실행',
      danger: true,
    });
    if (!c.ok) return;
    const r = await api('/api/tool/favorites/transfer', {
      method: 'POST', body: JSON.stringify(body),
    });
    toast(`복사 ${r.copied}건, 원본 삭제 ${r.deletedSource}건`);
    await this.reload();
  },

  async transferSelected() {
    const keys = Array.from(this.state.selected);
    if (!keys.length) return toast('항목을 선택하세요', 'err');
    // For selected rows we send kind='all' so a mixed selection still
    // works — the keys themselves bound the scope.
    await this._runTransfer(keys, 'all', `선택 ${keys.length}개 이전`);
  },

  async transferAll() {
    const kind = $('favKindFilter').value || 'all';
    const label = kind === 'like' ? '좋아요 전체 이전'
      : kind === 'dislike' ? '싫어요 전체 이전'
      : '좋아요+싫어요 전체 이전';
    await this._runTransfer(null, kind, label);
  },

  async bulkDelete(all) {
    const userId = Number($('favUserSelect').value);
    const kind = $('favKindFilter').value || 'all';
    const keys = all ? null : Array.from(this.state.selected);
    if (!all && !keys.length) return toast('항목을 선택하세요', 'err');
    // Selected-rows delete: the keys bound the scope, so server-side kind
    // stays 'all' to avoid an unexpected filter dropping rows the
    // operator visibly checked. Filter-scope delete uses the active kind.
    const effectiveKind = all ? kind : 'all';
    const body = { user_id: userId, keys, all, kind: effectiveKind };
    const plan = await api('/api/tool/favorites/bulk-delete/preview', {
      method: 'POST', body: JSON.stringify(body),
    });
    const scopeLabel = all
      ? (effectiveKind === 'like' ? '이 사용자의 좋아요 전체'
        : effectiveKind === 'dislike' ? '이 사용자의 싫어요 전체'
        : '이 사용자의 좋아요+싫어요 전체')
      : `선택 ${keys.length}개`;
    const m = await confirmModal({
      title: '삭제 확인',
      html: `
        <div class="${all ? 'danger' : 'warn'}">
          ${scopeLabel}을 삭제합니다.
        </div>
        <div class="preview-counts">
          <div class="box"><b>${fmtNum(plan.wouldDelete)}</b><span>삭제 예정</span></div>
          <div class="box"><b>${fmtNum(plan.kindBreakdown.likes)}</b><span>좋아요</span></div>
          <div class="box"><b>${fmtNum(plan.kindBreakdown.dislikes)}</b><span>싫어요</span></div>
        </div>
        <p class="muted">tombstone이 함께 기록되어 브라우저 캐시가 복원하지 않습니다.</p>`,
      confirmLabel: `${plan.wouldDelete}건 삭제`,
      danger: true,
    });
    if (!m.ok) return;
    const r = await api('/api/tool/favorites/bulk-delete', {
      method: 'POST', body: JSON.stringify(body),
    });
    toast(`${r.deleted}건 삭제`);
    await this.reload();
  },
};

function kindChip(kind) {
  if (kind === 'dislike') return '<span class="chip red">👎 싫어요</span>';
  return '<span class="chip indigo">❤️ 좋아요</span>';
}

// ── Bookmarks ──────────────────────────────────────────────────────────
// Same shape as favorites minus the like/dislike axis. sort_order is the
// user's visit order; transfers carry it verbatim so "1번 본 방, 2번 본
// 방" labels survive an account-restore. Tombstone semantics mirror the
// favorites tab so client localStorage merges behave identically.
tabs.bookmarks = {
  state: { bms: [], selected: new Set(), total: 0 },

  async load() {
    await this.refreshUsers();
    $('bmReload').addEventListener('click', () => this.reload().catch((e) => toast(e.message, 'err')));
    $('bmUserSelect').addEventListener('change', () => this.reload().catch((e) => toast(e.message, 'err')));
    $('bmSelectAll').addEventListener('change', (e) => this.toggleAll(e.target.checked));
    $('bmRows').addEventListener('change', (e) => {
      const cb = e.target.closest('input[type=checkbox][data-key]');
      if (!cb) return;
      if (cb.checked) this.state.selected.add(cb.dataset.key);
      else this.state.selected.delete(cb.dataset.key);
    });
    $('bmTransferBtn').addEventListener('click', () => this.transferSelected().catch((e) => toast(e.message, 'err')));
    $('bmTransferAllBtn').addEventListener('click', () => this.transferAll().catch((e) => toast(e.message, 'err')));
    $('bmBulkDeleteBtn').addEventListener('click', () => this.bulkDelete(false).catch((e) => toast(e.message, 'err')));
    $('bmDeleteAllBtn').addEventListener('click', () => this.bulkDelete(true).catch((e) => toast(e.message, 'err')));
    this.loaded = true;
  },

  refresh() { this.refreshUsers().catch((e) => toast(e.message, 'err')); },

  async refreshUsers() {
    const res = await api('/api/tool/users');
    const users = res.users || [];
    const sel = $('bmUserSelect');
    sel.innerHTML = users.map((u) =>
      `<option value="${u.id}">#${u.id} ${esc(u.username)}</option>`,
    ).join('');
    if (users.length) await this.reload();
  },

  async reload() {
    const userId = Number($('bmUserSelect').value);
    if (!userId) return;
    const params = new URLSearchParams({
      user_id: String(userId), limit: '500',
    });
    const src = $('bmSourceFilter').value;
    if (src) params.set('source', src);
    const q = $('bmSearch').value.trim();
    if (q) params.set('q', q);
    $('bmStatus').textContent = '불러오는 중...';
    const res = await api(`/api/tool/bookmarks?${params}`);
    this.state.bms = res.bookmarks || [];
    this.state.total = res.total || 0;
    this.state.selected.clear();
    $('bmSelectAll').checked = false;
    $('bmRows').innerHTML = this.state.bms.map((b) => `
      <tr>
        <td><input type="checkbox" data-key="${esc(b.key)}"></td>
        <td class="num"><b>${b.sortOrder}</b></td>
        <td class="mono ellipsis">${esc(b.key)}</td>
        <td><span class="chip">${esc(b.source || '-')}</span></td>
        <td class="mono">${esc(b.listingNo || '-')}</td>
        <td class="ellipsis">${esc((b.entry && b.entry.title) || '-')}</td>
        <td class="muted">${fmtTime(b.savedAt)}</td>
      </tr>`).join('') || '<tr><td colspan="7" class="empty">북마크가 없습니다.</td></tr>';
    $('bmStatus').textContent = `${this.state.bms.length}건 표시 · 사용자 합계 ${this.state.total}`;
  },

  toggleAll(check) {
    this.state.selected.clear();
    $$('input[type=checkbox][data-key]', $('bmRows')).forEach((cb) => {
      cb.checked = check;
      if (check) this.state.selected.add(cb.dataset.key);
    });
  },

  async _runTransfer(keys, scopeLabel) {
    const userId = Number($('bmUserSelect').value);
    const users = (await api('/api/tool/users')).users || [];
    const opts = users.filter((u) => u.id !== userId)
      .map((u) => `<option value="${u.id}">#${u.id} ${esc(u.username)}</option>`).join('');
    if (!opts) return toast('이전할 다른 사용자가 없습니다', 'err');
    const m = await confirmModal({
      title: scopeLabel,
      html: `
        <label class="field">대상 사용자
          <select data-input="to">${opts}</select>
        </label>
        <label class="field">충돌 시
          <select data-input="conflict">
            <option value="skip">skip (대상에 있으면 그대로)</option>
            <option value="overwrite">overwrite (덮어쓰기)</option>
          </select>
        </label>
        <label class="field">방식
          <select data-input="mode">
            <option value="copy">copy (원본 유지)</option>
            <option value="move">move (원본 삭제)</option>
          </select>
        </label>
        <div class="warn">sort_order는 원본 값을 그대로 가져갑니다. 대상의 기존 번호와 겹치면 그대로 표시됩니다.</div>`,
      confirmLabel: '미리보기',
      extraInputs: ['to', 'conflict', 'mode'],
    });
    if (!m.ok) return;
    const body = {
      from_user_id: userId,
      to_user_id: Number(m.values.to),
      keys: keys || null,
      mode: m.values.mode,
      on_conflict: m.values.conflict,
    };
    const plan = await api('/api/tool/bookmarks/transfer/preview', {
      method: 'POST', body: JSON.stringify(body),
    });
    const c = await confirmModal({
      title: '이전 실행 확인',
      html: `
        <div class="preview-counts">
          <div class="box"><b>${fmtNum(plan.source)}</b><span>원본</span></div>
          <div class="box"><b>${fmtNum(plan.conflictsOnTarget)}</b><span>충돌</span></div>
          <div class="box"><b>${fmtNum(plan.wouldCopy)}</b><span>복사 예정</span></div>
          <div class="box"><b>${fmtNum(plan.wouldOverwrite)}</b><span>덮어쓰기</span></div>
          <div class="box"><b>${fmtNum(plan.wouldDeleteSource)}</b><span>원본 삭제</span></div>
        </div>`,
      confirmLabel: '실행',
      danger: true,
    });
    if (!c.ok) return;
    const r = await api('/api/tool/bookmarks/transfer', {
      method: 'POST', body: JSON.stringify(body),
    });
    toast(`복사 ${r.copied}건, 원본 삭제 ${r.deletedSource}건`);
    await this.reload();
  },

  async transferSelected() {
    const keys = Array.from(this.state.selected);
    if (!keys.length) return toast('항목을 선택하세요', 'err');
    await this._runTransfer(keys, `선택 ${keys.length}개 북마크 이전`);
  },

  async transferAll() {
    await this._runTransfer(null, '필터 결과 북마크 전체 이전');
  },

  async bulkDelete(all) {
    const userId = Number($('bmUserSelect').value);
    const keys = all ? null : Array.from(this.state.selected);
    if (!all && !keys.length) return toast('항목을 선택하세요', 'err');
    const body = { user_id: userId, keys, all };
    const plan = await api('/api/tool/bookmarks/bulk-delete/preview', {
      method: 'POST', body: JSON.stringify(body),
    });
    const scopeLabel = all ? '이 사용자의 북마크 전체' : `선택 ${keys.length}개`;
    const m = await confirmModal({
      title: '북마크 삭제 확인',
      html: `
        <div class="${all ? 'danger' : 'warn'}">
          ${scopeLabel}을 삭제합니다.
        </div>
        <div class="preview-counts">
          <div class="box"><b>${fmtNum(plan.wouldDelete)}</b><span>삭제 예정</span></div>
        </div>
        <p class="muted">tombstone이 함께 기록되어 브라우저 캐시가 복원하지 않습니다.</p>`,
      confirmLabel: `${plan.wouldDelete}건 삭제`,
      danger: true,
    });
    if (!m.ok) return;
    const r = await api('/api/tool/bookmarks/bulk-delete', {
      method: 'POST', body: JSON.stringify(body),
    });
    toast(`${r.deleted}건 삭제`);
    await this.reload();
  },
};

// ── Listings ───────────────────────────────────────────────────────────
tabs.listings = {
  state: { meta: null, listings: [], selected: new Set(), total: 0, offset: 0, limit: 100 },

  async load() {
    this.state.meta = await api('/api/tool/listings/meta');
    const platformSel = $('listPlatform');
    platformSel.innerHTML = '<option value="">(전체 플랫폼)</option>' +
      this.state.meta.platforms.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join('');
    const regionSel = $('listRegion');
    regionSel.innerHTML = '<option value="">(전체 지역)</option>' +
      this.state.meta.regions.map((r) => `<option value="${r.id}">${esc(r.slug)} (${esc(r.status)})</option>`).join('');
    const statusSel = $('listStatus');
    statusSel.innerHTML = '<option value="">(전체 상태)</option>' +
      this.state.meta.statuses.map((s) => `<option value="${s}">${s}</option>`).join('');

    $('listReload').addEventListener('click', () => this.reload().catch((e) => toast(e.message, 'err')));
    $('listSelectAll').addEventListener('change', (e) => this.toggleAll(e.target.checked));
    $('listRows').addEventListener('change', (e) => {
      const cb = e.target.closest('input[type=checkbox][data-id]');
      if (!cb) return;
      const id = Number(cb.dataset.id);
      if (cb.checked) this.state.selected.add(id);
      else this.state.selected.delete(id);
    });
    $('listRows').addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-act]');
      if (!btn) return;
      const id = Number(btn.dataset.id);
      if (btn.dataset.act === 'edit-status') this.editStatus(id).catch((e) => toast(e.message, 'err'));
      else if (btn.dataset.act === 'view') this.viewDetail(id).catch((e) => toast(e.message, 'err'));
    });
    $('listBulkStatusBtn').addEventListener('click', () => this.bulkStatus().catch((e) => toast(e.message, 'err')));

    await this.reload();
    this.loaded = true;
  },

  async reload(offset = 0) {
    this.state.offset = offset;
    const params = new URLSearchParams({ limit: String(this.state.limit), offset: String(offset) });
    const pf = $('listPlatform').value;
    const rg = $('listRegion').value;
    const st = $('listStatus').value;
    const q = $('listSearch').value.trim();
    if (pf) params.set('platform_id', pf);
    if (rg) params.set('region_id', rg);
    if (st) params.set('status', st);
    if (q) params.set('q', q);
    $('listStatusMsg').textContent = '불러오는 중...';
    const res = await api(`/api/tool/listings?${params}`);
    this.state.listings = res.listings || [];
    this.state.total = res.total;
    this.state.selected.clear();
    $('listSelectAll').checked = false;
    $('listRows').innerHTML = this.state.listings.map((l) => `
      <tr>
        <td><input type="checkbox" data-id="${l.id}"></td>
        <td class="mono">#${l.id}</td>
        <td><span class="chip">${esc(l.platformCode)}</span></td>
        <td class="mono ellipsis">${esc(l.platformListingId)}</td>
        <td>${statusChip(l.currentStatus)}</td>
        <td class="num">${l.missCount}</td>
        <td class="muted">${fmtTime(l.lastSeenAt)}</td>
        <td class="num">${l.snapshotCount}</td>
        <td>
          <button class="btn xs" data-act="view" data-id="${l.id}">상세</button>
          <button class="btn xs" data-act="edit-status" data-id="${l.id}">상태</button>
        </td>
      </tr>`).join('') || '<tr><td colspan="9" class="empty">매물 없음</td></tr>';
    $('listStatusMsg').textContent = `${res.total}건 중 ${res.offset + 1}–${res.offset + this.state.listings.length}`;
    $('listPager').innerHTML = this.renderPager();
    $('listPager').addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-page]');
      if (!btn) return;
      this.reload(Number(btn.dataset.page)).catch((e) => toast(e.message, 'err'));
    }, { once: true });
  },

  renderPager() {
    const { total, offset, limit } = this.state;
    if (total <= limit) return '';
    const prev = Math.max(0, offset - limit);
    const next = offset + limit;
    return `
      <button class="btn sm" data-page="${prev}" ${offset === 0 ? 'disabled' : ''}>이전</button>
      <button class="btn sm" data-page="${next}" ${next >= total ? 'disabled' : ''}>다음</button>`;
  },

  toggleAll(check) {
    this.state.selected.clear();
    $$('input[type=checkbox][data-id]', $('listRows')).forEach((cb) => {
      cb.checked = check;
      if (check) this.state.selected.add(Number(cb.dataset.id));
    });
  },

  async editStatus(id) {
    const opts = this.state.meta.statuses.map((s) => `<option value="${s}">${s}</option>`).join('');
    const m = await confirmModal({
      title: `매물 #${id} 상태 변경`,
      html: `
        <label class="field">새 상태<select data-input="status">${opts}</select></label>
        <div class="warn">'active'로 되돌리면 miss_count는 0으로 리셋되고 reappeared_at이 기록됩니다.</div>`,
      confirmLabel: '저장',
      extraInputs: ['status'],
    });
    if (!m.ok) return;
    const r = await api(`/api/tool/listings/${id}/status`, {
      method: 'PATCH',
      body: JSON.stringify({ current_status: m.values.status }),
    });
    toast(`#${id} → ${r.currentStatus}`);
    await this.reload(this.state.offset);
  },

  async bulkStatus() {
    const ids = Array.from(this.state.selected);
    if (!ids.length) return toast('항목을 선택하세요', 'err');
    const opts = this.state.meta.statuses.map((s) => `<option value="${s}">${s}</option>`).join('');
    const m = await confirmModal({
      title: `선택 ${ids.length}개 상태 일괄 변경`,
      html: `<label class="field">새 상태<select data-input="status">${opts}</select></label>
             <div class="warn">미리보기 후 실행 확인 모달이 한 번 더 뜹니다.</div>`,
      confirmLabel: '미리보기',
      extraInputs: ['status'],
    });
    if (!m.ok) return;
    const body = { listing_ids: ids, current_status: m.values.status };
    const plan = await api('/api/tool/listings/bulk-status/preview', {
      method: 'POST', body: JSON.stringify(body),
    });
    const breakdown = Object.entries(plan.currentBreakdown)
      .map(([k, v]) => `<span class="chip gray">${esc(k)}: ${v}</span>`).join(' ');
    const c = await confirmModal({
      title: '일괄 변경 실행',
      html: `
        <div class="preview-counts">
          <div class="box"><b>${fmtNum(plan.requested)}</b><span>요청</span></div>
          <div class="box"><b>${fmtNum(plan.found)}</b><span>발견</span></div>
          <div class="box"><b>${fmtNum(plan.missing)}</b><span>없음</span></div>
        </div>
        <p>현재 상태 분포: ${breakdown}</p>
        <p>→ <b>${esc(plan.willTarget)}</b> 로 변경합니다.</p>`,
      confirmLabel: '실행',
      danger: true,
    });
    if (!c.ok) return;
    const r = await api('/api/tool/listings/bulk-status', {
      method: 'POST', body: JSON.stringify(body),
    });
    toast(`${r.updated}건 변경`);
    await this.reload(this.state.offset);
  },

  async viewDetail(id) {
    const d = await api(`/api/tool/listings/${id}`);
    const l = d.listing;
    const html = `
      <dl class="detail-grid">
        <dt>ID</dt><dd>${l.id}</dd>
        <dt>플랫폼</dt><dd>${esc(l.platformCode)} / ${esc(l.platformListingId)}</dd>
        <dt>URL</dt><dd><a href="${esc(l.sourceUrl || '#')}" target="_blank">${esc(l.sourceUrl || '-')}</a></dd>
        <dt>상태</dt><dd>${statusChip(l.currentStatus)} (miss ${l.missCount})</dd>
        <dt>처음 본 시각</dt><dd>${fmtTime(l.firstSeenAt)}</dd>
        <dt>마지막 본 시각</dt><dd>${fmtTime(l.lastSeenAt)}</dd>
        <dt>제거 시각</dt><dd>${fmtTime(l.removedAt)}</dd>
        <dt>재등장 시각</dt><dd>${fmtTime(l.reappearedAt)}</dd>
      </dl>
      <h4 style="margin:14px 0 6px">지역별 상태</h4>
      <div class="table-wrap" style="max-height:160px">
        <table class="table">
          <thead><tr><th>슬러그</th><th>상태</th><th>miss</th><th>마지막</th></tr></thead>
          <tbody>${d.regions.map((r) => `
            <tr><td><b>${esc(r.slug)}</b> ${esc(r.regionName)}</td>
                <td>${statusChip(r.currentStatus)}</td>
                <td class="num">${r.missCount}</td>
                <td>${fmtTime(r.lastSeenAt)}</td></tr>`).join('')
              || '<tr><td colspan="4" class="empty">없음</td></tr>'}</tbody>
        </table>
      </div>
      <h4 style="margin:14px 0 6px">최근 스냅샷 (${d.snapshots.length})</h4>
      <div class="table-wrap" style="max-height:200px">
        <table class="table">
          <thead><tr><th>시각</th><th>해시</th><th>제목</th><th>보증금/월세/관리비</th></tr></thead>
          <tbody>${d.snapshots.map((s) => `
            <tr><td class="muted">${fmtTime(s.capturedAt)}</td>
                <td class="mono">${esc(s.contentHash)}</td>
                <td class="ellipsis">${esc(s.title || '-')}</td>
                <td class="num">${fmtNum(s.depositWon)}/${fmtNum(s.monthlyRentWon)}/${fmtNum(s.maintenanceFeeWon)}</td>
            </tr>`).join('') || '<tr><td colspan="4" class="empty">스냅샷 없음</td></tr>'}</tbody>
        </table>
      </div>
      <h4 style="margin:14px 0 6px">이벤트 (${d.events.length})</h4>
      <div class="table-wrap" style="max-height:180px">
        <table class="table">
          <thead><tr><th>유형</th><th>시각</th><th>변경 필드</th><th>발송</th></tr></thead>
          <tbody>${d.events.map((e) => `
            <tr><td><span class="chip blue">${esc(e.eventType)}</span></td>
                <td>${fmtTime(e.eventAt)}</td>
                <td class="ellipsis">${esc(JSON.stringify(e.changedFields || []))}</td>
                <td>${e.webhookSentAt ? '<span class="chip green">sent</span>' : `<span class="chip amber">pending (${e.webhookAttempts})</span>`}</td>
            </tr>`).join('') || '<tr><td colspan="4" class="empty">이벤트 없음</td></tr>'}</tbody>
        </table>
      </div>`;
    await confirmModal({
      title: `매물 #${id} 상세`, html,
      confirmLabel: '닫기',
    });
  },
};

function statusChip(s) {
  const cls = s === 'active' ? 'green' : s === 'missing' ? 'amber'
    : s === 'removed' ? 'red' : s === 'expired' ? 'gray' : s === 'blocked' ? 'red' : 'gray';
  return `<span class="chip ${cls}">${esc(s || '-')}</span>`;
}

// ── Events ────────────────────────────────────────────────────────────
tabs.events = {
  state: { deliveries: [] },

  async load() {
    $('evReload').addEventListener('click', () => this.reload().catch((e) => toast(e.message, 'err')));
    $('evBulkRetryBtn').addEventListener('click', () => this.bulkRetry().catch((e) => toast(e.message, 'err')));
    $('evBulkMarkSentBtn').addEventListener('click', () => this.bulkMarkSent().catch((e) => toast(e.message, 'err')));
    $('evUnfannedReload').addEventListener('click', () => this.reloadUnfanned().catch((e) => toast(e.message, 'err')));
    $('evRows').addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-act]');
      if (!btn) return;
      const id = Number(btn.dataset.id);
      if (btn.dataset.act === 'retry') this.singleRetry(id).catch((e) => toast(e.message, 'err'));
      else if (btn.dataset.act === 'mark-sent') this.singleMarkSent(id).catch((e) => toast(e.message, 'err'));
    });
    await this.reload();
    await this.reloadUnfanned();
    this.loaded = true;
  },

  async reload() {
    const params = new URLSearchParams({ limit: '300' });
    const s = $('evStatus').value;
    if (s) params.set('status', s);
    $('evStatusMsg').textContent = '불러오는 중...';
    const res = await api(`/api/tool/events/deliveries?${params}`);
    this.state.deliveries = res.deliveries || [];
    $('evRows').innerHTML = this.state.deliveries.map((d) => `
      <tr>
        <td class="mono">#${d.id}</td>
        <td><div>#${d.eventId} <span class="chip blue">${esc(d.eventType)}</span></div>
            <div class="muted">${fmtTime(d.eventAt)} · 매물 #${d.listingId}</div></td>
        <td><div>${esc(d.webhookLabel || '-')}</div><div class="muted">${esc(d.webhookOwner || '-')}</div></td>
        <td>${deliveryStatusChip(d.status)}</td>
        <td class="num">${d.attempts}</td>
        <td class="muted">${fmtTime(d.nextTryAt)}</td>
        <td class="ellipsis">${esc(d.lastError || '-')}</td>
        <td>
          ${d.status !== 'sent' ? `<button class="btn xs" data-act="retry" data-id="${d.id}">재시도</button>` : ''}
          ${d.status !== 'sent' ? `<button class="btn xs warn" data-act="mark-sent" data-id="${d.id}">발송완료</button>` : ''}
        </td>
      </tr>`).join('') || '<tr><td colspan="8" class="empty">큐 비어있음</td></tr>';
    $('evStatusMsg').textContent = `${res.total}건 (표시 ${this.state.deliveries.length})`;
  },

  async reloadUnfanned() {
    const res = await api('/api/tool/events/unfanned');
    $('evUnfannedRows').innerHTML = (res.events || []).map((e) => `
      <tr><td class="mono">#${e.id}</td><td>${esc(e.platformCode)}</td>
          <td class="mono">${esc(e.platformListingId)}</td>
          <td><span class="chip blue">${esc(e.eventType)}</span></td>
          <td>${fmtTime(e.eventAt)}</td><td>${fmtTime(e.createdAt)}</td></tr>`).join('')
      || '<tr><td colspan="6" class="empty">없음</td></tr>';
  },

  async singleRetry(id) {
    const m = await confirmModal({
      title: '재시도 큐로 되돌리기',
      html: `<p>발송 #${id}를 pending으로 되돌립니다.</p>`,
      confirmLabel: '재시도',
    });
    if (!m.ok) return;
    await api(`/api/tool/events/deliveries/${id}/retry`, { method: 'POST' });
    toast(`#${id} pending으로 복귀`);
    await this.reload();
  },

  async singleMarkSent(id) {
    const m = await confirmModal({
      title: '발송 완료로 마킹',
      html: `<div class="warn">Discord에 실제로 보내지 않고 sent로 표시합니다.</div>`,
      confirmLabel: '마킹',
    });
    if (!m.ok) return;
    await api(`/api/tool/events/deliveries/${id}/mark-sent`, { method: 'POST' });
    toast(`#${id} sent로 마킹`);
    await this.reload();
  },

  async bulkRetry() {
    const status = $('evStatus').value || 'failed';
    if (!['pending', 'failed', 'suppressed'].includes(status)) {
      return toast('재시도 가능한 상태(pending/failed/suppressed)에서만 사용', 'err');
    }
    const body = { status_filter: status };
    const plan = await api('/api/tool/events/bulk-retry/preview', {
      method: 'POST', body: JSON.stringify(body),
    });
    const m = await confirmModal({
      title: '일괄 재시도',
      html: `<div class="preview-counts">
               <div class="box"><b>${fmtNum(plan.wouldRetry)}</b><span>재시도 예정</span></div>
             </div>`,
      confirmLabel: '실행',
      danger: true,
    });
    if (!m.ok) return;
    const r = await api('/api/tool/events/bulk-retry', {
      method: 'POST', body: JSON.stringify(body),
    });
    toast(`${r.retried}건 재시도`);
    await this.reload();
  },

  async bulkMarkSent() {
    const status = $('evStatus').value || 'failed';
    if (!['pending', 'failed', 'suppressed'].includes(status)) {
      return toast('가능한 상태(pending/failed/suppressed)에서만 사용', 'err');
    }
    const body = { status_filter: status };
    const plan = await api('/api/tool/events/bulk-mark-sent/preview', {
      method: 'POST', body: JSON.stringify(body),
    });
    const m = await confirmModal({
      title: '일괄 발송완료 처리',
      html: `<div class="danger">Discord에 보내지 않고 sent로 표시합니다.</div>
             <div class="preview-counts">
               <div class="box"><b>${fmtNum(plan.wouldMarkSent)}</b><span>마킹 예정</span></div>
             </div>`,
      confirmLabel: '실행',
      danger: true,
    });
    if (!m.ok) return;
    const r = await api('/api/tool/events/bulk-mark-sent', {
      method: 'POST', body: JSON.stringify(body),
    });
    toast(`${r.markedSent}건 마킹`);
    await this.reload();
  },
};

function deliveryStatusChip(s) {
  const cls = s === 'sent' ? 'green' : s === 'pending' ? 'amber'
    : s === 'failed' ? 'red' : 'gray';
  return `<span class="chip ${cls}">${esc(s)}</span>`;
}

// ── Regions ───────────────────────────────────────────────────────────
tabs.regions = {
  state: { regions: [], schedules: [], expanded: null },

  async load() {
    $('regReload').addEventListener('click', () => this.reload().catch((e) => toast(e.message, 'err')));
    $('regCreateForm').addEventListener('submit', (e) => this.create(e).catch((e) => toast(e.message, 'err')));
    $('regRows').addEventListener('click', (e) => this.handleClick(e));
    await this.reload();
    this.loaded = true;
  },

  async reload() {
    const [r1, r2] = await Promise.all([
      api('/api/tool/regions'),
      api('/api/tool/region-schedules'),
    ]);
    this.state.regions = r1.regions || [];
    this.state.schedules = r2.schedules || [];
    this.render();
  },

  render() {
    const rows = this.state.regions.map((r) => {
      const exp = r.id === this.state.expanded;
      const detail = exp ? `<tr><td colspan="6" style="background:#fafafa">${this.renderDetail(r)}</td></tr>` : '';
      return `
        <tr>
          <td><b>${esc(r.slug)}</b><br><span class="muted">${esc(r.name)}</span></td>
          <td>${esc(r.centerLat.toFixed(5))}, ${esc(r.centerLng.toFixed(5))}<br><span class="muted">${r.radiusKm} km</span></td>
          <td>${regionStatusChip(r.status)}</td>
          <td class="num">${r.scheduleCount}</td>
          <td class="num">${r.listingRegionCount}</td>
          <td><button class="btn xs" data-act="region-toggle" data-id="${r.id}">${exp ? '닫기' : '편집'}</button></td>
        </tr>${detail}`;
    }).join('');
    $('regRows').innerHTML = rows || '<tr><td colspan="6" class="empty">지역 없음</td></tr>';
  },

  renderDetail(r) {
    const schedules = this.state.schedules.filter((s) => s.regionId === r.id);
    const sourceOpts = ['all_light', 'naver', 'dabang', 'zigbang', 'daangn', 'peterpan']
      .map((s) => `<option value="${s}">${s}</option>`).join('');
    return `
      <div style="padding:14px">
        <form class="grid-3" data-region-form="${r.id}">
          <label class="field">슬러그<input name="slug" value="${esc(r.slug)}" pattern="[a-z0-9][a-z0-9_-]{1,62}"></label>
          <label class="field">이름<input name="name" value="${esc(r.name)}"></label>
          <label class="field">상태
            <select name="status">
              <option value="pending" ${r.status === 'pending' ? 'selected' : ''}>pending</option>
              <option value="approved" ${r.status === 'approved' ? 'selected' : ''}>approved</option>
              <option value="disabled" ${r.status === 'disabled' ? 'selected' : ''}>disabled</option>
            </select>
          </label>
          <label class="field">중심 위도<input name="center_lat" type="number" step="0.000001" value="${r.centerLat}"></label>
          <label class="field">중심 경도<input name="center_lng" type="number" step="0.000001" value="${r.centerLng}"></label>
          <label class="field">반경(km)<input name="radius_km" type="number" step="0.1" value="${r.radiusKm}"></label>
          <label class="field">최대 보증금(만원)<input name="max_deposit_manwon" type="number" value="${r.maxDepositManwon ?? ''}"></label>
          <label class="field">최대 월세(만원)<input name="max_rent_manwon" type="number" value="${r.maxRentManwon ?? ''}"></label>
          <div></div>
          <label class="field" style="grid-column:1/-1">메모<textarea name="note">${esc(r.note || '')}</textarea></label>
          <label class="field" style="grid-column:1/-1">naver cortarNos (한 줄에 하나)
            <textarea name="naver_cortar_nos">${esc(r.naverCortarNos.join('\n'))}</textarea></label>
          <label class="field" style="grid-column:1/-1">daangn region_ids (한 줄에 하나)
            <textarea name="daangn_region_ids">${esc(r.daangnRegionIds.join('\n'))}</textarea></label>
          <label class="field" style="grid-column:1/-1">naver ms= URLs (한 줄에 하나)
            <textarea name="naver_urls">${esc(r.naverUrls.join('\n'))}</textarea></label>
          <div style="grid-column:1/-1;display:flex;gap:8px;flex-wrap:wrap">
            <button class="btn primary" type="submit">저장</button>
            <button class="btn danger" type="button" data-act="region-delete" data-id="${r.id}">삭제</button>
          </div>
        </form>
        <h4 style="margin:14px 0 6px">스케줄 (${schedules.length})</h4>
        <div class="table-wrap" style="max-height:200px">
          <table class="table">
            <thead><tr><th>소스</th><th>cron</th><th>활성</th><th>최근 실행</th><th></th></tr></thead>
            <tbody>${schedules.map((s) => `
              <tr><td><span class="chip">${esc(s.source)}</span></td>
                  <td class="mono">${esc(s.cronExpr)}</td>
                  <td>${s.enabled ? '<span class="chip green">on</span>' : '<span class="chip gray">off</span>'}</td>
                  <td class="muted">${fmtTime(s.lastRunAt)} ${esc(s.lastStatus || '')}</td>
                  <td>
                    <button class="btn xs" data-act="sched-toggle" data-id="${s.id}" data-enabled="${s.enabled}">${s.enabled ? '비활성' : '활성'}</button>
                    <button class="btn xs danger" data-act="sched-delete" data-id="${s.id}">삭제</button>
                  </td>
              </tr>`).join('') || '<tr><td colspan="5" class="empty">스케줄 없음</td></tr>'}</tbody>
          </table>
        </div>
        <form class="grid-3" data-schedule-form="${r.id}" style="margin-top:8px">
          <label class="field">소스<select name="source">${sourceOpts}</select></label>
          <label class="field">cron (5필드)<input name="cron_expr" placeholder="0 6,9,12,15,18 * * *"></label>
          <div style="align-self:end"><button class="btn primary sm" type="submit">스케줄 추가</button></div>
        </form>
      </div>`;
  },

  async handleClick(e) {
    const btn = e.target.closest('button[data-act]');
    if (btn) {
      const id = Number(btn.dataset.id);
      const act = btn.dataset.act;
      try {
        if (act === 'region-toggle') {
          this.state.expanded = this.state.expanded === id ? null : id;
          this.render();
        } else if (act === 'region-delete') {
          const r = this.state.regions.find((x) => x.id === id);
          const prev = await api(`/api/tool/regions/${id}/delete-preview`);
          const m = await confirmModal({
            title: `${r.slug} 지역 삭제`,
            html: `
              <div class="danger">cascade로 listing_regions, region_schedules가 함께 삭제됩니다.</div>
              <div class="preview-counts">
                <div class="box"><b>${fmtNum(prev.listingRegionRows)}</b><span>listing_regions</span></div>
                <div class="box"><b>${fmtNum(prev.scheduleRows)}</b><span>스케줄</span></div>
              </div>
              <p class="muted">데이터 디렉터리 <code>${esc(prev.dataDirectoryHint)}</code> 는 자동 삭제되지 않습니다.</p>
              <label class="field">확인을 위해 슬러그 <b>${esc(r.slug)}</b>를 입력
                <input type="text" data-input="confirm_slug" placeholder="${esc(r.slug)}">
              </label>`,
            confirmLabel: '삭제',
            danger: true,
            extraInputs: ['confirm_slug'],
          });
          if (!m.ok) return;
          await api(`/api/tool/regions/${id}`, {
            method: 'DELETE',
            body: JSON.stringify({ confirm_slug: m.values.confirm_slug }),
          });
          toast('삭제 완료');
          await this.reload();
        } else if (act === 'sched-toggle') {
          const en = btn.dataset.enabled === 'true';
          await api(`/api/tool/region-schedules/${id}`, {
            method: 'PATCH', body: JSON.stringify({ enabled: !en }),
          });
          await this.reload();
        } else if (act === 'sched-delete') {
          const m = await confirmModal({
            title: '스케줄 삭제',
            html: '<div class="warn">크롤 스케줄 1개가 삭제됩니다.</div>',
            confirmLabel: '삭제', danger: true,
          });
          if (!m.ok) return;
          await api(`/api/tool/region-schedules/${id}`, { method: 'DELETE' });
          await this.reload();
        }
      } catch (ex) { toast(ex.message, 'err'); }
    }
    // form submit handlers
    const regForm = e.target.closest('form[data-region-form]');
    if (regForm) {
      e.preventDefault?.();
    }
  },

  async create(e) {
    e.preventDefault();
    const fd = new FormData(e.target);
    await api('/api/tool/regions', {
      method: 'POST',
      body: JSON.stringify({
        slug: fd.get('slug'),
        name: fd.get('name'),
        status: fd.get('status'),
        center_lat: Number(fd.get('center_lat')),
        center_lng: Number(fd.get('center_lng')),
        radius_km: Number(fd.get('radius_km')),
      }),
    });
    e.target.reset();
    toast('지역 생성');
    await this.reload();
  },
};

function regionStatusChip(s) {
  const cls = s === 'approved' ? 'green' : s === 'pending' ? 'amber' : 'gray';
  return `<span class="chip ${cls}">${esc(s)}</span>`;
}

// Delegate region/schedule form submits at document level so re-rendered
// markup keeps working without rebinding.
document.addEventListener('submit', async (e) => {
  const regionForm = e.target.closest('form[data-region-form]');
  if (regionForm) {
    e.preventDefault();
    const id = Number(regionForm.dataset.regionForm);
    const fd = new FormData(regionForm);
    const lines = (k) => (fd.get(k) || '').split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    try {
      await api(`/api/tool/regions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({
          slug: fd.get('slug'), name: fd.get('name'),
          status: fd.get('status'),
          center_lat: Number(fd.get('center_lat')),
          center_lng: Number(fd.get('center_lng')),
          radius_km: Number(fd.get('radius_km')),
          max_deposit_manwon: fd.get('max_deposit_manwon') ? Number(fd.get('max_deposit_manwon')) : null,
          max_rent_manwon: fd.get('max_rent_manwon') ? Number(fd.get('max_rent_manwon')) : null,
          note: fd.get('note') || null,
          naver_cortar_nos: lines('naver_cortar_nos'),
          daangn_region_ids: lines('daangn_region_ids').map((s) => Number(s)),
          naver_urls: lines('naver_urls'),
        }),
      });
      toast('지역 저장');
      await tabs.regions.reload();
    } catch (ex) { toast(ex.message, 'err'); }
    return;
  }
  const schedForm = e.target.closest('form[data-schedule-form]');
  if (schedForm) {
    e.preventDefault();
    const id = Number(schedForm.dataset.scheduleForm);
    const fd = new FormData(schedForm);
    try {
      await api('/api/tool/region-schedules', {
        method: 'POST',
        body: JSON.stringify({
          region_id: id,
          source: fd.get('source'),
          cron_expr: (fd.get('cron_expr') || '').trim(),
          enabled: true,
        }),
      });
      schedForm.reset();
      toast('스케줄 추가');
      await tabs.regions.reload();
    } catch (ex) { toast(ex.message, 'err'); }
  }
});

// ── Audit ──────────────────────────────────────────────────────────────
tabs.audit = {
  state: { entries: [] },

  async load() {
    $('auditReload').addEventListener('click', () => this.reload().catch((e) => toast(e.message, 'err')));
    $('auditRows').addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-act]');
      if (!btn) return;
      const id = Number(btn.dataset.id);
      if (btn.dataset.act === 'audit-rollback') this.rollback(id).catch((e) => toast(e.message, 'err'));
      else if (btn.dataset.act === 'audit-view') this.viewEntry(id).catch((e) => toast(e.message, 'err'));
    });
    await this.reload();
    this.loaded = true;
  },

  async reload() {
    const params = new URLSearchParams({ limit: '200' });
    const t = $('auditTable').value.trim();
    const a = $('auditAction').value.trim();
    const onlyU = $('auditUnreverted').checked;
    if (t) params.set('target_table', t);
    if (a) params.set('action', a);
    if (onlyU) params.set('unreverted_only', 'true');
    const res = await api(`/api/tool/audit?${params}`);
    this.state.entries = res.entries || [];
    $('auditStatusMsg').textContent = `${res.total}건 중 최근 ${this.state.entries.length}`;
    $('auditRows').innerHTML = this.state.entries.map((e) => `
      <tr>
        <td class="mono">#${e.id}</td>
        <td class="muted">${fmtTime(e.createdAt)}</td>
        <td>${esc(e.actorUsername)}</td>
        <td><span class="chip indigo">${esc(e.action)}</span></td>
        <td><span class="chip">${esc(e.targetTable)}</span> ${esc(e.targetId || '')} ${e.targetCount > 1 ? `<span class="muted">×${e.targetCount}</span>` : ''}</td>
        <td>
          <button class="btn xs" data-act="audit-view" data-id="${e.id}">보기</button>
        </td>
        <td>
          ${e.revertedAt
            ? `<span class="chip gray">reverted by ${esc(e.revertedByUsername || '?')}</span>`
            : e.revertible
              ? `<button class="btn xs warn" data-act="audit-rollback" data-id="${e.id}">롤백</button>`
              : '<span class="muted">불가</span>'}
        </td>
      </tr>`).join('') || '<tr><td colspan="7" class="empty">감사 로그 없음</td></tr>';
  },

  async viewEntry(id) {
    const e = this.state.entries.find((x) => x.id === id);
    if (!e) return;
    const html = `
      <dl class="detail-grid">
        <dt>actor</dt><dd>${esc(e.actorUsername)} (#${e.actorUserId})</dd>
        <dt>action</dt><dd>${esc(e.action)}</dd>
        <dt>대상</dt><dd>${esc(e.targetTable)} ${esc(e.targetId || '')} (rows: ${e.targetCount})</dd>
        <dt>요청</dt><dd>${esc(e.requestIp || '-')} ${esc(e.requestPath || '')}</dd>
        <dt>시각</dt><dd>${fmtTime(e.createdAt)}</dd>
      </dl>
      <h4 style="margin:12px 0 4px">before</h4>
      <pre class="json-block">${esc(JSON.stringify(e.before, null, 2))}</pre>
      <h4 style="margin:12px 0 4px">after</h4>
      <pre class="json-block">${esc(JSON.stringify(e.after, null, 2))}</pre>
      <h4 style="margin:12px 0 4px">cmd_payload (scrubbed)</h4>
      <pre class="json-block">${esc(JSON.stringify(e.cmdPayload, null, 2))}</pre>`;
    await confirmModal({ title: `감사 로그 #${id}`, html, confirmLabel: '닫기' });
  },

  async rollback(id) {
    const e = this.state.entries.find((x) => x.id === id);
    const m = await confirmModal({
      title: `#${id} 변경 롤백`,
      html: `
        <div class="warn">서버에 저장된 reverse SQL을 실행합니다.<br>
        롤백 자체도 새 감사 로그 한 줄로 기록됩니다.</div>
        <p>action: <b>${esc(e.action)}</b> on ${esc(e.targetTable)} ${esc(e.targetId || '')}</p>`,
      confirmLabel: '롤백',
      danger: true,
    });
    if (!m.ok) return;
    const r = await api(`/api/tool/audit/${id}/rollback`, { method: 'POST' });
    toast(`롤백 완료 (${r.rowsAffected}행)`);
    await this.reload();
  },
};

// ── Kickoff ────────────────────────────────────────────────────────────
boot().catch((e) => {
  document.body.innerHTML = `<div class="empty">초기화 실패: ${esc(e.message)}</div>`;
});
