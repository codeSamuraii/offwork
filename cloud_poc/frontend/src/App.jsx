import { useEffect, useMemo, useState } from 'react'

const apiBase = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000'

async function fetchJson(path, options = {}) {
  const response = await fetch(`${apiBase}${path}`, options)
  if (!response.ok) {
    const text = await response.text()
    throw new Error(text || `Request failed with ${response.status}`)
  }
  return response.json()
}

function SummaryCard({ label, value }) {
  return (
    <div className="card">
      <span className="label">{label}</span>
      <strong>{value}</strong>
    </div>
  )
}

export default function App() {
  const [registration, setRegistration] = useState({ email: '', password: '' })
  const [apiKey, setApiKey] = useState('')
  const [brokerUrl, setBrokerUrl] = useState('')
  const [summary, setSummary] = useState(null)
  const [tasks, setTasks] = useState([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const authHeaders = useMemo(() => (apiKey ? { 'X-Pyfuse-API-Key': apiKey } : {}), [apiKey])

  useEffect(() => {
    if (!apiKey) {
      return
    }
    const load = async () => {
      try {
        const [me, usage, recentTasks] = await Promise.all([
          fetchJson('/api/v1/users/me', { headers: authHeaders }),
          fetchJson('/api/v1/usage/summary', { headers: authHeaders }),
          fetchJson('/api/v1/usage/tasks', { headers: authHeaders }),
        ])
        setBrokerUrl(me.broker_url)
        setSummary(usage)
        setTasks(recentTasks)
        setError('')
      } catch (err) {
        setError(err.message)
      }
    }
    load()
    const intervalId = window.setInterval(load, 5000)
    return () => window.clearInterval(intervalId)
  }, [apiKey, authHeaders])

  async function register(event) {
    event.preventDefault()
    setLoading(true)
    try {
      const result = await fetchJson('/api/v1/users/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(registration),
      })
      setApiKey(result.api_key)
      setBrokerUrl(result.broker_url)
      setError('')
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="layout">
      <section>
        <h1>pyfuse cloud POC</h1>
        <p>
          Register a user, copy the returned broker URL into <code>pyfuse.connect(...)</code>,
          then watch tasks and usage from this dashboard.
        </p>
      </section>

      <section className="panel">
        <h2>Register</h2>
        <form onSubmit={register} className="stack">
          <input
            type="email"
            placeholder="Email"
            value={registration.email}
            onChange={(event) => setRegistration({ ...registration, email: event.target.value })}
          />
          <input
            type="password"
            placeholder="Password"
            value={registration.password}
            onChange={(event) => setRegistration({ ...registration, password: event.target.value })}
          />
          <button type="submit" disabled={loading}>{loading ? 'Registering…' : 'Register user'}</button>
        </form>
      </section>

      <section className="panel">
        <h2>Access</h2>
        <div className="stack">
          <input
            type="text"
            placeholder="Paste API key"
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
          />
          <textarea readOnly value={brokerUrl} rows={3} />
        </div>
      </section>

      {error && <section className="panel error">{error}</section>}

      <section className="panel">
        <h2>Usage summary</h2>
        {summary ? (
          <div className="cards">
            <SummaryCard label="Total tasks" value={summary.total_tasks} />
            <SummaryCard label="Queued" value={summary.queued_tasks} />
            <SummaryCard label="Running" value={summary.running_tasks} />
            <SummaryCard label="Completed" value={summary.completed_tasks} />
            <SummaryCard label="Failed" value={summary.failed_tasks} />
            <SummaryCard label="Cancelled" value={summary.cancelled_tasks} />
          </div>
        ) : (
          <p>Enter an API key to load usage.</p>
        )}
      </section>

      <section className="panel">
        <h2>Recent tasks</h2>
        <table>
          <thead>
            <tr>
              <th>Task</th>
              <th>Function</th>
              <th>Status</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {tasks.map((task) => (
              <tr key={task.task_id}>
                <td><code>{task.task_id.slice(0, 8)}</code></td>
                <td>{task.function_name}</td>
                <td>{task.status}</td>
                <td>{task.created_at ? new Date(task.created_at).toLocaleString() : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </main>
  )
}
