'use strict';
const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('gameCapture', { complete: value => ipcRenderer.send('capture:complete', value), progress: value => ipcRenderer.send('capture:progress', value) });
