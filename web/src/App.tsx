import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from './api'
import { timeUntil } from './format'
import Login from './Login'
import Activity from './pages/Activity'
import Applied from './pages/Applied'
import Discover from './pages/Discover'
import Matches from './pages/Matches'
import Settings from './pages/Settings'
import Sources from './pages/Sources'
import type { Me } from './types'
import { Icon, ToastProvider } from './ui'
import { useData, useToast } from './hooks'

const PAGES = [
  { id: 'matches', label: 'Matches', icon: 'spark' },
  { id: 'applied', label: 'Applied', icon: 'briefcase' },
  { id: 'discover', label: 'Discover', icon: 'compass' },
  { id: 'sources', label: 'Sources', icon: 'plug' },
  { id: 'activity', label: 'Activity', icon: 'activity' },
  { id: 'settings', label: 'Settings', icon: 'settings' },
] as const
type PageId = (typeof PAGES)[number]['id']

const pageFromHash = (): PageId => {
  const h = window.location.hash.replace(/^#\/?/, '') as PageId
  return PAGES.some((p) => p.id === h) ? h : 'matches'
}

export default function App() {
  const [me, setMe] = useState<Me | null>(null)
  const [checking, setChecking] = useState(true)

  useEffect(() => {
    api.me().then(setMe).catch(() => setMe(null)).finally(() => setChecking(false))
  }, [])

  if (checking) return <div className="boot">appa</div>
  return (
    <ToastProvider>
      {me ? <Shell me={me} onLogout={() => setMe(null)} /> : <Login onLogin={setMe} />}
    </ToastProvider>
  )
}

function Shell({ me, onLogout }: { me: Me; onLogout: () => void }) {
  const [page, setPage] = useState<PageId>(pageFromHash)
  const toast = useToast()
  const { data: ov, reload } = useData(() => api.overview(), [], 30_000)
  const { data: sys, reload: reloadSys } = useData(() => api.system(), [], 30_000)

  useEffect(() => {
    const onHash = () => setPage(pageFromHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const go = (id: PageId) => {
    window.history.pushState(null, '', `#/${id}`)
    setPage(id)
  }

  const refresh = useCallback(() => {
    reload()
    reloadSys()
  }, [reload, reloadSys])

  const onError = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) return onLogout()
      toast(e instanceof Error ? e.message : String(e), { error: true })
    },
    [onLogout, toast],
  )

  const togglePause = async () => {
    try {
      if (sys?.paused) {
        await api.resume()
        toast('Resumed: sweeps run on schedule again')
      } else {
        const r = await api.pause()
        toast(`Paused${r.cancelled ? `, stopped ${r.cancelled} run(s)` : ''}`)
      }
      refresh()
    } catch (e) {
      onError(e)
    }
  }

  const counts: Partial<Record<PageId, number>> = {
    matches: ov ? (ov.matches['To Apply'] ?? 0) + (ov.matches['Needs Review'] ?? 0) + (ov.matches['Apply Myself'] ?? 0) : undefined,
    applied: ov?.applied_total,
    discover: ov?.inbox.new,
  }
  const props = { onError, refresh, overview: ov }

  return (
    <div className="shell">
      <nav className="sidebar" aria-label="Main">
        <div className="brand">
          <div className="brand-mark">a</div>
          appa
        </div>
        {PAGES.map((p, i) => (
          <div key={p.id} style={{ display: 'contents' }}>
            {i === 3 && <div className="nav-sep" />}
            <button className={`nav-item ${page === p.id ? 'active' : ''}`} onClick={() => go(p.id)}>
              <Icon name={p.icon} />
              {p.label}
              {counts[p.id] !== undefined && counts[p.id]! > 0 && <span className="count">{counts[p.id]!.toLocaleString()}</span>}
            </button>
          </div>
        ))}
        <div className="sidebar-foot">
          {sys && (
            <div className="row muted">
              <span className={`status-dot ${sys.paused ? '' : 'pulse'}`} style={{ color: sys.paused ? 'var(--amber)' : 'var(--green)' }} />
              <span className="grow">
                {sys.paused ? 'Paused' : ov?.next_sweep_at ? `Next sweep ${timeUntil(ov.next_sweep_at)}` : 'Running'}
              </span>
              <button className="btn ghost sm icon" title={sys.paused ? 'Resume' : 'Pause everything'} onClick={togglePause}>
                <Icon name={sys.paused ? 'play' : 'pause'} size={14} />
              </button>
            </div>
          )}
          <div className="row faint">
            <Icon name="user" size={14} />
            <span className="grow ellipsis">{me.username}</span>
            <button
              className="btn ghost sm"
              onClick={async () => {
                await api.logout().catch(() => {})
                onLogout()
              }}
            >
              Sign out
            </button>
          </div>
        </div>
      </nav>
      <main className="main">
        {page === 'matches' && <Matches {...props} />}
        {page === 'applied' && <Applied {...props} />}
        {page === 'discover' && <Discover {...props} />}
        {page === 'sources' && <Sources {...props} />}
        {page === 'activity' && <Activity {...props} />}
        {page === 'settings' && <Settings {...props} />}
      </main>
    </div>
  )
}
