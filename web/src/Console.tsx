import { useCallback, useEffect, useRef, useState, type DragEvent, type KeyboardEvent } from 'react'
import { api, ApiError } from './api'
import { HELP, parse, type Budget } from './commands'
import Panel from './Panel'
import { renderEvent, STREAM_KINDS, type Line, type Tone } from './render'
import type { Me, Question, Run, Schedule, SystemStatus } from './types'

const MAX_LINES = 3000
const REFRESH_MS = 10_000

export type PanelData = { system: SystemStatus | null; questions: Question[]; runs: Run[]; schedules: Schedule[] }

export default function Console({ me, onLogout }: { me: Me; onLogout: () => void }) {
  const [lines, setLines] = useState<Line[]>([
    { text: `appa · logged in as ${me.username}. Type help, or drop screenshots anywhere.`, tone: 'dim' },
  ])
  const [input, setInput] = useState('')
  const [history, setHistory] = useState<string[]>([])
  const [histIdx, setHistIdx] = useState(-1)
  const [panel, setPanel] = useState<PanelData>({ system: null, questions: [], runs: [], schedules: [] })
  const [editor, setEditor] = useState<{ text: string; saving: boolean } | null>(null)
  const [dragging, setDragging] = useState(false)
  const streams = useRef(new Map<number, EventSource>())
  const outRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const print = useCallback((text: string | Line[], tone: Tone = 'out') => {
    const add: Line[] = typeof text === 'string' ? text.split('\n').map((t) => ({ text: t, tone })) : text
    setLines((prev) => [...prev, ...add].slice(-MAX_LINES))
  }, [])

  const refresh = useCallback(async () => {
    try {
      const [system, questions, runs, schedules] = await Promise.all([api.system(), api.questions(), api.runs(15), api.schedules()])
      setPanel({ system, questions, runs, schedules })
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onLogout()
    }
  }, [onLogout])

  useEffect(() => {
    // Polling the API is syncing with an external system; state is only set after the request resolves.
    // eslint-disable-next-line react/set-state-in-effect
    refresh()
    const t = setInterval(refresh, REFRESH_MS)
    const open = streams.current
    return () => {
      clearInterval(t)
      open.forEach((es) => es.close())
    }
  }, [refresh])

  useEffect(() => {
    outRef.current?.scrollTo({ top: outRef.current.scrollHeight })
  }, [lines])

  const follow = useCallback((id: number) => {
    if (streams.current.has(id)) return
    const es = new EventSource(api.streamUrl(id))   // same origin: the session cookie goes along
    streams.current.set(id, es)
    const tag = (ls: Line[]) => ls.map((l) => ({ ...l, text: `#${id} ${l.text}` }))
    for (const kind of STREAM_KINDS) {
      es.addEventListener(kind, (m) => {
        const ev = JSON.parse((m as MessageEvent).data)
        print(tag(renderEvent(ev)))
        if (kind === 'question' || kind === 'decision') refresh()
      })
    }
    es.addEventListener('end', () => {
      es.close()
      streams.current.delete(id)
      refresh()
    })
    es.onerror = () => {
      if (es.readyState === EventSource.CLOSED) {
        streams.current.delete(id)
        print(`#${id} stream closed`, 'dim')
      }
    }
  }, [print, refresh])

  const startRun = useCallback(async (role: string, inputData: Record<string, unknown>, model?: string, budget?: Budget) => {
    const run = await api.startRun(role, inputData, model, budget as Record<string, number> | undefined)
    print(`run #${run.id} (${role}) queued${model ? ` · ${model}` : ''}`, 'dim')
    follow(run.id)
    refresh()
  }, [follow, print, refresh])

  const uploadFiles = useCallback(async (files: FileList | File[]) => {
    const ids: number[] = []
    for (const f of Array.from(files)) {
      try {
        const up = await api.upload(f)
        ids.push(up.id)
        print(`uploaded ${f.name} → #${up.id} (${up.width}×${up.height})`, 'dim')
      } catch (e) {
        print(`upload ${f.name} failed: ${(e as Error).message}`, 'err')
      }
    }
    if (ids.length) await startRun('scout', { upload_ids: ids }).catch((e) => print((e as Error).message, 'err'))
  }, [print, startRun])

  async function exec(line: string) {
    const cmd = parse(line)
    if (cmd.kind === 'error') {
      if (cmd.message) print(cmd.message, 'err')
      return
    }
    try {
      switch (cmd.kind) {
        case 'help':
          return print(HELP, 'dim')
        case 'clear':
          return setLines([])
        case 'run':
          return await startRun(cmd.role, cmd.input, cmd.model, cmd.budget)
        case 'upload':
          return fileRef.current?.click()
        case 'runs': {
          const runs = await api.runs(20)
          return print(runs.map((r) => `#${String(r.id).padEnd(5)} ${r.role.padEnd(10)} ${r.status.padEnd(16)} $${r.cost_usd.toFixed(4).padEnd(8)} ${r.model ?? 'default'}`).join('\n') || 'no runs yet')
        }
        case 'follow': {
          const r = await api.run(cmd.id)
          print(`following run #${r.id} (${r.role}, ${r.status})`, 'dim')
          return follow(cmd.id)
        }
        case 'cancel': {
          const r = await api.cancel(cmd.id)
          refresh()
          return print(`run #${r.id}: ${r.status}`, 'warn')
        }
        case 'schedule-add': {
          const s = await api.addSchedule(cmd.role, cmd.cron, cmd.input, cmd.model, cmd.budget as Record<string, number> | undefined)
          refresh()
          return print(`schedule #${s.id}: ${s.role} at "${s.cron}" UTC, next ${new Date(s.next_run_at).toLocaleString()}`, 'ok')
        }
        case 'schedule-list': {
          const ss = await api.schedules()
          return print(ss.map((s) => `#${s.id} ${s.enabled ? 'on ' : 'off'} ${s.role} "${s.cron}" next ${new Date(s.next_run_at).toLocaleString()} ${s.model ?? ''}`).join('\n') || 'no schedules')
        }
        case 'schedule-set': {
          if (cmd.action === 'rm') await api.deleteSchedule(cmd.id)
          else await api.setSchedule(cmd.id, cmd.action === 'on')
          refresh()
          return print(`schedule #${cmd.id}: ${cmd.action}`, 'ok')
        }
        case 'review': {
          const qs = await api.questions()
          return print(qs.map((q) => `#${q.id} [${q.kind}] ${q.prompt}`).join('\n') || 'nothing waiting on you', qs.length ? 'accent' : 'dim')
        }
        case 'answer': {
          const r = await api.answer(cmd.id, cmd.action, cmd.choice)
          refresh()
          print(`question #${cmd.id}: ${cmd.action}`, 'ok')
          if (r.run_id) follow(r.run_id)
          return
        }
        case 'models': {
          const m = await api.models()
          return print(m.models.map((x) => `${x.name.padEnd(44)} $${x.price_per_mtok[0]}/$${x.price_per_mtok[1]} per Mtok${x.vision ? ' · vision' : ''}${Object.values(m.defaults).includes(x.name) ? ' · default' : ''}`).join('\n'))
        }
        case 'roles': {
          const rs = await api.roles()
          return print(rs.map((r) => `${r.name.padEnd(10)} ${r.description}`).join('\n'))
        }
        case 'status': {
          const s = await api.system()
          return print(`${s.paused ? 'PAUSED · ' : ''}${s.active_runs} active run(s) · ${s.open_questions} open question(s) · spent $${s.spent_24h_usd.toFixed(4)} of $${s.daily_cap_usd ?? '∞'} (24h)`)
        }
        case 'profile': {
          const p = await api.profile()
          if (cmd.edit) return setEditor({ text: p.text, saving: false })
          return print(p.text || '(no profile.md yet: type `profile edit`)', 'out')
        }
        case 'policy': {
          const p = await api.policy()
          return print(JSON.stringify(p.policy, null, 2), 'out')
        }
        case 'stop-all': {
          const r = await api.stopAll()
          refresh()
          return print(`paused. cancelled ${r.cancelled} active run(s). type resume to undo`, 'warn')
        }
        case 'resume':
          await api.resume()
          refresh()
          return print('resumed', 'ok')
        case 'logout':
          await api.logout()
          return onLogout()
        case 'apply':
          for (const appId of cmd.ids) {
            try {
              const a = await api.startApply(appId)
              print(`#${appId}: attempt #${a.id} queued for your runner (${a.ats}); you'll approve before anything is submitted`, 'ok')
              follow(a.run_id)
            } catch (e) {
              print(`#${appId}: ${(e as Error).message}`, 'err')
            }
          }
          return refresh()
        case 'apps': {
          const apps = await api.applications(cmd.status)
          return print(apps.map((a) => `#${String(a.id).padEnd(5)} ${String(a.status).padEnd(13)} ${a.company} / ${a.role}`).join('\n') || `no applications with status ${cmd.status}`)
        }
        case 'attempts': {
          const at = await api.attempts()
          return print(at.map((a) => `#${String(a.id).padEnd(4)} ${String(a.status).padEnd(18)} app #${a.application_id} ${a.ats}${a.error ? '  ' + a.error.slice(0, 80) : ''}`).join('\n') || 'no attempts yet')
        }
        case 'answers': {
          const r = await api.answers()
          const lines = Object.entries(r.answers).sort().map(([k, v]) => `${k.padEnd(30)} ${v.slice(0, 80)}`)
          const unset = r.standard_keys.filter((k) => !(k in r.answers))
          return print([...lines, unset.length ? `not set: ${unset.join(', ')}` : ''].join('\n'))
        }
        case 'answer-set':
          await api.setAnswer(cmd.key, cmd.value)
          return print(`${cmd.key} saved`, 'ok')
        case 'takeover':
          for (const appId of cmd.ids) {
            try {
              const r = await api.takeover(appId)
              print(`#${r.id} is yours (Apply Myself): ${r.link ?? 'no link on file'}`, 'ok')
            } catch (e) {
              print(`#${appId}: ${(e as Error).message}`, 'err')
            }
          }
          return refresh()
        case 'feeds': {
          const r = await api.feeds()
          const lists = r.lists.map((l) => `${String(l.name).padEnd(22)} ${(l.ready ? (l.default ? 'ready' : 'ready (not in all)') : 'needs ' + l.needs.join(', ')).padEnd(30)} ${l.description}`)
          const boards = Object.entries(r.boards).map(([k, v]) => `${k} ${v}`).join(', ') || 'none yet'
          const inbox = Object.entries(r.inbox).map(([k, v]) => `${k} ${v}`).join(', ') || 'empty'
          return print([...lists, '', `board ATSs: ${r.board_ats.join(', ')}`, `boards in rotation: ${boards}`, `inbox: ${inbox}`].join('\n'))
        }
        case 'listings': {
          const rows = await api.listings(cmd.status, cmd.q)
          return print(rows.map((l) => `#${String(l.id).padEnd(6)} ${(l.posted_at ?? '').padEnd(10)} ${String(l.company).slice(0, 24).padEnd(24)} ${String(l.title).slice(0, 48).padEnd(48)} ${String(l.location).slice(0, 24)}${l.reason && l.status !== 'new' ? '  (' + String(l.reason).slice(0, 50) + ')' : ''}`).join('\n') || `no ${cmd.status} listings`)
        }
        case 'listing-action':
          for (const lid of cmd.ids) {
            try {
              const r = await api.listingAction(lid, cmd.action)
              print(cmd.action === 'track' ? `#${lid} -> application #${r.application_id}${r.created ? '' : ' (already tracked)'}` : `#${lid}: ${r.status}`, 'ok')
            } catch (e) {
              print(`#${lid}: ${(e as Error).message}`, 'err')
            }
          }
          return refresh()
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return onLogout()
      print(e instanceof Error ? e.message : String(e), 'err')
    }
  }

  function onKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter') {
      const line = input.trim()
      if (!line) return
      print(`› ${line}`, 'cmd')
      setHistory((h) => [line, ...h.filter((x) => x !== line)].slice(0, 200))
      setHistIdx(-1)
      setInput('')
      exec(line)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      const i = Math.min(histIdx + 1, history.length - 1)
      if (history[i] !== undefined) {
        setHistIdx(i)
        setInput(history[i])
      }
    } else if (e.key === 'ArrowDown') {
      e.preventDefault()
      const i = histIdx - 1
      setHistIdx(Math.max(i, -1))
      setInput(i >= 0 ? history[i] : '')
    } else if (e.key === 'l' && e.ctrlKey) {
      e.preventDefault()
      setLines([])
    }
  }

  async function answerField(qid: number, value: string) {
    print(`› answer #${qid}: ${value}`, 'cmd')
    try {
      const r: any = await api.answer(qid, 'answer', value)
      print(`saved. ${r.resumed_attempts?.length ? `resumed attempt(s) ${r.resumed_attempts.join(', ')}` : 'other questions still open for this form'}`, 'ok')
    } catch (e) {
      print((e as Error).message, 'err')
    }
    refresh()
  }

  async function saveProfile() {
    if (!editor) return
    setEditor({ ...editor, saving: true })
    try {
      const r = await api.saveProfile(editor.text)
      print(`profile.md saved (version ${r.version_id})`, 'ok')
      setEditor(null)
    } catch (e) {
      print(`profile not saved: ${(e as Error).message}`, 'err')
      setEditor({ ...editor, saving: false })
    }
  }

  const onDrop = (e: DragEvent) => {
    e.preventDefault()
    setDragging(false)
    if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files)
  }

  return (
    <div className="shell" onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
      onDragLeave={() => setDragging(false)} onDrop={onDrop}>
      <main className="term" onClick={() => window.getSelection()?.isCollapsed && inputRef.current?.focus()}>
        <div className="out" ref={outRef} role="log" aria-live="polite">
          {lines.map((l, i) => <div key={i} className={`line ${l.tone}`}>{l.text || ' '}</div>)}
        </div>
        <div className="prompt">
          <span className="ps1">{me.username}@appa ›</span>
          <input ref={inputRef} autoFocus spellCheck={false} autoComplete="off" value={input}
            onChange={(e) => setInput(e.target.value)} onKeyDown={onKey} aria-label="command" />
        </div>
      </main>
      <Panel data={panel} onCommand={(c) => { print(`› ${c}`, 'cmd'); exec(c) }} onAnswer={answerField} />
      <input ref={fileRef} type="file" accept="image/png,image/jpeg,image/webp" multiple hidden
        onChange={(e) => { if (e.target.files?.length) uploadFiles(e.target.files); e.target.value = '' }} />
      {dragging && <div className="dropzone">drop screenshots to scout them</div>}
      {editor && (
        <div className="modal" role="dialog" aria-label="edit profile.md">
          <div className="modal-box">
            <div className="modal-head">profile.md: who applies (judgment). Hard rules live in your policy.</div>
            <textarea value={editor.text} spellCheck={false} onChange={(e) => setEditor({ ...editor, text: e.target.value })} />
            <div className="modal-actions">
              <button onClick={() => setEditor(null)} disabled={editor.saving}>cancel</button>
              <button onClick={saveProfile} disabled={editor.saving}>{editor.saving ? 'saving…' : 'save new version'}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
