import { useState, type FormEvent } from 'react'
import { api } from './api'
import type { Me } from './types'
import { Spinner } from './ui'

export default function Login({ onLogin }: { onLogin: (me: Me) => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [totp, setTotp] = useState('')
  const [needTotp, setNeedTotp] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      onLogin(await api.login(username, password, totp || undefined))
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      if (/code|totp/i.test(msg)) setNeedTotp(true)
      setError(msg)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login">
      <div className="card login-card">
        <div className="brand-mark">a</div>
        <h1>Sign in to appa</h1>
        <p className="muted">Your job search, surfaced while you sleep.</p>
        <form onSubmit={submit}>
          <div>
            <label className="label" htmlFor="u">Username</label>
            <input id="u" className="input" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} autoFocus />
          </div>
          <div>
            <label className="label" htmlFor="p">Password</label>
            <input id="p" className="input" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
          </div>
          {needTotp && (
            <div>
              <label className="label" htmlFor="t">Authenticator code</label>
              <input id="t" className="input" inputMode="numeric" autoComplete="one-time-code" value={totp} onChange={(e) => setTotp(e.target.value)} />
            </div>
          )}
          {error && <p className="error-text">{error}</p>}
          <button className="btn primary" disabled={busy || !username || !password}>
            {busy ? <Spinner /> : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  )
}
