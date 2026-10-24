const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('lenz', {
  analyseUseCase: prompt => ipcRenderer.invoke('lenz:analyse-use-case', prompt),
  openDashboard: () => ipcRenderer.invoke('lenz:open-dashboard')
});
