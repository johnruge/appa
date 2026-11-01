import { useState } from 'react'
import { api } from './api'
import type { PanelData } from './Console'
import type { Question } from './types'

// Only http(s) links are ever rendered as links; anything else is plain text.
function safeUrl(u: unknown): string | null {
  if (typeof u !== 'string') return null
  try {
    const url = new URL(u)
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : null
  } catch {
    return null
  }
}

function Ext({ href, children }: { href: string | null; children: React.ReactNode }) {
  if (!href) return <>{children}</>
  return <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>
}

function FieldCard({ q, onCommand, onAnswer }: { q: Question; onCommand: (c: string) => void; onAnswer: (id: number, v: string) => void }) {
  const [value, setValue] = useState('')
  const options = (q.context.options ?? []) as string[]
  return (
    <div className="card">
      <div className="card-text"><span className="dim">#{q.id} form question</span> {q.prompt}</div>
      {q.context.reason && <div className="dim small">{q.context.reason}</div>}
      {options.length > 0 && options.length <= 12 ? (
        <div className="actions column">
          {options.map((o) => <button key={o} onClick={() => onAnswer(q.id, o)}>{o}</button>)}
        </div>
      ) : (
        <form className="answer" onSubmit={(e) => { e.preventDefault(); if (value.trim()) onAnswer(q.id, value) }}>
          {q.context.kind === 'textarea'
            ? <textarea rows={5} value={value} onChange={(e) => setValue(e.target.value)} placeholder="your answer (saved for next time)" />
            : <input value={value} onChange={(e) => setValue(e.target.value)} placeholder={options.length ? 'type one of the options' : 'your answer (saved for next time)'} />}
          <button type="submit" disabled={!value.trim()}>save answer</button>
        </form>
      )}
      <div className="actions"><button className="link danger" onClick={() => onCommand(`answer ${q.id} dismiss`)}>I'll apply myself</button></div>
    </div>
  )
}

function ApprovalCard({ q, onCommand }: { q: Question; onCommand: (c: string) => void }) {
  const [open, setOpen] = useState(false)
  const shot = q.context.preview_upload_id
  return (
    <div className="card approve">
      <div className="card-text"><span className="dim">#{q.id}</span> <Ext href={safeUrl(q.context.apply_url)}>{q.prompt}</Ext></div>
      {shot && (
        <a href={api.uploadUrl(shot)} target="_blank" rel="noopener noreferrer">
          <img className="shot" src={api.uploadUrl(shot)} alt="the filled application form" loading="lazy" />
        </a>
      )}
      {(q.context.failed ?? []).length > 0 && (
        <div className="err small">didn't fill: {(q.context.failed ?? []).map((f) => f.label || 'a field').join(', ')}</div>
      )}
      <button className="link" onClick={() => setOpen(!open)}>{open ? 'hide' : 'show'} what will be submitted</button>
      {open && (
        <ul className="plan">
          {(q.context.plan ?? []).map((p, i) => (
            <li key={i}><span className="dim">{p.label}:</span> {Array.isArray(p.value) ? p.value.join('; ') : p.value} <span className="dim small">({p.source})</span></li>
          ))}
        </ul>
      )}
      <div className="actions">
        <button disabled={q.context.blocked} title={q.context.blocked ? 'a required field did not fill' : ''}
          onClick={() => onCommand(`answer ${q.id} approve`)}>submit application</button>
        <button onClick={() => onCommand(`answer ${q.id} skip`)}>skip</button>
      </div>
    </div>
  )
}

export default function Panel({ data, onCommand, onAnswer }: { data: PanelData; onCommand: (cmd: string) => void; onAnswer: (id: number, v: string) => void }) {
  const { system, questions, runs, schedules } = data
  return (
    <aside className="panel">
      <section className={`sys ${system?.paused ? 'paused' : ''}`}>
        {system ? (
          <>
            <div>{system.paused ? '⏸ paused' : '● running'} · {system.active_runs} active</div>
            <div className="dim">${system.spent_24h_usd.toFixed(4)} / ${system.daily_cap_usd ?? '∞'} in 24h</div>
            {system.paused
              ? <button onClick={() => onCommand('resume')}>resume</button>
              : <button className="danger" onClick={() => onCommand('stop-all')}>stop all</button>}
          </>
        ) : <div className="dim">connecting…</div>}
      </section>

      <section>
        <h2>waiting on you <span className="count">{questions.length}</span></h2>
        {questions.length === 0 && <div className="dim">nothing right now</div>}
        {questions.slice(0, 30).map((q) => q.kind === 'apply_field'
          ? <FieldCard key={q.id} q={q} onCommand={onCommand} onAnswer={onAnswer} />
          : q.kind === 'apply_approval'
          ? <ApprovalCard key={q.id} q={q} onCommand={onCommand} />
          : (
          <div key={q.id} className="card">
            <div className="card-text">
              <span className="dim">#{q.id}</span> <Ext href={safeUrl(q.context?.link)}>{q.prompt}</Ext>
            </div>
            {q.kind === 'review' && q.application_id && (
              <div className="actions">
                <button onClick={() => onCommand(`answer ${q.id} approve`)}>approve</button>
                <button onClick={() => onCommand(`answer ${q.id} apply_myself`)}>I'll apply</button>
                <button onClick={() => onCommand(`answer ${q.id} skip`)}>skip</button>
              </div>
            )}
            {q.kind === 'answer_needed' && (
              <div className="actions column">
                {(q.context?.options ?? []).map((o: any) => safeUrl(o.url) && (
                  <button key={o.url} onClick={() => onCommand(`answer ${q.id} choose ${o.url}`)}>{o.title} · {o.location}</button>
                ))}
                <button onClick={() => onCommand(`answer ${q.id} dismiss`)}>none of these</button>
              </div>
            )}
          </div>
        ))}
      </section>

      <section>
        <h2>runs</h2>
        {runs.map((r) => (
          <div key={r.id} className={`row status-${r.status}`}>
            <button className="link" onClick={() => onCommand(`follow ${r.id}`)}>#{r.id}</button>
            <span>{r.role}</span>
            <span className="st">{r.status}</span>
            <span className="dim">${r.cost_usd.toFixed(3)}</span>
            {!['succeeded', 'failed', 'cancelled'].includes(r.status) &&
              <button className="link danger" onClick={() => onCommand(`cancel ${r.id}`)}>cancel</button>}
          </div>
        ))}
      </section>

      <section>
        <h2>schedules</h2>
        {schedules.length === 0 && <div className="dim">none: try schedule add scout "0 */4 * * *" --board config</div>}
        {schedules.map((s) => (
          <div key={s.id} className="row">
            <span className="dim">#{s.id}</span>
            <span>{s.role}</span>
            <code>{s.cron}</code>
            <button className="link" onClick={() => onCommand(`schedule ${s.enabled ? 'off' : 'on'} ${s.id}`)}>{s.enabled ? 'on' : 'off'}</button>
            <button className="link danger" onClick={() => onCommand(`schedule rm ${s.id}`)}>rm</button>
          </div>
        ))}
      </section>
    </aside>
  )
}
