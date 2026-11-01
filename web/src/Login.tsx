import { useState, type FormEvent } from 'react'
import { api, ApiError } from './api'
import type { Me } from './types'

export default function Login({ onLogin }: { onLogin: (me: Me) => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [totp, setTotp] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      onLogin(await api.login(username, password, totp || undefined))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'could not reach the server')
      setPassword('')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login">
      <form onSubmit={submit} className="login-box">
        <div className="login-title">appa<span className="cursor">_</span></div>
        <label>
          username
          <input autoFocus autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} />
        </label>
        <label>
          password
          <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </label>
        <label>
          2FA code <span className="dim">(if enabled)</span>
          <input inputMode="numeric" autoComplete="one-time-code" maxLength={6} value={totp}
            onChange={(e) => setTotp(e.target.value.replace(/\D/g, ''))} />
        </label>
        {error && <div className="err">{error}</div>}
        <button type="submit" disabled={busy || !username || !password}>{busy ? 'logging in…' : 'log in'}</button>
      </form>
    </div>
  )
}
