import { useEffect, useState, type FormEvent } from 'react'
import { abs } from '../api'
import { Alert, Modal, fmtDate } from './ui'

interface Settings {
  local_enabled: boolean; sharepoint_enabled: boolean; gdrive_enabled: boolean; default_policy: string; default_retention_days: number
  max_artifact_mb: number; spool_max_hours: number; base_folder: string; local_last_check_at: string | null; local_last_ok_at: string | null; local_last_error: string
}
interface TRow { tenant_id: number; slug: string; name: string; status: string; policy_override: string | null; policy: string; policy_label: string
  mode: string; provider: string; provider_label: string; storage_status: string; last_ok_at: string | null; last_error: string; needs_reselection: boolean; pending_uploads: number }
interface Affected { tenant_id: number; slug: string; name: string; current: string; policy: string }
interface Data {
  settings: Settings; policies: { value: string; label: string }[]; tenants: TRow[]
  local: { total_gb: number; free_gb: number; used_pct: number | null }
  audit: { id: number; at: string; user: string | null; organization: string | null; action: string; target: string }[]
  needs_acknowledgement?: boolean; affected?: Affected[]; message?: string; ok?: boolean
}

const call = <T,>(method: string, url: string, body?: unknown) => fetch(url, {
  method, credentials: 'same-origin', headers: { 'X-Requested-With': 'TestAgent', 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body),
}).then(async (r) => { const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(typeof j.detail === 'string' ? j.detail : `${r.status}`); return j as T })

