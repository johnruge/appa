// Where postings come from, what's connected, and when appa searches next.

import { api } from '../api'
import { plural, sourceName, timeAgo, timeUntil, usd } from '../format'
import type { Overview } from '../types'
import { Empty, Icon, Spinner } from '../ui'
import { useData, useToast } from '../hooks'

type Props = { onError: (e: unknown) => void; refresh: () => void; overview: Overview | null }

export default function Sources({ onError, refresh, overview }: Props) {
  const feeds = useData(() => api.feeds(), [])
  const schedules = useData(() => api.schedules(), [])
  const toast = useToast()

  const sweep = async () => {
    try {
      await api.sweepNow()
      toast('Searching every source now')
      refresh()
    } catch (e) {
      onError(e)
    }
  }

  const toggle = async (id: number, enabled: boolean) => {
    try {
      await api.setSchedule(id, enabled)
      schedules.reload()
      refresh()
    } catch (e) {
      onError(e)
    }
  }

  const addDefault = async () => {
    try {
      await api.addSchedule('0 */6 * * *')
      toast('appa will search every 6 hours')
      schedules.reload()
      refresh()
    } catch (e) {
      onError(e)
    }
  }

  const lists = feeds.data?.lists ?? []
  const ready = lists.filter((l) => l.ready)
  const needKey = lists.filter((l) => !l.ready)
  const boards = Object.entries(feeds.data?.boards ?? {}).sort((a, b) => b[1] - a[1])
  const sweeps = (schedules.data ?? []).filter((s) => s.role === 'scout')
  const last = overview?.last_sweep

  return (
    <div className="page">
      <div className="page-head">
        <div className="grow">
          <h1>Sources</h1>
          <p className="sub">{ready.length} job lists connected · {plural(overview?.boards ?? 0, 'company board')} checked in rotation</p>
        </div>
        <button className="btn primary" onClick={sweep}><Icon name="refresh" size={14} /> Search now</button>
      </div>

      <div className="stats">
        <div className="card stat"><div className="n">{overview?.next_sweep_at ? timeUntil(overview.next_sweep_at) : 'Off'}</div><div className="l">Next search</div></div>
        <div className="card stat"><div className="n">{last ? timeAgo(last.finished_at ?? last.started_at) : '·'}</div><div className="l">Last search{last ? ` · ${last.status}` : ''}</div></div>
        <div className="card stat"><div className="n">{(overview?.inbox.new ?? 0).toLocaleString()}</div><div className="l">Waiting to be screened</div></div>
        <div className="card stat"><div className="n">{usd(overview?.spent_24h_usd)}</div><div className="l">Model spend today</div></div>
      </div>

      <div className="section-title">Schedule</div>
      <div className="card">
        {schedules.loading && !schedules.data ? <div className="empty"><Spinner /></div> : sweeps.length === 0 ? (
          <Empty icon="refresh" title="appa isn't searching on its own yet">
            <button className="btn primary" style={{ marginTop: 12 }} onClick={addDefault}>Search every 6 hours</button>
          </Empty>
        ) : (
          <div className="list">
            {sweeps.map((s) => (
              <div key={s.id} className="item" style={{ cursor: 'default' }}>
                <div className="grow">
                  <div className="title">Search every source{describeCron(s.cron)}</div>
                  <div className="meta">{s.enabled ? `Next ${timeUntil(s.next_run_at)}` : 'Paused'}{s.last_run_at ? ` · last ran ${timeAgo(s.last_run_at)}` : ''}</div>
                </div>
                <label className="check">
                  <input type="checkbox" checked={s.enabled} onChange={(e) => toggle(s.id, e.target.checked)} />
                  On
                </label>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="section-title">Job lists</div>
      <div className="card list">
        {feeds.loading && !feeds.data ? <div className="empty"><Spinner /></div> : (
          [...ready, ...needKey].map((l) => (
            <div key={l.name} className="item" style={{ cursor: 'default' }}>
              <span className="status-dot" style={{ color: l.ready ? (l.default ? 'var(--green)' : 'var(--text-3)') : 'var(--border-strong)' }} />
              <div className="grow">
                <div className="title">{sourceName(l.name)}</div>
                <div className="meta ellipsis">{l.description}</div>
              </div>
              <div className="side">
                {l.ready ? (
                  <span className={`badge ${l.default ? 'green' : ''}`}>{l.default ? 'Connected' : 'Available'}</span>
                ) : (
                  <span className="badge" title={`Add ${l.needs.join(' and ')} to your .env file`}>Needs {l.needs.join(' + ')}</span>
                )}
              </div>
            </div>
          ))
        )}
      </div>

      <div className="section-title">Company boards</div>
      <div className="card card-pad">
        <p className="muted" style={{ marginBottom: 12 }}>
          Every link a source finds adds that company's whole job board to the rotation. Each search checks the next batch.
        </p>
        <div className="row" style={{ flexWrap: 'wrap', gap: 6 }}>
          {boards.map(([ats, n]) => <span key={ats} className="badge">{sourceName(ats)} · {n.toLocaleString()}</span>)}
        </div>
      </div>
    </div>
  )
}

function describeCron(cron: string): string {
  const m = cron.match(/^0 \*\/(\d+) \* \* \*$/)
  if (m) return `, every ${m[1]} hours`
  if (cron === '0 * * * *') return ', every hour'
  return ` (${cron} UTC)`
}
