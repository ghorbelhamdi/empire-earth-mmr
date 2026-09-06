'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const helpers = require('../src/ui/state');
const initialState = overrides => ({ server: 'https://ladder.example', hasToken: false, hasSavedToken: false, players: [], version: '0.1.2', status: 'Ready', watching: false, busy: false, locked: false, screenshot: null,
  draft: { submission_id: 'original-draft', rows: [], winner: '', stats_confirmed: false, evidence: null, result: null }, ...overrides });
const settled = () => new Promise(resolve => setImmediate(resolve));
function rendererHarness(initial, options = {}) {
  let current = structuredClone(initial);
  const elements = new Map(), calls = [];
  class Element {
    constructor() { this.listeners = {}; this.children = []; this.value = ''; this.textContent = ''; this.disabled = false; this.hidden = false; this.classList = { toggle() {} }; }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    append(...values) { this.children.push(...values); }
    replaceChildren(...values) { this.children = values; }
    setAttribute(name, value) { this[name] = value; }
    removeAttribute(name) { delete this[name]; }
    focus() {}
    showModal() { this.open = true; }
    close() { this.open = false; this.listeners.close?.(); }
    click() { if (!this.disabled) return this.listeners.click?.(); }
  }
  const element = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const document = { getElementById: element, createElement: () => new Element(), querySelectorAll: () => [] };
  const invoke = async (channel, value) => {
    calls.push({ channel, value: value === undefined ? undefined : JSON.parse(JSON.stringify(value)) });
    if (channel === 'state:get') return structuredClone(current);
    if (channel === 'players:load') {
      if (options.loadPlayers) return options.loadPlayers(() => structuredClone(current));
      current.players = [{ id: 1, name: 'Alice', mmr: 1000 }]; return structuredClone(current);
    }
    if (channel === 'settings:save') {
      const save = () => {
        current.hasToken = value.clearToken ? false : Boolean(value.token || current.hasToken);
        current.hasSavedToken = current.hasToken; current.server = value.server;
        return structuredClone(current);
      };
      return options.saveSettings ? options.saveSettings(save) : save();
    }
    if (channel === 'draft:save') {
      const save = () => { current.draft = { ...current.draft, ...JSON.parse(JSON.stringify(value)) }; return { saved: true }; };
      return options.saveDraft ? options.saveDraft(save) : save();
    }
    if (channel === 'draft:new') {
      if (!options.cancelNew) current.draft = { ...initialState().draft, submission_id: 'new-draft' };
      return structuredClone(current);
    }
    if (channel === 'capture:reread') { current.draft = { ...current.draft, submission_id: 'reread-draft' }; return structuredClone(current); }
    if (channel === 'watch:start') { assert.equal(current.draft.rows.length, 0, 'Existing rows must not be overwritten by watching'); current.watching = true; return { watching: true }; }
    if (channel === 'watch:stop') { current.watching = false; return { watching: false }; }
    throw new Error(`Unexpected action ${channel}`);
  };
  const context = { document, window: { companionUI: helpers, companion: { invoke, onStatus() {}, onDraft() {} } }, clearTimeout, console };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../src/ui/renderer.js'), 'utf8'), context);
  return { element, calls, current: () => current };
}

test('saved token automatically reconnects once at startup without rewriting or displaying it', async () => {
  const ui = rendererHarness(initialState({ hasToken: true })); await settled();
  assert.deepEqual(ui.calls.map(call => call.channel), ['state:get', 'players:load']);
  assert.equal(ui.element('token').value, '');
  assert.match(ui.element('token-status').textContent, /Token saved securely/);
  assert.match(ui.element('connection-status').textContent, /1 players connected/);
});
test('a failed reconnect keeps saved-token status and a useful retry message', async () => {
  const ui = rendererHarness(initialState({ hasToken: true }), { loadPlayers: async () => { throw new Error('Server is offline'); } }); await settled();
  assert.match(ui.element('connection-status').textContent, /Token saved.*connection failed/);
  assert.match(ui.element('error').textContent, /token is saved.*Server is offline/);
  assert.equal(ui.element('watch').disabled, false);
  assert.equal(ui.element('connect').disabled, false);
});

