// Home: the jobs appa surfaced for you, what needs your input, and one-click actions for each.

import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { host, plural, reasons, safeHref, sourceName, splitQuote, timeAgo } from '../format'
import type { Application, Overview, Question } from '../types'
import { Avatar, Drawer, Empty, ExternalLink, Icon, Kbd, Spinner, StatusBadge } from '../ui'
import { useData, useToast } from '../hooks'

type Props = { onError: (e: unknown) => void; refresh: () => void; overview: Overview | null }

const TABS = [
  { status: 'To Apply', label: 'Good matches', hint: 'Passed every check. Open it and apply when you are ready.' },
  { status: 'Needs Review', label: 'Check first', hint: 'Something worth a look first (sponsorship wording, years, an odd page).' },
  { status: 'Apply Myself', label: 'Apply directly', hint: 'Companies you prefer to handle yourself, per your preferences.' },
]

export default function Matches({ onError, refresh, overview }: Props) {
  const [tab, setTab] = useState('To Apply')
  const [q, setQ] = useState('')
  const [selected, setSelected] = useState<number | null>(null)
  const [open, setOpen] = useState<number | null>(null)
  const toast = useToast()
  const apps = useData(() => api.applications(TABS.map((t) => t.status)), [], 60_000)
  const questions = useData(() => api.questions(), [], 30_000)


  const byStatus = useMemo(() => {
    const c: Record<string, number> = {}
    for (const a of apps.data ?? []) c[a.status] = (c[a.status] ?? 0) + 1
    return c
  }, [apps.data])

  // Open on the first tab that has something, until you pick a tab yourself.
  const [picked, setPicked] = useState(false)
  const shown = picked || (byStatus[tab] ?? 0) > 0 ? tab : (TABS.find((t) => (byStatus[t.status] ?? 0) > 0)?.status ?? tab)

  const rows = useMemo(() => {
    const term = q.trim().toLowerCase()
    return (apps.data ?? []).filter(
      (a) => a.status === shown && (!term || `${a.company} ${a.role} ${a.location ?? ''}`.toLowerCase().includes(term)),
    )
  }, [apps.data, shown, q])

  const reloadAll = useCallback(() => {
    apps.reload()
    questions.reload()
    refresh()
  }, [apps, questions, refresh])

  const setStatus = useCallback(
    async (a: Application, status: string, message: string) => {
      const before = a.status
      apps.setData((all) => (all ?? []).map((x) => (x.id === a.id ? { ...x, status } : x)))
      try {
        await api.updateApplication(a.id, { status })
        toast(message, {
          undo: async () => {
            await api.updateApplication(a.id, { status: before }).catch(onError)
            reloadAll()
          },
        })
        refresh()
      } catch (e) {
        onError(e)
        apps.reload()
      }
    },
    [apps, toast, onError, refresh, reloadAll],
  )

  const applied = useCallback(
    async (a: Application) => {
      try {
        await api.markApplied(a.id)
        apps.setData((all) => (all ?? []).filter((x) => x.id !== a.id))
        toast(`Marked ${a.company} as applied`, {
          undo: async () => {
            await api.updateApplication(a.id, { status: before(a) }).catch(onError)
            reloadAll()
          },
        })
        refresh()
      } catch (e) {
        onError(e)
      }
    },
    [apps, toast, onError, refresh, reloadAll],
  )

  // Keyboard: j/k move, enter opens, o opens the posting, a = applied, x = not for me.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = e.target instanceof Element && e.target.closest('input, textarea, select')
      if (open !== null || e.metaKey || e.ctrlKey || typing) return
      const idx = rows.findIndex((r) => r.id === selected)
      const cur = rows[idx]
      if (e.key === 'j' || e.key === 'ArrowDown') setSelected(rows[Math.min(idx + 1, rows.length - 1)]?.id ?? null)
      else if (e.key === 'k' || e.key === 'ArrowUp') setSelected(rows[Math.max(idx - 1, 0)]?.id ?? null)
      else if (e.key === 'Enter' && cur) setOpen(cur.id)
      else if (e.key === 'o' && safeHref(cur?.link)) window.open(safeHref(cur?.link)!, '_blank', 'noopener,noreferrer')
      else if (e.key === 'a' && cur) applied(cur)
      else if (e.key === 'x' && cur) setStatus(cur, 'Skipped', `Hid ${cur.company}`)
      else return
      e.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [rows, selected, open, applied, setStatus])

  const findMore = async () => {
    try {
      await api.sweepNow()
      toast('Looking for new matches now. New ones show up here as they are screened.')
      refresh()
    } catch (e) {
      onError(e)
    }
  }

  const last = overview?.last_sweep
  const active = TABS.find((t) => t.status === shown)!
  const openQs = (questions.data ?? []).filter((x) => x.kind !== 'review' || x.application_id)

  return (
    <div className="page">
      <div className="page-head">
        <div className="grow">
          <h1>Matches</h1>
          <p className="sub">
            {overview ? (
              <>
                {plural(overview.new_matches_24h, 'new match', 'new matches')} in the last day
                {last && <> · last search {timeAgo(last.finished_at ?? last.started_at)}</>}
              </>
            ) : (
              'Jobs appa found and screened for you.'
            )}
          </p>
        </div>
        <AddLink onError={onError} />
        <button className="btn primary" onClick={findMore}>
          <Icon name="refresh" size={14} /> Find more now
        </button>
      </div>

      {openQs.length > 0 && (
        <>
          <div className="section-title">Needs you</div>
          <div className="needs">
            {openQs.map((x) => (
              <NeedCard key={x.id} q={x} onDone={reloadAll} onError={onError} />
            ))}
          </div>
        </>
      )}

      <div className="tabs" role="tablist">
        {TABS.map((t) => (
          <button key={t.status} role="tab" aria-selected={shown === t.status} className={`tab ${shown === t.status ? 'active' : ''}`}
            onClick={() => { setTab(t.status); setPicked(true); setSelected(null) }}>
            {t.label} <span className="count">{byStatus[t.status] ?? 0}</span>
          </button>
        ))}
        <div className="grow" />
        <div className="search" style={{ width: 240, marginBottom: 6 }}>
          <Icon name="search" size={14} />
          <input className="input" placeholder="Filter by company, role, place" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
      </div>
      <p className="muted" style={{ marginBottom: 14, fontSize: 13 }}>{active.hint}</p>

      {apps.loading && !apps.data ? (
        <div className="empty"><Spinner /></div>
      ) : rows.length === 0 ? (
        <div className="card">
          <Empty icon="spark" title={q ? 'Nothing matches that filter' : 'All caught up'}>
            {q ? 'Try another company or role.' : 'New matches appear here after each search. You can also find more now.'}
          </Empty>
        </div>
      ) : (
        <div className="card list">
          {rows.map((a) => (
            <MatchRow key={a.id} a={a} selected={selected === a.id} onSelect={() => setSelected(a.id)} onOpen={() => setOpen(a.id)}
              onApplied={() => applied(a)} onHide={() => setStatus(a, 'Skipped', `Hid ${a.company}`)} />
          ))}
        </div>
      )}
      {rows.length > 0 && (
        <p className="faint" style={{ marginTop: 12, fontSize: 12.5 }}>
          <Kbd>j</Kbd> <Kbd>k</Kbd> move · <Kbd>enter</Kbd> details · <Kbd>o</Kbd> open posting · <Kbd>a</Kbd> I applied · <Kbd>x</Kbd> not for me
        </p>
      )}

      {open !== null && (
        <MatchDrawer id={open} onClose={() => setOpen(null)} onError={onError}
          onChanged={() => { setOpen(null); reloadAll() }} />
      )}
    </div>
  )
}

