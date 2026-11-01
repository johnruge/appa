// Hooks and context shared by the pages (kept apart from components so fast refresh works).

import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'

export type Toast = { id: number; text: string; error?: boolean; undo?: () => void }
export const ToastCtx = createContext<(text: string, opts?: { error?: boolean; undo?: () => void }) => void>(() => {})

/** Show a short message, optionally with Undo. */
export const useToast = () => useContext(ToastCtx)

/** Load data, reload on demand or on an interval; keeps the last good value while reloading. */
export function useData<T>(load: () => Promise<T>, deps: unknown[] = [], every?: number) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const loadRef = useRef(load)
  useEffect(() => {
    loadRef.current = load // always call the latest loader (it closes over the caller's current filters)
  })
  const reload = useCallback(async () => {
    try {
      const v = await loadRef.current()
      setData(v)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])
  const key = JSON.stringify(deps) // reload whenever what the caller depends on changes
  useEffect(() => {
    // eslint-disable-next-line react/set-state-in-effect -- loading from the API is syncing with an external system
    setLoading(true)
    reload()
    if (!every) return
    const t = setInterval(reload, every)
    return () => clearInterval(t)
  }, [key, every, reload])
  return { data, error, loading, reload, setData }
}
