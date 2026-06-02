(function () {
  'use strict';

  // Parse Korean rent/deposit notation into a 만원-unit number.
  //
  // Mirrors scripts/rentmap.py:parse_manwon_from_text — same input shapes,
  // same 만원 output unit, so a value the backend stores as `deposit_manwon`
  // round-trips through the filter input without unit drift.
  //
  // Accepts (all return 만원-unit numbers):
  //   ""           → null
  //   "0"          → 0
  //   "3000"       → 3000        (bare number, already in 만원)
  //   "30000"      → 30000
  //   "1,500"      → 1500        (commas stripped)
  //   "2억"        → 20000
  //   "2억500만"   → 20500
  //   "2억 500만"  → 20500
  //   "2억500"     → 20500       (trailing bare number treated as 만)
  //   "2억500만원" → 20500       ("원" suffix tolerated)
  //   "1.5억"      → 15000
  //   "500만"      → 500
  //   "abc"        → null
  function parseManwonText(value) {
    if (value === null || value === undefined) return null;
    const raw = String(value).replace(/,/g, '').trim();
    if (!raw) return null;

    const eok = raw.match(/([0-9]+(?:\.[0-9]+)?)\s*억/);
    const man = raw.match(/([0-9]+(?:\.[0-9]+)?)\s*만/);
    if (eok || man) {
      const eokVal = eok ? parseFloat(eok[1]) * 10000 : 0;
      let manVal = man ? parseFloat(man[1]) : 0;
      // "2억500" — trailing bare number after 억 with no 만 marker is treated
      // as 만 since that's the natural shorthand a user types. "2억" alone
      // (no digits trailing) falls through with manVal=0.
      if (eok && !man) {
        const tail = raw.slice(eok.index + eok[0].length);
        const tailNum = tail.match(/([0-9]+(?:\.[0-9]+)?)/);
        if (tailNum) manVal = parseFloat(tailNum[1]);
      }
      const total = eokVal + manVal;
      return Number.isFinite(total) ? total : null;
    }

    // Bare number — already in 만원 (this matches the existing
    // "<input> 만원" UI contract, e.g. value="3000" means 3,000만원).
    const num = parseFloat(raw);
    return Number.isFinite(num) ? num : null;
  }

  window.PriceParser = { parseManwonText };
})();
