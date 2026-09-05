'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { parseMilitary, linesFromBlocks, isEmpireEarthWindow, selectMilitaryResult } = require('../src/lib/parser');
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
const word = (text, x, y, width = 40, height = 20) => ({ text, confidence: 95, bbox: { x0: x, x1: x + width, y0: y, y1: y + height } });
const blocks = words => words.map(value => ({ paragraphs: [{ lines: [{ words: [value] }] }] }));
test('six-column native layout reads kills/losses by position, never final army/conversion values', () => {
  const words = [word('Units', 740, 225, 26, 10), word('Killed', 771, 225, 26, 10), word('Units', 1030, 225, 26, 10), word('Lost', 1061, 225, 22, 10)];
  const metrics = [[3, 7, 0, 0, 0, 18], [2, 4, 0, 1, 0, 20], [5, 2, 0, 1, 0, 6], [2, 0, 1, 0, 0, 8]];
  for (let i = 0; i < 4; i++) {
    words.push(word(`Player${i + 1}`, 239, 279 + i * 56, 70, 12));
    metrics[i].forEach((value, column) => words.push(word(String(value), 760 + column * 288, 277 + i * 56, 16, 16)));
  }
  const result = parseMilitary({ text: 'Post Game Statistics - Military', blocks: blocks(words) });
  assert.deepEqual(result.rows.map(row => [row.units_killed, row.units_lost]), [[3, 7], [2, 4], [5, 2], [2, 0]]);
  assert.equal(result.quality.trusted_columns, true);
});
test('missing boxed military digits stay null even when buildings and army values were read', () => {
  const words = [word('Units', 740, 225, 26, 10), word('Killed', 771, 225, 26, 10), word('Units', 1030, 225, 26, 10), word('Lost', 1061, 225, 22, 10), word('Alice', 239, 280, 60, 12), word('18', 2200, 277, 22, 16)];
  const result = parseMilitary({ text: 'Post Game Statistics - Military', blocks: blocks(words) });
  assert.equal(result.rows[0].units_killed, null); assert.equal(result.rows[0].units_lost, null);
});
test('thin decorative lines do not become a player row or shorten the row spacing', () => {
  const words = [word('Units', 740, 225, 26, 10), word('Killed', 771, 225, 26, 10), word('Units', 1030, 225, 26, 10), word('Lost', 1061, 225, 22, 10), word('Alice', 239, 280, 60, 12), word('Bob', 239, 336, 60, 12), word('J—', 190, 364, 20, 1), word('Carol', 239, 392, 60, 12)];
  const result = parseMilitary({ text: 'Post Game Statistics - Military', blocks: blocks(words) });
  assert.deepEqual(result.rows.map(row => row.ocr_name), ['Alice', 'Bob', 'Carol']);
});
test('ambiguous text-only rows with extra numbers are refused', () => {
  const result = parseMilitary({ text: 'Units Killed Units Lost\n1 Alice 136 55 1200 0\n1 Bob 91 55 1000 1' });
  assert.equal(result.isMilitary, true); assert.deepEqual(result.rows, []);
});
test('Military title alone enables manual review; nav tab alone does not', () => {
  assert.equal(parseMilitary({ text: 'Post Game Statistics - Military' }).isMilitary, true);
  assert.equal(parseMilitary({ text: 'Military\nPost Game Statistics - Economy' }).isMilitary, false);
});
test('successful Military detection survives an empty or failed alternate result', () => {
  const military = parseMilitary({ text: 'Post Game Statistics - Military' });
  assert.equal(selectMilitaryResult(undefined, parseMilitary({ text: '' }), military), military);
});
