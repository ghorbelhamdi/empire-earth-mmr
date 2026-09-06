'use strict';
const { app, BrowserWindow, ipcMain, desktopCapturer, session, dialog, nativeImage, safeStorage, shell } = require('electron');
const fs = require('node:fs/promises');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { pathToFileURL } = require('node:url');
const { createWorker } = require('tesseract.js');
const { normalizeServer, apiRequest } = require('./lib/client');
const { newDraft, buildPayload, submitDraft } = require('./lib/draft');
const { isEmpireEarthWindow } = require('./lib/parser');
const { readMilitary } = require('./lib/ocr');

const DEFAULT_SERVER = 'https://empire-mmr.duckdns.org';
let mainWindow, captureWindow, config = { server: DEFAULT_SERVER }, token = '', players = [], draft = newDraft();
let watching = false, watchTimer, watchGeneration = 0, busy = false, submitting = false, settingsSaving = false, workerPromise;
let status = 'Connect your ladder, then capture a Military screen or enter a match.';
let dataDir;
const uiFile = path.join(__dirname, 'ui', 'index.html');
const smokeMode = process.argv.includes('--smoke-test');
const smokeProfile = process.argv.find(argument => argument.startsWith('--smoke-profile='))?.slice('--smoke-profile='.length);
const smokeWatchImage = process.argv.find(argument => argument.startsWith('--smoke-watch-image='));
const smokeWatch = smokeMode && (process.argv.includes('--smoke-watch-game') || Boolean(smokeWatchImage));
// Diagnostic runs must not load or modify a player's real token and match draft.
if (smokeMode) {
  if (smokeProfile && !/^[a-z0-9_-]{1,64}$/i.test(smokeProfile)) throw new Error('Use a simple name for the isolated smoke profile.');
  app.setPath('userData', path.join(app.getPath('temp'), `empire-earth-companion-smoke${smokeProfile ? '-' + smokeProfile : ''}`));
}
if (!app.requestSingleInstanceLock()) app.exit(0);
app.on('second-instance', () => { if (mainWindow?.isMinimized()) mainWindow.restore(); mainWindow?.focus(); });

async function readJSON(filename, fallback) {
  try { return JSON.parse(await fs.readFile(path.join(dataDir, filename), 'utf8')); } catch { return fallback; }
}
async function writeJSON(filename, value) {
  const destination = path.join(dataDir, filename);
  await fs.writeFile(destination + '.tmp', JSON.stringify(value, null, 2));
  await fs.rename(destination + '.tmp', destination);
}
let saveChain = Promise.resolve();
function saveDraft() {
  const value = JSON.parse(JSON.stringify(draft));
  saveChain = saveChain.catch(() => {}).then(() => writeJSON('draft.json', value));
  return saveChain;
}
async function archiveDraft() {
  if (draft.rows.length || draft.evidence || draft.pendingPayload) {
    await saveChain;
    await fs.mkdir(path.join(dataDir, 'history'), { recursive: true });
    await fs.writeFile(path.join(dataDir, 'history', `${draft.submission_id}.json`), JSON.stringify(draft, null, 2));
  }
}
function updateStatus(message) { status = message; mainWindow?.webContents.send('state:status', { status, watching, busy }); }
async function publicState() {
  let screenshot = null;
  if (draft.screenshotPath) {
    try { screenshot = 'data:image/png;base64,' + (await fs.readFile(draft.screenshotPath)).toString('base64'); } catch { /* Draft remains usable if the local image was removed. */ }
  }
  const { pendingPayload, screenshotPath, ...publicDraft } = draft;
  return { server: config.server, hasToken: Boolean(token), hasSavedToken: Boolean(config.encryptedToken), players, draft: publicDraft, locked: Boolean(pendingPayload), screenshot, watching, busy, status, version: app.getVersion() };
}
function stopWatch() {
  watching = false;
  watchGeneration += 1;
  clearTimeout(watchTimer);
}
function ensureEditable() {
  if (draft.pendingPayload) throw new Error('This draft was submitted or is awaiting a retry. Retry the saved submission or start a new match.');
}

async function ocr(buffer) {
  if (!workerPromise) workerPromise = createWorker('eng', 1, {
    langPath: path.dirname(require.resolve('@tesseract.js-data/eng/4.0.0_best_int/eng.traineddata.gz')),
    cachePath: path.join(dataDir, 'ocr'), cacheMethod: 'none', gzip: true,
    logger: () => {}
  }).catch(error => { workerPromise = null; throw error; });
  const worker = await workerPromise;
  return readMilitary(worker, buffer, players);
}

