// Command line -> structured command. Pure functions, unit-tested (commands.test.ts).

export type Budget = { max_usd?: number; max_steps?: number }

export type Command =
  | { kind: 'help' }
  | { kind: 'clear' }
  | { kind: 'run'; role: string; input: Record<string, unknown>; model?: string; budget?: Budget }
  | { kind: 'upload' }
  | { kind: 'runs' }
  | { kind: 'follow'; id: number }
  | { kind: 'cancel'; id: number }
  | { kind: 'schedule-add'; role: string; cron: string; input: Record<string, unknown>; model?: string; budget?: Budget }
  | { kind: 'schedule-list' }
  | { kind: 'schedule-set'; id: number; action: 'rm' | 'on' | 'off' }
  | { kind: 'review' }
  | { kind: 'answer'; id: number; action: string; choice?: string }
  | { kind: 'models' }
  | { kind: 'roles' }
  | { kind: 'status' }
  | { kind: 'profile'; edit: boolean }
  | { kind: 'policy' }
  | { kind: 'stop-all' }
  | { kind: 'resume' }
  | { kind: 'logout' }
  | { kind: 'apply'; ids: number[] }
  | { kind: 'apps'; status?: string }
  | { kind: 'attempts' }
  | { kind: 'answers' }
  | { kind: 'answer-set'; key: string; value: string }
  | { kind: 'takeover'; ids: number[] }
  | { kind: 'feeds' }
  | { kind: 'listings'; status?: string; q?: string }
  | { kind: 'listing-action'; action: 'track' | 'skip' | 'restore'; ids: number[] }

export type Parsed = Command | { kind: 'error'; message: string }

export const HELP = `commands
  scout [--board greenhouse:slug|config]... [--url URL]... [--model M] [--max-usd N] [--max-new N]
  poll [--board ...] [--model M] [--max-usd N]      scout your boards (default: all in your policy)
  add URL... [--model M] [--force]                  screen job links
  screen URL...                                     screener role on links
  concierge                                         turn review items into questions
  upload                                            pick screenshots (or drag & drop them anywhere)
  runs | follow ID | cancel ID
  schedule add ROLE "CRON" [--board ...] [--model M] [--max-usd N]
  schedule list | schedule rm|on|off ID
  review                                            open questions
  answer ID approve|apply_myself|skip|dismiss       answer ID choose URL
  apps [STATUS]                                     your applications (default: To Apply)
  apply ID...                                       fill in your browser; waits for your approval
  takeover ID...                                    you apply yourself: stops appa's attempt, shows the link
  sweep [--max-usd N] [--max-new N]                 read every job source, poll company boards, screen the newest
  feeds                                             job sources, inbox and board counts
  listings [new|filtered|screened|skipped|error] [--q TEXT]    the inbox of postings found
  track|skip|restore LISTING_ID...                  add a listing as To Apply / skip it / un-filter it
  attempts                                          application attempts and their status
  answers | answers set KEY "VALUE"                 your answers bank for forms
  models | roles | status | profile [edit] | policy
  stop-all | resume | clear | logout`

/** Split a command line into words, honoring "double" and 'single' quotes. */
export function tokenize(line: string): string[] {
  const out: string[] = []
  let cur = ''
  let quote: string | null = null
  let started = false
  for (const ch of line) {
    if (quote) {
      if (ch === quote) quote = null
      else cur += ch
    } else if (ch === '"' || ch === "'") {
      quote = ch
      started = true
    } else if (/\s/.test(ch)) {
      if (started || cur) out.push(cur)
      cur = ''
      started = false
    } else {
      cur += ch
      started = true
    }
  }
  if (quote) throw new Error('unclosed quote')
  if (started || cur) out.push(cur)
  return out
}

type Flags = { positional: string[]; values: Record<string, string[]>; bools: Set<string> }
const BOOL_FLAGS = new Set(['force'])

