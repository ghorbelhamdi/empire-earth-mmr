'use strict';
const { PSM } = require('tesseract.js');
const { readImage, fullImageScale, prepareRegion, prepareForOCR, trimInk } = require('./image');
const { parseMilitary, selectMilitaryResult, finishMilitaryResult } = require('./parser');
const { selectNameCandidate } = require('./names');
async function recognizeName(worker, image, rectangle, fallback) {
  const candidates = [];
  // A separate enlarged name crop avoids tiny fonts and adjacent team/stat columns.
  // Scale and segmentation variants recover different bitmap-font characters.
  for (const variant of [
    { scale: 4, threshold: 145, psm: PSM.SINGLE_WORD },
    { scale: 3, threshold: 120, psm: PSM.SINGLE_LINE },
    { scale: 2, threshold: 160, psm: PSM.SINGLE_LINE },
    { scale: 3, threshold: 145, psm: PSM.SINGLE_WORD },
    { scale: 4, threshold: 145, psm: PSM.SINGLE_LINE }
  ]) try {
    const crop = trimInk(prepareRegion(image, rectangle, variant));
    if (!crop) continue;
    await worker.setParameters({ tessedit_pageseg_mode: variant.psm, tessedit_char_whitelist: '' });
    const { data } = await worker.recognize(crop, {}, { text: true });
    const text = data.text.trim().replace(/^[|[\]\s]+|[|[\]\s]+$/g, '');
    candidates.push({ text, confidence: data.confidence });
  } catch { /* Other variants and the original name remain available for review. */ }
  return selectNameCandidate(candidates, fallback);
}
async function recognizeNumber(worker, images) {
  for (const image of images) try {
    if (!image) continue;
    const variant = Buffer.isBuffer(image) ? { buffer: image } : image;
    await worker.setParameters({ tessedit_pageseg_mode: variant.psm || PSM.SINGLE_LINE, tessedit_char_whitelist: '0123456789' });
    const { data } = await worker.recognize(variant.buffer, {}, { text: true });
    const text = data.text.trim();
    if (/^\d{1,7}$/.test(text) && Number(text) <= 1000000 && data.confidence >= (variant.minConfidence ?? 60)) return Number(text);
  } catch { /* An unreadable cell stays blank, never an invented zero. */ }
  return null;
}
async function readMilitary(worker, buffer, players = []) {
  const image = readImage(buffer), scale = fullImageScale(image), passes = [];
  for (const pass of [{ prepare: () => prepareForOCR(image), scale }, { prepare: () => buffer, scale: 1 }]) try {
    await worker.setParameters({ tessedit_pageseg_mode: PSM.SPARSE_TEXT, tessedit_char_whitelist: '' });
    const { data } = await worker.recognize(pass.prepare(), {}, { text: true, blocks: true });
    const parsed = parseMilitary(data, players); parsed.coordinateScale = pass.scale; passes.push(parsed);
    if (parsed.layout?.rows.length >= 2) break;
  } catch { /* A failed pass cannot erase another pass's Military detection. */ }
  if (!passes.length) throw new Error('Local OCR could not read the image. Try Capture now, import a screenshot, or enter the statistics manually.');
  let selected = selectMilitaryResult(passes);
  if (!selected.layout?.rows.length) return selected;
  const layout = selected.layout, unit = selected.coordinateScale, spacing = layout.spacing / unit;
  const ys = layout.rows.map(row => row.y / unit);
  const rowGap = ys.length > 1 ? Math.min(...ys.slice(1).map((y, i) => y - ys[i])) : layout.headerHeight / unit * 5;
  const cellHeight = Math.min(28, rowGap * 0.55), cellWidth = Math.min(140, spacing * 0.49);
  const namePositions = layout.rows.map(row => row.nameX).filter(value => value !== null).sort((a, b) => a - b);
  const nameX = namePositions.length ? namePositions[Math.floor(namePositions.length / 2)] / unit : null;
  const rows = [];
  for (const sourceRow of layout.rows) {
    const y = sourceRow.y / unit;
    const numberImages = x => {
      const primary = prepareRegion(image, { left: x - cellWidth / 2, top: y - cellHeight / 2, width: cellWidth, height: cellHeight });
      const alternate = prepareRegion(image, { left: x - Math.min(100, cellWidth) / 2, top: y - Math.min(32, rowGap * 0.65) / 2, width: Math.min(100, cellWidth), height: Math.min(32, rowGap * 0.65) }, { threshold: 120, scale: 2 });
      return [trimInk(primary), trimInk(alternate)].filter(Boolean).map(buffer => ({ buffer, psm: PSM.SINGLE_WORD, minConfidence: 80 }))
        .concat([{ buffer: primary, minConfidence: 80 }, { buffer: alternate, minConfidence: 80 }]);
    };
    const units_killed = await recognizeNumber(worker, numberImages(layout.killed.x / unit));
    const units_lost = await recognizeNumber(worker, numberImages(layout.lost.x / unit));
    let name = sourceRow.ocr_name, nameCandidates = [name], detectedTeam = sourceRow.detected_team;
    if (nameX !== null) {
      const nameWidth = Math.min(spacing * 1.3, layout.nameRight / unit - nameX);
      const result = await recognizeName(worker, image, { left: nameX - 4, top: y - Math.min(24, cellHeight) / 2, width: nameWidth, height: Math.min(24, cellHeight) }, name);
      name = result.text; nameCandidates = result.candidates;
      const teamCenter = nameX - spacing * 0.14;
      const teamImage = prepareRegion(image, { left: teamCenter - 15, top: y - 11, width: 30, height: 22 }, { threshold: 160, scale: 2 });
      const team = await recognizeNumber(worker, [teamImage]);
      detectedTeam = team !== null && team >= 1 && team <= 10 ? String(team) : detectedTeam;
    }
    rows.push({ ocr_name: name, ocr_name_raw: sourceRow.ocr_name, ocr_name_candidates: nameCandidates,
      detected_team: detectedTeam, units_killed, units_lost });
  }
  selected = finishMilitaryResult({ ...selected, rows }, players);
  try { await worker.setParameters({ tessedit_pageseg_mode: PSM.SPARSE_TEXT, tessedit_char_whitelist: '' }); } catch { /* Next pass also resets worker settings. */ }
  return selected;
}
module.exports = { readMilitary, recognizeNumber, recognizeName };