function before(a: Application) {
  return a.status
}

function MatchRow({ a, selected, onSelect, onOpen, onApplied, onHide }: {
  a: Application
  selected: boolean
  onSelect: () => void
  onOpen: () => void
  onApplied: () => void
  onHide: () => void
}) {
  const why = reasons(a.notes).find((r) => !/^(passes all filters|auto:)/i.test(r))
  return (
    <div className={`item ${selected ? 'selected' : ''}`} onClick={onOpen} onMouseEnter={onSelect}>
      <Avatar name={a.company} />
      <div className="grow">
        <div className="title ellipsis">{a.role}</div>
        <div className="meta">
          <span>{a.company}</span>
          {a.location && <><span className="dot-sep" /><span className="ellipsis" style={{ maxWidth: 260 }}>{a.location}</span></>}
          {why && a.status !== 'To Apply' && <><span className="dot-sep" /><span className="ellipsis" style={{ maxWidth: 320 }}>{splitQuote(why).text}</span></>}
        </div>
      </div>
      <div className="side">
        <span className="side-time nowrap">{timeAgo(a.created_at)}</span>
        <div className="actions" onClick={(e) => e.stopPropagation()}>
          {a.link && <ExternalLink href={a.link} className="btn sm">Open <Icon name="external" size={12} /></ExternalLink>}
          <button className="btn sm" onClick={onApplied}><Icon name="check" size={13} /> Applied</button>
          <button className="btn sm ghost" title="Not for me" onClick={onHide}><Icon name="x" size={13} /></button>
        </div>
      </div>
    </div>
  )
}

