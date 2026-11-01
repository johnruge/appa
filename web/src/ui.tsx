// Shared building blocks: icons, buttons, badges, avatar, drawer, toasts, data hooks.

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { ToastCtx, type Toast } from './hooks'
import { hue, initials } from './format'

// ---- icons (inline SVG, 16px, stroke) ----------------------------------------------------------

const paths: Record<string, string> = {
  spark: 'M8 1.5v3M8 11.5v3M1.5 8h3M11.5 8h3M3.4 3.4l2.1 2.1M10.5 10.5l2.1 2.1M3.4 12.6l2.1-2.1M10.5 5.5l2.1-2.1',
  inbox: 'M2 9.5h3.2l1 1.8h3.6l1-1.8H14M2 9.5l1.6-6h8.8l1.6 6v3.2a.8.8 0 0 1-.8.8H2.8a.8.8 0 0 1-.8-.8z',
  check: 'M3 8.5l3.2 3L13 4.5',
  briefcase: 'M2 5.5h12v7.5a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1zM5.5 5.5V3.5a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v2M2 9h12',
  compass: 'M8 14.5A6.5 6.5 0 1 0 8 1.5a6.5 6.5 0 0 0 0 13zM10.6 5.4L9.2 9.2 5.4 10.6 6.8 6.8z',
  plug: 'M6 1.5v3.5M10 1.5v3.5M4 5h8v2.5a4 4 0 0 1-8 0zM8 11.5v3',
  activity: 'M1.5 8h3l2-5 3 10 2-5h3',
  settings: 'M8 10.2a2.2 2.2 0 1 0 0-4.4 2.2 2.2 0 0 0 0 4.4zM13 8a5 5 0 0 0-.1-1l1.4-1.1-1.4-2.4-1.7.6a5 5 0 0 0-1.7-1L9.2 1.5H6.8l-.3 1.6a5 5 0 0 0-1.7 1l-1.7-.6-1.4 2.4L3.1 7a5 5 0 0 0 0 2l-1.4 1.1 1.4 2.4 1.7-.6a5 5 0 0 0 1.7 1l.3 1.6h2.4l.3-1.6a5 5 0 0 0 1.7-1l1.7.6 1.4-2.4L12.9 9a5 5 0 0 0 .1-1z',
  external: 'M9.5 2.5h4v4M13.5 2.5L7.5 8.5M12 9.5v3.2a.8.8 0 0 1-.8.8H3.3a.8.8 0 0 1-.8-.8V4.8a.8.8 0 0 1 .8-.8h3.2',
  x: 'M4 4l8 8M12 4l-8 8',
  search: 'M7 12.5a5.5 5.5 0 1 0 0-11 5.5 5.5 0 0 0 0 11zM14.5 14.5l-3.6-3.6',
  play: 'M5 3l8 5-8 5z',
  pause: 'M5.5 3v10M10.5 3v10',
  wand: 'M2.5 13.5l7-7M11 2v2M14 5h-2M13.2 2.8l-1.4 1.4M9.5 4.5l2 2',
  alert: 'M8 5.5v3.2M8 11h.01M7.1 2.3L1.8 11.6a1 1 0 0 0 .9 1.4h10.6a1 1 0 0 0 .9-1.4L8.9 2.3a1 1 0 0 0-1.8 0z',
  minus: 'M3.5 8h9',
  link: 'M6.8 9.2a3 3 0 0 0 4.2 0l2-2a3 3 0 0 0-4.2-4.2l-.6.6M9.2 6.8a3 3 0 0 0-4.2 0l-2 2a3 3 0 0 0 4.2 4.2l.6-.6',
  user: 'M8 8a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM2.5 14.5a5.5 5.5 0 0 1 11 0',
  refresh: 'M13.5 3v3.5H10M2.5 13v-3.5H6M3.3 6.2a5 5 0 0 1 8.9-1.4l1.3 1.7M12.7 9.8a5 5 0 0 1-8.9 1.4L2.5 9.5',
}

export function Icon({ name, size = 16, className }: { name: keyof typeof paths | string; size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"
      strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden="true">
      <path d={paths[name] ?? ''} />
    </svg>
  )
}

// ---- small pieces ------------------------------------------------------------------------------

export function Avatar({ name, size = 34 }: { name: string; size?: number }) {
  const h = hue(name)
  return (
    <div className="avatar" style={{ width: size, height: size, background: `hsl(${h} 45% 92% / 0.9)`, color: `hsl(${h} 45% 32%)` }}>
      {initials(name)}
    </div>
  )
}

const STATUS_TONE: Record<string, string> = {
  'To Apply': 'green',
  'Needs Review': 'amber',
  'Apply Myself': 'violet',
  Applied: 'blue',
  Interviewing: 'blue',
  Offer: 'green',
  Rejected: 'red',
  Ghosted: '',
  Withdrawn: '',
  Skipped: '',
}

const STATUS_LABEL: Record<string, string> = {
  'To Apply': 'Good match',
  'Needs Review': 'Check first',
  'Apply Myself': 'Apply directly',
}

export function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`badge ${STATUS_TONE[status] ?? ''}`}>
      <span className="status-dot" />
      {STATUS_LABEL[status] ?? status}
    </span>
  )
}

export function Spinner() {
  return <span className="spinner" aria-label="loading" />
}

export function Empty({ icon, title, children }: { icon: string; title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <div className="icon"><Icon name={icon} size={18} /></div>
      <h3>{title}</h3>
      {children && <div>{children}</div>}
    </div>
  )
}

export function Kbd({ children }: { children: ReactNode }) {
  return <span className="kbd">{children}</span>
}

export function ExternalLink({ href, children, className = 'btn' }: { href: string; children: ReactNode; className?: string }) {
  return (
    <a className={className} href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  )
}

// ---- drawer ------------------------------------------------------------------------------------

export function Drawer({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true">{children}</aside>
    </>
  )
}

// ---- toasts ------------------------------------------------------------------------------------

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const next = useRef(1)
  const push = useCallback((text: string, opts: { error?: boolean; undo?: () => void } = {}) => {
    const id = next.current++
    setToasts((t) => [...t.slice(-2), { id, text, ...opts }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), opts.undo ? 6000 : 3500)
  }, [])
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.error ? 'error' : ''}`}>
            <span>{t.text}</span>
            {t.undo && (
              <button onClick={() => { t.undo?.(); setToasts((all) => all.filter((x) => x.id !== t.id)) }}>Undo</button>
            )}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  )
}