async function captureGameWindow(testSourceId = null) {
  // Metadata enumeration uses zero-size thumbnails. Only the selected game window gets a video stream.
  const sources = await desktopCapturer.getSources({ types: ['window'], thumbnailSize: { width: 0, height: 0 } });
  const games = sources.filter(source => testSourceId ? source.id === testSourceId : isEmpireEarthWindow(source.name));
  if (!games.length) throw new Error('Empire Earth window not found. Open the original game in windowed mode, or import a screenshot.');
  if (games.length > 1) throw new Error('More than one Empire Earth window was found. Close extra game windows before capturing.');
  const captureSession = session.fromPartition('empire-capture');
  captureSession.setPermissionRequestHandler((_webContents, permission, callback) => callback(permission === 'media' || permission === 'display-capture'));
  captureSession.setDisplayMediaRequestHandler((_request, callback) => callback({ video: games[0] }));
  const win = new BrowserWindow({ show: false, width: 400, height: 300, webPreferences: {
    preload: path.join(__dirname, 'capture-preload.js'), session: captureSession,
    contextIsolation: true, sandbox: true, nodeIntegration: false, backgroundThrottling: false
  } });
  captureWindow = win;
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', event => event.preventDefault());
  try {
    return await new Promise((resolve, reject) => {
      let phase = 'Opening the capture helper';
      const progress = (event, value) => { if (event.sender === win.webContents) phase = String(value); };
      ipcMain.on('capture:progress', progress);
      const timeout = setTimeout(() => finish(new Error(`${phase} timed out. Use windowed mode or import a saved screenshot.`)), 15000);
      const finish = (error, value) => {
        clearTimeout(timeout);
        ipcMain.removeListener('capture:complete', complete);
        ipcMain.removeListener('capture:progress', progress);
        if (error) reject(error); else resolve(value);
      };
      const complete = (event, value) => {
        if (event.sender !== win.webContents || event.senderFrame !== win.webContents.mainFrame) return;
        if (value.error) return finish(new Error(String(value.error).slice(0, 250)));
        if (typeof value.image !== 'string' || !value.image.startsWith('data:image/png;base64,') || value.image.length > 50_000_000) return finish(new Error('Invalid game capture.'));
        finish(null, Buffer.from(value.image.split(',')[1], 'base64'));
      };
      ipcMain.on('capture:complete', complete);
      win.loadFile(path.join(__dirname, 'ui', 'capture.html')).then(() => win.webContents.executeJavaScript('window.captureGame()', true)).catch(error => finish(error));
    });
  } finally { if (!win.isDestroyed()) win.destroy(); if (captureWindow === win) captureWindow = null; }
}

async function acceptScreenshot(buffer, result) {
  const sha256 = createHash('sha256').update(buffer).digest('hex');
  const screenshotPath = path.join(dataDir, 'captures', `${sha256}.png`);
  await fs.mkdir(path.dirname(screenshotPath), { recursive: true });
  await fs.writeFile(screenshotPath, buffer);
  await archiveDraft();
  draft = { ...newDraft(), screenshotPath, evidence: { sha256, source: 'screenshot', captured_at: new Date().toISOString() },
    rows: result.rows, ocr_text: result.text.slice(0, 12000), warning: result.warning };
  await saveDraft();
  stopWatch();
  updateStatus(result.warning);
  mainWindow?.webContents.send('state:draft', await publicState());
}

async function takeCapture({ manual = false, imported = null } = {}) {
  ensureEditable();
  if (settingsSaving) throw new Error('Wait for connection settings to finish saving.');
  if (busy) throw new Error('A capture is already being read.');
  busy = true;
  const generation = watchGeneration;
  updateStatus(imported ? 'Reading the imported image locally…' : 'Looking for the Empire Earth Military screen…');
  try {
    const buffer = imported || await captureGameWindow();
    updateStatus('Reading game statistics locally…');
    let result;
    try { result = await ocr(buffer); }
    catch (error) {
      if (!manual) throw new Error('Local OCR could not read the capture. Import an image or enter statistics manually.');
      result = { rows: [], text: '', warning: 'The image is saved locally. OCR failed; enter its statistics manually.' };
    }
    if (!manual && (!watching || watchGeneration !== generation)) return;
    // A recognized Military screen is useful even when OCR misses player rows.
    // Keep the evidence and expose editable fields instead of silently polling it forever.
    if (manual || result.isMilitary) await acceptScreenshot(buffer, result);
    else updateStatus('Watching Empire Earth. Open the post-game Military tab when the match ends.');
  } catch (error) { updateStatus(error.message); if (manual) throw error; }
  finally { busy = false; updateStatus(status); }
}
async function watchTick() {
  if (!watching) return;
  if (!settingsSaving) await takeCapture();
  if (watching) watchTimer = setTimeout(watchTick, 8000);
}

