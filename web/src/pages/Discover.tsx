// Everything the sources found, before and after screening, including what the free filters set aside and why.

import { useState } from 'react'
import { api } from '../api'
import { plural, sourceName, timeAgo } from '../format'
import type { Listing, Overview } from '../types'
import { Avatar, Empty, ExternalLink, Icon, Spinner } from '../ui'
import { useData, useToast } from '../hooks'

type Props = { onError: (e: unknown) => void; refresh: () => void; overview: Overview | null }

const VIEWS = [
  { status: 'new', label: 'Waiting', hint: 'Found and passed the free filters; screened on the next search.' },
  { status: 'screened', label: 'Screened', hint: 'Checked against your rules. Matches are on the Matches page.' },
  { status: 'filtered', label: 'Set aside', hint: "Didn't pass a free filter (title, level, age, location). Restore anything that was wrong." },
  { status: 'skipped', label: 'Skipped', hint: 'You chose to skip these.' },
]

export default function Discover({ onError, refresh, overview }: Props) {
  const [view, setView] = useState('new')
  const [q, setQ] = useState('')
  const toast = useToast()
  const rows = useData(() => api.listings(view, q.trim() || undefined, 150), [view, q])
  const inbox = overview?.inbox ?? {}

  const act = async (l: Listing, action: 'track' | 'skip' | 'restore') => {
    try {
      await api.listingAction(l.id, action)
      rows.setData((all) => (all ?? []).filter((x) => x.id !== l.id))
      toast(action === 'track' ? `Added ${l.company} to your matches` : action === 'skip' ? 'Skipped' : 'Restored: it will be screened next')
      refresh()
    } catch (e) {
      onError(e)
    }
  }

  const total = Object.values(inbox).reduce((a, b) => a + b, 0)
  const active = VIEWS.find((v) => v.status === view)!

  return (
    <div className="page">
      <div className="page-head">
        <div className="grow">
          <h1>Discover</h1>
          <p className="sub">
            {total ? `${plural(total, 'posting')} found across ${plural(overview?.boards ?? 0, 'company board')} and every list` : 'Everything the sources found.'}
          </p>
        </div>
        <div className="search" style={{ width: 260 }}>
          <Icon name="search" size={14} />
          <input className="input" placeholder="Search company or title" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
      </div>

      <div className="tabs" role="tablist">
        {VIEWS.map((v) => (
          <button key={v.status} role="tab" className={`tab ${view === v.status ? 'active' : ''}`} onClick={() => setView(v.status)}>
            {v.label} <span className="count">{(inbox[v.status] ?? 0).toLocaleString()}</span>
          </button>
        ))}
      </div>
      <p className="muted" style={{ marginBottom: 14, fontSize: 13 }}>{active.hint}</p>

      {rows.loading && !rows.data ? (
        <div className="empty"><Spinner /></div>
      ) : !rows.data?.length ? (
        <div className="card"><Empty icon="compass" title="Nothing here">{q ? 'No postings match that search.' : 'Postings appear here after the next search.'}</Empty></div>
      ) : (
        <div className="card list">
          {rows.data.map((l) => (
            <div key={l.id} className="item" style={{ cursor: 'default' }}>
              <Avatar name={l.company} size={30} />
              <div className="grow">
                <div className="title ellipsis">{l.title}</div>
                <div className="meta">
                  <span>{l.company}</span>
                  {l.location && <><span className="dot-sep" /><span className="ellipsis" style={{ maxWidth: 240 }}>{l.location}</span></>}
                  <span className="dot-sep" /><span>{sourceName(l.sources[0])}{l.sources.length > 1 ? ` +${l.sources.length - 1}` : ''}</span>
                  {l.reason && view !== 'new' && <><span className="dot-sep" /><span className="ellipsis" style={{ maxWidth: 300 }}>{l.reason}</span></>}
                </div>
              </div>
              <div className="side">
                <span className="side-time nowrap">{timeAgo(l.posted_at ?? l.first_seen_at)}</span>
                <div className="actions">
                  <ExternalLink href={l.url} className="btn sm">Open <Icon name="external" size={12} /></ExternalLink>
                  {(view === 'new' || view === 'filtered') && <button className="btn sm" onClick={() => act(l, 'track')}>Add to matches</button>}
                  {view === 'new' && <button className="btn sm ghost" onClick={() => act(l, 'skip')} title="Skip"><Icon name="x" size={13} /></button>}
                  {(view === 'filtered' || view === 'skipped') && <button className="btn sm ghost" onClick={() => act(l, 'restore')}>Restore</button>}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
