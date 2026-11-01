// What appa has been doing: each search and prefill, with its log and cost.

import { useState } from 'react'
import { api } from '../api'
import { timeAgo, usd } from '../format'
import type { Overview, Run, RunEvent } from '../types'
import { Drawer, Empty, Icon, Spinner } from '../ui'
import { useData } from '../hooks'

type Props = { onError: (e: unknown) => void; refresh: () => void; overview: Overview | null }

const ROLE: Record<string, string> = { scout: 'Search', screener: 'Screening', applier: 'Prefill', concierge: 'Tidy-up' }
const TONE: Record<string, string> = { succeeded: 'green', failed: 'red', running: 'blue', queued: '', cancelled: '', waiting_for_you: 'amber' }

export default function Activity({ onError }: Props) {
  const runs = useData(() => api.runs(40), [], 15_000)
  const [open, setOpen] = useState<Run | null>(null)

  return (
    <div className="page">
      <div className="page-head">
        <div className="grow">
          <h1>Activity</h1>
          <p className="sub">Every search and prefill appa ran, with what it cost.</p>
        </div>
      </div>
      {runs.loading && !runs.data ? (
        <div className="empty"><Spinner /></div>
      ) : !runs.data?.length ? (
        <div className="card"><Empty icon="activity" title="Nothing yet">Runs appear here as appa works.</Empty></div>
      ) : (
        <div className="card">
          <table className="table">
            <thead><tr><th>What</th><th>Status</th><th>Result</th><th>Cost</th><th>When</th></tr></thead>
            <tbody>
              {runs.data.map((r) => (
                <tr key={r.id} className="clickable" onClick={() => setOpen(r)}>
                  <td style={{ fontWeight: 550 }}>{ROLE[r.role] ?? r.role} <span className="faint mono">#{r.id}</span></td>
                  <td><span className={`badge ${TONE[r.status] ?? ''}`}><span className={`status-dot ${r.status === 'running' ? 'pulse' : ''}`} />{r.status.replace(/_/g, ' ')}</span></td>
                  <td className="muted ellipsis" style={{ maxWidth: 320 }}>{summary(r)}</td>
                  <td className="muted nowrap">{usd(r.cost_usd)}</td>
                  <td className="muted nowrap">{timeAgo(r.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {open && <RunLog run={open} onClose={() => setOpen(null)} onError={onError} />}
    </div>
  )
}

function summary(r: Run): string {
  if (r.error) return r.error
  const c = (r.result?.counts ?? {}) as Record<string, number>
  const parts = Object.entries(c).filter(([, n]) => n).map(([k, n]) => `${n} ${LABEL[k] ?? k}`)
  return parts.join(' · ') || (r.status === 'running' ? 'working…' : '')
}

const LABEL: Record<string, string> = { candidate: 'good', review: 'to check', manual: 'apply directly', reject: 'rejected', already_seen: 'seen before', duplicate: 'duplicates' }

function RunLog({ run, onClose, onError }: { run: Run; onClose: () => void; onError: (e: unknown) => void }) {
  const events = useData(() => api.events(run.id), [run.id], run.status === 'running' || run.status === 'queued' ? 3000 : undefined)
  const lines = (events.data ?? []).map(line).filter(Boolean)
  return (
    <Drawer onClose={onClose}>
      <div className="drawer-head">
        <div className="grow">
          <h2 style={{ fontSize: 17 }}>{ROLE[run.role] ?? run.role} #{run.id}</h2>
          <div className="muted">{run.status.replace(/_/g, ' ')} · {usd(run.cost_usd)} · {run.steps} steps · started {timeAgo(run.created_at)}</div>
        </div>
        {['running', 'queued'].includes(run.status) && (
          <button className="btn sm danger" onClick={() => api.cancelRun(run.id).then(onClose).catch(onError)}>Stop</button>
        )}
        <button className="btn ghost icon" onClick={onClose} aria-label="Close"><Icon name="x" /></button>
      </div>
      <div className="drawer-body">
        {events.loading && !events.data ? <Spinner /> : <div className="log">{lines.join('\n') || 'No log yet.'}</div>}
      </div>
    </Drawer>
  )
}

function line(e: RunEvent): string {
  const t = new Date(e.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  const p = e.payload
  if (e.kind === 'log') return `${t}  ${p.message ?? ''}`
  if (e.kind === 'decision') return `${t}  ${p.verdict}: ${p.company} / ${p.title}`
  if (e.kind === 'status') return `${t}  ${p.status}${p.error ? `: ${p.error}` : ''}`
  if (e.kind === 'tool_call') return ''
  return `${t}  ${e.kind}`
}
