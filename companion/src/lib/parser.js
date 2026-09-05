'use strict';
const normalizeName = value => String(value).normalize('NFKC').toLowerCase().replace(/[^a-z0-9]/g, '');
const cleanHeader = value => String(value).toLowerCase().replace(/[^a-z]/g, '');
const centerX = word => (word.bbox.x0 + word.bbox.x1) / 2;
const centerY = word => (word.bbox.y0 + word.bbox.y1) / 2;
function wordsFromBlocks(blocks) {
  return (blocks || []).flatMap(block => (block.paragraphs || []).flatMap(paragraph => (paragraph.lines || []).flatMap(line => line.words || []))).filter(word => word.text && word.bbox);
}
function groupWords(words) {
  const groups = [];
  for (const word of [...words].sort((a, b) => centerY(a) - centerY(b))) {
    const y = centerY(word), height = word.bbox.y1 - word.bbox.y0;
    const group = groups.find(group => Math.abs(group.y - y) <= Math.max(6, Math.min(height, group.height) * 0.7));
    if (group) { group.words.push(word); group.y = group.words.reduce((sum, value) => sum + centerY(value), 0) / group.words.length; group.height = Math.max(group.height, height); }
    else groups.push({ y, height, words: [word] });
  }
  return groups.map(group => ({ ...group, words: group.words.sort((a, b) => a.bbox.x0 - b.bbox.x0) }));
}
function linesFromBlocks(blocks) { return groupWords(wordsFromBlocks(blocks)).map(group => group.words.map(word => word.text).join(' ')); }
function distance(a, b) {
  const row = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    let previous = row[0]; row[0] = i;
    for (let j = 1; j <= b.length; j++) { const saved = row[j]; row[j] = Math.min(row[j] + 1, row[j - 1] + 1, previous + (a[i - 1] === b[j - 1] ? 0 : 1)); previous = saved; }
  }
  return row[b.length];
}
function columnHeader(groups, label, allowFuzzy) {
  for (const group of groups) for (let i = 0; i < group.words.length; i++) {
    const first = group.words[i], second = group.words[i + 1];
    const firstText = cleanHeader(first.text), secondText = cleanHeader(second?.text || '');
    let matched = firstText === `units${label}`, end = first;
    if (second && (firstText === 'units' || firstText === 'unit' || allowFuzzy && distance(firstText, 'units') <= 2) &&
      (secondText === label || allowFuzzy && distance(secondText, label) <= (label === 'killed' ? 2 : 1)) && second.bbox.x0 - first.bbox.x1 < group.height * 5) { matched = true; end = second; }
    if (matched) return { x: (first.bbox.x0 + end.bbox.x1) / 2, y: group.y, height: Math.max(first.bbox.y1, end.bbox.y1) - Math.min(first.bbox.y0, end.bbox.y0) };
  }
  return null;
}
function findMilitaryLayout(data) {
  const words = wordsFromBlocks(data.blocks), groups = groupWords(words);
  const hasTitle = /post\s*game\s+statistics\s*[-–:]?\s*military/i.test(data.text || '');
  const killed = columnHeader(groups, 'killed', hasTitle), lost = columnHeader(groups, 'lost', hasTitle);
  if (!killed || !lost || lost.x <= killed.x || Math.abs(killed.y - lost.y) > Math.max(killed.height, lost.height)) return null;
  const spacing = lost.x - killed.x;
  if (spacing < Math.max(killed.height, lost.height) * 5) return null;
  const headerY = (killed.y + lost.y) / 2, headerHeight = Math.max(killed.height, lost.height);
  const nameLeft = Math.max(0, killed.x - spacing * 2.2), nameRight = killed.x - spacing * 0.65;
  const candidates = groupWords(words.filter(word => centerY(word) > headerY + headerHeight * 2 && centerY(word) < headerY + spacing * 2.8 && word.bbox.y1 - word.bbox.y0 >= headerHeight * 0.5 && word.bbox.y1 - word.bbox.y0 < spacing * 0.22));
  const rows = [];
  for (const group of candidates) {
    const names = group.words.filter(word => word.bbox.x0 >= nameLeft && word.bbox.x1 <= nameRight && /[a-z?]/i.test(word.text));
    const stats = group.words.filter(word => centerX(word) > killed.x - spacing * 0.35 && /^\d{1,7}$/.test(word.text));
    if (!names.length && !stats.length) continue;
    if (names.some(word => /summary|military|timeline|epoch|exit|statistics/i.test(word.text))) continue;
    if (rows.length && group.y - rows[rows.length - 1].y < headerHeight * 2) continue;
    if (rows.length >= 2 && group.y - rows[rows.length - 1].y > (rows[rows.length - 1].y - rows[rows.length - 2].y) * 1.9) break;
    const numericAt = x => { const found = stats.filter(word => Math.abs(centerX(word) - x) < spacing * 0.25); return found.length === 1 ? Number(found[0].text) : null; };
    const firstNameX = names.length ? Math.min(...names.map(word => word.bbox.x0)) : null;
    const team = firstNameX === null ? null : group.words.find(word => word.bbox.x1 < firstNameX && /^\d{1,2}$/.test(word.text));
    rows.push({ y: group.y, height: group.height, nameX: firstNameX, ocr_name: names.map(word => word.text).join(' ').trim(), detected_team: team?.text || null, units_killed: numericAt(killed.x), units_lost: numericAt(lost.x) });
    if (rows.length === 10) break;
  }
  return { killed, lost, spacing, headerY, headerHeight, nameLeft, nameRight, rows };
}
function matchPlayer(name, players) {
  const normalized = normalizeName(name); if (!normalized) return null;
  const matches = players.filter(player => normalizeName(player.name) === normalized); return matches.length === 1 ? matches[0].id : null;
}
function finishMilitaryResult(result, players = []) {
  result.rows = result.rows.map(row => ({ ...row, player_id: matchPlayer(row.ocr_name, players), team: '' }));
  const teams = [...new Set(result.rows.map(row => row.detected_team).filter(Boolean))];
  if (teams.length === 2) result.rows.forEach(row => { row.team = row.detected_team === teams[0] ? 'team1' : row.detected_team === teams[1] ? 'team2' : ''; });
  const complete = result.rows.filter(row => Number.isSafeInteger(row.units_killed) && Number.isSafeInteger(row.units_lost)).length;
  result.quality = { trusted_columns: Boolean(result.layout), complete_rows: complete, mapped_players: result.rows.filter(row => row.player_id).length };
  result.warning = !result.isMilitary ? 'Open the post-game Military tab so Units Killed and Units Lost are visible.' : result.rows.length < 2 ? 'Military screen detected. Add the players and check the image below; unreadable statistics remain blank.' : complete < result.rows.length ? 'Military screen detected. Some values could not be read and are blank. Check all players, teams, and statistics.' : 'OCR is a draft. Check every player and number against the image; choose the winner yourself.';
  return result;
}
function parseMilitary(data, players = []) {
  const text = String(data.text || ''), aligned = linesFromBlocks(data.blocks), allText = text + '\n' + aligned.join('\n');
  const layout = findMilitaryLayout(data);
  const isMilitary = Boolean(layout) || /units?\s+killed/i.test(allText) && /units?\s+lost/i.test(allText) || /post\s*game\s+statistics\s*[-–:]?\s*military/i.test(allText);
  const rows = [];
  if (layout) rows.push(...layout.rows.map(({ y, height, nameX, ...row }) => row));
  else if (isMilitary) for (const line of aligned.length ? aligned : text.split(/\r?\n/)) {
    const match = line.replace(/[★☆|[\]]/g, ' ').match(/^\s*(?:(\d{1,2})\s+)?(.+?)\s+(\d{1,7})\s+(\d{1,7})\s*$/);
    if (!match || /units|summary|timeline|epoch|post.?game|military/i.test(match[2]) || /\s+\d+\s*$/.test(match[2])) continue;
    // Extra trailing numbers are ambiguous without coordinates. Never use army/building stats as kills/losses.
    const name = match[2].trim(); if (!/[a-z?]/i.test(name)) continue;
    rows.push({ ocr_name: name, detected_team: match[1] || null, units_killed: Number(match[3]), units_lost: Number(match[4]) });
  }
  return finishMilitaryResult({ isMilitary, rows, text, layout }, players);
}
function selectMilitaryResult(...results) {
  const score = result => result.isMilitary ? 10000 + (result.quality?.trusted_columns ? 1000 : 0) + (result.quality?.complete_rows || 0) * 30 + result.rows.length * 5 + (result.quality?.mapped_players || 0) : 0;
  return results.flat().filter(Boolean).sort((a, b) => score(b) - score(a))[0] || parseMilitary({ text: '' });
}
function isEmpireEarthWindow(name) { return /^Empire\s*Earth(?:\s*[-–:]\s*(?:NeoEE|dreXmod).*)?$/i.test(String(name).trim()); }
module.exports = { normalizeName, wordsFromBlocks, groupWords, linesFromBlocks, findMilitaryLayout, matchPlayer, finishMilitaryResult, parseMilitary, selectMilitaryResult, isEmpireEarthWindow };
