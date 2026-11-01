// Thin client for the appa API. Relative URLs only (same origin, through the proxy).
// The session cookie is HttpOnly, so JavaScript never sees it; writes carry the CSRF token.

import type { Application, Feeds, Listing, Me, Overview, Question, Run, RunEvent, Schedule, SystemStatus } from './types'

const BASE = '/api/v1'
let csrf = ''

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {}
  if (method !== 'GET' && csrf) headers['X-CSRF-Token'] = csrf
  let payload: BodyInit | undefined
  if (method !== 'GET') {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(body ?? {})
  }
  const res = await fetch(BASE + path, { method, headers, body: payload, credentials: 'same-origin' })
  const text = await res.text()
  const data = text ? safeJson(text) : {}
  if (!res.ok) {
    const msg = (data && typeof data === 'object' && 'error' in data ? String(data.error) : '') || res.statusText
    throw new ApiError(res.status, msg)
  }
  return data as T
}

function safeJson(text: string): any {
  try {
    return JSON.parse(text)
  } catch {
    return { error: text.slice(0, 200) }
  }
}

const qs = (params: Record<string, string | number | string[] | undefined>) => {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === '') continue
    for (const one of Array.isArray(v) ? v : [v]) p.append(k, String(one))
  }
  const s = p.toString()
  return s ? `?${s}` : ''
}

export const MATCH_STATUSES = ['To Apply', 'Needs Review', 'Apply Myself']
export const APPLIED_STATUSES = ['Applied', 'Interviewing', 'Offer', 'Rejected', 'Ghosted', 'Withdrawn']

export const api = {
  async me(): Promise<Me> {
    const me = await call<Me>('GET', '/auth/me')
    csrf = me.csrf_token
    return me
  },
  async login(username: string, password: string, totp?: string): Promise<Me> {
    const me = await call<Me>('POST', '/auth/login', { username, password, ...(totp ? { totp } : {}) })
    csrf = me.csrf_token
    return me
  },
  async logout() {
    await call('POST', '/auth/logout')
    csrf = ''
  },

  overview: () => call<Overview>('GET', '/system/overview'),
  system: () => call<SystemStatus>('GET', '/system'),
  pause: () => call<{ cancelled: number }>('POST', '/system/stop-all'),
  resume: () => call('POST', '/system/resume'),

  applications: (status: string[], q = '', limit = 200) =>
    call<{ applications: Application[] }>('GET', `/applications${qs({ status, q, limit })}`).then((r) => r.applications),
  application: (id: number) => call<Application>('GET', `/applications/${id}`),
  updateApplication: (id: number, patch: Partial<Pick<Application, 'status' | 'notes'>>) =>
    call<Application>('PATCH', `/applications/${id}`, patch),
  markApplied: (id: number) => call<{ id: number; status: string }>('POST', `/applications/${id}/applied`),
  prefill: (id: number) => call<{ id: number; ats: string; run_id: number }>('POST', `/applications/${id}/apply`),
  takeover: (id: number) => call<{ id: number; status: string; link: string | null }>('POST', `/applications/${id}/takeover`),

  questions: () => call<{ questions: Question[] }>('GET', '/questions').then((r) => r.questions),
  answer: (id: number, action: string, choice?: string) =>
    call<Question & { run_id?: number }>('POST', `/questions/${id}/answer`, { action, ...(choice ? { choice } : {}) }),

  listings: (status?: string, q?: string, limit = 100) =>
    call<{ listings: Listing[] }>('GET', `/listings${qs({ status, q, limit })}`).then((r) => r.listings),
  listingAction: (id: number, action: 'track' | 'skip' | 'restore') => call<any>('POST', `/listings/${id}/${action}`),
  feeds: () => call<Feeds>('GET', '/feeds'),

  runs: (limit = 30) => call<{ runs: Run[] }>('GET', `/runs?limit=${limit}`).then((r) => r.runs),
  events: (id: number, after = 0) =>
    call<{ events: RunEvent[] }>('GET', `/runs/${id}/events?after=${after}`).then((r) => r.events),
  cancelRun: (id: number) => call<Run>('POST', `/runs/${id}/cancel`),
  sweepNow: () => call<Run>('POST', '/runs', { role: 'scout', input: { sweep: true } }),
  addLinks: (urls: string[]) => call<Run>('POST', '/runs', { role: 'scout', input: { urls } }),

  schedules: () => call<{ schedules: Schedule[] }>('GET', '/schedules').then((r) => r.schedules),
  addSchedule: (cron: string) =>
    call<Schedule>('POST', '/schedules', { role: 'scout', cron, input: { sweep: true, screen_inbox: 600, max_new: 700 } }),
  setSchedule: (id: number, enabled: boolean) => call<Schedule>('PATCH', `/schedules/${id}`, { enabled }),

  profile: () => call<{ text: string }>('GET', '/profile'),
  saveProfile: (text: string) => call('PUT', '/profile', { text }),
  policy: () => call<{ policy: Record<string, any> }>('GET', '/policy').then((r) => r.policy),
  savePolicy: (policy: Record<string, any>) => call<{ policy: Record<string, any> }>('PUT', '/policy', policy),
  answers: () => call<{ answers: Record<string, string>; standard_keys: string[] }>('GET', '/answers'),
  setAnswer: (key: string, value: string) => call('PUT', '/answers', { key, value }),
  deleteAnswer: (key: string) => call('DELETE', `/answers/${encodeURIComponent(key)}`),

  uploadUrl: (id: number) => `${BASE}/uploads/${id}`,
}
