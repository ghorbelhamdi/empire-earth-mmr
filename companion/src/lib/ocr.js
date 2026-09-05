'use strict';
const { PSM } = require('tesseract.js');
const { readImage, fullImageScale, prepareRegion, prepareForOCR } = require('./image');
const { parseMilitary, selectMilitaryResult, finishMilitaryResult } = require('./parser');
async function recognizeNumber(worker, images) {
  try { await worker.setParameters({ tessedit_pageseg_mode: PSM.SINGLE_LINE, tessedit_char_whitelist: '0123456789' }); } catch { return null; }
  for (const image of images) try {
    const { data } = await worker.recognize(image, {}, { text: true });
    const text = data.text.trim();
    if (/^\d{1,7}$/.test(text) && Number(text) <= 1000000 && data.confidence >= 60) return Number(text);
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
    const numberImages = x => [
      prepareRegion(image, { left: x - cellWidth / 2, top: y - cellHeight / 2, width: cellWidth, height: cellHeight }),
      prepareRegion(image, { left: x - Math.min(100, cellWidth) / 2, top: y - Math.min(32, rowGap * 0.65) / 2, width: Math.min(100, cellWidth), height: Math.min(32, rowGap * 0.65) }, { threshold: 120, scale: 2 })
    ];
    const units_killed = await recognizeNumber(worker, numberImages(layout.killed.x / unit));
    const units_lost = await recognizeNumber(worker, numberImages(layout.lost.x / unit));
    let name = sourceRow.ocr_name, detectedTeam = sourceRow.detected_team;
    if (nameX !== null) {
      const nameWidth = Math.min(spacing * 1.3, layout.nameRight / unit - nameX);
      try {
        await worker.setParameters({ tessedit_pageseg_mode: PSM.SINGLE_LINE, tessedit_char_whitelist: '' });
        const nameImage = prepareRegion(image, { left: nameX - 3, top: y - cellHeight / 2, width: nameWidth, height: cellHeight }, { threshold: 145, scale: 4 });
        const result = await worker.recognize(nameImage, {}, { text: true });
        const candidate = result.data.text.trim().replace(/^[|[\]\s]+|[|[\]\s]+$/g, '');
        if (candidate && /[a-z?]/i.test(candidate)) name = candidate;
      } catch { /* Keep the first name for explicit mapping. */ }
      const teamCenter = nameX - spacing * 0.14;
      const teamImage = prepareRegion(image, { left: teamCenter - 15, top: y - 11, width: 30, height: 22 }, { threshold: 160, scale: 2 });
      const team = await recognizeNumber(worker, [teamImage]);
      detectedTeam = team !== null && team >= 1 && team <= 10 ? String(team) : detectedTeam;
    }
    rows.push({ ocr_name: name, detected_team: detectedTeam, units_killed, units_lost });
  }
  selected = finishMilitaryResult({ ...selected, rows }, players);
  try { await worker.setParameters({ tessedit_pageseg_mode: PSM.SPARSE_TEXT, tessedit_char_whitelist: '' }); } catch { /* Next pass also resets worker settings. */ }
  return selected;
}
module.exports = { readMilitary, recognizeNumber };
