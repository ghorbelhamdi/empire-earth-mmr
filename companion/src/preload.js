'use strict';
const { contextBridge, ipcRenderer } = require('electron');
const allowed = new Set(['state:get', 'settings:save', 'players:load', 'watch:start', 'watch:stop', 'capture:game', 'capture:import', 'capture:reread', 'draft:save', 'draft:new', 'match:preview', 'match:submit', 'captures:open']);
contextBridge.exposeInMainWorld('companion', {
  invoke: async (channel, value) => {
    if (!allowed.has(channel)) throw new Error('Unsupported action.');
    const result = await ipcRenderer.invoke(channel, value);
    if (!result.ok) throw new Error(result.error);
    return result.value;
  },
  onStatus: callback => { const listener = (_event, value) => callback(value); ipcRenderer.on('state:status', listener); return () => ipcRenderer.removeListener('state:status', listener); },
  onDraft: callback => { const listener = (_event, value) => callback(value); ipcRenderer.on('state:draft', listener); return () => ipcRenderer.removeListener('state:draft', listener); }
});
