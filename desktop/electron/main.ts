// Electron 主进程 — 窗口管理、IPC、自动启动后端

import { app, BrowserWindow, ipcMain } from "electron"
import { join } from "path"
import { fileURLToPath } from "url"
import { dirname } from "path"

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)

let mainWindow: BrowserWindow | null = null

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    title: "Athena",
    backgroundColor: "#0f0f0f",
    webPreferences: {
      preload: join(__dirname, "../preload/preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  })

  // electron-vite dev mode sets ELECTRON_RENDERER_URL
  const rendererUrl = process.env["ELECTRON_RENDERER_URL"]
  if (rendererUrl) {
    mainWindow.loadURL(rendererUrl)
    mainWindow.webContents.openDevTools()
  } else {
    mainWindow.loadFile(join(__dirname, "../renderer/index.html"))
  }

  mainWindow.on("closed", () => {
    mainWindow = null
  })
}

app.whenReady().then(() => {
  createWindow()

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow()
    }
  })
})

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit()
  }
})

// IPC: 获取后端 API 基础 URL
ipcMain.handle("get-api-base", () => {
  return process.env["API_BASE"] || "http://127.0.0.1:8000"
})

// IPC: 获取 WebSocket URL
ipcMain.handle("get-ws-url", (_event, sessionId: string) => {
  const wsBase = process.env["WS_BASE"] || "ws://127.0.0.1:8000"
  return `${wsBase}/ws/${sessionId}`
})
