'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { pathToFileURL } = require('node:url');
const { normalizeServer } = require('../src/lib/client');

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const plain = value => JSON.parse(JSON.stringify(value));

// Execute the real main process and its IPC guards without starting Electron,
// accessing a real profile, making requests, or creating capture windows.
async function mainHarness(options = {}) {
  const sourceDir = path.resolve(__dirname, '../src');
  const uiURL = pathToFileURL(path.join(sourceDir, 'ui/index.html')).href;
  const settings = options.settings || { server: 'https://old.example', encryptedToken: Buffer.from('cipher:old-token').toString('base64') };
  const files = new Map([['settings.json', JSON.stringify(settings)]]);
  const ready = deferred(), ipc = new Map(), timers = new Map(), windows = [];
  const calls = { writes: [], renames: [], requests: [], captures: 0, messages: [] };
  let writeSettings = options.writeSettings || (async () => {});
  let renameSettings = options.renameSettings || (async () => {});
  let request = options.request || (async (_server, _token, endpoint) => endpoint.endsWith('/players') ? { players: [{ id: 1, name: 'Old player' }] } : { api_version: 1 });
  const fakeFS = {
    mkdir: async () => {},
    readFile: async filename => {
      const name = path.basename(filename);
      if (!files.has(name)) throw new Error('ENOENT');
      return files.get(name);
    },
    writeFile: async (filename, data) => {
      const name = path.basename(filename); calls.writes.push({ name, data: String(data) });
      if (name === 'settings.json.tmp') await writeSettings();
      files.set(name, String(data));
    },
    rename: async (from, to) => {
      calls.renames.push({ from: path.basename(from), to: path.basename(to) });
      if (path.basename(to) === 'settings.json') await renameSettings();
      files.set(path.basename(to), files.get(path.basename(from))); files.delete(path.basename(from));
    }
  };
  class FakeWindow {
    constructor() {
      this.webContents = { mainFrame: { url: uiURL }, send: (channel, value) => calls.messages.push({ channel, value }),
        setWindowOpenHandler: () => {}, on: () => {}, session: { setPermissionRequestHandler: () => {} } };
      windows.push(this);
    }
    removeMenu() {}
    on() {}
    async loadFile() { ready.resolve(); }
  }
  const electron = {
    app: { requestSingleInstanceLock: () => true, on: () => {}, whenReady: () => Promise.resolve(), getPath: () => 'mock-profile', getVersion: () => 'test', quit: () => {}, exit: () => {} },
    BrowserWindow: FakeWindow,
    ipcMain: { handle: (channel, handler) => ipc.set(channel, handler), on: () => {}, removeListener: () => {} },
    desktopCapturer: { getSources: async () => { calls.captures++; return []; } },
    session: {}, dialog: { showErrorBox: (_title, message) => ready.reject(new Error(message)) }, nativeImage: {}, shell: {},
    safeStorage: { isEncryptionAvailable: () => true, encryptString: value => Buffer.from('cipher:' + value),
      decryptString: value => { if (options.decryptFails) throw new Error('Windows cannot decrypt'); return value.toString().replace(/^cipher:/, ''); } }
  };
  const requireMock = name => {
    if (name === 'electron') return electron;
    if (name === 'node:fs/promises') return fakeFS;
    if (name === 'tesseract.js') return { createWorker: () => { throw new Error('Unexpected OCR startup'); } };
    if (name === './lib/client') return { normalizeServer, apiRequest: (...args) => { calls.requests.push(args); return request(...args); } };
    if (name === './lib/ocr') return { readMilitary: () => { throw new Error('Unexpected OCR call'); } };
    if (name.startsWith('./')) return require(path.join(sourceDir, name));
    return require(name);
  };
  const context = vm.createContext({ require: requireMock, __dirname: sourceDir, Buffer, console,
    process: { argv: ['node', 'main.js'] },
    setTimeout: (callback, delay) => { const id = Symbol('timer'); timers.set(id, { callback, delay }); return id; },
    clearTimeout: id => timers.delete(id) });
  const source = fs.readFileSync(path.join(sourceDir, 'main.js'), 'utf8');
  vm.runInContext(source + `\n;globalThis.testMain = {
    watchTick, takeCapture,
    snapshot: () => ({ config, token, watching, watchGeneration, settingsSaving, players, status })
  };`, context, { filename: 'main.js' });
  await ready.promise;
  const invoke = async (channel, value) => {
    const webContents = windows[0].webContents;
    return plain(await ipc.get(channel)({ sender: webContents, senderFrame: webContents.mainFrame }, value));
  };
  return { invoke, calls, timers, files, internal: context.testMain,
    snapshot: () => plain(context.testMain.snapshot()),
    setWrite: next => { writeSettings = next; }, setRename: next => { renameSettings = next; }, setRequest: next => { request = next; },
    flush: () => new Promise(resolve => setImmediate(resolve)) };
}