// ---- details -----------------------------------------------------------------------------------

function MatchDrawer({ id, onClose, onChanged, onError }: { id: number; onClose: () => void; onChanged: () => void; onError: (e: unknown) => void }) {
  const { data: a } = useData(() => api.application(id), [id])
  const [busy, setBusy] = useState('')
  const toast = useToast()

  const act = async (label: string, fn: () => Promise<unknown>, message: string) => {
    setBusy(label)
    try {
      await fn()
      toast(message)
      onChanged()
    } catch (e) {
      onError(e)
    } finally {
      setBusy('')
    }
  }

  if (!a) return <Drawer onClose={onClose}><div className="empty"><Spinner /></div></Drawer>
  const sc = a.screening
  const rs = (sc?.reasons?.length ? sc.reasons : reasons(a.notes)).filter((r) => !/^passes all filters$/i.test(r))
  const prefilling = a.prefill && ['preparing', 'needs_you', 'ready'].includes(a.prefill.status)

  return (
    <Drawer onClose={onClose}>
      <div className="drawer-head">
        <Avatar name={a.company} size={42} />
        <div className="grow">
          <h2 style={{ fontSize: 17 }}>{a.role}</h2>
          <div className="muted">{a.company}{a.location ? ` · ${a.location}` : ''}</div>
          <div className="row" style={{ marginTop: 10 }}>
            <StatusBadge status={a.status} />
            {sc?.is_entry_level && <span className="badge">Entry level</span>}
            {a.source && <span className="badge">via {sourceName(a.source)}</span>}
          </div>
        </div>
        <button className="btn ghost icon" onClick={onClose} aria-label="Close"><Icon name="x" /></button>
      </div>

      <div className="drawer-body">
        {sc && (
          <section>
            <div className="section-title" style={{ marginTop: 0 }}>Checks</div>
            <div className="check-list">
              <Check ok={!sc.citizenship_required && !sc.clearance_required} label="No citizenship or clearance requirement" />
              <Check ok={!['no_sponsorship_now_or_future'].includes(sc.sponsorship ?? '')}
                warn={sc.sponsorship === 'no_sponsorship_unspecified'}
                label={SPONSORSHIP[sc.sponsorship ?? 'not_mentioned'] ?? 'Sponsorship'} quote={sc.sponsorship_quote} />
              <Check ok={sc.min_years_experience == null || sc.min_years_experience <= 2 || !!sc.is_entry_level}
                label={sc.min_years_experience == null ? 'No years-of-experience requirement found' : `Asks for ${sc.min_years_experience}+ years`}
                quote={sc.experience_quote} />
            </div>
          </section>
        )}
        {rs.length > 0 && (
          <section>
            <div className="section-title" style={{ marginTop: 0 }}>{a.status === 'To Apply' ? 'Why it matched' : 'What to check'}</div>
            <div className="check-list">
              {rs.map((r, i) => {
                const { text, quote } = splitQuote(r)
                return (
                  <div key={i}>
                    <div>{text.replace(/^auto: /, '')}</div>
                    {quote && <div className="quote">{quote}</div>}
                  </div>
                )
              })}
            </div>
          </section>
        )}
        <section>
          <dl className="kv">
            {safeHref(a.link) && <><dt>Posting</dt><dd className="ellipsis"><a href={safeHref(a.link)!} target="_blank" rel="noopener noreferrer">{host(a.link)}</a></dd></>}
            <dt>Found</dt><dd>{timeAgo(a.created_at)}</dd>
            {a.job_id && <><dt>Job ID</dt><dd className="mono">{a.job_id}</dd></>}
            {a.prefill && <><dt>Prefill</dt><dd>{PREFILL[a.prefill.status] ?? a.prefill.status}{a.prefill.error ? ` (${a.prefill.error})` : ''}</dd></>}
          </dl>
        </section>
        <p className="faint" style={{ fontSize: 12.5 }}>
          appa never applies for you. "Prefill in my browser" fills the form in your appa Chrome window and leaves it open;
          you check it and press submit yourself.
        </p>
      </div>

      <div className="drawer-foot">
        {a.link && <ExternalLink href={a.link} className="btn primary">Open posting <Icon name="external" size={13} /></ExternalLink>}
        <button className="btn" disabled={!!busy} onClick={() => act('applied', () => api.markApplied(a.id), `Marked ${a.company} as applied`)}>
          <Icon name="check" size={14} /> I applied
        </button>
        <button className="btn" disabled={!!busy || !!prefilling} title="Fill the form in your browser; you submit it"
          onClick={() => act('prefill', () => api.prefill(a.id), 'Prefilling in your browser. You will see it in Needs you when it is ready.')}>
          {busy === 'prefill' ? <Spinner /> : <Icon name="wand" size={14} />} {prefilling ? 'Prefill in progress' : 'Prefill in my browser'}
        </button>
        <div className="grow" />
        {a.status === 'Needs Review' && (
          <button className="btn ghost" disabled={!!busy} onClick={() => act('ok', () => api.updateApplication(a.id, { status: 'To Apply' }), 'Moved to good matches')}>
            Looks fine
          </button>
        )}
        <button className="btn ghost" disabled={!!busy} onClick={() => act('skip', () => api.updateApplication(a.id, { status: 'Skipped' }), `Hid ${a.company}`)}>
          Not for me
        </button>
      </div>
    </Drawer>
  )
}

