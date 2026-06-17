import { Routes, Route, Navigate } from 'react-router'
import Layout from './components/layout/Layout'
import ChatPage from './pages/ChatPage'
import DashboardPage from './pages/DashboardPage'
import MCPServersPage from './pages/MCPServersPage'
import SkillsPage from './pages/SkillsPage'
import DevicesPage from './pages/DevicesPage'
import HarnessPage from './pages/HarnessPage'
import AuditPage from './pages/AuditPage'

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
      </Route>
    </Routes>
  )
}