export default function PlatformStorage() {
  const [d, setD] = useState<Data | null>(null), [f, setF] = useState<Settings | null>(null)
  const [err, setErr] = useState(''), [msg, setMsg] = useState('')
  const [ack, setAck] = useState<{ affected: Affected[]; retry: (ids: number[]) => Promise<void> } | null>(null)
  useEffect(() => { abs.get<Data>('/api/platform/storage').then((x) => { setD(x); setF(x.settings) }).catch((x) => setErr(x.message)) }, [])
  if (!d || !f) return <Alert>{err}</Alert>
  const apply = (x: Data, retry: (ids: number[]) => Promise<void>) => {
    setD(x); setF(x.settings)
    if (x.needs_acknowledgement && x.affected) { setAck({ affected: x.affected, retry }); return }
    setAck(null); if (x.ok === false) setErr(x.message ?? 'Failed'); else setMsg(x.message ?? 'Saved')
  }
  const save = async (e?: FormEvent, ids: number[] = []) => {
    e?.preventDefault(); setErr(''); setMsg('')
    try { apply(await call<Data>('PUT', '/api/platform/storage', { ...pick(f), acknowledge: ids }), (ids2) => save(undefined, ids2)) } catch (x) { setErr((x as Error).message) }
  }
  const setPolicy = async (t: TRow, v: string | null, ids: number[] = []) => {
    setErr(''); setMsg('')
    try { apply(await call<Data>('PUT', `/api/platform/storage/tenants/${t.tenant_id}`, { policy_override: v, acknowledge: ids }), (ids2) => setPolicy(t, v, ids2)) } catch (x) { setErr((x as Error).message) }
  }
  const test = async () => { setErr(''); setMsg(''); try { const x = await call<Data>('POST', '/api/platform/storage/test'); setD(x); setF(x.settings); x.ok ? setMsg(x.message ?? '') : setErr(x.message ?? '') } catch (x) { setErr((x as Error).message) } }
  const b = (k: keyof Settings) => (e: { target: { checked: boolean } }) => setF({ ...f, [k]: e.target.checked })
  const n = (k: keyof Settings) => (e: { target: { value: string } }) => setF({ ...f, [k]: Number(e.target.value) })
  return (
    <>
      <Alert>{err}</Alert><Alert kind="ok">{msg}</Alert>
      <div className="grid2">
        <form className="panel" onSubmit={save}>
          <h2>Storage infrastructure</h2>
          <p className="muted small" style={{ marginTop: -6 }}>Which storage organizations may use. A change never switches an organization's choice: affected organizations are listed for you to acknowledge, and they must choose again before running tests.</p>
          <label className="check small"><input type="checkbox" checked={f.local_enabled} onChange={b('local_enabled')} /><span><b>Unical-managed storage</b> (this server's evidence volume)</span></label>
          <label className="check small"><input type="checkbox" checked={f.sharepoint_enabled} onChange={b('sharepoint_enabled')} /><span><b>Customer SharePoint</b> (customer's own Microsoft Entra app, Sites.Selected)</span></label>
          <label className="check small"><input type="checkbox" checked={f.gdrive_enabled} onChange={b('gdrive_enabled')} /><span><b>Customer Google Drive</b> (customer's own service account, Shared Drives)</span></label>
          <label className="f" htmlFor="dp">Default policy for organizations</label>
          <select className="i" id="dp" value={f.default_policy} onChange={(e) => setF({ ...f, default_policy: e.target.value })}>
            {d.policies.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}</select>
          <div className="grid2" style={{ gap: 12 }}>
            <div><label className="f" htmlFor="rd">Suggested retention for new organizations (days, 0 = keep)</label><input className="i" id="rd" type="number" min={0} max={3650} value={f.default_retention_days} onChange={n('default_retention_days')} /></div>
            <div><label className="f" htmlFor="mx">Maximum artifact size (MB)</label><input className="i" id="mx" type="number" min={1} max={2048} value={f.max_artifact_mb} onChange={n('max_artifact_mb')} /></div>
            <div><label className="f" htmlFor="sp">Hold files for unavailable customer storage (hours)</label><input className="i" id="sp" type="number" min={1} max={720} value={f.spool_max_hours} onChange={n('spool_max_hours')} /></div>
            <div><label className="f" htmlFor="bf">Default top folder in customer storage</label><input className="i" id="bf" value={f.base_folder} onChange={(e) => setF({ ...f, base_folder: e.target.value })} /></div>
          </div>
          <button className="btn primary" style={{ marginTop: 14 }}>Save</button>
        </form>
        <div className="panel">
          <h2>Unical-managed storage health</h2>
          <div className="stats" style={{ marginBottom: 8 }}>
            <div className="stat"><div className="k">Free</div><div className="v" style={{ fontSize: 15 }}>{d.local.free_gb} GB</div></div>
            <div className="stat"><div className="k">Used</div><div className="v" style={{ fontSize: 15, color: (d.local.used_pct ?? 0) > 85 ? 'var(--bad)' : undefined }}>{d.local.used_pct ?? '—'}%</div></div>
          </div>
          <p className="small">Last check {fmtDate(d.settings.local_last_check_at)} · last success {fmtDate(d.settings.local_last_ok_at)}{d.settings.local_last_error && <><br /><span style={{ color: 'var(--bad)' }}>{d.settings.local_last_error}</span></>}</p>
          <button className="btn sm" onClick={test}>Test now</button>
          <p className="hint">Customer storage credentials are visible only to that organization's administrators; Unical never holds a credential that can reach customer storage on its own.</p>
        </div>
      </div>
      <div className="panel" style={{ padding: 0 }}>
        <div style={{ padding: '14px 20px 0' }}><h2>Organizations</h2></div>
        <table className="t"><thead><tr><th style={{ paddingLeft: 20 }}>Organization</th><th>Allowed</th><th>Chosen</th><th>Health</th><th>Waiting</th></tr></thead>
          <tbody>{d.tenants.map((t) => (
            <tr key={t.tenant_id}><td style={{ paddingLeft: 20 }}><b>{t.name}</b><div className="muted small mono">/{t.slug}</div></td>
              <td><select className="i" style={{ minWidth: 200 }} value={t.policy_override ?? ''} onChange={(e) => setPolicy(t, e.target.value || null)} aria-label={`Policy for ${t.name}`}>
                <option value="">Platform default ({d.policies.find((p) => p.value === f.default_policy)?.label})</option>
                {d.policies.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}</select></td>
              <td className="small">{t.provider_label}{t.needs_reselection && <div><span className="pill warn">must choose again</span></div>}</td>
              <td className="small"><span className={`pill ${t.storage_status === 'ok' ? 'ok' : t.storage_status === 'error' ? 'bad' : ''}`}>{t.storage_status}</span>
                {t.last_ok_at && <div className="muted">ok {fmtDate(t.last_ok_at)}</div>}{t.last_error && <div style={{ color: 'var(--bad)' }}>{t.last_error.slice(0, 120)}</div>}</td>
              <td className="small">{t.pending_uploads || '—'}</td></tr>))}
          </tbody></table>
      </div>
      <div className="panel" style={{ padding: 0 }}>
        <div style={{ padding: '14px 20px 0' }}><h2>Storage audit (latest 50)</h2></div>
        <table className="t"><thead><tr><th style={{ paddingLeft: 20 }}>When</th><th>By</th><th>Organization</th><th>Action</th><th>Target</th></tr></thead>
          <tbody>{d.audit.map((a) => <tr key={a.id}><td style={{ paddingLeft: 20 }} className="small">{fmtDate(a.at)}</td><td className="small">{a.user ?? '—'}</td>
            <td className="small">{a.organization ?? '—'}</td><td className="mono small">{a.action}</td><td className="small">{a.target}</td></tr>)}</tbody></table>
      </div>
      {ack && <Modal title="Organizations affected by this change" onClose={() => setAck(null)}>
        <p className="small">Their current storage would no longer be permitted. Nothing is switched automatically: if you continue, each one is flagged and
          <b> cannot run tests until an administrator chooses an allowed storage</b>. Tell them before you continue.</p>
        <table className="t"><thead><tr><th>Organization</th><th>Current</th><th>New policy</th></tr></thead>
          <tbody>{ack.affected.map((a) => <tr key={a.tenant_id}><td>{a.name} <span className="muted mono small">/{a.slug}</span></td><td className="small">{a.current}</td><td className="small">{a.policy}</td></tr>)}</tbody></table>
        <div className="row" style={{ justifyContent: 'flex-end', marginTop: 14 }}><button className="btn" onClick={() => setAck(null)}>Cancel</button>
          <button className="btn danger" onClick={() => ack.retry(ack.affected.map((a) => a.tenant_id))}>Acknowledge and apply</button></div>
      </Modal>}
    </>
  )
}

function pick(f: Settings) {
  const { local_enabled, sharepoint_enabled, gdrive_enabled, default_policy, default_retention_days, max_artifact_mb, spool_max_hours, base_folder } = f
  return { local_enabled, sharepoint_enabled, gdrive_enabled, default_policy, default_retention_days, max_artifact_mb, spool_max_hours, base_folder }
}
