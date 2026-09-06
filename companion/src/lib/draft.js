'use strict';
const { randomUUID } = require('node:crypto');

function newDraft() {
  return { submission_id: randomUUID(), created_at: new Date().toISOString(), rows: [], winner: '', stats_confirmed: false, evidence: null, screenshotPath: null, pendingPayload: null, result: null };
}

function validateRows(rows, players) {
  if (!Array.isArray(rows) || rows.length < 2 || rows.length > 10) throw new Error('Add between 2 and 10 players.');
  const ids = new Set();
  return rows.map(row => {
    const id = row.player_id;
    if (!Number.isSafeInteger(id) || id <= 0 || !players.some(player => player.id === id)) throw new Error('Match every row to a ladder player.');
    if (ids.has(id)) throw new Error('A player appears more than once.');
    ids.add(id);
    if (!['team1', 'team2'].includes(row.team)) throw new Error('Select a team for every player.');
    for (const field of ['units_killed', 'units_lost']) {
      if (!Number.isSafeInteger(row[field]) || row[field] < 0 || row[field] > 1000000) throw new Error('Kills and losses must be whole numbers from 0 to 1,000,000.');
    }
    return { player_id: id, team: row.team, units_killed: row.units_killed, units_lost: row.units_lost };
  });
}

function buildPayload(draft, players) {
  const rows = validateRows(draft.rows, players);
  if (!['team1', 'team2'].includes(draft.winner)) throw new Error('Choose the winning team. Military statistics cannot determine the winner.');
  if (draft.stats_confirmed !== true) throw new Error('Confirm the players, teams, result, and statistics before continuing.');
  const team1 = rows.filter(row => row.team === 'team1').map(row => row.player_id);
  const team2 = rows.filter(row => row.team === 'team2').map(row => row.player_id);
  if (!team1.length || !team2.length) throw new Error('Both teams need at least one player.');
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(draft.submission_id)) throw new Error('Invalid draft ID.');
  const stats = rows.map(({ player_id, units_killed, units_lost }) => ({ player_id, units_killed, units_lost }));
  const evidence = draft.evidence || { source: 'manual', captured_at: draft.created_at || new Date().toISOString() };
  if (!['screenshot', 'manual'].includes(evidence.source) || evidence.source === 'screenshot' && !/^[a-f0-9]{64}$/.test(evidence.sha256) || !Number.isFinite(Date.parse(evidence.captured_at))) throw new Error('Invalid local evidence. Capture again or start a manual draft.');
  return { submission_id: draft.submission_id, team1, team2, winner: draft.winner, stats, stats_confirmed: true,
    evidence: { ...(evidence.source === 'screenshot' ? { sha256: evidence.sha256 } : {}), source: evidence.source, captured_at: evidence.captured_at } };
}

// Persist the exact payload before sending. A timeout must never create a new match ID.
async function submitDraft(draft, players, save, request) {
  if (draft.result) return draft.result;
  if (!draft.pendingPayload) {
    draft.pendingPayload = buildPayload(draft, players);
  }
  await save(draft);
  let result;
  try { result = await request(draft.pendingPayload); }
  catch (error) {
    // These statuses prove that the server rejected validation before creating a match.
    // Ambiguous failures and conflicts remain frozen for idempotent retries.
    if ([400, 413, 422].includes(error.status)) { draft.pendingPayload = null; draft.stats_confirmed = false; await save(draft); }
    throw error;
  }
  if (!result || !Number.isInteger(result.match_id) || !['pending', 'approved', 'denied'].includes(result.status)) throw new Error('Unexpected submission response. Retry to check the saved submission ID.');
  draft.result = result;
  await save(draft);
  return result;
}

module.exports = { newDraft, validateRows, buildPayload, submitDraft };
