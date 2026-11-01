// Your tracker: everything you applied to, and where each one stands.

import { useMemo, useState } from 'react'
import { api, APPLIED_STATUSES } from '../api'
import { timeAgo } from '../format'
import type { Overview } from '../types'
import { Avatar, Empty, ExternalLink, Icon, Spinner, StatusBadge } from '../ui'
import { useData, useToast } from '../hooks'

type Props = { onError: (e: unknown) => void; refresh: () => void; overview: Overview | null }

export default function Applied({ onError, refresh, overview }: Props) {
  const [filter, setFilter] = useState('All')
  const [q, setQ] = useState('')
  const toast = useToast()
  const apps = useData(() => api.applications(APPLIED_STATUSES, '', 500), [])

  const rows = useMemo(() => {
    const term = q.trim().toLowerCase()
    return (apps.data ?? []).filter(
      (a) => (filter === 'All' || a.status === filter) && (!term || `${a.company} ${a.role}`.toLowerCase().includes(term)),
    )
  }, [apps.data, filter, q])

  const counts = useMemo(() => {
    const c: Record<string, number> = { All: apps.data?.length ?? 0 }
    for (const a of apps.data ?? []) c[a.status] = (c[a.status] ?? 0) + 1
    return c
  }, [apps.data])

  const change = async (id: number, status: string) => {
    apps.setData((all) => (all ?? []).map((a) => (a.id === id ? { ...a, status } : a)))
    try {
      await api.updateApplication(id, { status })
      toast(`Moved to ${status}`)
      refresh()
    } catch (e) {
      onError(e)
      apps.reload()
    }
  }

  return (
    <div className="page">
      <div className="page-head">
        <div className="grow">
          <h1>Applied</h1>
          <p className="sub">{overview ? `${overview.applied_7d} this week · ${overview.applied_total} in total` : 'Where every application stands.'}</p>
        </div>
        <div className="search" style={{ width: 260 }}>
          <Icon name="search" size={14} />
          <input className="input" placeholder="Search company or role" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
      </div>

      <div className="tabs" role="tablist">
        {['All', ...APPLIED_STATUSES].map((s) => (
          <button key={s} role="tab" className={`tab ${filter === s ? 'active' : ''}`} onClick={() => setFilter(s)}>
            {s} <span className="count">{counts[s] ?? 0}</span>
          </button>
        ))}
      </div>

      {apps.loading && !apps.data ? (
        <div className="empty"><Spinner /></div>
      ) : rows.length === 0 ? (
        <div className="card"><Empty icon="briefcase" title="Nothing here yet">When you apply to a match, mark it applied and it shows up here.</Empty></div>
      ) : (
        <div className="card">
          <table className="table">
            <thead>
              <tr><th>Role</th><th>Status</th><th>Applied</th><th /></tr>
            </thead>
            <tbody>
              {rows.map((a) => (
                <tr key={a.id}>
                  <td>
                    <div className="row" style={{ gap: 12 }}>
                      <Avatar name={a.company} size={30} />
                      <div className="grow">
                        <div style={{ fontWeight: 550 }} className="ellipsis">{a.role}</div>
                        <div className="muted" style={{ fontSize: 13 }}>{a.company}{a.location ? ` · ${a.location}` : ''}</div>
                      </div>
                    </div>
                  </td>
                  <td style={{ width: 190 }}>
                    <label className="row" style={{ position: 'relative' }}>
                      <StatusBadge status={a.status} />
                      <select className="select" aria-label="Status" value={a.status} onChange={(e) => change(a.id, e.target.value)}
                        style={{ position: 'absolute', inset: 0, opacity: 0, cursor: 'pointer' }}>
                        {[...APPLIED_STATUSES, 'Skipped'].map((s) => <option key={s}>{s}</option>)}
                      </select>
                    </label>
                  </td>
                  <td className="muted nowrap" style={{ width: 120 }}>{a.date_applied ? timeAgo(a.date_applied) : '·'}</td>
                  <td style={{ width: 60, textAlign: 'right' }}>
                    {a.link && <ExternalLink href={a.link} className="btn ghost sm icon"><Icon name="external" size={13} /></ExternalLink>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
