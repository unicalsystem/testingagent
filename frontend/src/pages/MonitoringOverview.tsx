import { Link } from 'react-router-dom'
import { api, type Monitor } from '../api'
import { pct } from '../components/ApiTesting'
import { MonitorBadge, dur } from '../components/Monitoring'
import { Alert, fmtDate, useAsync } from '../components/ui'

export default function MonitoringOverview() {
  const mons = useAsync(() => api.get<Monitor[]>('/monitoring/overview'), [])
  const list = mons.data ?? []
  const down = list.filter((m) => m.status === 'down').length
  const up = list.filter((m) => m.status === 'up').length
  return (
    <>
      <div className="topbar">
        <div><h1>API monitoring</h1><div className="small muted">Uptime monitors across the projects you can access. Add monitors from a project's “Monitoring” tab.</div></div>
        <button className="btn" onClick={mons.reload}>Refresh</button>
      </div>
      <Alert>{mons.error}</Alert>
      <div className="stats">
        <div className="stat"><div className="k">Monitors</div><div className="v">{list.length}</div></div>
        <div className="stat"><div className="k">Up</div><div className="v" style={{ color: 'var(--ok)' }}>{up}</div></div>
        <div className="stat"><div className="k">Down</div><div className="v" style={{ color: down ? 'var(--bad)' : undefined }}>{down}</div></div>
        <div className="stat"><div className="k">Paused / pending</div><div className="v">{list.length - up - down}</div></div>
      </div>
      {mons.data && !list.length && <div className="panel empty">No monitors yet. Open a project and use the <b>Monitoring</b> tab to add one.</div>}
      {!!list.length && (
        <div className="panel" style={{ padding: 0 }}>
          <table className="t resp"><thead><tr><th style={{ paddingLeft: 16 }}>Monitor</th><th>Project</th><th>Status</th><th>24 h</th><th>30 days</th><th>Last check</th></tr></thead>
            <tbody>{[...list].sort((a, b) => (a.status === 'down' ? 0 : 1) - (b.status === 'down' ? 0 : 1)).map((m) => (
              <tr key={m.id}>
                <td style={{ paddingLeft: 16 }}><Link to={`/monitors/${m.id}`}><b>{m.name}</b></Link><div className="mono small muted" style={{ wordBreak: 'break-all' }}>{m.url}</div></td>
                <td data-label="Project" className="small"><Link to={`/projects/${m.project_id}?tab=monitors`}>{m.project_name}</Link></td>
                <td data-label="Status"><MonitorBadge s={m.status} />{m.status === 'down' && m.failing_since && <div className="small muted">for {dur((Date.now() - new Date(m.failing_since).getTime()) / 1000)}</div>}</td>
                <td data-label="24 h">{pct(m.uptime_24h)}</td>
                <td data-label="30 days">{pct(m.uptime_30d ?? null)}</td>
                <td data-label="Last check" className="small muted">{fmtDate(m.last_check_at)}</td>
              </tr>
            ))}</tbody></table>
        </div>
      )}
    </>
  )
}
