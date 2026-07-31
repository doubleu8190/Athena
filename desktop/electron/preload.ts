/**
 * Electron preload 脚本 — 暴露安全的 IPC API 给渲染进程.
 */
import { contextBridge, ipcRenderer } from "electron"

contextBridge.exposeInMainWorld("athena", {
  getApiBase: (): Promise<string> => ipcRenderer.invoke("get-api-base"),
  getWsUrl: (sessionId: string): Promise<string> =>
    ipcRenderer.invoke("get-ws-url", sessionId),
})