test('an unreadable saved token can be forgotten without attempting an authenticated reconnect', async () => {
  const ui = rendererHarness(initialState({ hasSavedToken: true })); await settled();
  assert.deepEqual(ui.calls.map(call => call.channel), ['state:get']);
  assert.match(ui.element('connection-status').textContent, /Saved token needs replacement/);
  assert.match(ui.element('token-status').textContent, /Windows could not unlock it.*Forget token/);
  assert.equal(ui.element('clear-token').disabled, false);
  assert.equal(ui.element('token').value, '');
  ui.element('clear-token').click(); await settled();
  assert.equal(ui.calls.at(-1).value.clearToken, true);
  assert.equal(ui.current().hasSavedToken, false);
  assert.equal(ui.element('clear-token').disabled, true);
  assert.match(ui.element('token-status').textContent, /No token saved yet/);
});

test('explicit settings save blocks new draft actions until persistence finishes, then permits offline capture during connection', async () => {
  let finishDraft, finishSettings, finishPlayers;
  const ui = rendererHarness(initialState({ screenshot: 'data:image/png;base64,local-test-image' }), {
    saveDraft: save => new Promise(resolve => { finishDraft = () => resolve(save()); }),
    saveSettings: save => new Promise(resolve => { finishSettings = () => resolve(save()); }),
    loadPlayers: current => new Promise(resolve => { finishPlayers = () => resolve(current()); })
  }); await settled();
  ui.element('server').value = 'https://another-ladder.example';
  ui.element('token').value = 'dummy-replacement-token';
  ui.element('connect').click(); await settled();
  const actions = ['watch', 'capture', 'import', 'new-match', 'reread-image', 'add-player', 'winner', 'confirmed'];
  for (const id of actions) { assert.equal(ui.element(id).disabled, true, `${id} must wait for draft persistence`); ui.element(id).click(); }
  assert.deepEqual(ui.calls.map(call => call.channel), ['state:get', 'draft:save']);
  finishDraft(); await settled();
  for (const id of actions) { assert.equal(ui.element(id).disabled, true, `${id} must wait for settings persistence`); ui.element(id).click(); }
  assert.deepEqual(ui.calls.map(call => call.channel), ['state:get', 'draft:save', 'settings:save']);
  finishSettings(); await settled();
  for (const id of ['watch', 'capture', 'import', 'new-match', 'reread-image']) assert.equal(ui.element(id).disabled, false, `${id} is local and need not wait for the network`);
  assert.equal(ui.element('connect').disabled, true);
  assert.equal(ui.element('token').value, '');
  finishPlayers(); await settled();
  assert.equal(ui.element('connect').disabled, false);
});

