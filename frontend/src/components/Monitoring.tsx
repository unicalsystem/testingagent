import { useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { CHANNEL_LABEL, api, type AlertChannel, type ApiSuite, type Monitor, type MonitorStatus, type Project } from '../api'
import { pct } from './ApiTesting'
import { Alert, Modal, fmtDate, useAsync } from './ui'

export const INTERVALS: [number, string][] = [[60, 'Every minute'], [120, 'Every 2 minutes'], [300, 'Every 5 minutes'], [600, 'Every 10 minutes'], [900, 'Every 15 minutes'], [1800, 'Every 30 minutes'], [3600, 'Every hour']]
const STATUS_LABEL: Record<MonitorStatus, string> = { up: 'Up', down: 'Down', unknown: 'Pending', paused: 'Paused' }
const STATUS_CLASS: Record<MonitorStatus, string> = { up: 'passed', down: 'failed', unknown: 'queued', paused: 'cancelled' }
export function MonitorBadge({ s }: { s: MonitorStatus }) { return <span className={`badge ${STATUS_CLASS[s]}`}>{STATUS_LABEL[s]}</span> }
export function dur(seconds: number) {
  const s = Math.max(0, Math.round(seconds)), d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60)
  return d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : m ? `${m} min ${s % 60} s` : `${s} s`
}

/** Response-time bars for the last 24 h; red where checks failed. */
export function ResponseChart({ series }: { series: { t: string; ok: number; failed: number; avg_ms: number }[] }) {
  if (!series.length) return <div className="small muted">No checks yet.</div>
  const W = 720, H = 120, max = Math.max(50, ...series.map((x) => x.avg_ms)), bw = W / series.length
  return (
    <svg viewBox={`0 0 ${W} ${H + 18}`} width="100%" role="img" aria-label="Response time over the last 24 hours" style={{ display: 'block' }}>
      {series.map((x, i) => {
        const h = x.failed && !x.ok ? H : Math.max(2, (x.avg_ms / max) * H)
        return <rect key={i} x={i * bw + 1} y={H - h} width={Math.max(1, bw - 2)} height={h} rx={1.5} fill={x.failed ? (x.ok ? '#d99a1e' : '#d64535') : '#2f9e62'}>
          <title>{`${new Date(x.t).toLocaleString('en-IN', { timeStyle: 'short', dateStyle: 'short' })} — ${x.ok} ok, ${x.failed} failed, avg ${x.avg_ms} ms`}</title></rect>
      })}
      <text x={0} y={H + 14} fontSize={10} fill="#6b7a8f">{new Date(series[0].t).toLocaleTimeString('en-IN', { timeStyle: 'short' })}</text>
      <text x={W} y={H + 14} fontSize={10} fill="#6b7a8f" textAnchor="end">now · max {max} ms</text>
    </svg>
  )
}

export interface MonitorFormValue {
  name: string; url: string; method: 'GET' | 'HEAD'; suite_id: number | null; use_suite_auth: boolean; expected_status: string; body_contains: string
  max_response_ms: number; timeout_ms: number; interval_seconds: number; failure_threshold: number; enabled: boolean
  alert_channels: AlertChannel[]; alert_emails: string; webhook_kind: 'slack' | 'teams' | 'generic'; webhook_url: string
}
function toForm(m?: Monitor, p?: Project): MonitorFormValue {
  return {
    name: m?.name ?? '', url: m?.url ?? (p ? p.base_url.replace(/\/$/, '') + '/health' : ''), method: m?.method ?? 'GET', suite_id: m?.suite_id ?? null,
    use_suite_auth: m?.use_suite_auth ?? false, expected_status: (m?.expected_status ?? ['2xx']).join(', '), body_contains: m?.body_contains ?? '',
    max_response_ms: m?.max_response_ms ?? 0, timeout_ms: m?.timeout_ms ?? 10000, interval_seconds: m?.interval_seconds ?? 300,
    failure_threshold: m?.failure_threshold ?? 2, enabled: m?.enabled ?? true, alert_channels: m?.alert_channels ?? ['inapp', 'email'],
    alert_emails: (m?.alert_emails ?? []).join(', '), webhook_kind: m?.webhook_kind ?? 'slack', webhook_url: '',
  }
}

