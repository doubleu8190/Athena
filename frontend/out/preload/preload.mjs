import { contextBridge, ipcRenderer } from "electron";
contextBridge.exposeInMainWorld("athena", {
  getApiBase: () => ipcRenderer.invoke("get-api-base"),
  getWsUrl: (sessionId) => ipcRenderer.invoke("get-ws-url", sessionId)
});
