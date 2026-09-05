'use strict';
const $ = id => document.getElementById(id);
const api = (channel, value) => window.companion.invoke(channel, value);
let state, previewReady = false, actionBusy = false, connectionBusy = false, connectionFailed = false, saveTimer, zoomLevel = 2, imageDrag = null;
function showError(error) { $('error').textContent = error.message || String(error); $('error').hidden = false; }
function clearError() { $('error').hidden = true; }
function updateControls() {
  if (!state) return;
  const blocked = actionBusy || state.busy || state.watching;
  const locked = state.locked || Boolean(state.draft.result);
  for (const id of ['capture', 'import']) $(id).disabled = blocked || locked;
  for (const id of ['add-player', 'winner', 'confirmed']) $(id).disabled = blocked || locked || connectionBusy;
  $('add-player').disabled ||= state.draft.rows.length >= 10;
  const watch = window.companionUI.watchState(state, actionBusy);
  $('watch').disabled = watch.disabled;
  $('watch').textContent = watch.label;
  $('watch-hint').textContent = watch.hint;
  $('new-match').disabled = blocked;
  $('connect').disabled = actionBusy || connectionBusy;
  $('clear-token').disabled = actionBusy || connectionBusy || !state.hasToken;
  $('zoom-image').disabled = !state.screenshot;
  $('reread-image').disabled = !state.screenshot || blocked || locked;
  $('image-tools').hidden = !state.screenshot;
  $('connect').textContent = connectionBusy ? 'Connecting…' : 'Save & connect';
  const connection = window.companionUI.connectionState(state, connectionBusy, connectionFailed);
  $('connection-status').textContent = connection.status;
  $('token-status').textContent = connection.tokenNote;
  $('token').placeholder = connection.tokenPlaceholder;
  $('preview-button').disabled = blocked || connectionBusy || Boolean(state.draft.result) || !state.players.length || !state.draft.stats_confirmed;
  $('submit').disabled = blocked || connectionBusy || Boolean(state.draft.result) || (!state.locked && (!previewReady || !state.draft.stats_confirmed));
  $('submit').textContent = state.locked && !state.draft.result ? 'Retry saved submission →' : 'Submit for approval →';
  document.querySelectorAll('#players-body input, #players-body select, #players-body button').forEach(element => { element.disabled = blocked || locked || connectionBusy; });
  $('status-bar').classList.toggle('watching', state.watching);
}
function setStatus(value) {
  if (state) Object.assign(state, value);
  $('status-text').textContent = value.status || state?.status || '';
  updateControls();
}
function option(value, label) { const result = document.createElement('option'); result.value = value; result.textContent = label; return result; }
function applyZoom(level) {
  zoomLevel = level;
  const image = $('zoomed-image');
  const width = image.naturalWidth || $('evidence-image').naturalWidth;
  const height = image.naturalHeight || $('evidence-image').naturalHeight;
  if (width && height) { image.width = Math.round(width * level); image.height = Math.round(height * level); }
  for (const [id, value] of [['zoom-100', 1], ['zoom-200', 2], ['zoom-400', 4]]) $(id).setAttribute('aria-pressed', String(value === level));
  $('zoom-level').textContent = `${Math.round(level * 100)}%`;
}
function openImageZoom() {
  if (!state.screenshot) return;
  const image = $('zoomed-image');
  image.onload = () => applyZoom(zoomLevel);
  image.src = state.screenshot;
  applyZoom(2);
  $('image-dialog').showModal();
  $('image-viewport').scrollLeft = 0; $('image-viewport').scrollTop = 0;
}
function edited() {
  state.draft.stats_confirmed = false;
  $('confirmed').checked = false;
  previewReady = false; $('preview').hidden = true;
  scheduleSave(); updateControls();
}
function scheduleSave() { void persist().catch(showError); }
async function persist() {
  clearTimeout(saveTimer);
  if (state.locked || state.draft.result) return;
  await api('draft:save', { rows: state.draft.rows, winner: state.draft.winner, stats_confirmed: state.draft.stats_confirmed });
}
window.flushDraftForClose = () => state?.locked || !state ? Promise.resolve() : persist();
function renderRows() {
  $('players-body').replaceChildren();
  for (const [index, row] of state.draft.rows.entries()) {
    const tr = document.createElement('tr');
    const playerCell = document.createElement('td');
    const select = document.createElement('select'); select.setAttribute('aria-label', `Ladder player for row ${index + 1}`);
    select.append(option('', state.players.length ? 'Match player…' : 'Connect ladder first…'));
    state.players.forEach(player => select.append(option(player.id, player.name)));
    if (row.player_id && !state.players.some(player => player.id === row.player_id)) select.append(option(row.player_id, `Player #${row.player_id} · reconnect`));
    select.value = row.player_id || '';
    let suggestion = null;
    select.addEventListener('change', () => { row.player_id = select.value ? Number(select.value) : null; if (suggestion) suggestion.hidden = Boolean(row.player_id); edited(); });
    playerCell.append(select);
    if (row.ocr_name) { const label = document.createElement('div'); label.className = 'ocr-name'; label.textContent = `Read: ${row.ocr_name}`; if (row.ocr_name_raw && row.ocr_name_raw !== row.ocr_name) label.setAttribute('title', `Initial OCR: ${row.ocr_name_raw}`); playerCell.append(label); }
    const possible = state.players.find(player => player.id === row.suggested_player_id);
    if (possible && !row.player_id) { suggestion = document.createElement('div'); suggestion.className = 'ocr-name'; suggestion.textContent = `Possible match: ${possible.name}. Select this player in the list to confirm.`; playerCell.append(suggestion); }
    tr.append(playerCell);
    const teamCell = document.createElement('td');
    const team = document.createElement('select'); team.setAttribute('aria-label', `Team for row ${index + 1}`);
    team.append(option('', 'Select…'), option('team1', 'Team 1'), option('team2', 'Team 2')); team.value = row.team || '';
    team.addEventListener('change', () => { row.team = team.value; edited(); }); teamCell.append(team); tr.append(teamCell);
    for (const field of ['units_killed', 'units_lost']) {
      const cell = document.createElement('td'); const input = document.createElement('input'); input.type = 'number'; input.min = '0'; input.max = '1000000'; input.step = '1'; input.placeholder = '—'; input.value = row[field] ?? '';
      input.setAttribute('aria-label', `${field === 'units_killed' ? 'Units killed' : 'Units lost'} for row ${index + 1}`);
      input.addEventListener('input', () => { row[field] = input.value === '' ? null : Number(input.value); edited(); }); cell.append(input); tr.append(cell);
    }
    const removeCell = document.createElement('td'); const remove = document.createElement('button'); remove.className = 'remove'; remove.textContent = '×'; remove.setAttribute('aria-label', `Remove player row ${index + 1}`);
    remove.addEventListener('click', () => { state.draft.rows.splice(index, 1); edited(); renderRows(); updateControls(); }); removeCell.append(remove); tr.append(removeCell);
    $('players-body').append(tr);
  }
  $('rows-empty').hidden = state.draft.rows.length > 0;
  $('player-count').textContent = `${state.draft.rows.length} / 10 players`;
}
function render(next) {
  state = next;
  $('server').value = state.server;
  $('version').textContent = `v${state.version}`;
  if (!state.hasToken) $('connection').open = true;
  $('winner').value = state.draft.winner || '';
  $('confirmed').checked = state.draft.stats_confirmed === true;
  $('draft-id').textContent = state.draft.submission_id;
  $('draft-tag').textContent = state.draft.result ? String(state.draft.result.status).toUpperCase() : state.locked ? 'RETRY SAVED' : 'DRAFT';
  $('evidence-image').hidden = !state.screenshot; $('image-empty').hidden = Boolean(state.screenshot);
  if (state.screenshot) $('evidence-image').src = state.screenshot; else $('evidence-image').removeAttribute('src');
  $('image-frame').classList.toggle('has-image', Boolean(state.screenshot));
  $('ocr-text').textContent = state.draft.ocr_text || 'No recognized text. You can enter the statistics manually.';
  $('receipt').hidden = !state.draft.result;
  if (state.draft.result) $('receipt').textContent = `Match #${state.draft.result.match_id} · ${state.draft.result.status}. ${state.draft.result.status === 'pending' ? 'Your match is on the ladder’s approval queue.' : 'The administrator has already reviewed this match.'}`;
  $('submission-note').textContent = state.locked && !state.draft.result ? 'This submission is saved and locked. Retry sends the same ID and values so a connection failure cannot submit it twice.' : 'Nothing is submitted automatically. Your draft saves on this device.';
  renderRows(); setStatus(state); updateControls();
}
async function action(callback) {
  if (actionBusy) return;
  clearError(); actionBusy = true; updateControls();
  try { await callback(); }
  catch (error) { showError(error); }
  finally { actionBusy = false; updateControls(); }
}
async function connectLadder({ saveSettings = false } = {}) {
  if (connectionBusy) return;
  clearError(); connectionBusy = true; connectionFailed = false; updateControls();
  let savedSettings = !saveSettings;
  try {
    if (saveSettings) {
      if (!state.busy && !state.watching) await persist();
      previewReady = false; $('preview').hidden = true;
      render(await api('settings:save', { server: $('server').value, token: $('token').value }));
      savedSettings = true;
      $('token').value = '';
    }
    render(await api('players:load'));
    if (saveSettings) $('connection').open = false;
  } catch (error) {
    connectionFailed = true; $('connection').open = true;
    showError(new Error(`${savedSettings && state.hasToken ? 'Your token is saved on this PC. ' : ''}${error.message}`));
  } finally { connectionBusy = false; updateControls(); }
}
$('connect').addEventListener('click', () => connectLadder({ saveSettings: true }));
$('clear-token').addEventListener('click', () => action(async () => { render(await api('settings:save', { server: state.server, clearToken: true })); $('token').value = ''; connectionFailed = false; }));
$('watch').addEventListener('click', () => {
  if (state.watching) { api('watch:stop').then(value => { Object.assign(state, value); updateControls(); }).catch(showError); return; }
  const startNew = window.companionUI.watchState(state).startNew;
  action(async () => {
    await persist();
    if (startNew) {
      const previousId = state.draft.submission_id;
      render(await api('draft:new'));
      if (state.draft.submission_id === previousId) return;
      previewReady = false; $('preview').hidden = true;
    }
    Object.assign(state, await api('watch:start')); updateControls();
  });
});
for (const [id, channel] of [['capture', 'capture:game'], ['import', 'capture:import']]) $(id).addEventListener('click', () => action(async () => { await persist(); const next = await api(channel); previewReady = false; $('preview').hidden = true; render(next); }));
$('new-match').addEventListener('click', () => action(async () => { await persist(); render(await api('draft:new')); previewReady = false; $('preview').hidden = true; }));
$('add-player').addEventListener('click', () => { state.draft.rows.push({ ocr_name: '', player_id: null, team: '', units_killed: null, units_lost: null }); edited(); renderRows(); updateControls(); });
$('winner').addEventListener('change', () => { state.draft.winner = $('winner').value; edited(); });
$('confirmed').addEventListener('change', () => { state.draft.stats_confirmed = $('confirmed').checked; previewReady = false; $('preview').hidden = true; scheduleSave(); updateControls(); });
$('preview-button').addEventListener('click', () => action(async () => {
  await persist(); const result = await api('match:preview');
  $('preview-values').replaceChildren();
  for (const [name, delta] of Object.entries(result.changes || {})) { const chip = document.createElement('div'); chip.className = 'rating-chip'; const label = document.createElement('span'); label.textContent = name; const amount = document.createElement('strong'); amount.textContent = String(delta); amount.classList.toggle('negative', String(delta).startsWith('-')); chip.append(label, amount); $('preview-values').append(chip); }
  $('preview').hidden = false; previewReady = true;
}));
$('submit').addEventListener('click', () => action(async () => {
  await persist();
  try { await api('match:submit'); }
  finally { render(await api('state:get')); }
}));
$('open-captures').addEventListener('click', () => action(() => api('captures:open')));
$('zoom-image').addEventListener('click', openImageZoom);
$('reread-image').addEventListener('click', () => action(async () => {
  await persist();
  const next = await api('capture:reread');
  previewReady = false; $('preview').hidden = true; render(next);
}));
$('zoom-close').addEventListener('click', () => $('image-dialog').close());
for (const [id, level] of [['zoom-100', 1], ['zoom-200', 2], ['zoom-400', 4]]) $(id).addEventListener('click', () => applyZoom(level));
$('image-dialog').addEventListener('close', () => { imageDrag = null; $('zoom-image').focus(); });
$('image-dialog').addEventListener('click', event => { if (event.target === $('image-dialog')) $('image-dialog').close(); });
$('image-viewport').addEventListener('pointerdown', event => {
  if (event.button !== 0) return;
  imageDrag = { x: event.clientX, y: event.clientY, left: $('image-viewport').scrollLeft, top: $('image-viewport').scrollTop };
  $('image-viewport').setPointerCapture(event.pointerId); $('image-viewport').focus(); event.preventDefault();
});
$('image-viewport').addEventListener('pointermove', event => {
  if (!imageDrag) return;
  $('image-viewport').scrollLeft = imageDrag.left + imageDrag.x - event.clientX;
  $('image-viewport').scrollTop = imageDrag.top + imageDrag.y - event.clientY;
});
for (const event of ['pointerup', 'pointercancel', 'lostpointercapture']) $('image-viewport').addEventListener(event, () => { imageDrag = null; });
window.companion.onStatus(setStatus);
window.companion.onDraft(next => { previewReady = false; $('preview').hidden = true; render(next); });
api('state:get').then(async next => { render(next); if (next.hasToken) await connectLadder(); }).catch(showError);
