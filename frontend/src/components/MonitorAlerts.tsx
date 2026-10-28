import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type MonitorAlert } from '../api'

/** In-app alert channel: a banner while any monitor the user can see (with in-app alerts on) is down. */
export function MonitorAlerts() {
  const [down, setDown] = useState<MonitorAlert[]>([])
  useEffect(() => {
    let live = true
    const load = () => api.get<MonitorAlert[]>('/monitoring/alerts').then((r) => live && setDown(r)).catch(() => {})
    load()
    const t = window.setInterval(load, 60000)
    return () => { live = false; clearInterval(t) }
  }, [])
  if (!down.length) return null
  return (
    <div className="alert err" role="alert">
      <b>API down:</b>{' '}
      {down.map((m, i) => (
        <span key={m.id}>{i > 0 && ', '}<Link to={`/monitors/${m.id}`}>{m.name}</Link>
          {m.failing_since && <span className="small"> (since {new Date(m.failing_since).toLocaleString('en-IN', { timeStyle: 'short', dateStyle: 'medium' })})</span>}</span>
      ))}
    </div>
  )
}
