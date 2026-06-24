import { useState, useEffect } from 'react'
import { Outlet } from 'react-router'
import Sidebar from './Sidebar'
import Header from './Header'

export default function Layout() {
  const [dark, setDark] = useState(() => {
    if (typeof window !== 'undefined') {
      const stored = localStorage.getItem('athena-theme')
      if (stored) return stored === 'dark'
      return window.matchMedia('(prefers-color-scheme: dark)').matches
    }
    return true
  })

  const [mounted, setMounted] = useState(false)

  useEffect(() => {
    setMounted(true)
  }, [])

  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark)
    localStorage.setItem('athena-theme', dark ? 'dark' : 'light')
  }, [dark])

  // Avoid flash of wrong theme
  if (!mounted) {
    return (
      <div className="flex h-screen bg-gray-50 dark:bg-gray-950">
        <div className="w-52 bg-white dark:bg-gray-900 border-r border-gray-200 dark:border-gray-800" />
        <div className="flex-1" />
      </div>
    )
  }

  return (
    <div className="flex h-screen overflow-hidden bg-gray-50 dark:bg-gray-950">
      <Sidebar dark={dark} onToggleTheme={() => setDark(!dark)} />
      <div className="flex-1 flex flex-col overflow-hidden">
        <Header />
        <main className="flex-1 overflow-auto">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