const SPONSORSHIP: Record<string, string> = {
  will_sponsor: 'Offers visa sponsorship',
  no_sponsorship_now_or_future: 'Will not sponsor, now or in the future',
  no_sponsorship_unspecified: 'Says no sponsorship (unclear whether OPT is fine)',
  authorized_to_work_only: 'Only asks that you are authorized to work',
  not_mentioned: 'Sponsorship not mentioned',
}

const PREFILL: Record<string, string> = {
  preparing: 'Filling it in your browser',
  needs_you: 'Waiting for your answers',
  ready: 'Filled and open in your browser: submit it there',
  applied: 'You applied',
  failed: 'Could not fill it',
  cancelled: 'Cancelled',
}

function Check({ ok, warn, label, quote }: { ok: boolean; warn?: boolean; label: string; quote?: string | null }) {
  const tone = warn ? 'amber' : ok ? 'green' : 'red'
  return (
    <div className="check-row">
      <span className="mark" style={{ background: `var(--${tone}-bg)`, color: `var(--${tone})` }}>
        <Icon name={ok && !warn ? 'check' : warn ? 'minus' : 'x'} size={11} />
      </span>
      <div className="grow">
        <div>{label}</div>
        {quote && <div className="quote">{quote}</div>}
      </div>
    </div>
  )
}

// ---- needs-you cards ---------------------------------------------------------------------------

