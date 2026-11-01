// Thin client for the appa API. Relative URLs only (same origin, through the proxy).
// The session cookie is HttpOnly, so JavaScript never sees it; writes carry the CSRF token.

import type { Me, ModelInfo, Question, Run, RunEvent, Schedule, SystemStatus } from './types'

const BASE = '/api/v1'
let csrf = ''

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function call<T>(method: string, path: string, body?: unknown, form?: FormData): Promise<T> {
  const headers: Record<string, string> = {}
  if (method !== 'GET' && csrf) headers['X-CSRF-Token'] = csrf
  let payload: BodyInit | undefined
  if (form) payload = form
  else if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  } else if (method !== 'GET') {
    headers['Content-Type'] = 'application/json'
    payload = '{}'
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
  startRun: (role: string, input: Record<string, unknown>, model?: string, budget?: Record<string, number>) =>
    call<Run>('POST', '/runs', { role, input, ...(model ? { model } : {}), ...(budget ? { budget } : {}) }),
  runs: (limit = 20) => call<{ runs: Run[] }>('GET', `/runs?limit=${limit}`).then((r) => r.runs),
  run: (id: number) => call<Run>('GET', `/runs/${id}`),
  events: (id: number, after = 0) =>
    call<{ events: RunEvent[] }>('GET', `/runs/${id}/events?after=${after}`).then((r) => r.events),
  cancel: (id: number) => call<Run>('POST', `/runs/${id}/cancel`),
  questions: () => call<{ questions: Question[] }>('GET', '/questions').then((r) => r.questions),
  answer: (id: number, action: string, choice?: string) =>
    call<Question & { run_id?: number }>('POST', `/questions/${id}/answer`, { action, ...(choice ? { choice } : {}) }),
  schedules: () => call<{ schedules: Schedule[] }>('GET', '/schedules').then((r) => r.schedules),
  addSchedule: (role: string, cron: string, input: Record<string, unknown>, model?: string, budget?: Record<string, number>) =>
    call<Schedule>('POST', '/schedules', { role, cron, input, ...(model ? { model } : {}), ...(budget ? { budget } : {}) }),
  setSchedule: (id: number, enabled: boolean) => call<Schedule>('PATCH', `/schedules/${id}`, { enabled }),
  deleteSchedule: (id: number) => call('DELETE', `/schedules/${id}`),
  profile: () => call<{ text: string; versions: number }>('GET', '/profile'),
  saveProfile: (text: string) => call<{ version_id: number }>('PUT', '/profile', { text }),
  policy: () => call<{ policy: Record<string, any>; versions: number }>('GET', '/policy'),
  models: () => call<{ defaults: Record<string, string>; models: ModelInfo[] }>('GET', '/models'),
  roles: () => call<{ roles: { name: string; description: string }[] }>('GET', '/roles').then((r) => r.roles),
  system: () => call<SystemStatus>('GET', '/system'),
  stopAll: () => call<{ cancelled: number }>('POST', '/system/stop-all'),
  resume: () => call('POST', '/system/resume'),
  upload(file: File) {
    const form = new FormData()
    form.append('file', file)
    return call<{ id: number; width: number; height: number }>('POST', '/uploads', undefined, form)
  },
  streamUrl: (id: number) => `${BASE}/runs/${id}/stream`,
  uploadUrl: (id: number) => `${BASE}/uploads/${id}`,
  startApply: (appId: number) => call<{ id: number; ats: string; run_id: number }>('POST', `/applications/${appId}/apply`),
  attempts: () => call<{ attempts: any[] }>('GET', '/apply/attempts?limit=20').then((r) => r.attempts),
  applications: (status?: string) =>
    call<{ applications: any[] }>('GET', `/applications?limit=50${status ? `&status=${encodeURIComponent(status)}` : ''}`).then((r) => r.applications),
  answers: () => call<{ answers: Record<string, string>; standard_keys: string[] }>('GET', '/answers'),
  setAnswer: (key: string, value: string) => call('PUT', '/answers', { key, value }),
  takeover: (appId: number) => call<{ id: number; status: string; link: string | null }>('POST', `/applications/${appId}/takeover`),
  feeds: () => call<{ lists: any[]; board_ats: string[]; boards: Record<string, number>; inbox: Record<string, number>; load_errors: string[] }>('GET', '/feeds'),
  listings: (status?: string, q?: string) =>
    call<{ listings: any[] }>('GET', `/listings?limit=50${status ? `&status=${encodeURIComponent(status)}` : ''}${q ? `&q=${encodeURIComponent(q)}` : ''}`).then((r) => r.listings),
  listingAction: (id: number, action: 'track' | 'skip' | 'restore') => call<any>('POST', `/listings/${id}/${action}`),
}
