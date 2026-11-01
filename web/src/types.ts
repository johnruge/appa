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
  kind: string // apply_field | apply_ready | review | answer_needed
  prompt: string
  context: {
    options?: any[]
    link?: string
    label?: string
    kind?: string
    reason?: string
    attempt_id?: number
    preview_upload_id?: number | null
    apply_url?: string
    required_unfilled?: boolean
    failed?: { label: string; error: string }[]
    plan?: { label: string; value: string | string[]; source: string }[]
    card?: { company: string; title: string; location: string }
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

export type Application = {
  id: number
  company: string
  role: string
  status: string
  link: string | null
  location: string | null
  date_applied: string | null
  notes: string | null
  source: string | null
  job_id: string | null
  created_at: string
  updated_at: string
  screening?: Screening | null
  prefill?: { id: number; status: string; error: string | null } | null
}

export type Screening = {
  verdict: string
  reasons: string[]
  source: string
  sponsorship: string | null
  sponsorship_quote: string | null
  min_years_experience: number | null
  experience_quote: string | null
  is_entry_level: boolean | null
  seniority: string | null
  citizenship_required: boolean | null
  clearance_required: boolean | null
}

export type Overview = {
  matches: Record<string, number>
  new_matches_24h: number
  applied_total: number
  applied_7d: number
  by_status: Record<string, number>
  inbox: Record<string, number>
  boards: number
  last_sweep: {
    id: number
    status: string
    started_at: string
    finished_at: string | null
    cost_usd: number
    result: Record<string, any> | null
  } | null
  next_sweep_at: string | null
  questions: Record<string, number>
  spent_24h_usd: number
}

export type Listing = {
  id: number
  company: string
  title: string
  location: string
  url: string
  sources: string[]
  posted_at: string | null
  status: string
  reason: string | null
  application_id: number | null
  first_seen_at: string
}

export type Source = { name: string; description: string; default: boolean; terms: string; ready: boolean; needs: string[] }

export type Feeds = {
  lists: Source[]
  board_ats: string[]
  boards: Record<string, number>
  inbox: Record<string, number>
  load_errors: string[]
}
