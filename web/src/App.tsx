import { useEffect, useState } from 'react'
import { api } from './api'
import Console from './Console'
import Login from './Login'
import type { Me } from './types'

export default function App() {
  const [me, setMe] = useState<Me | null>(null)
  const [checking, setChecking] = useState(true)

  useEffect(() => {
    api.me().then(setMe).catch(() => setMe(null)).finally(() => setChecking(false))
  }, [])

  if (checking) return <div className="boot">appa</div>
  if (!me) return <Login onLogin={setMe} />
  return <Console me={me} onLogout={() => setMe(null)} />
}
