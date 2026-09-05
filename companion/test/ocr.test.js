'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { PNG } = require('pngjs');
const { recognizeNumber, readMilitary } = require('../src/lib/ocr');
const { readImage, prepareRegion, prepareForOCR } = require('../src/lib/image');

test('cropped glyph preprocessing removes colored pixels, scales and adds white padding', () => {
  const source = new PNG({ width: 3, height: 1 });
  source.data.set([255, 255, 255, 255, 255, 0, 0, 255, 80, 80, 80, 255]);
  const crop = PNG.sync.read(prepareRegion(source, { left: 0, top: 0, width: 3, height: 1 }, { scale: 2, padding: 1 }));
  assert.equal(crop.width, 8); assert.equal(crop.height, 4);
  assert.equal(crop.data[0], 255);
  assert.equal(crop.data[(crop.width + 1) * 4], 0);
  assert.equal(crop.data[(crop.width + 3) * 4], 255);
  assert.equal(crop.data[(crop.width + 5) * 4], 255);
  assert.equal(readImage(prepareForOCR(source)).width, 6);
});
test('numeric OCR uses a second crop for blanks and keeps zero valid', async () => {
  const settings = []; let count = 0;
  const worker = { setParameters: async value => settings.push(value), recognize: async () => ({ data: { text: count++ ? '0\n' : '', confidence: 95 } }) };
  assert.equal(await recognizeNumber(worker, [Buffer.alloc(1), Buffer.alloc(1)]), 0);
  assert.equal(count, 2); assert.equal(settings[0].tessedit_pageseg_mode, '7');
  assert.equal(settings[0].tessedit_char_whitelist, '0123456789');
});
test('unreadable, low-confidence and failed numeric crops stay blank instead of zero', async () => {
  for (const data of [{ text: 'O', confidence: 90 }, { text: '3', confidence: 10 }, { text: '1000001', confidence: 96 }, { text: '', confidence: 0 }]) {
    assert.equal(await recognizeNumber({ setParameters: async () => {}, recognize: async () => ({ data }) }, [Buffer.alloc(1)]), null);
  }
  assert.equal(await recognizeNumber({ setParameters: async () => { throw new Error('worker failed'); } }, []), null);
});
test('a failed second pass retains a Military-title detection for manual review', async () => {
  const source = new PNG({ width: 2, height: 2 }); source.data.fill(255); let count = 0;
  const worker = { setParameters: async () => {}, recognize: async () => {
    if (count++) throw new Error('fallback failed');
    return { data: { text: 'Post Game Statistics - Military', blocks: [] } };
  } };
  const result = await readMilitary(worker, PNG.sync.write(source));
  assert.equal(result.isMilitary, true); assert.equal(result.rows.length, 0);
});
test('a nonmilitary page never creates a draft simply because Military is a visible tab', async () => {
  const source = new PNG({ width: 2, height: 2 }); source.data.fill(255);
  const worker = { setParameters: async () => {}, recognize: async () => ({ data: { text: 'Summary Military Economy\nPost Game Statistics - Miscellaneous\nMouse clicks Hotkeys used', blocks: [] } }) };
  assert.equal((await readMilitary(worker, PNG.sync.write(source))).isMilitary, false);
});
test('a failed first pass can recover on the original image', async () => {
  const source = new PNG({ width: 2, height: 2 }); source.data.fill(255); let count = 0;
  const worker = { setParameters: async () => {}, recognize: async () => {
    if (!count++) throw new Error('enhanced OCR failed');
    return { data: { text: 'Post Game Statistics - Military', blocks: [] } };
  } };
  assert.equal((await readMilitary(worker, PNG.sync.write(source))).isMilitary, true);
});
test('complete OCR failure reports an error rather than asking for an already open Military tab', async () => {
  const source = new PNG({ width: 2, height: 2 }); source.data.fill(255);
  const worker = { setParameters: async () => {}, recognize: async () => { throw new Error('OCR worker failed'); } };
  await assert.rejects(readMilitary(worker, PNG.sync.write(source)), /Local OCR could not read/);
});
