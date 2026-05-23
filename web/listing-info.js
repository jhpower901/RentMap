// Shared "📌 매물 정보" panel renderer used by the platform pages (expand-row
// detail) and the favorites page (in-card detail). Single source of truth so
// the two pages stay in sync when fields are added.
//
// Public API:
//   ListingInfo.esc(value)               — HTML-escape any value
//   ListingInfo.typeLabel(d, source)     — Daangn enum → Korean label, otherwise raw
//   ListingInfo.buildSection(d, source)  — full "📌 매물 정보" HTML block (may be '')
//
// All callers must include the matching CSS rules (`.info-*`, `.sec-*`).
// platform-common.css and favorites.html both ship those styles.

(function () {
  // Daangn list API returns enum codes for room_type; map to Korean labels.
  const DAANGN_TYPE_LABEL = {
    ONE_ROOM: '원룸',
    OPEN_ONE_ROOM: '오픈형 원룸',
    SPLIT_ONE_ROOM: '분리형 원룸',
    TWO_ROOM: '투룸',
    THREE_ROOM: '쓰리룸 이상',
    OFFICETEL: '오피스텔',
    APARTMENT: '아파트',
    VILLA: '빌라/연립',
    HOUSE: '단독/다가구',
  };

  function esc(v) {
    return String(v == null ? '' : v)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function typeLabel(d, source) {
    const raw = d.type || d.room_type || '';
    if (source === 'daangn' && DAANGN_TYPE_LABEL[raw]) return DAANGN_TYPE_LABEL[raw];
    return raw;
  }

  // Build the "📌 매물 정보" section. Returns '' when there's nothing to show
  // (so the caller can omit the wrapper entirely).
  function buildSection(d, source) {
    const pairs = [
      ['용도',     d.building_use],
      ['방유형',   typeLabel(d, source)],
      ['구조',     d.room_structure],
      ['층 구조',  d.duplex],
      ['방향',     d.direction],
      ['방수',     d.room_count],
      ['욕실',     d.bathroom_count],
      ['주차',     d.parking],
      ['입주',     d.move_in],
      ['사용승인', d.approval_date],
    ].filter(([, v]) => v != null && String(v).trim() !== '');

    const description = (d.description || '').trim();
    const options = (d.options || '').trim();
    const security = (d.security_options || '').trim();

    if (!pairs.length && !description && !options && !security) return '';

    const gridHtml = pairs.length
      ? `<div class="info-grid">${pairs.map(([k, v]) =>
          `<div class="info-item"><span class="info-key">${k}</span><span class="info-val">${esc(v)}</span></div>`
        ).join('')}</div>`
      : '';

    const tagsHtml = (raw, cls = '') => {
      const items = raw.split(/[;,]\s*/).map(s => s.trim()).filter(Boolean);
      if (!items.length) return '';
      return `<div class="info-tags">${items.map(t =>
        `<span class="info-tag${cls ? ' ' + cls : ''}">${esc(t)}</span>`
      ).join('')}</div>`;
    };

    const descHtml = description
      ? `<div class="info-long"><div class="info-long-key">📝 소개</div><div class="info-long-val">${esc(description)}</div></div>`
      : '';
    const optsHtml = options
      ? `<div class="info-long"><div class="info-long-key">🛋️ 옵션</div>${tagsHtml(options)}</div>`
      : '';
    const secHtml = security
      ? `<div class="info-long"><div class="info-long-key">🔒 보안</div>${tagsHtml(security, 'sec')}</div>`
      : '';

    return `
      <div class="sec">
        <div class="sec-title">📌 매물 정보</div>
        ${gridHtml}
        ${descHtml}${optsHtml}${secHtml}
      </div>
    `;
  }

  window.ListingInfo = { esc, typeLabel, buildSection };
})();
