// Small, pure helpers for display. Unit-tested in format.test.ts.

const MIN = 60_000
const HOUR = 60 * MIN
const DAY = 24 * HOUR

/** "just now", "5m ago", "3h ago", "2d ago", then a short date. */
export function timeAgo(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return ''
  const diff = now - new Date(iso).getTime()
  if (Number.isNaN(diff)) return ''
  if (diff < MIN) return 'just now'
  if (diff < HOUR) return `${Math.floor(diff / MIN)}m ago`
  if (diff < DAY) return `${Math.floor(diff / HOUR)}h ago`
  if (diff < 7 * DAY) return `${Math.floor(diff / DAY)}d ago`
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

/** "in 12m", "in 3h", "in 2d"; "now" once it's due. */
export function timeUntil(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return ''
  const diff = new Date(iso).getTime() - now
  if (Number.isNaN(diff)) return ''
  if (diff <= MIN) return 'now'
  if (diff < HOUR) return `in ${Math.round(diff / MIN)}m`
  if (diff < DAY) return `in ${Math.round(diff / HOUR)}h`
  return `in ${Math.round(diff / DAY)}d`
}

/** The screening reasons stored in an application's notes, one per item. */
export function reasons(notes: string | null | undefined): string[] {
  return (notes ?? '')
    .split(/;\s+/)
    .map((r) => r.trim())
    .filter(Boolean)
}

/** A reason split into its claim and the quoted passage, when it has one. */
export function splitQuote(reason: string): { text: string; quote: string | null } {
  const m = reason.match(/^(.*?):\s*["'“](.+?)["'”]?\s*$/s)
  return m ? { text: m[1].trim(), quote: m[2].trim() } : { text: reason, quote: null }
}

/** The host of a link, without www. */
export function host(url: string | null | undefined): string {
  if (!url) return ''
  try {
    return new URL(url).hostname.replace(/^www\./, '')
  } catch {
    return ''
  }
}

/** Up to two initials for a company avatar. */
export function initials(name: string): string {
  const words = name.replace(/[^\p{L}\p{N} ]/gu, ' ').trim().split(/\s+/).filter(Boolean)
  if (!words.length) return '?'
  return (words.length === 1 ? words[0].slice(0, 2) : words[0][0] + words[1][0]).toUpperCase()
}

/** A stable hue (0-359) for a name, so each company keeps its avatar color. */
export function hue(name: string): number {
  let h = 0
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) % 360
  return h
}

/** "simplify_newgrad" or "feed:board:workday" -> a readable source name. */
export function sourceName(source: string | null | undefined): string {
  if (!source) return ''
  const last = source.split(':').filter((p) => !['pipeline', 'feed', 'board', 'cli', 'listing'].includes(p)).pop() ?? source
  return last.replace(/_/g, ' ').replace(/\b(newgrad|new grad)\b/i, 'new grad').replace(/^\w/, (c) => c.toUpperCase())
}

/** Dollars with sensible precision for tiny model costs. */
export function usd(n: number | null | undefined): string {
  if (!n) return '$0'
  return n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`
}

/** "1 match" / "3 matches". */
export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`
}

/** Comma-separated text to a clean list (for filter fields). */
export function toList(text: string): string[] {
  return text
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
}
