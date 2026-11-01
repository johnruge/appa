export type Me = { username: string; csrf_token: string; totp_enabled: boolean }

export type Run = {
  id: number
  role: string
  model: string | null
  status: string
  input: Record<string, unknown>
  budget: Record<string, number>
  cost_usd: number
  steps: number
  model_calls: number
  result: Record<string, unknown> | null
  error: string | null
  created_by: string
  created_at: string
}

export type RunEvent = { id: number; ts: string; kind: string; payload: Record<string, any> }

export type Question = {
  id: number
  kind: string
  prompt: string
  context: {
    options?: any[]
    link?: string
    status?: string
    reason?: string
    kind?: string
    attempt_id?: number
    preview_upload_id?: number | null
    apply_url?: string
    blocked?: boolean
    failed?: { label: string; error: string }[]
    plan?: { label: string; value: string | string[]; source: string }[]
  }
  status: string
  application_id: number | null
  run_id: number | null
  created_at: string
}

export type Schedule = {
  id: number
  role: string
  cron: string
  model: string | null
  input: Record<string, unknown>
  budget: Record<string, number>
  enabled: boolean
  next_run_at: string
  last_run_at: string | null
}

export type SystemStatus = {
  paused: boolean
  active_runs: number
  open_questions: number
  spent_24h_usd: number
  daily_cap_usd: number | null
}

export type ModelInfo = { name: string; vision: boolean; price_per_mtok: [number, number] }
