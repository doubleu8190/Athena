# Athena Electron

Athena Desktop Application built with Electron, providing the same functionality as the web frontend.

## Prerequisites

- Node.js >= 20.x
- npm >= 10.x

## Getting Started

### Installation

```bash
# Install dependencies for all sub-projects
npm run install:all
```

### Development

```bash
# Start all services in development mode
npm run dev
```

This will:
1. Watch main process TypeScript files
2. Watch preload script TypeScript files
3. Start renderer process dev server (Vite)
4. Launch Electron application

### Build

```bash
# Build all components
npm run build
```

### Package

```bash
# Package for current platform
npm run package

# Package for specific platform
npm run package:mac
npm run package:win
npm run package:linux
```

## Project Structure

```
electron/
├── src/                    # Main process source
│   └── main.ts             # Electron main process entry
├── preload/                # Preload scripts
│   ├── src/
│   │   └── preload.ts      # Preload script (context bridge)
│   └── tsconfig.json       # Preload TypeScript config
├── renderer/               # Renderer process (React app)
│   ├── src/                # React source files (same as web frontend)
│   │   ├── api/            # API client
│   │   ├── components/     # UI components
│   │   ├── hooks/          # Custom hooks
│   │   ├── pages/          # Page components
│   │   ├── stores/         # Zustand stores
│   │   ├── App.tsx         # React app entry
│   │   ├── main.tsx        # React DOM entry
│   │   └── index.css       # Global styles
│   ├── index.html          # Renderer HTML template
│   ├── vite.config.ts      # Vite config
│   └── package.json        # Renderer dependencies
├── resources/              # Static resources (icons, etc.)
├── dist/                   # Built main process output
├── release/                # Packaging output
├── package.json            # Main package config
├── tsconfig.json           # Main process TypeScript config
└── .gitignore              # Git ignore rules
```

## Key Features

- **Dev Mode**: Connects to local Vite dev server (`http://localhost:5173`)
- **Production Mode**: Loads built renderer files from `dist/renderer/`
- **Context Bridge**: Secure IPC communication between main and renderer processes
- **Auto-refresh**: Hot module replacement in development
- **Cross-platform**: Supports macOS, Windows, and Linux

## API Proxy

The renderer process proxies API requests to the backend server at `http://localhost:8000`.
Make sure the Athena backend is running before starting the Electron app.