function NeedCard({ q, onDone, onError }: { q: Question; onDone: () => void; onError: (e: unknown) => void }) {
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const toast = useToast()
  const send = async (action: string, choice?: string, message?: string) => {
    setBusy(true)
    try {
      await api.answer(q.id, action, choice)
      if (message) toast(message)
      onDone()
    } catch (e) {
      onError(e)
    } finally {
      setBusy(false)
    }
  }

  if (q.kind === 'apply_ready') {
    return (
      <div className="card need">
        <span className="need-icon" style={{ background: 'var(--green-bg)', color: 'var(--green)' }}><Icon name="wand" /></span>
        <div className="body">
          <div className="q">Ready for you to submit</div>
          <div className="muted">{q.prompt}</div>
          {q.context.failed && q.context.failed.length > 0 && (
            <div className="hint">Didn't fill: {q.context.failed.map((f) => f.label).join(', ')}</div>
          )}
          <div className="controls">
            <button className="btn primary sm" disabled={busy} onClick={() => send('applied', undefined, 'Marked as applied')}>I submitted it</button>
            {q.context.apply_url && <ExternalLink href={q.context.apply_url} className="btn sm">Open form <Icon name="external" size={12} /></ExternalLink>}
            <button className="btn ghost sm" disabled={busy} onClick={() => send('skip')}>Skip</button>
          </div>
        </div>
        {q.context.preview_upload_id && <img className="thumb" src={api.uploadUrl(q.context.preview_upload_id)} alt="The filled form" />}
      </div>
    )
  }

  if (q.kind === 'apply_field') {
    const opts = (q.context.options ?? []) as string[]
    return (
      <div className="card need">
        <span className="need-icon" style={{ background: 'var(--blue-bg)', color: 'var(--blue)' }}><Icon name="alert" /></span>
        <div className="body">
          <div className="q">{q.context.label}</div>
          <div className="muted">{q.prompt.split(':')[0]} asks this. Your answer is saved and reused on every form that asks it.</div>
          <form className="controls" onSubmit={(e) => { e.preventDefault(); if (value.trim()) send('answer', value.trim(), 'Saved') }}>
            {opts.length ? (
              <select className="select" style={{ maxWidth: 320 }} value={value} onChange={(e) => setValue(e.target.value)}>
                <option value="">Choose…</option>
                {opts.map((o) => <option key={o} value={o}>{o}</option>)}
              </select>
            ) : q.context.kind === 'textarea' ? (
              <textarea className="textarea" rows={4} value={value} onChange={(e) => setValue(e.target.value)} placeholder="Your answer" />
            ) : (
              <input className="input" style={{ maxWidth: 420 }} value={value} onChange={(e) => setValue(e.target.value)} placeholder="Your answer" />
            )}
            <button className="btn primary sm" disabled={busy || !value.trim()}>Save answer</button>
            <button type="button" className="btn ghost sm" disabled={busy} onClick={() => send('dismiss')}>Skip</button>
          </form>
        </div>
      </div>
    )
  }

  if (q.kind === 'answer_needed') {
    const opts = (q.context.options ?? []) as { title: string; location: string; url: string }[]
    return (
      <div className="card need">
        <span className="need-icon" style={{ background: 'var(--amber-bg)', color: 'var(--amber)' }}><Icon name="search" /></span>
        <div className="body">
          <div className="q">Which posting did you mean?</div>
          <div className="muted">{q.prompt}</div>
          <div className="controls">
            {opts.map((o) => (
              <button key={o.url} className="btn sm" disabled={busy} onClick={() => send('choose', o.url, 'Screening it now')}>
                {o.title}{o.location ? ` · ${o.location}` : ''}
              </button>
            ))}
            <button className="btn ghost sm" disabled={busy} onClick={() => send('dismiss')}>None of these</button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="card need">
      <span className="need-icon" style={{ background: 'var(--amber-bg)', color: 'var(--amber)' }}><Icon name="alert" /></span>
      <div className="body">
        <div className="muted">{q.prompt}</div>
        <div className="controls">
          {q.context.link && <ExternalLink href={q.context.link} className="btn sm">Open <Icon name="external" size={12} /></ExternalLink>}
          <button className="btn sm" disabled={busy} onClick={() => send('approve', undefined, 'Moved to good matches')}>Keep it</button>
          <button className="btn ghost sm" disabled={busy} onClick={() => send('skip')}>Not for me</button>
        </div>
      </div>
    </div>
  )
}

function AddLink({ onError }: { onError: (e: unknown) => void }) {
  const [open, setOpen] = useState(false)
  const [url, setUrl] = useState('')
  const toast = useToast()
  if (!open) return <button className="btn" onClick={() => setOpen(true)}><Icon name="link" size={14} /> Add a link</button>
  return (
    <form className="row" onSubmit={async (e) => {
      e.preventDefault()
      const urls = url.split(/\s+/).filter((u) => /^https?:\/\//i.test(u))
      if (!urls.length) return
      try {
        await api.addLinks(urls)
        toast(`Screening ${plural(urls.length, 'link')}`)
        setUrl('')
        setOpen(false)
      } catch (err) {
        onError(err)
      }
    }}>
      <input className="input" style={{ width: 300 }} autoFocus placeholder="Paste a job link" value={url} onChange={(e) => setUrl(e.target.value)} />
      <button className="btn">Check it</button>
      <button type="button" className="btn ghost icon" onClick={() => setOpen(false)} aria-label="Cancel"><Icon name="x" /></button>
    </form>
  )
}
