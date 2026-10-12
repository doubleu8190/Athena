import { FormEvent, useState } from "react"
import { LogIn } from "lucide-react"
import { apiClient } from "../api/client"

interface LoginPageProps {
  onAuthenticated: () => void
}

export default function LoginPage({ onAuthenticated }: LoginPageProps) {
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      await apiClient.login(username, password)
      onAuthenticated()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "登录失败")
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-athena-bg px-6">
      <form onSubmit={submit} className="w-full max-w-sm space-y-6 rounded-xl border border-athena-border bg-athena-surface p-8 shadow-xl">
        <div>
          <div className="mb-3 flex h-10 w-10 items-center justify-center rounded-lg bg-athena-accent text-white"><LogIn size={20} /></div>
          <h1 className="text-2xl font-semibold text-athena-text">登录 Athena</h1>
          <p className="mt-2 text-sm text-athena-muted">使用配置的账号继续。</p>
        </div>
        <label className="block text-sm text-athena-muted">
          用户名
          <input className="input mt-2" value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" required />
        </label>
        <label className="block text-sm text-athena-muted">
          密码
          <input className="input mt-2" type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" required />
        </label>
        {error && <p role="alert" className="text-sm text-red-400">{error}</p>}
        <button className="btn-primary w-full" type="submit" disabled={submitting}>
          {submitting ? "登录中..." : "登录"}
        </button>
      </form>
    </main>
  )
}