test('failed settings persistence restores draft actions and retains the typed replacement', async () => {
  const ui = rendererHarness(initialState(), { saveSettings: async () => { throw new Error('Cannot save settings'); } }); await settled();
  ui.element('token').value = 'dummy-replacement-token';
  ui.element('connect').click(); await settled();
  assert.equal(ui.element('watch').disabled, false);
  assert.equal(ui.element('capture').disabled, false);
  assert.equal(ui.element('new-match').disabled, false);
  assert.equal(ui.element('connect').disabled, false);
  assert.equal(ui.element('token').value, 'dummy-replacement-token');
  assert.match(ui.element('error').textContent, /Cannot save settings/);
  assert.doesNotMatch(ui.element('error').textContent, /dummy-replacement-token/);
});
test('Watch next game requires the existing draft confirmation and respects cancellation', async () => {
  const draft = { ...initialState().draft, rows: [{ player_id: 1, team: 'team1', units_killed: 3, units_lost: 7 }] };
  const ui = rendererHarness(initialState({ draft }), { cancelNew: true }); await settled();
  assert.equal(ui.element('watch').disabled, false); assert.match(ui.element('watch').textContent, /Watch next game/);
  ui.element('watch').click(); await settled();
  assert.deepEqual(ui.calls.map(call => call.channel), ['state:get', 'draft:save', 'draft:new']);
  assert.equal(ui.current().draft.submission_id, 'original-draft'); assert.equal(ui.current().draft.rows.length, 1);
});
test('Watch next game starts only after the prior draft is archived and replaced', async () => {
  const draft = { ...initialState().draft, evidence: { source: 'screenshot' } };
  const ui = rendererHarness(initialState({ draft })); await settled();
  ui.element('watch').click(); await settled();
  assert.deepEqual(ui.calls.map(call => call.channel), ['state:get', 'draft:save', 'draft:new', 'watch:start']);
  assert.equal(ui.current().draft.submission_id, 'new-draft'); assert.equal(ui.current().watching, true);
});
test('unresolved submission explains why another watch cannot start', () => {
  const description = helpers.watchState(initialState({ locked: true }));
  assert.equal(description.disabled, true); assert.match(description.hint, /Retry saved submission/);
});
test('token can be saved while watching and OCR is busy', async () => {
  const ui = rendererHarness(initialState({ watching: true, busy: true })); await settled();
  assert.equal(ui.element('connect').disabled, false);
  ui.element('token').value = 'dummy-device-token';
  ui.element('connect').click(); await settled();
  assert.deepEqual(ui.calls.map(call => call.channel), ['state:get', 'settings:save', 'players:load']);
  assert.equal(ui.current().hasToken, true); assert.equal(ui.current().watching, true);
  assert.equal(ui.element('token').value, ''); assert.match(ui.element('token-status').textContent, /Token saved securely/);
});
test('background startup reconnection does not disable local watching', async () => {
  let complete;
  const ui = rendererHarness(initialState({ hasToken: true }), { loadPlayers: current => new Promise(resolve => { complete = () => resolve(current()); }) }); await settled();
  assert.equal(ui.element('connect').disabled, true);
  assert.equal(ui.element('watch').disabled, false);
  ui.element('watch').click(); await settled();
  assert.equal(ui.current().watching, true);
  complete(); await settled();
});
test('local screenshot viewer opens at200% and supports100/400% without making API calls', async () => {
  const ui = rendererHarness(initialState({ screenshot: 'data:image/png;base64,local-test-image' })); await settled();
  ui.element('evidence-image').naturalWidth = 2560; ui.element('evidence-image').naturalHeight = 1440;
  ui.element('zoom-image').click();
  assert.equal(ui.element('image-dialog').open, true);
  assert.equal(ui.element('zoomed-image').width, 5120); assert.equal(ui.element('zoomed-image').height, 2880);
  ui.element('zoom-400').click(); assert.equal(ui.element('zoomed-image').width, 10240);
  ui.element('zoom-100').click(); assert.equal(ui.element('zoomed-image').width, 2560);
  ui.element('zoom-close').click(); assert.equal(ui.element('image-dialog').open, false);
  assert.deepEqual(ui.calls.map(call => call.channel), ['state:get']);
});
test('a suggested name is displayed but does not select a ladder player until the user chooses it', async () => {
  const draft = { ...initialState().draft, rows: [{ player_id: null, ocr_name: 'Al1ce', ocr_name_raw: 'AlIce', suggested_player_id: 1, team: 'team1', units_killed: 3, units_lost: 7 }] };
  const ui = rendererHarness(initialState({ draft, players: [{ id: 1, name: 'Alice', mmr: 1000 }] })); await settled();
  const playerCell = ui.element('players-body').children[0].children[0];
  const [select, label, suggestion] = playerCell.children;
  assert.equal(select.value, ''); assert.match(suggestion.textContent, /Possible match: Alice/);
  assert.equal(label.title, 'Initial OCR: AlIce');
  select.value = '1'; select.listeners.change(); await settled();
  assert.equal(ui.current().draft.rows[0].player_id, 1); assert.equal(suggestion.hidden, true);
});
test('Read image again uses the saved image action and is disabled during watching', async () => {
  const ui = rendererHarness(initialState({ screenshot: 'data:image/png;base64,local-test-image' })); await settled();
  ui.element('reread-image').click(); await settled();
  assert.deepEqual(ui.calls.map(call => call.channel), ['state:get', 'draft:save', 'capture:reread']);
  assert.equal(ui.current().draft.submission_id, 'reread-draft');
  const watching = rendererHarness(initialState({ screenshot: 'data:image/png;base64,local-test-image', watching: true })); await settled();
  assert.equal(watching.element('reread-image').disabled, true);
});