const handlers = {
  'state:get': () => publicState(),
  'settings:save': async value => {
    if (submitting) throw new Error('Wait for the submission to finish.');
    const server = normalizeServer(value.server);
    const serverChanged = server !== config.server;
    if (serverChanged && draft.pendingPayload && !draft.result) throw new Error('Retry the pending submission before changing servers.');
    if (serverChanged && busy) throw new Error('Wait for the current capture to finish before changing the server.');
    if (!value.clearToken && !serverChanged && !String(value.token || '').trim() && config.encryptedToken && !token) {
      throw new Error('A token is saved, but Windows could not unlock it. Paste a replacement token or use Forget token. Your saved token has not been changed.');
    }
    let nextToken = typeof value.token === 'string' && value.token.trim() ? value.token.trim() : serverChanged ? '' : token;
    if (value.clearToken) nextToken = '';
    if (nextToken.length > 4096 || /[\r\n]/.test(nextToken)) throw new Error('Invalid token.');
    if (nextToken && !safeStorage.isEncryptionAvailable()) throw new Error('Windows secure credential storage is unavailable. The token was not saved.');
    const nextConfig = { server, encryptedToken: nextToken ? safeStorage.encryptString(nextToken).toString('base64') : null };
    settingsSaving = true;
    try {
      await writeJSON('settings.json', nextConfig);
      config = nextConfig;
      token = nextToken;
      players = [];
      if (serverChanged) stopWatch();
      updateStatus(token ? 'Connection saved. Load players to check your token.' : 'Enter a companion token from the ladder administrator.');
      if (serverChanged && !draft.pendingPayload) {
        draft.rows = draft.rows.map(row => ({ ...row, player_id: null, suggested_player_id: null, suggested_player_name: null }));
        draft.stats_confirmed = false;
        await saveDraft();
      }
      return await publicState();
    } finally {
      settingsSaving = false;
    }
  },
  'players:load': async () => {
    const requestedConfig = config;
    const [data, metadata] = await Promise.all([apiRequest(config.server, token, '/api/v1/players'), apiRequest(config.server, token, '/api/v1/companion')]);
    if (requestedConfig !== config) throw new Error('Connection settings changed. Connect again to load the current players.');
    if (!Array.isArray(data.players) || data.players.some(player => !Number.isInteger(player.id) || typeof player.name !== 'string')) throw new Error('Unexpected player list from server.');
    players = data.players;
    updateStatus(`Connected · ${players.length} ladder players loaded`);
    return { ...(await publicState()), metadata };
  },
  'watch:start': async () => {
    ensureEditable();
    if (busy) throw new Error('Wait until the current capture finishes.');
    if (draft.rows.length || draft.evidence) throw new Error('Review this draft or start a new match before watching again.');
    if (!watching) { watching = true; void watchTick(); }
    return { watching };
  },
  'watch:stop': async () => { stopWatch(); updateStatus('Watch stopped.'); return { watching }; },
  'capture:game': async () => {
    stopWatch();
    await takeCapture({ manual: true });
    return publicState();
  },
  'capture:reread': async () => {
    ensureEditable();
    if (busy || submitting) throw new Error('Wait for the current action to finish.');
    if (!draft.screenshotPath) throw new Error('Capture or import an image first.');
    if (draft.rows.length) {
      const answer = await dialog.showMessageBox(mainWindow, { type: 'question', buttons: ['Keep draft', 'Read image again'], defaultId: 0, cancelId: 0,
        message: 'Read this capture again?', detail: 'The current draft will be archived locally. Check the new numbers and player mappings, then choose the winner again.' });
      if (answer.response !== 1) return publicState();
    }
    stopWatch();
    const buffer = await fs.readFile(draft.screenshotPath);
    await takeCapture({ manual: true, imported: buffer });
    return publicState();
  },
  'capture:import': async () => {
    ensureEditable();
    stopWatch();
    if (busy) throw new Error('Wait until the current capture finishes.');
    const chosen = await dialog.showOpenDialog(mainWindow, { title: 'Import an Empire Earth Military screenshot', filters: [{ name: 'Images', extensions: ['png', 'jpg', 'jpeg', 'bmp', 'webp'] }], properties: ['openFile'] });
    if (chosen.canceled) return publicState();
    const file = chosen.filePaths[0];
    if ((await fs.stat(file)).size > 25_000_000) throw new Error('Choose an image smaller than 25 MB.');
    const image = nativeImage.createFromPath(file);
    if (image.isEmpty()) throw new Error('This file could not be opened as an image.');
    const size = image.getSize();
    if (size.width * size.height > 40_000_000) throw new Error('Choose an image smaller than 40 megapixels.');
    await takeCapture({ manual: true, imported: image.toPNG() });
    return publicState();
  },
  'draft:save': async value => {
    ensureEditable();
    if (busy) throw new Error('Wait until the capture finishes before editing the draft.');
    if (!Array.isArray(value.rows) || value.rows.length > 10) throw new Error('A match supports at most 10 players.');
    draft.rows = value.rows.map(row => ({ ocr_name: String(row.ocr_name || '').slice(0, 120),
      ocr_name_raw: String(row.ocr_name_raw || '').slice(0, 120),
      ocr_name_candidates: Array.isArray(row.ocr_name_candidates) ? row.ocr_name_candidates.slice(0, 5).map(name => String(name).slice(0, 120)) : [],
      suggested_player_id: Number.isInteger(row.suggested_player_id) ? row.suggested_player_id : null,
      suggested_player_name: String(row.suggested_player_name || '').slice(0, 120),
      player_id: Number.isInteger(row.player_id) ? row.player_id : null,
      team: ['team1', 'team2'].includes(row.team) ? row.team : '', units_killed: row.units_killed, units_lost: row.units_lost }));
    draft.winner = ['team1', 'team2'].includes(value.winner) ? value.winner : '';
    draft.stats_confirmed = value.stats_confirmed === true;
    await saveDraft();
    return { saved: true };
  },
  'draft:new': async () => {
    if (busy || submitting) throw new Error('Wait for the current action to finish.');
    const ambiguous = draft.pendingPayload && !draft.result;
    if (ambiguous || (draft.rows.length || draft.evidence) && !draft.result) {
      const answer = await dialog.showMessageBox(mainWindow, { type: 'warning', buttons: ['Keep draft', 'Start new match'], defaultId: 0, cancelId: 0,
        message: ambiguous ? 'The previous submission may already exist on the ladder.' : 'Start a new match?',
        detail: ambiguous ? 'Retry first to retrieve its result. Starting another draft for the same match could create a duplicate. The current draft will be archived locally.' : 'Your current draft will be archived locally before starting a new one.' });
      if (answer.response !== 1) return publicState();
    }
    stopWatch();
    await archiveDraft();
    draft = newDraft();
    await saveDraft(); updateStatus('New draft ready. Capture a Military screen or add players manually.');
    return publicState();
  },
  'match:preview': async () => {
    if (busy || submitting) throw new Error('Wait for the current action to finish.');
    return apiRequest(config.server, token, '/api/v1/matches/preview', draft.pendingPayload || buildPayload(draft, players));
  },
  'match:submit': async () => {
    if (busy || submitting) throw new Error('A request is already in progress.');
    submitting = true; stopWatch();
    try { return await submitDraft(draft, players, saveDraft, payload => apiRequest(config.server, token, '/api/v1/matches', payload)); }
    finally { submitting = false; }
  },
  'captures:open': async () => { await fs.mkdir(path.join(dataDir, 'captures'), { recursive: true }); await shell.openPath(path.join(dataDir, 'captures')); return true; }
};

