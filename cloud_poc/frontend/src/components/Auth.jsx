import { useState } from 'react'
import { api } from '../api'

export default function Auth({ onAuth }) {
  const [mode, setMode] = useState('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const submit = async (event) => {
    event.preventDefault()
    setLoading(true)
    setError('')
    try {
      const fn = mode === 'login' ? api.login : api.register
      const result = await fn(email, password)
      onAuth(result)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="auth-shell">
      <div className="auth-card">
        <h1>
          <span className="brand-mark" aria-hidden />
          pyfuse cloud
        </h1>
        <p className="subtitle">Distributed Python execution, hosted.</p>

        <div className="tabs" role="tablist">
          <button
            type="button"
            className={`tab ${mode === 'login' ? 'active' : ''}`}
            onClick={() => { setMode('login'); setError('') }}
          >
            Sign in
          </button>
          <button
            type="button"
            className={`tab ${mode === 'register' ? 'active' : ''}`}
            onClick={() => { setMode('register'); setError('') }}
          >
            Register
          </button>
        </div>

        {error && <div className="error-banner">{error}</div>}

        <form onSubmit={submit}>
          <div className="field">
            <label htmlFor="email">Email</label>
            <input
              id="email"
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              required
              minLength={8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          <button type="submit" className="primary" disabled={loading}>
            {loading ? 'Please wait…' : (mode === 'login' ? 'Sign in' : 'Create account')}
          </button>
        </form>
      </div>
    </div>
  )
}
