'use strict';
const normalizeName = value => String(value).normalize('NFKC').toLowerCase().replace(/[^a-z0-9]/g, '');

function linesFromBlocks(blocks) {
  const words = (blocks || []).flatMap(block => (block.paragraphs || []).flatMap(paragraph => (paragraph.lines || []).flatMap(line => line.words || [])))
    .filter(word => word.text && word.bbox).sort((a, b) => (a.bbox.y0 + a.bbox.y1) / 2 - (b.bbox.y0 + b.bbox.y1) / 2);
  const groups = [];
  for (const word of words) {
    const y = (word.bbox.y0 + word.bbox.y1) / 2;
    const height = word.bbox.y1 - word.bbox.y0;
    const group = groups.find(group => Math.abs(group.y - y) <= Math.max(7, height * 0.7));
    if (group) { group.words.push(word); group.y = group.words.reduce((sum, w) => sum + (w.bbox.y0 + w.bbox.y1) / 2, 0) / group.words.length; }
    else groups.push({ y, words: [word] });
  }
  return groups.map(group => group.words.sort((a, b) => a.bbox.x0 - b.bbox.x0).map(word => word.text).join(' '));
}

function parseMilitary(data, players = []) {
  const text = String(data.text || '');
  const aligned = linesFromBlocks(data.blocks);
  const allText = text + '\n' + aligned.join('\n');
  const isMilitary = /units?\s+killed/i.test(allText) && /units?\s+lost/i.test(allText);
  if (!isMilitary) return { isMilitary: false, rows: [], text, warning: 'Open the post-game Military tab so Units Killed and Units Lost are visible.' };
  const rows = [];
  const candidates = aligned.length ? aligned : text.split(/\r?\n/);
  for (const line of candidates) {
    const match = line.replace(/[★☆]/g, ' ').match(/^\s*(?:(\d{1,2})\s+)?(.+?)\s+(\d{1,7})\s+(\d{1,7})\s*$/);
    if (!match || /units|summary|timeline|epoch|post.?game|military/i.test(match[2])) continue;
    const name = match[2].trim();
    if (!/[a-z]/i.test(name)) continue;
    const normalized = normalizeName(name);
    const matchingPlayers = players.filter(player => normalizeName(player.name) === normalized);
    rows.push({ ocr_name: name, player_id: matchingPlayers.length === 1 ? matchingPlayers[0].id : null,
      detected_team: match[1] || null, team: '', units_killed: Number(match[3]), units_lost: Number(match[4]) });
  }
  const detectedTeams = [...new Set(rows.map(row => row.detected_team).filter(Boolean))];
  if (detectedTeams.length === 2) rows.forEach(row => { row.team = row.detected_team === detectedTeams[0] ? 'team1' : row.detected_team === detectedTeams[1] ? 'team2' : ''; });
  return { isMilitary, rows, text, warning: rows.length < 2 ? 'Military screen detected, but rows could not be read. Enter or correct the values below.' : 'OCR is a draft. Check every player and number against the image; choose the winner yourself.' };
}

function isEmpireEarthWindow(name) {
  return /^Empire\s*Earth(?:\s*[-–:]\s*(?:NeoEE|dreXmod).*)?$/i.test(String(name).trim());
}

module.exports = { normalizeName, linesFromBlocks, parseMilitary, isEmpireEarthWindow };
