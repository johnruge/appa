// Run events -> terminal lines. Mirrors appa/runtime/render.py so CLI and UI read the same.

import type { RunEvent } from './types'

export type Tone = 'out' | 'cmd' | 'err' | 'ok' | 'dim' | 'warn' | 'accent'
export type Line = { text: string; tone: Tone }

const VERDICT: Record<string, [string, Tone]> = {
  candidate: ['✅ to apply', 'ok'],
  manual: ['⭐ apply myself', 'accent'],
  review: ['🟡 review', 'warn'],
  reject: ['❌ reject', 'dim'],
  duplicate: ['↩ duplicate', 'dim'],
}

function time(ts: string): string {
  return new Date(ts).toLocaleTimeString([], { hour12: false })
}

export function renderEvent(ev: RunEvent): Line[] {
  const p = ev.payload || {}
  const t = time(ev.ts)
  switch (ev.kind) {
    case 'status': {
      const s = String(p.status)
      if (['succeeded', 'failed', 'cancelled'].includes(s)) {
        const lines: Line[] = [{
          text: `${t} ● ${s}  $${Number(p.cost_usd ?? 0).toFixed(4)} · ${p.steps ?? 0} steps · ${p.model_calls ?? 0} model calls`,
          tone: s === 'succeeded' ? 'ok' : s === 'failed' ? 'err' : 'warn',
        }]
        if (p.error) lines.push({ text: `           ${p.error}`, tone: 'err' })
        return lines
      }
      return [{ text: `${t} ● ${s}${p.model ? `  model: ${p.model}` : ''}`, tone: 'dim' }]
    }
    case 'log':
      return [{ text: `${t}   ${p.message}`, tone: 'out' }]
    case 'tool_call': {
      const args = Object.values(p.args ?? {}).join(' ').slice(0, 120)
      const tail = p.ok ? p.result : p.error
      return [{ text: `${t}   ${p.ok ? '→' : '✗'} ${p.tool}(${args})${tail ? '  ' + tail : ''}`, tone: p.ok ? 'dim' : 'err' }]
    }
    case 'model_call':
      if (p.ok === false) return [{ text: `${t}   ✗ model ${p.purpose ?? ''}: ${p.error}`, tone: 'err' }]
      return [{
        text: `${t}   ◇ ${p.purpose ?? ''} [${p.model}] ${p.input_tokens ?? 0}+${p.output_tokens ?? 0} tok $${Number(p.cost_usd ?? 0).toFixed(5)}`,
        tone: 'dim',
      }]
    case 'decision': {
      const [label, tone] = VERDICT[p.verdict] ?? [String(p.verdict), 'out' as Tone]
      const lines: Line[] = [{ text: `${t}   ${label.padEnd(16)} ${p.company} | ${p.title}`, tone }]
      for (const r of (p.reasons ?? []).slice(0, 3)) lines.push({ text: `                              - ${String(r).slice(0, 200)}`, tone: 'dim' })
      return lines
    }
    case 'question':
      return [{ text: `${t}   ? question #${p.question_id}: ${p.prompt}`, tone: 'accent' }]
    case 'result': {
      const counts = p.counts ?? p.waiting_on_you ?? {}
      const parts = Object.entries(counts).map(([k, v]) => `${k} ${v}`)
      return [{ text: `${t} ■ result: ${parts.join(', ') || JSON.stringify(p).slice(0, 200)}`, tone: 'ok' }]
    }
    default:
      return [{ text: `${t}   ${ev.kind}: ${JSON.stringify(p).slice(0, 200)}`, tone: 'dim' }]
  }
}

export const STREAM_KINDS = ['status', 'log', 'tool_call', 'model_call', 'decision', 'question', 'result']