function flags(words: string[]): Flags {
  const f: Flags = { positional: [], values: {}, bools: new Set() }
  for (let i = 0; i < words.length; i++) {
    const w = words[i]
    if (w.startsWith('--')) {
      const name = w.slice(2)
      if (BOOL_FLAGS.has(name)) {
        f.bools.add(name)
        continue
      }
      const v = words[i + 1]
      if (v === undefined || v.startsWith('--')) throw new Error(`--${name} needs a value`)
      ;(f.values[name] ??= []).push(v)
      i++
    } else f.positional.push(w)
  }
  return f
}

function one(f: Flags, name: string): string | undefined {
  const v = f.values[name]
  if (v && v.length > 1) throw new Error(`--${name} given more than once`)
  return v?.[0]
}

function num(f: Flags, name: string): number | undefined {
  const v = one(f, name)
  if (v === undefined) return undefined
  const n = Number(v)
  if (!Number.isFinite(n) || n <= 0) throw new Error(`--${name} must be a positive number`)
  return n
}

function id(word: string | undefined, what: string): number {
  const n = Number(word)
  if (!word || !Number.isInteger(n) || n <= 0) throw new Error(`${what} needs a numeric id`)
  return n
}

function budget(f: Flags): Budget | undefined {
  const b: Budget = {}
  const usd = num(f, 'max-usd')
  if (usd !== undefined) b.max_usd = usd
  const steps = num(f, 'max-steps')
  if (steps !== undefined) b.max_steps = Math.floor(steps)
  return Object.keys(b).length ? b : undefined
}

function isUrl(s: string): boolean {
  return /^https?:\/\/\S+$/i.test(s)
}

const KNOWN = new Set(['board', 'url', 'model', 'max-usd', 'max-steps', 'max-new', 'q'])
const LISTING_STATUSES = ['new', 'filtered', 'duplicate', 'screened', 'skipped', 'error']

