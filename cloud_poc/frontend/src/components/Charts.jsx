/* Inline SVG charts — no chart library. */

const STATUS_COLORS = {
  completed: '#10b981',
  running:   '#3b82f6',
  queued:    '#f59e0b',
  failed:    '#ef4444',
  cancelled: '#94a3b8',
}

export function StatusDonut({ summary }) {
  const slices = [
    ['completed', summary.completed_tasks],
    ['running',   summary.running_tasks],
    ['queued',    summary.queued_tasks],
    ['failed',    summary.failed_tasks],
    ['cancelled', summary.cancelled_tasks],
  ].filter(([, n]) => n > 0)

  const total = slices.reduce((acc, [, n]) => acc + n, 0)

  if (total === 0) {
    return <div className="empty-chart">No tasks yet — submit one to see status breakdown.</div>
  }

  const radius = 64
  const strokeW = 22
  const circumference = 2 * Math.PI * radius
  let offset = 0

  return (
    <div className="donut-container">
      <svg className="donut-svg" viewBox="0 0 160 160">
        <circle cx="80" cy="80" r={radius} fill="none" stroke="#eef0f5" strokeWidth={strokeW} />
        {slices.map(([key, n]) => {
          const fraction = n / total
          const dash = fraction * circumference
          const seg = (
            <circle
              key={key}
              cx="80" cy="80" r={radius}
              fill="none"
              stroke={STATUS_COLORS[key]}
              strokeWidth={strokeW}
              strokeDasharray={`${dash} ${circumference - dash}`}
              strokeDashoffset={-offset}
              transform="rotate(-90 80 80)"
            />
          )
          offset += dash
          return seg
        })}
        <text x="80" y="76" textAnchor="middle" fontSize="22" fontWeight="600" fill="#1a2030">{total}</text>
        <text x="80" y="94" textAnchor="middle" fontSize="11" fill="#6b7280">tasks</text>
      </svg>

      <div className="donut-legend">
        {slices.map(([key, n]) => (
          <div className="legend-row" key={key}>
            <span className="legend-swatch" style={{ background: STATUS_COLORS[key] }} />
            <span style={{ textTransform: 'capitalize' }}>{key}</span>
            <span className="count">{n}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

export function TasksTimeline({ tasks }) {
  /* Group recent tasks into 1-minute buckets for the last 30 minutes. */
  const now = Date.now()
  const window = 30 * 60 * 1000
  const buckets = Array.from({ length: 30 }, () => 0)

  for (const t of tasks) {
    if (!t.created_at) continue
    const ts = new Date(t.created_at).getTime()
    const age = now - ts
    if (age < 0 || age > window) continue
    const idx = 29 - Math.floor(age / (60 * 1000))
    if (idx >= 0 && idx < 30) buckets[idx] += 1
  }

  const max = Math.max(1, ...buckets)
  const w = 600, h = 160, pad = 12
  const innerW = w - pad * 2, innerH = h - pad * 2
  const barW = innerW / buckets.length

  if (tasks.length === 0) {
    return <div className="empty-chart">No tasks in the last 30 minutes.</div>
  }

  return (
    <svg className="chart-svg" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
      {/* gridlines */}
      {[0.25, 0.5, 0.75, 1].map((p) => (
        <line key={p}
          x1={pad} x2={w - pad}
          y1={pad + innerH * (1 - p)} y2={pad + innerH * (1 - p)}
          stroke="#eef0f5" strokeWidth="1" />
      ))}

      {buckets.map((n, i) => {
        const barH = (n / max) * innerH
        return (
          <rect
            key={i}
            x={pad + i * barW + 1}
            y={pad + innerH - barH}
            width={barW - 2}
            height={barH}
            fill="#4f46e5"
            opacity={n === 0 ? 0 : 0.85}
            rx="2"
          />
        )
      })}

      <text x={pad} y={h - 1} fontSize="9" fill="#6b7280">-30m</text>
      <text x={w - pad} y={h - 1} fontSize="9" fill="#6b7280" textAnchor="end">now</text>
      <text x={pad} y={pad + 8} fontSize="9" fill="#6b7280">peak {max}</text>
    </svg>
  )
}
