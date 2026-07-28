import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { HashRouter } from 'react-router'
import App from './App'
import { ToastProvider } from './components/ui/Toast'
import './index.css'

// Use HashRouter instead of BrowserRouter for Electron file:// protocol compatibility.
// BrowserRouter relies on HTML5 History API which doesn't work with file:// URLs.
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <HashRouter>
      <ToastProvider>
        <App />
      </ToastProvider>
    </HashRouter>
  </StrictMode>,
)
