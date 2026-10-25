const { contextBridge } = require('electron');
contextBridge.exposeInMainWorld('lenz', { platform: process.platform });
