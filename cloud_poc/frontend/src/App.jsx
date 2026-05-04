import { useEffect, useState } from 'react'
import Auth from './components/Auth'
import Dashboard from './components/Dashboard'

const STORAGE_KEY = 'pyfuse-cloud-session'

export default function App() {
  const [session, setSession] = useState(() => {
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY)
      return raw ? JSON.parse(raw) : null
    } catch {
      return null
    }
  })

  useEffect(() => {
    if (session) {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(session))
    } else {
      window.localStorage.removeItem(STORAGE_KEY)
    }
  }, [session])

  if (!session) return <Auth onAuth={setSession} />
  return <Dashboard session={session} onLogout={() => setSession(null)} />
}
