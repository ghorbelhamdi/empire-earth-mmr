'use strict';
const normalizeName = value => String(value || '').normalize('NFKC').toLowerCase().replace(/[^a-z0-9]/g, '');
// These substitutions are used for suggestions only, never for automatic identity mapping.
const visualName = value => normalizeName(value).replace(/[o0]/g, '0').replace(/[il1]/g, '1').replace(/[s5]/g, '5');
function editDistance(a, b) {
  const row = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    let previous = row[0]; row[0] = i;
    for (let j = 1; j <= b.length; j++) {
      const old = row[j]; row[j] = Math.min(row[j] + 1, row[j - 1] + 1, previous + (a[i - 1] === b[j - 1] ? 0 : 1)); previous = old;
    }
  }
  return row[b.length];
}
function matchPlayer(name, players) {
  const normalized = normalizeName(name); if (!normalized) return null;
  const matches = players.filter(player => normalizeName(player.name) === normalized);
  return matches.length === 1 ? matches[0].id : null;
}
function identifyName(row, players = []) {
  const candidates = [...new Set([row.ocr_name, row.ocr_name_raw, ...(row.ocr_name_candidates || [])].filter(Boolean))];
  const exactIds = new Set(candidates.map(name => matchPlayer(name, players)).filter(id => id !== null));
  if (exactIds.size > 1) return { player_id: null, suggested_player_id: null, suggested_player_name: null };
  const exact = matchPlayer(row.ocr_name, players);
  if (exact !== null) return { player_id: exact, suggested_player_id: null, suggested_player_name: null };
  const ranked = players.map(player => {
    const target = visualName(player.name);
    let score = Infinity;
    for (const candidate of candidates) {
      const observed = visualName(candidate);
      if (Math.min(target.length, observed.length) < 4) continue;
      if (observed === target) score = Math.min(score, 0.05);
      const distance = editDistance(observed, target), length = Math.max(observed.length, target.length);
      if (distance <= Math.max(1, Math.floor(length * 0.15))) score = Math.min(score, 0.1 + distance / length);
      // Game aliases often extend a ladder handle. Suggest a unique prefix, requiring a manual choice.
      if (observed.startsWith(target) && observed.length > target.length) score = Math.min(score, 0.3 + (observed.length - target.length) / observed.length * 0.1);
    }
    return { player, score };
  }).filter(entry => Number.isFinite(entry.score)).sort((a, b) => a.score - b.score);
  const unique = ranked.length && (ranked.length === 1 || ranked[1].score - ranked[0].score >= 0.08) ? ranked[0].player : null;
  return { player_id: null, suggested_player_id: unique?.id ?? null, suggested_player_name: unique?.name ?? null };
}
function selectNameCandidate(candidates, fallback = '') {
  const valid = candidates.filter(row => typeof row.text === 'string' && /[a-z?]/i.test(row.text) && row.text.trim().length <= 120);
  if (!valid.length) return { text: fallback, candidates: fallback ? [fallback] : [] };
  const votes = new Map();
  for (const row of valid) votes.set(normalizeName(row.text), (votes.get(normalizeName(row.text)) || 0) + 1);
  const score = row => (Number.isFinite(row.confidence) ? row.confidence : 0) + Math.min(3, votes.get(normalizeName(row.text))) * 4 - (row.text.match(/[^a-z0-9 _?'\-]/gi) || []).length * 8;
  valid.sort((a, b) => score(b) - score(a));
  return { text: valid[0].text.trim(), candidates: [...new Set(valid.map(row => row.text.trim()))].slice(0, 5) };
}
module.exports = { normalizeName, matchPlayer, identifyName, selectNameCandidate };
