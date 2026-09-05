'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { parseMilitary, linesFromBlocks, isEmpireEarthWindow } = require('../src/lib/parser');
const roster = [{ id: 1, name: 'De3ech' }, { id: 2, name: 'momo007mkaouar' }, { id: 3, name: 'Mouaddeb2' }, { id: 4, name: 'brahim129' }];

test('reads numeric names, kills/losses, team identifiers without inferring winner', () => {
  const data = parseMilitary({ text: 'Military\nUnits Killed Units Lost\n1 De3ech 136 55\n1 momo007mkaouar 91 55\n3 Mouaddeb2 93 175\n3 brahim129 16 52' }, roster);
  assert.equal(data.isMilitary, true);
  assert.deepEqual(data.rows.map(row => [row.player_id, row.units_killed, row.units_lost, row.team]), [[1, 136, 55, 'team1'], [2, 91, 55, 'team1'], [3, 93, 175, 'team2'], [4, 16, 52, 'team2']]);
  assert.equal(data.winner, undefined);
});
test('does not fabricate rows when OCR separates columns into blocks', () => {
  const word = (text, x0, y0) => ({ text, bbox: { x0, x1: x0 + 50, y0, y1: y0 + 20 } });
  const blocks = [word('De3ech', 80, 201), word('55', 600, 200), word('136', 400, 202), word('1', 10, 200)].map(w => ({ paragraphs: [{ lines: [{ words: [w] }] }] }));
  assert.deepEqual(linesFromBlocks(blocks), ['1 De3ech 136 55']);
  const result = parseMilitary({ text: 'Units Killed\nUnits Lost', blocks }, roster);
  assert.equal(result.rows[0].player_id, 1);
  assert.equal(result.rows[0].team, '');
});
test('non-military screens never produce a match', () => {
  assert.equal(parseMilitary({ text: 'Summary\n1 De3ech 136 55' }, roster).isMilitary, false);
});
test('unrecognized player names need explicit mapping', () => {
  const result = parseMilitary({ text: 'Units Killed Units Lost\n1 De3eoh 136 55' }, roster);
  assert.equal(result.rows[0].player_id, null);
});
test('original Empire Earth window matching excludes companion, browsers, sequels', () => {
  assert.equal(isEmpireEarthWindow('Empire Earth'), true);
  assert.equal(isEmpireEarthWindow('Empire Earth - NeoEE'), true);
  for (const title of ['Empire Earth Companion', 'Empire Earth II', 'Empire Earth 3', 'Empire Earth MMR', 'empire earth mmr', 'Empire Earth - Google Chrome', 'Empire Earth - Codex']) assert.equal(isEmpireEarthWindow(title), false);
});