app.whenReady().then(async () => {
  dataDir = app.getPath('userData');
  await fs.mkdir(dataDir, { recursive: true });
  config = await readJSON('settings.json', { server: DEFAULT_SERVER });
  try { config.server = normalizeServer(config.server); } catch { config = { server: DEFAULT_SERVER }; }
  if (config.encryptedToken && safeStorage.isEncryptionAvailable()) {
    try { token = safeStorage.decryptString(Buffer.from(config.encryptedToken, 'base64')); } catch { status = 'Saved token could not be decrypted. Enter a fresh token.'; }
  }
  const savedDraft = await readJSON('draft.json', null);
  if (savedDraft && typeof savedDraft.submission_id === 'string' && Array.isArray(savedDraft.rows)) draft = savedDraft;
  mainWindow = new BrowserWindow({ show: !smokeMode || process.argv.includes('--smoke-capture'), width: 1320, height: 940, minWidth: 1050, minHeight: 750, backgroundColor: '#0a0b0d', title: 'Empire Earth Companion',
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false, sandbox: true, webSecurity: true } });
  mainWindow.removeMenu();
  let closeReady = false;
  mainWindow.on('close', event => {
    if (closeReady || process.argv.includes('--smoke-test')) return;
    event.preventDefault(); stopWatch();
    mainWindow.webContents.executeJavaScript('window.flushDraftForClose()').then(() => saveChain).then(() => {
      closeReady = true; mainWindow.close();
    }).catch(error => { dialog.showErrorBox('Draft could not be saved', error.message); });
  });
  mainWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  mainWindow.webContents.on('will-navigate', event => event.preventDefault());
  mainWindow.webContents.session.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
  for (const [channel, handler] of Object.entries(handlers)) ipcMain.handle(channel, async (event, value) => {
    if (event.sender !== mainWindow.webContents || event.senderFrame !== mainWindow.webContents.mainFrame || event.senderFrame.url !== pathToFileURL(uiFile).href) throw new Error('Untrusted request.');
    try {
      if (settingsSaving && !['state:get', 'watch:stop', 'captures:open'].includes(channel)) throw new Error('Wait for connection settings to finish saving.');
      return { ok: true, value: await handler(value) };
    }
    catch (error) { return { ok: false, error: error.message }; }
  });
  await mainWindow.loadFile(uiFile);
  if (process.argv.includes('--smoke-test')) {
    await new Promise(resolve => setTimeout(resolve, 1500));
    const check = await mainWindow.webContents.executeJavaScript('({title: document.title, bridge: typeof window.companion, rows: document.querySelectorAll("#players-body tr").length, node: typeof require, text: document.body.innerText})');
    check.connection = { hasToken: Boolean(token), players: players.length };
    if (smokeWatch) {
      draft = newDraft(); watching = true;
      await takeCapture(smokeWatchImage ? { imported: await fs.readFile(smokeWatchImage.slice('--smoke-watch-image='.length)) } : {});
      check.watch = { detected: Boolean(draft.evidence), stopped: !watching, rows: draft.rows.map(row => ({ units_killed: row.units_killed, units_lost: row.units_lost })), confirmed: draft.stats_confirmed, winner: draft.winner, status };
      for (let attempt = 0; attempt < 50; attempt++) {
        check.watch.rendered = await mainWindow.webContents.executeJavaScript('({rows: document.querySelectorAll("#players-body tr").length, image: !document.querySelector("#evidence-image").hidden && document.querySelector("#evidence-image").complete && document.querySelector("#evidence-image").naturalWidth > 0, watching: document.querySelector("#watch").textContent.includes("Stop watching")})');
        if (check.watch.rendered.rows === draft.rows.length && check.watch.rendered.image === Boolean(draft.evidence) && !check.watch.rendered.watching) break;
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      stopWatch();
    }
    if (process.argv.includes('--smoke-capture')) {
      mainWindow.setTitle('Empire Earth');
      try { check.captureBytes = (await captureGameWindow(mainWindow.getMediaSourceId())).length; } catch (error) { check.captureError = error.message; }
    }
    const ocrArgument = process.argv.find(argument => argument.startsWith('--smoke-ocr='));
    if (ocrArgument) {
      try { const parsed = await ocr(await fs.readFile(ocrArgument.slice('--smoke-ocr='.length))); check.ocr = { military: parsed.isMilitary, rows: parsed.rows.length, values: parsed.rows.map(row => ({ units_killed: row.units_killed, units_lost: row.units_lost })), warning: parsed.warning }; }
      catch (error) { check.ocrError = error.message; }
    }
    await fs.writeFile(path.join(process.cwd(), 'smoke-result.json'), JSON.stringify(check, null, 2));
    await fs.writeFile(path.join(process.cwd(), 'smoke-ui.png'), (await mainWindow.webContents.capturePage()).toPNG());
    app.quit();
  }
}).catch(error => { dialog.showErrorBox('Empire Earth Companion could not start', error.message); app.quit(); });

app.on('window-all-closed', () => app.quit());
app.on('before-quit', () => { stopWatch(); captureWindow?.destroy(); if (workerPromise) void workerPromise.then(worker => worker.terminate()).catch(() => {}); });
