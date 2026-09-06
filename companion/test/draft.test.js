'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { newDraft, buildPayload, submitDraft } = require('../src/lib/draft');
const players = [{ id: 1, name: 'A' }, { id: 2, name: 'B' }];
const valid = () => ({ ...newDraft(), rows: [
  { player_id: 1, team: 'team1', units_killed: 136, units_lost: 55 },
  { player_id: 2, team: 'team2', units_killed: 55, units_lost: 136 }
], winner: 'team1', stats_confirmed: true });

test('payload is strict, contains no local screenshot path/OCR/token, manual has no screenshot fingerprint', () => {
  const draft = { ...valid(), screenshotPath: 'C:/private/image.png', ocr_text: 'private text', token: 'secret' };
  const payload = buildPayload(draft, players);
  assert.deepEqual(Object.keys(payload).sort(), ['evidence', 'stats', 'stats_confirmed', 'submission_id', 'team1', 'team2', 'winner']);
  assert.deepEqual(payload.evidence, { source: 'manual', captured_at: draft.created_at });
  assert.equal(JSON.stringify(payload).includes('private'), false);
});
test('explicit winner, checked stats, unique roster and complete nonnegative integer stats required', () => {
  for (const patch of [{ winner: '' }, { stats_confirmed: false }]) assert.throws(() => buildPayload({ ...valid(), ...patch }, players));
  for (const [field, bad] of [['units_killed', -1], ['units_lost', 1.5], ['units_lost', null], ['units_lost', 1000001], ['player_id', 9], ['team', '']]) {
    const draft = valid(); draft.rows[1][field] = bad; assert.throws(() => buildPayload(draft, players));
  }
  const duplicate = valid(); duplicate.rows[1].player_id = 1; assert.throws(() => buildPayload(duplicate, players), /more than once/);
});
test('screenshot evidence has a valid hash and date', () => {
  const draft = valid(); draft.evidence = { source: 'screenshot', sha256: 'not-a-hash', captured_at: draft.created_at };
  assert.throws(() => buildPayload(draft, players));
  draft.evidence.sha256 = 'a'.repeat(64); assert.equal(buildPayload(draft, players).evidence.sha256, 'a'.repeat(64));
});
test('network loss persists exact payload before send and retry retains original id/values', async () => {
  const draft = valid(); const order = []; const sent = [];
  const save = async () => { order.push('save'); };
  await assert.rejects(submitDraft(draft, players, save, async payload => { order.push('send'); sent.push(JSON.stringify(payload)); throw new Error('offline'); }));
  assert.deepEqual(order, ['save', 'send']);
  draft.rows[0].units_killed = 999;
  const result = await submitDraft(draft, players, save, async payload => { sent.push(JSON.stringify(payload)); return { match_id: 42, status: 'approved', duplicate: true }; });
  assert.equal(sent[0], sent[1]); assert.equal(result.match_id, 42);
  assert.equal(await submitDraft(draft, players, save, () => { throw new Error('must not send twice'); }), result);
});
test('failed persistence never sends an unrecorded submission', async () => {
  let sent = false;
  await assert.rejects(submitDraft(valid(), players, async () => { throw new Error('disk full'); }, async () => { sent = true; }));
  assert.equal(sent, false);
});
test('ten players and uneven teams are supported but eleven are rejected', () => {
  const roster = Array.from({ length: 11 }, (_, i) => ({ id: i + 1, name: `Player ${i + 1}` }));
  const draft = valid(); draft.rows = roster.slice(0, 10).map((player, i) => ({ player_id: player.id, team: i < 4 ? 'team1' : 'team2', units_killed: 0, units_lost: 0 }));
  const payload = buildPayload(draft, roster); assert.equal(payload.team1.length, 4); assert.equal(payload.team2.length, 6);
  draft.rows.push({ player_id: 11, team: 'team2', units_killed: 0, units_lost: 0 }); assert.throws(() => buildPayload(draft, roster), /10 players/);
});
test('definite validation rejection unlocks correction; ambiguous failure remains locked', async () => {
  for (const status of [400, 413, 422, 500, 409]) {
    const draft = valid(); const originalID = draft.submission_id;
    await assert.rejects(submitDraft(draft, players, async () => {}, async () => { const error = new Error('server error'); error.status = status; throw error; }));
    assert.equal(Boolean(draft.pendingPayload), ![400, 413, 422].includes(status));
    assert.equal(draft.submission_id, originalID);
  }
});
