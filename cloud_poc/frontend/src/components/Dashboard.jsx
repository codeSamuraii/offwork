import { useEffect, useState } from 'react'
import { api } from '../api'
import { StatusDonut, TasksTimeline } from './Charts'

function formatBytes(n) {
  if (!n) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let i = 0
  let v = n
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1 }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`
}

function timeAgo(iso) {
  if (!iso) return '—'
  const diff = Date.now() - new Date(iso).getTime()
  const s = Math.floor(diff / 1000)
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return new Date(iso).toLocaleDateString()
}

function StatusPill({ status }) {
  const cls = `status-pill status-${status || 'queued'}`
  return <span className={cls}>{status || 'queued'}</span>
}

export default function Dashboard({ session, onLogout }) {
  const [summary, setSummary] = useState(null)
  const [tasks, setTasks] = useState([])
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    let active = true
    const load = async () => {
      try {
        const [s, t] = await Promise.all([
          api.summary(session.api_key),
          api.tasks(session.api_key, 25),
        ])
        if (active) {
          setSummary(s)
          setTasks(t)
          setError('')
        }
      } catch (err) {
        if (active) setError(err.message)
      }
    }
    load()
    const id = window.setInterval(load, 4000)
    return () => { active = false; window.clearInterval(id) }
  }, [session.api_key])

  const copyBroker = async () => {
    try {
      await navigator.clipboard.writeText(session.broker_url)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch { /* ignore */ }
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden />
          pyfuse cloud
        </div>
        <div className="user-info">
          <span>{session.email}</span>
          <button className="ghost" onClick={onLogout}>Sign out</button>
        </div>
      </header>

      <div className="dashboard">
        {error && <div className="error-banner">{error}</div>}

        <section>
          <div className="cards">
            <div className="card"><span className="card-label">Total tasks</span>
              <span className="card-value">{summary?.total_tasks ?? '—'}</span></div>
            <div className="card accent-success"><span className="card-label">Completed</span>
              <span className="card-value">{summary?.completed_tasks ?? '—'}</span></div>
            <div className="card accent-info"><span className="card-label">Running</span>
              <span className="card-value">{summary?.running_tasks ?? '—'}</span></div>
            <div className="card accent-warn"><span className="card-label">Queued</span>
              <span className="card-value">{summary?.queued_tasks ?? '—'}</span></div>
            <div className="card accent-error"><span className="card-label">Failed</span>
              <span className="card-value">{summary?.failed_tasks ?? '—'}</span></div>
            <div className="card"><span className="card-label">Data transferred</span>
              <span className="card-value">
                {summary ? formatBytes((summary.total_task_bytes || 0) + (summary.total_result_bytes || 0)) : '—'}
              </span></div>
          </div>
        </section>

        <section className="panel">
          <div className="panel-header">
            <h2>Broker URL</h2>
            <span className="hint">Use with <code>pyfuse.connect(...)</code></span>
          </div>
          <div className="broker-url">
            <input type="text" readOnly value={session.broker_url} />
            <button className="ghost" onClick={copyBroker}>{copied ? 'Copied' : 'Copy'}</button>
          </div>
        </section>

        <section className="chart-row">
          <div className="panel">
            <div className="panel-header">
              <h2>Status breakdown</h2>
              <span className="hint">All time</span>
            </div>
            {summary && <StatusDonut summary={summary} />}
          </div>
          <div className="panel">
            <div className="panel-header">
              <h2>Submission rate</h2>
              <span className="hint">Last 30 minutes</span>
            </div>
            <TasksTimeline tasks={tasks} />
          </div>
        </section>

        <section className="panel">
          <div className="panel-header">
            <h2>Recent tasks</h2>
            <span className="hint">{tasks.length} latest</span>
          </div>
          <table className="tasks-table">
            <thead>
              <tr>
                <th>Task</th>
                <th>Function</th>
                <th>Status</th>
                <th>Size</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {tasks.length === 0 ? (
                <tr><td colSpan={5} className="empty-row">No tasks yet.</td></tr>
              ) : tasks.map((t) => (
                <tr key={t.task_id}>
                  <td><code>{t.task_id.slice(0, 8)}</code></td>
                  <td>{t.function_name || '—'}</td>
                  <td><StatusPill status={t.status} /></td>
                  <td className="mono">{formatBytes(t.task_bytes)}</td>
                  <td>{timeAgo(t.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
    </div>
  )
}
