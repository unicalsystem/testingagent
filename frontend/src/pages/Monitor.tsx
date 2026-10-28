import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { CHANNEL_LABEL, api, apiBase, type Monitor } from '../api'
import { pct } from '../components/ApiTesting'
import { MonitorBadge, MonitorForm, ResponseChart, dur } from '../components/Monitoring'
import { Alert, fmtDate } from '../components/ui'

export default function MonitorPage() {
  const { mid } = useParams()
  const nav = useNavigate()
  const [m, setM] = useState<Monitor | null>(null), [err, setErr] = useState(''), [msg, setMsg] = useState('')
  const [editing, setEditing] = useState(false), [busy, setBusy] = useState(false), [days, setDays] = useState(30)
  const load = () => api.get<Monitor>(`/monitors/${mid}`).then((x) => { setM(x); setErr('') }).catch((e) => setErr(e.message))
  useEffect(() => {
    load()
    const t = window.setInterval(load, 30000)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mid])
  if (err && !m) return <Alert>{err}</Alert>
  if (!m) return null
  const has = (perm: string) => (m.my_permissions ?? []).includes(perm)
  const act = async (fn: () => Promise<void>) => { setBusy(true); setErr(''); setMsg(''); try { await fn() } catch (x) { setErr((x as Error).message) } finally { setBusy(false) } }
  const toggle = () => act(async () => {
    const body = { ...m, alert_emails: m.alert_emails, webhook_url: null, enabled: !m.enabled }
    setM({ ...m, ...(await api.put<Monitor>(`/monitors/${m.id}`, body)) })
  })
  const st = m.stats
  return (
    <>
      <div className="topbar">
        <div><div className="crumbs"><Link to="/monitoring">API monitoring</Link> / <Link to={`/projects/${m.project_id}?tab=monitors`}>Project</Link> /</div>
          <h1>{m.name} <MonitorBadge s={m.status} /></h1>
          <div className="mono small muted" style={{ wordBreak: 'break-all' }}>{m.method} {m.url}</div></div>
        <div className="row">
          {has('run.execute') && m.enabled && <button className="btn" disabled={busy} onClick={() => act(async () => { await api.post(`/monitors/${m.id}/check-now`); setMsg('A check will run within a few seconds.'); window.setTimeout(load, 8000) })}>↻ Check now</button>}
          {has('project.update') && <button className="btn" disabled={busy} onClick={() => act(async () => {
            const r = await api.post<{ results: { channel: string; ok: boolean; error?: string; to?: string }[] }>(`/monitors/${m.id}/test-alert`)
            setMsg(r.results.length ? r.results.map((x) => `${x.channel}${x.to ? ` (${x.to})` : ''}: ${x.ok ? 'sent' : `failed — ${x.error}`}`).join(' · ') : 'No alert channel is configured.')
          })}>Send test alert</button>}
          {has('project.update') && <button className="btn" disabled={busy} onClick={toggle}>{m.enabled ? '⏸ Pause' : '▶ Resume'}</button>}
          {has('project.update') && <button className="btn" onClick={() => setEditing(true)}>Edit</button>}
          <select className="i" style={{ width: 'auto' }} value={days} onChange={(e) => setDays(+e.target.value)} aria-label="Report period">
            {[7, 30, 90].map((d) => <option key={d} value={d}>Last {d} days</option>)}</select>
          <a className="btn primary" href={`${apiBase()}/monitors/${m.id}/report?days=${days}&format=html`}>⬇ Downtime report</a>
        </div>
      </div>
      <Alert>{err}</Alert><Alert kind="ok">{msg}</Alert>
      {m.policy_problems.length > 0 && <Alert kind="warn">Not being checked: {m.policy_problems.join('; ')}. An administrator must authorise the host.</Alert>}
      {m.status === 'down' && <Alert>Down since {fmtDate(m.failing_since)} ({m.failing_since ? dur((Date.now() - new Date(m.failing_since).getTime()) / 1000) : ''}). Last error: {m.last_error}</Alert>}
      {m.status !== 'down' && m.last_error && m.consecutive_failures > 0 && <Alert kind="warn">Last check failed ({m.consecutive_failures} of {m.failure_threshold} before it is marked down): {m.last_error}</Alert>}
      <div className="stats">
        {(['24h', '7d', '30d'] as const).map((k) => <div key={k} className="stat"><div className="k">Uptime {k}</div><div className="v">{pct(st?.[k].uptime_pct ?? null, 3)}</div></div>)}
        <div className="stat"><div className="k">Downtime 30d</div><div className="v" style={{ fontSize: 15 }}>{st ? dur(st['30d'].downtime_seconds) : '—'}</div></div>
        <div className="stat"><div className="k">Incidents 30d</div><div className="v">{st?.['30d'].incidents ?? 0}</div></div>
        <div className="stat"><div className="k">Avg / p95 (24h)</div><div className="v" style={{ fontSize: 15 }}>{st ? `${st['24h'].avg_ms} / ${st['24h'].p95_ms} ms` : '—'}</div></div>
      </div>
      <div className="panel">
        <div className="row" style={{ justifyContent: 'space-between' }}><h2 style={{ margin: 0 }}>Last 24 hours</h2>
          <span className="small muted">Checked {m.interval_seconds >= 3600 ? 'hourly' : `every ${m.interval_seconds / 60} min`} · last {fmtDate(m.last_check_at)}{m.enabled ? ` · next ${fmtDate(m.next_check_at)}` : ' · paused'}</span></div>
        <div style={{ marginTop: 12 }}><ResponseChart series={m.series ?? []} /></div>
      </div>
      <div className="grid2">
        <div className="panel" style={{ padding: 0 }}>
          <h2 style={{ padding: '14px 16px 0' }}>Incidents</h2>
          {!m.incidents?.length && <div className="empty">No downtime recorded.</div>}
          <table className="t resp"><tbody>{m.incidents?.map((i) => (
            <tr key={i.id}><td style={{ paddingLeft: 16 }}><b>{fmtDate(i.started_at)}</b><div className="small muted">{i.ended_at ? `recovered ${fmtDate(i.ended_at)}` : 'ongoing'}</div></td>
              <td data-label="Duration">{dur(i.duration_seconds)}</td>
              <td data-label="Cause" className="small">{i.cause}<div className="muted">{i.alerts.filter((a) => a.event !== 'recovered').map((a) => `${a.channel}${a.ok ? ' ✔' : ' ✘'}`).join(' · ')}</div></td></tr>
          ))}</tbody></table>
        </div>
        <div className="panel" style={{ padding: 0 }}>
          <h2 style={{ padding: '14px 16px 0' }}>Recent checks</h2>
          {!m.recent_checks?.length && <div className="empty">No checks yet.</div>}
          <table className="t resp"><tbody>{m.recent_checks?.map((c, i) => (
            <tr key={i}><td style={{ paddingLeft: 16 }} className="small">{fmtDate(c.checked_at)}</td>
              <td><span className={`pill ${c.ok ? 'ok' : 'bad'}`}>{c.ok ? 'ok' : 'failed'}</span></td>
              <td data-label="HTTP" className="small">{c.http_status ?? '—'} · {c.duration_ms} ms</td>
              <td className="small muted">{c.error}</td></tr>
          ))}</tbody></table>
        </div>
      </div>
      <div className="panel">
        <h2>Alerting</h2>
        <p className="small" style={{ margin: 0 }}>Down after {m.failure_threshold} failed check(s) in a row. Channels: {m.alert_channels.length ? m.alert_channels.map((c) => CHANNEL_LABEL[c]).join(', ') : 'none (recorded only)'}
          {m.alert_emails.length > 0 && <> · e-mail to {m.alert_emails.join(', ')}</>}{m.has_webhook && <> · {m.webhook_kind} webhook ({m.webhook_hint})</>}</p>
      </div>
      {has('project.update') && <div className="row" style={{ justifyContent: 'flex-end' }}>
        <button className="btn danger" onClick={() => act(async () => { if (confirm('Delete this monitor and its history?')) { await api.del(`/monitors/${m.id}`); nav(`/projects/${m.project_id}?tab=monitors`) } })}>Delete monitor</button></div>}
      {editing && <MonitorForm pid={m.project_id} initial={m} onClose={() => setEditing(false)} onSaved={() => { setEditing(false); load() }} />}
    </>
  )
}
