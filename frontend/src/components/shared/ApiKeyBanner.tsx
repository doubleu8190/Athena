import { useState } from 'react'
import { useAuthStore } from '../../stores/authStore'

export default function ApiKeyBanner() {
  const { apiKey, isDevMode, setApiKey, clearApiKey } = useAuthStore()
  const [inputValue, setInputValue] = useState('')
  const [showInput, setShowInput] = useState(false)

  // Don't show banner if already authenticated or in dev mode
  if (apiKey || isDevMode === null) return null
  if (isDevMode) return null

  const handleSave = () => {
    const trimmed = inputValue.trim()
    if (trimmed) {
      setApiKey(trimmed)
      setInputValue('')
      setShowInput(false)
    }
  }

  const handleClear = () => {
    clearApiKey()
    setInputValue('')
  }

  if (!showInput && !apiKey) {
    return (
      <div className="flex items-center justify-between gap-3 px-4 py-2 bg-warning/10 border-b border-warning/20">
        <span className="text-sm text-warning">
          ⚠ API Key not configured — API calls will be rejected.
        </span>
        <button
          onClick={() => setShowInput(true)}
          className="px-3 py-1 text-xs font-medium bg-warning text-black rounded-lg hover:opacity-90 transition-opacity"
        >
          Set API Key
        </button>
      </div>
    )
  }

  if (showInput && !apiKey) {
    return (
      <div className="flex items-center gap-3 px-4 py-2 bg-bg-elevated border-b border-border">
        <span className="text-sm text-text-secondary">X-API-Key:</span>
        <input
          type="password"
          value={inputValue}
          onChange={(e) => setInputValue(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleSave()}
          placeholder="Enter your API key..."
          className="flex-1 px-3 py-1.5 text-sm bg-bg border border-border rounded-lg text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent transition-colors"
          autoFocus
        />
        <button
          onClick={handleSave}
          className="px-3 py-1.5 text-xs font-medium bg-accent text-white rounded-lg hover:bg-accent-hover transition-colors"
        >
          Save
        </button>
        <button
          onClick={() => setShowInput(false)}
          className="px-2 py-1.5 text-xs text-text-secondary hover:text-text-primary transition-colors"
        >
          Cancel
        </button>
      </div>
    )
  }

  // Show current key status with change/clear options
  return (
    <div className="flex items-center justify-between gap-3 px-4 py-2 bg-success/10 border-b border-success/20">
      <span className="text-sm text-success">✓ API Key configured</span>
      <div className="flex gap-2">
        <button
          onClick={() => setShowInput(true)}
          className="px-3 py-1 text-xs font-medium bg-bg-elevated text-text-secondary rounded-lg hover:bg-border transition-colors"
        >
          Change
        </button>
        <button
          onClick={handleClear}
          className="px-3 py-1 text-xs font-medium bg-bg-elevated text-text-secondary rounded-lg hover:bg-border transition-colors"
        >
          Clear
        </button>
      </div>
    </div>
  )
}