export function MonitorForm({ pid, initial, onClose, onSaved }: { pid: number; initial?: Monitor; onClose: () => void; onSaved: (m: Monitor) => void }) {
  const proj = useAsync(() => api.get<Project>(`/projects/${pid}`), [pid])
  const suites = useAsync(() => api.get<ApiSuite[]>(`/projects/${pid}/api-suites`), [pid])
  const [f, setF] = useState<MonitorFormValue | null>(initial ? toForm(initial) : null)
  const [removeHook, setRemoveHook] = useState(false)
  const [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  if (!f) { if (proj.data) setF(toForm(undefined, proj.data)); return null }
  const set = (patch: Partial<MonitorFormValue>) => setF({ ...f, ...patch })
  const chan = (c: AlertChannel, on: boolean) => set({ alert_channels: on ? [...new Set([...f.alert_channels, c])] : f.alert_channels.filter((x) => x !== c) })
  const authSuites = (suites.data ?? []).filter((s) => s.auth_type !== 'none')
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr(''); setBusy(true)
    const body = {
      ...f, expected_status: f.expected_status.split(/[,\s]+/).filter(Boolean).map((x) => /^\d+$/.test(x) ? +x : x),
      alert_emails: f.alert_emails.split(/[,;\s]+/).filter(Boolean),
      webhook_url: removeHook ? '' : f.webhook_url.trim() ? f.webhook_url.trim() : initial ? null : '',
      suite_id: f.use_suite_auth ? f.suite_id : f.suite_id || null,
    }
    try { onSaved(initial ? await api.put<Monitor>(`/monitors/${initial.id}`, body) : await api.post<Monitor>(`/projects/${pid}/monitors`, body)) }
    catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  return (
    <Modal title={initial ? 'Edit monitor' : 'New uptime monitor'} onClose={onClose}>
      <form onSubmit={submit}>
        <Alert>{err}</Alert>
        <label className="f" htmlFor="mn">Name</label>
        <input className="i" id="mn" value={f.name} onChange={(e) => set({ name: e.target.value })} required maxLength={120} placeholder="e.g. Orders API health" />
        <div className="f2">
          <div style={{ gridColumn: '1 / -1' }}><label className="f" htmlFor="mu">Endpoint URL</label>
            <input className="i mono" id="mu" value={f.url} onChange={(e) => set({ url: e.target.value })} required placeholder="https://api.example.com/health" /></div>
          <div><label className="f" htmlFor="mm">Method</label><select className="i" id="mm" value={f.method} onChange={(e) => set({ method: e.target.value as 'GET' | 'HEAD' })}><option>GET</option><option>HEAD</option></select></div>
          <div><label className="f" htmlFor="mi">Check</label><select className="i" id="mi" value={f.interval_seconds} onChange={(e) => set({ interval_seconds: +e.target.value })}>{INTERVALS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></div>
          <div><label className="f" htmlFor="me">Expected status</label><input className="i mono" id="me" value={f.expected_status} onChange={(e) => set({ expected_status: e.target.value })} placeholder="2xx or 200, 204" /></div>
          <div><label className="f" htmlFor="mt">Down after</label><select className="i" id="mt" value={f.failure_threshold} onChange={(e) => set({ failure_threshold: +e.target.value })}>
            {[1, 2, 3, 5].map((n) => <option key={n} value={n}>{n} failed check{n > 1 ? 's' : ''} in a row</option>)}</select></div>
          <div><label className="f" htmlFor="ml">Slow if over (ms, 0 = off)</label><input className="i" id="ml" type="number" min={0} max={120000} value={f.max_response_ms} onChange={(e) => set({ max_response_ms: +e.target.value })} /></div>
          <div><label className="f" htmlFor="mto">Timeout (ms)</label><input className="i" id="mto" type="number" min={1000} max={60000} value={f.timeout_ms} onChange={(e) => set({ timeout_ms: +e.target.value })} /></div>
        </div>
        <label className="f" htmlFor="mb">Response must contain (optional)</label>
        <input className="i" id="mb" value={f.body_contains} onChange={(e) => set({ body_contains: e.target.value })} maxLength={500} placeholder='e.g. "status":"ok"' />
        {authSuites.length > 0 && (
          <label className="check"><input type="checkbox" checked={f.use_suite_auth} onChange={(e) => set({ use_suite_auth: e.target.checked, suite_id: e.target.checked ? (f.suite_id ?? authSuites[0].id) : f.suite_id })} />
            <span>Authenticated endpoint — use the credentials of
              <select className="i" style={{ display: 'inline-block', width: 'auto', marginLeft: 6 }} value={f.suite_id ?? ''} disabled={!f.use_suite_auth} onChange={(e) => set({ suite_id: +e.target.value })} aria-label="API suite">
                {authSuites.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select>
              <div className="small muted">Credentials are only sent to that suite's own host.</div></span></label>
        )}

        <h3 style={{ marginTop: 18 }}>Alerts when the API goes down and when it recovers</h3>
        {(Object.keys(CHANNEL_LABEL) as AlertChannel[]).map((c) => (
          <label key={c} className="check"><input type="checkbox" checked={f.alert_channels.includes(c)} onChange={(e) => chan(c, e.target.checked)} /><span><b>{CHANNEL_LABEL[c]}</b>
            {c === 'inapp' && <div className="small muted">Shows a banner to everyone with access to this project while it is down.</div>}</span></label>
        ))}
        {f.alert_channels.includes('email') && <>
          <label className="f" htmlFor="ae">E-mail addresses</label>
          <input className="i" id="ae" value={f.alert_emails} onChange={(e) => set({ alert_emails: e.target.value })} placeholder="oncall@example.com, qa@example.com" />
          <div className="hint">Sent through the platform's SMTP settings.</div>
        </>}
        {f.alert_channels.includes('webhook') && <>
          <div className="f2">
            <div><label className="f" htmlFor="wk">Webhook type</label><select className="i" id="wk" value={f.webhook_kind} onChange={(e) => set({ webhook_kind: e.target.value as MonitorFormValue['webhook_kind'] })}>
              <option value="slack">Slack incoming webhook</option><option value="teams">Microsoft Teams (Workflows webhook)</option><option value="generic">Generic JSON (POST)</option></select></div>
          </div>
          <label className="f" htmlFor="wu">Webhook URL</label>
          <input className="i mono" id="wu" type="password" autoComplete="off" value={f.webhook_url} onChange={(e) => { set({ webhook_url: e.target.value }); setRemoveHook(false) }}
            placeholder={initial?.has_webhook ? `Stored (${initial.webhook_hint}) — leave empty to keep` : 'https://hooks.slack.com/services/…'} />
          {initial?.has_webhook && <label className="check small"><input type="checkbox" checked={removeHook} onChange={(e) => setRemoveHook(e.target.checked)} /> Remove the stored webhook</label>}
          <div className="hint">HTTPS only. The URL is stored encrypted.</div>
        </>}
        {f.alert_channels.length === 0 && <Alert kind="warn">No alert channel selected: downtime is still recorded and reported, but nobody is notified.</Alert>}
        <label className="check"><input type="checkbox" checked={f.enabled} onChange={(e) => set({ enabled: e.target.checked })} /> Monitoring active</label>
        <div className="row" style={{ justifyContent: 'flex-end', marginTop: 14 }}>
          <button type="button" className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={busy}>{busy ? 'Saving…' : initial ? 'Save' : 'Start monitoring'}</button>
        </div>
      </form>
    </Modal>
  )
}

export function MonitorsTab({ p, has }: { p: Project; has: (perm: string) => boolean }) {
  const mons = useAsync(() => api.get<Monitor[]>(`/projects/${p.id}/monitors`), [p.id])
  const [creating, setCreating] = useState(false)
  return (
    <>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 12 }}>
        <span className="muted small">Optional uptime monitoring: TestAgent calls the endpoint on a schedule, records downtime and alerts you by e-mail, Slack / Teams webhook and/or in the app.</span>
        {has('project.update') && <button className="btn primary" onClick={() => setCreating(true)}>+ New monitor</button>}
      </div>
      <Alert>{mons.error}</Alert>
      {mons.data?.length === 0 && <div className="panel empty"><p><b>No monitors.</b></p><p className="small">Monitoring is off until you add one.</p>
        {has('project.update') && <button className="btn primary" onClick={() => setCreating(true)}>+ New monitor</button>}</div>}
      {!!mons.data?.length && (
        <div className="panel" style={{ padding: 0 }}>
          <table className="t resp"><thead><tr><th style={{ paddingLeft: 16 }}>Monitor</th><th>Status</th><th>Uptime 24 h</th><th>Last check</th><th>Alerts</th></tr></thead>
            <tbody>{mons.data.map((m) => (
              <tr key={m.id}>
                <td style={{ paddingLeft: 16 }}><Link to={`/monitors/${m.id}`}><b>{m.name}</b></Link><div className="mono small muted" style={{ wordBreak: 'break-all' }}>{m.method} {m.url}</div>
                  {m.policy_problems.length > 0 && <div className="small" style={{ color: 'var(--warn)' }}>Host not authorised</div>}</td>
                <td data-label="Status"><MonitorBadge s={m.status} />{m.status === 'down' && m.failing_since && <div className="small muted">for {dur((Date.now() - new Date(m.failing_since).getTime()) / 1000)}</div>}</td>
                <td data-label="Uptime 24h">{pct(m.uptime_24h)}</td>
                <td data-label="Last check" className="small muted">{fmtDate(m.last_check_at)}</td>
                <td data-label="Alerts" className="small">{m.alert_channels.map((c) => <span key={c} className="tag">{c}</span>)}</td>
              </tr>
            ))}</tbody></table>
        </div>
      )}
      {creating && <MonitorForm pid={p.id} onClose={() => setCreating(false)} onSaved={() => { setCreating(false); mons.reload() }} />}
    </>
  )
}
