'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { identifyName, selectNameCandidate } = require('../src/lib/names');
const { finishMilitaryResult } = require('../src/lib/parser');
const players = [{ id: 1, name: '3amar404' }, { id: 2, name: 'De3ech' }, { id: 3, name: 'momo' },
  { id: 4, name: 'old_kala5la5' }, { id: 5, name: 'ibrahim129' }];

test('zoomed exact readings map automatically while visual character corrections only suggest', () => {
  assert.equal(identifyName({ ocr_name: 'De3ech', ocr_name_raw: 'Dedech' }, players).player_id, 2);
  const possible = identifyName({ ocr_name: '3amar4o4', ocr_name_raw: 'Jamar4o4' }, players);
  assert.equal(possible.player_id, null); assert.equal(possible.suggested_player_id, 1);
  assert.equal(possible.suggested_player_name, '3amar404');
});

test('unique alias prefixes and a missing character require explicit identity selection', () => {
  for (const [ocr_name, id] of [['momo 7 mkaoar', 3], ['Oldkalaslas laslas', 4], ['brahim 129', 5]]) {
    const possible = identifyName({ ocr_name }, players);
    assert.equal(possible.player_id, null); assert.equal(possible.suggested_player_id, id);
  }
  assert.equal(identifyName({ ocr_name: 'Meddeb' }, players).suggested_player_id, null);
});

test('similar roster names and conflicting exact OCR variants never choose an identity', () => {
  const ambiguous = identifyName({ ocr_name: 'Luka5' }, [{ id: 1, name: 'Lukas' }, { id: 2, name: 'LukaS_' }]);
  assert.equal(ambiguous.player_id, null); assert.equal(ambiguous.suggested_player_id, null);
  const conflict = identifyName({ ocr_name: 'Alice', ocr_name_raw: 'Alicia' }, [{ id: 1, name: 'Alice' }, { id: 2, name: 'Alicia' }]);
  assert.equal(conflict.player_id, null); assert.equal(conflict.suggested_player_id, null);
  assert.equal(identifyName({ ocr_name: 'Al' }, [{ id: 1, name: 'A1' }]).suggested_player_id, null);
});

test('candidate selection retains alternatives and prefers clear supported text without rewriting to roster names', () => {
  const result = selectNameCandidate([{ text: 'Dedech', confidence: 19 }, { text: 'De3ech', confidence: 75 }, { text: 'De3ech', confidence: 50 }], 'Dedech');
  assert.equal(result.text, 'De3ech'); assert.deepEqual(result.candidates, ['De3ech', 'Dedech']);
  assert.deepEqual(selectNameCandidate([], 'original'), { text: 'original', candidates: ['original'] });
});

test('finishing a match preserves original and alternate OCR names for local review', () => {
  const row = { ocr_name: '3amar4o4', ocr_name_raw: 'Jamar4o4', ocr_name_candidates: ['3amar4o4', 'Samar404'], units_killed: 1, units_lost: 6 };
  const result = finishMilitaryResult({ isMilitary: true, rows: [row] }, players).rows[0];
  assert.equal(result.ocr_name, row.ocr_name); assert.equal(result.ocr_name_raw, row.ocr_name_raw);
  assert.deepEqual(result.ocr_name_candidates, row.ocr_name_candidates);
  assert.equal(result.player_id, null); assert.equal(result.suggested_player_id, 1);
});