test('settings writes block capture and watch IPC while background watch skips capture', async () => {
  const harness = await mainHarness();
  await harness.invoke('watch:start'); await harness.flush();
  const before = harness.snapshot(), captureCount = harness.calls.captures;
  const writing = deferred(); harness.setWrite(() => writing.promise);
  const pending = harness.invoke('settings:save', { server: 'https://new.example', token: 'new-token' });
  assert.equal(harness.snapshot().settingsSaving, true);
  for (const channel of ['capture:game', 'capture:import', 'capture:reread', 'watch:start', 'settings:save']) {
    const blocked = await harness.invoke(channel, { server: 'https://another.example' });
    assert.equal(blocked.ok, false); assert.match(blocked.error, /settings.*saving/);
  }
  await assert.rejects(harness.internal.takeCapture({ manual: true }), /settings.*saving/);
  await harness.internal.watchTick();
  assert.equal(harness.calls.captures, captureCount);
  assert.equal(harness.snapshot().watching, before.watching);
  assert.equal(harness.snapshot().watchGeneration, before.watchGeneration);
  assert.equal(harness.snapshot().token, 'old-token');
  writing.resolve();
  const saved = await pending;
  assert.equal(saved.ok, true); assert.equal(saved.value.server, 'https://new.example');
  assert.equal(saved.value.hasToken, true); assert.equal(saved.value.hasSavedToken, true);
  assert.equal(harness.snapshot().settingsSaving, false); assert.equal(harness.snapshot().watching, false);
});

test('failed settings write or rename preserves config, token, player list and running watch', async () => {
  for (const stage of ['write', 'rename']) {
    const harness = await mainHarness();
    await harness.invoke('players:load'); await harness.invoke('watch:start'); await harness.flush();
    const before = harness.snapshot(), savedFile = harness.files.get('settings.json');
    const fail = async () => { throw new Error('Disk write failed'); };
    if (stage === 'write') harness.setWrite(fail); else harness.setRename(fail);
    const result = await harness.invoke('settings:save', { server: 'https://new.example', token: 'replacement' });
    assert.equal(result.ok, false); assert.match(result.error, /Disk write failed/);
    assert.deepEqual(harness.snapshot(), before, stage);
    assert.equal(harness.files.get('settings.json'), savedFile, stage);
  }
});

test('stale player responses cannot replace the roster after server or token changes', async () => {
  for (const server of ['https://old.example', 'https://new.example']) {
    const harness = await mainHarness(), response = deferred();
    harness.setRequest((_server, _token, endpoint) => endpoint.endsWith('/players') ? response.promise : Promise.resolve({ api_version: 1 }));
    const pendingPlayers = harness.invoke('players:load');
    assert.equal((await harness.invoke('settings:save', { server, token: 'replacement' })).ok, true);
    response.resolve({ players: [{ id: 55, name: 'Stale response' }] });
    const stale = await pendingPlayers;
    assert.equal(stale.ok, false); assert.match(stale.error, /settings changed/);
    assert.deepEqual(harness.snapshot().players, []);
    assert.equal(harness.snapshot().config.server, server);
  }
});

test('saved-but-undecryptable token remains distinct from an available token and blank saves preserve it', async () => {
  const harness = await mainHarness({ decryptFails: true });
  const state = (await harness.invoke('state:get')).value;
  assert.equal(state.hasSavedToken, true); assert.equal(state.hasToken, false);
  const before = harness.files.get('settings.json');
  const result = await harness.invoke('settings:save', { server: state.server, token: '' });
  assert.equal(result.ok, false); assert.match(result.error, /Windows could not unlock/);
  assert.equal(harness.files.get('settings.json'), before); assert.equal(harness.calls.writes.length, 0);
  assert.equal((await harness.invoke('settings:save', { server: state.server, token: 'replacement' })).ok, true);
  const replaced = (await harness.invoke('state:get')).value;
  assert.equal(replaced.hasToken, true); assert.equal(replaced.hasSavedToken, true);
});

test('forgetting a saved token clears both token states only after successful persistence', async () => {
  const harness = await mainHarness(), writing = deferred();
  harness.setWrite(() => writing.promise);
  const pending = harness.invoke('settings:save', { server: 'https://old.example', clearToken: true });
  const during = (await harness.invoke('state:get')).value;
  assert.equal(during.hasToken, true); assert.equal(during.hasSavedToken, true);
  writing.resolve(); assert.equal((await pending).ok, true);
  const after = (await harness.invoke('state:get')).value;
  assert.equal(after.hasToken, false); assert.equal(after.hasSavedToken, false);
});