export function parse(line: string): Parsed {
  try {
    const words = tokenize(line.trim())
    if (!words.length) return { kind: 'error', message: '' }
    const [head, ...rest] = words
    const f = flags(rest)
    for (const k of Object.keys(f.values)) if (!KNOWN.has(k)) throw new Error(`unknown option --${k}`)
    const model = one(f, 'model')
    switch (head.toLowerCase()) {
      case 'help':
      case '?':
        return { kind: 'help' }
      case 'clear':
        return { kind: 'clear' }
      case 'scout':
      case 'poll': {
        const input: Record<string, unknown> = {}
        const boards = f.values.board ?? (head === 'poll' ? ['config'] : [])
        if (boards.length) input.boards = boards
        if (f.values.url) input.urls = f.values.url
        const maxNew = num(f, 'max-new')
        if (maxNew) input.max_new = Math.floor(maxNew)
        if (!boards.length && !f.values.url) throw new Error('scout needs --board or --url (or use poll)')
        return { kind: 'run', role: 'scout', input, model, budget: budget(f) }
      }
      case 'add':
      case 'screen': {
        if (!f.positional.length) throw new Error(`${head} needs at least one job link`)
        const bad = f.positional.find((u) => !isUrl(u))
        if (bad) throw new Error(`not a link: ${bad}`)
        const role = head === 'add' ? 'scout' : 'screener'
        return { kind: 'run', role, input: { urls: f.positional, ...(f.bools.has('force') ? { force: true } : {}) }, model, budget: budget(f) }
      }
      case 'concierge':
        return { kind: 'run', role: 'concierge', input: {}, budget: budget(f) }
      case 'upload':
        return { kind: 'upload' }
      case 'runs':
        return { kind: 'runs' }
      case 'follow':
        return { kind: 'follow', id: id(f.positional[0], 'follow') }
      case 'cancel':
        return { kind: 'cancel', id: id(f.positional[0], 'cancel') }
      case 'schedule':
      case 'schedules': {
        const [sub, ...args] = f.positional
        if (!sub || sub === 'list') return { kind: 'schedule-list' }
        if (sub === 'add') {
          const [role, cron] = args
          if (!role || !cron) throw new Error('usage: schedule add ROLE "CRON" (e.g. schedule add scout "0 */4 * * *" --board config)')
          if (cron.trim().split(/\s+/).length !== 5) throw new Error('cron needs 5 fields in quotes, e.g. "0 */4 * * *"')
          const input: Record<string, unknown> = {}
          if (f.values.board) input.boards = f.values.board
          return { kind: 'schedule-add', role, cron, input, model, budget: budget(f) }
        }
        if (sub === 'rm' || sub === 'on' || sub === 'off') return { kind: 'schedule-set', id: id(args[0], `schedule ${sub}`), action: sub }
        throw new Error(`unknown schedule command: ${sub}`)
      }
      case 'review':
      case 'questions':
        return { kind: 'review' }
      case 'answer': {
        const [qid, action, choice] = f.positional
        const actions = ['approve', 'apply_myself', 'skip', 'dismiss', 'choose']
        if (!action || !actions.includes(action)) throw new Error(`usage: answer ID ${actions.join('|')}`)
        if (action === 'choose' && (!choice || !isUrl(choice))) throw new Error('usage: answer ID choose URL')
        return { kind: 'answer', id: id(qid, 'answer'), action, choice }
      }
      case 'models':
        return { kind: 'models' }
      case 'roles':
        return { kind: 'roles' }
      case 'status':
        return { kind: 'status' }
      case 'profile':
        return { kind: 'profile', edit: f.positional[0] === 'edit' }
      case 'policy':
        return { kind: 'policy' }
      case 'stop-all':
        return { kind: 'stop-all' }
      case 'resume':
        return { kind: 'resume' }
      case 'logout':
        return { kind: 'logout' }
      case 'apply': {
        if (!f.positional.length) throw new Error('usage: apply ID... (see `apps`)')
        return { kind: 'apply', ids: f.positional.map((w) => id(w, 'apply')) }
      }
      case 'apps':
        return { kind: 'apps', status: f.positional.length ? f.positional.join(' ') : 'To Apply' }
      case 'takeover': {
        if (!f.positional.length) throw new Error('usage: takeover APP_ID... (see `apps`)')
        return { kind: 'takeover', ids: f.positional.map((w) => id(w, 'takeover')) }
      }
      case 'sweep': {
        const input: Record<string, unknown> = { sweep: true }
        const maxNew = num(f, 'max-new')
        if (maxNew) input.max_new = Math.floor(maxNew)
        return { kind: 'run', role: 'scout', input, model, budget: budget(f) }
      }
      case 'feeds':
        return { kind: 'feeds' }
      case 'listings': {
        const status = f.positional[0]
        if (status && !LISTING_STATUSES.includes(status)) throw new Error(`status is one of ${LISTING_STATUSES.join('|')}`)
        return { kind: 'listings', status: status ?? 'new', q: one(f, 'q') }
      }
      case 'track':
      case 'skip':
      case 'restore': {
        if (!f.positional.length) throw new Error(`usage: ${head} LISTING_ID... (see \`listings\`)`)
        return { kind: 'listing-action', action: head as 'track' | 'skip' | 'restore', ids: f.positional.map((w) => id(w, head)) }
      }
      case 'attempts':
        return { kind: 'attempts' }
      case 'answers': {
        if (f.positional[0] === 'set') {
          const [, key, value] = f.positional
          if (!key || value === undefined) throw new Error('usage: answers set KEY "VALUE"')
          if (!/^[a-z_]+$/.test(key)) throw new Error('KEY is a standard key like first_name (see `answers`)')
          return { kind: 'answer-set', key, value }
        }
        return { kind: 'answers' }
      }
      default:
        return { kind: 'error', message: `unknown command: ${head} (type help)` }
    }
  } catch (e) {
    return { kind: 'error', message: (e as Error).message }
  }
}
