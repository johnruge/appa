// What appa looks for and how: your preferences in plain words, the hard filters, and your saved answers.

import { useEffect, useState } from 'react'
import { api } from '../api'
import { toList } from '../format'
import type { Overview } from '../types'
import { Icon, Spinner } from '../ui'
import { useData, useToast } from '../hooks'

type Props = { onError: (e: unknown) => void; refresh: () => void; overview: Overview | null }

export default function Settings({ onError }: Props) {
  return (
    <div className="page" style={{ maxWidth: 820 }}>
      <div className="page-head">
        <div className="grow">
          <h1>Settings</h1>
          <p className="sub">What appa looks for, and the answers it uses to prefill forms.</p>
        </div>
      </div>
      <Filters onError={onError} />
      <Preferences onError={onError} />
      <Answers onError={onError} />
    </div>
  )
}

function Filters({ onError }: { onError: (e: unknown) => void }) {
  const { data: policy, setData } = useData(() => api.policy(), [])
  const [form, setForm] = useState<Record<string, any> | null>(null)
  const [saving, setSaving] = useState(false)
  const toast = useToast()

  useEffect(() => {
    if (policy && !form) {
      // eslint-disable-next-line react/set-state-in-effect -- initial copy of the loaded policy into the form
      setForm({
        includes: (policy.rules?.title_includes ?? []).join(', '),
        excludes: (policy.rules?.title_excludes ?? []).join(', '),
        never: (policy.rules?.never_apply ?? []).join(', '),
        manual: (policy.rules?.manual_companies ?? []).join(', '),
        years: policy.rules?.max_years_experience ?? 2,
        age: policy.feeds?.max_age_days ?? 45,
        usOnly: policy.feeds?.us_only ?? true,
      })
    }
  }, [policy, form])

  if (!form || !policy) return <section className="card card-pad"><Spinner /></section>
  const set = (k: string, v: any) => setForm({ ...form, [k]: v })

  const save = async () => {
    setSaving(true)
    try {
      const next = structuredClone(policy)
      next.rules = { ...next.rules, title_includes: toList(form.includes), title_excludes: toList(form.excludes),
        never_apply: toList(form.never), manual_companies: toList(form.manual), max_years_experience: Number(form.years) }
      next.feeds = { ...next.feeds, max_age_days: Number(form.age), us_only: !!form.usOnly }
      const r = await api.savePolicy(next)
      setData(r.policy)
      toast('Filters saved. They apply from the next search.')
    } catch (e) {
      onError(e)
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <div className="section-title">Filters</div>
      <section className="card card-pad stack">
        <Field label="Titles to look for" hint="A posting's title must contain one of these.">
          <input className="input" value={form.includes} onChange={(e) => set('includes', e.target.value)} />
        </Field>
        <Field label="Titles to skip" hint="Whole words, e.g. senior, staff, intern.">
          <input className="input" value={form.excludes} onChange={(e) => set('excludes', e.target.value)} />
        </Field>
        <div className="row" style={{ gap: 16, alignItems: 'flex-start' }}>
          <Field label="Most years of experience" hint="Unless the role is new-grad or entry level.">
            <input className="input" type="number" min={0} max={20} value={form.years} onChange={(e) => set('years', e.target.value)} />
          </Field>
          <Field label="Ignore postings older than" hint="Days, when the source dates them.">
            <input className="input" type="number" min={1} max={365} value={form.age} onChange={(e) => set('age', e.target.value)} />
          </Field>
        </div>
        <Field label="Companies to always skip">
          <input className="input" value={form.never} onChange={(e) => set('never', e.target.value)} placeholder="Comma separated" />
        </Field>
        <Field label="Companies you'll handle yourself" hint='These go to "Apply directly".'>
          <input className="input" value={form.manual} onChange={(e) => set('manual', e.target.value)} placeholder="Comma separated" />
        </Field>
        <label className="check">
          <input type="checkbox" checked={form.usOnly} onChange={(e) => set('usOnly', e.target.checked)} />
          Only US roles (remote and multi-location roles stay in)
        </label>
        <div><button className="btn primary" disabled={saving} onClick={save}>{saving ? <Spinner /> : 'Save filters'}</button></div>
      </section>
    </>
  )
}

function Preferences({ onError }: { onError: (e: unknown) => void }) {
  const { data } = useData(() => api.profile(), [])
  const [text, setText] = useState<string | null>(null)
  const toast = useToast()
  const value = text ?? data?.text ?? ''
  return (
    <>
      <div className="section-title">Preferences in your words</div>
      <section className="card card-pad stack">
        <p className="muted">
          Plain language appa uses to decide which good matches you'd rather apply to directly (for example big companies you
          know well). It only ever sees the company, title and location.
        </p>
        <textarea className="textarea mono" rows={10} value={value} onChange={(e) => setText(e.target.value)} />
        <div>
          <button className="btn primary" disabled={text === null} onClick={async () => {
            try { await api.saveProfile(value); setText(null); toast('Preferences saved') } catch (e) { onError(e) }
          }}>Save preferences</button>
        </div>
      </section>
    </>
  )
}

function Answers({ onError }: { onError: (e: unknown) => void }) {
  const { data, reload } = useData(() => api.answers(), [])
  const [edit, setEdit] = useState<Record<string, string>>({})
  const toast = useToast()
  const answers = data?.answers ?? {}
  const standard = (data?.standard_keys ?? []).filter((k) => !(k in answers))
  const saved = Object.entries(answers).sort(([a], [b]) => (a.startsWith('q:') ? 1 : 0) - (b.startsWith('q:') ? 1 : 0) || a.localeCompare(b))

  const save = async (key: string) => {
    try {
      await api.setAnswer(key, edit[key])
      setEdit((all) => Object.fromEntries(Object.entries(all).filter(([k]) => k !== key)))
      toast('Saved')
      reload()
    } catch (e) {
      onError(e)
    }
  }

  return (
    <>
      <div className="section-title">Your answers</div>
      <section className="card">
        <p className="muted card-pad" style={{ paddingBottom: 0 }}>
          Used only to prefill forms in your browser. Questions you answer from a card are saved here too.
        </p>
        <table className="table">
          <tbody>
            {saved.map(([k, v]) => (
              <tr key={k}>
                <td className="muted" style={{ width: '42%' }}>{label(k)}</td>
                <td>
                  <div className="row">
                    <input className="input" value={edit[k] ?? v} onChange={(e) => setEdit({ ...edit, [k]: e.target.value })} />
                    {edit[k] !== undefined && edit[k] !== v && <button className="btn sm primary" onClick={() => save(k)}>Save</button>}
                    <button className="btn ghost sm icon" title="Remove" onClick={async () => {
                      try { await api.deleteAnswer(k); reload() } catch (e) { onError(e) }
                    }}><Icon name="x" size={13} /></button>
                  </div>
                </td>
              </tr>
            ))}
            {standard.map((k) => (
              <tr key={k}>
                <td className="faint" style={{ width: '42%' }}>{label(k)}</td>
                <td>
                  <div className="row">
                    <input className="input" placeholder="Not set" value={edit[k] ?? ''} onChange={(e) => setEdit({ ...edit, [k]: e.target.value })} />
                    {edit[k] && <button className="btn sm primary" onClick={() => save(k)}>Save</button>}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  )
}

function label(key: string): string {
  if (key.startsWith('q:')) return `“${key.slice(2)}”`
  return key.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase())
}

function Field({ label: text, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="grow">
      <span className="label">{text}</span>
      {children}
      {hint && <div className="hint">{hint}</div>}
    </div>
  )
}
