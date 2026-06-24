import { Routes, Route, Navigate } from 'react-router'
import Layout from './components/layout/Layout'
import ChatPage from './pages/ChatPage'
import DashboardPage from './pages/DashboardPage'
import MCPServersPage from './pages/MCPServersPage'
import SkillsPage from './pages/SkillsPage'
import DevicesPage from './pages/DevicesPage'
import HarnessPage from './pages/HarnessPage'
import AuditPage from './pages/AuditPage'

function NotFound() {
  return (
    <div className="flex items-center justify-center h-full">
      <div className="text-center py-16">
        <div className="text-6xl mb-4">🦉</div>
        <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100 mb-2">
          Page Not Found
        </h2>
        <p className="text-sm text-gray-500 dark:text-gray-400">
          The page you are looking for doesn&apos;t exist or has been moved.
        </p>
      </div>
    </div>
  )
}

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Navigate to="/console" replace />} />
        <Route path="/console" element={<ChatPage />} />
        <Route path="/admin/dashboard" element={<DashboardPage />} />
        <Route path="/admin/mcp-servers" element={<MCPServersPage />} />
        <Route path="/admin/skills" element={<SkillsPage />} />
        <Route path="/admin/devices" element={<DevicesPage />} />
        <Route path="/admin/harness" element={<HarnessPage />} />
        <Route path="/admin/audit" element={<AuditPage />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  )
}
