import { useEffect, useState, type FormEvent } from 'react'
import { api } from '../api'
import { Alert, fmtDate } from './ui'

interface Sso {
  platform_organization: boolean; redirect_uri: string
  allowed: { password: boolean; microsoft: boolean }
  policy: { password_enabled: boolean; microsoft_enabled: boolean; trusted_ips: string[] }
  effective: { password: boolean; microsoft: boolean }
  microsoft: null | {
    entra_tenant_id: string; client_id: string; secret_set: boolean; secret_hint: string; secret_updated_at: string | null
    cloud_host: string; allowed_domains: string[]; allowed_group_ids: string[]; provisioning: string; auto_role: string
    status: string; last_validated_at: string | null; last_error: string
  }
}

const lines = (s: string) => s.split(/[\n,]+/).map((x) => x.trim()).filter(Boolean)

export default function SsoSettings() {
  const [d, setD] = useState<Sso | null>(null)
  const [err, setErr] = useState(''), [msg, setMsg] = useState('')
  const [f, setF] = useState({ entra_tenant_id: '', client_id: '', client_secret: '', cloud_host: 'login.microsoftonline.com', domains: '', groups: '', provisioning: 'invite_only', auto_role: 'auditor' })
  const [pol, setPol] = useState({ password_enabled: true, microsoft_enabled: false, trusted: '' })
  const load = (x: Sso) => {
    setD(x)
    setPol({ password_enabled: x.policy.password_enabled, microsoft_enabled: x.policy.microsoft_enabled, trusted: x.policy.trusted_ips.join('\n') })
    if (x.microsoft) setF({ entra_tenant_id: x.microsoft.entra_tenant_id, client_id: x.microsoft.client_id, client_secret: '', cloud_host: x.microsoft.cloud_host,
      domains: x.microsoft.allowed_domains.join('\n'), groups: x.microsoft.allowed_group_ids.join('\n'), provisioning: x.microsoft.provisioning, auto_role: x.microsoft.auto_role })
  }
  useEffect(() => { api.get<Sso>('/admin/sso').then(load).catch((x) => setErr(x.message)) }, [])
  const run = async (fn: () => Promise<Sso & { ok?: boolean; message?: string }>, okMsg: string) => {
    setErr(''); setMsg('')
    try { const r = await fn(); load(r); if (r.ok === false) setErr(r.message ?? 'Failed'); else setMsg(r.message ?? okMsg) } catch (x) { setErr((x as Error).message) }
  }
  const saveMs = (e: FormEvent) => {
    e.preventDefault()
    run(() => api.put('/admin/sso/microsoft', { entra_tenant_id: f.entra_tenant_id, client_id: f.client_id, client_secret: f.client_secret || undefined,
      cloud_host: f.cloud_host, allowed_domains: lines(f.domains), allowed_group_ids: lines(f.groups), provisioning: f.provisioning, auto_role: f.auto_role }), 'Saved. Run the connection test before enabling.')
  }
  const savePolicy = async (e: FormEvent) => {
    e.preventDefault()
    let confirm = false
    if (!pol.password_enabled) {
      if (!window.confirm('Only Microsoft sign-in will remain. Everyone signed in with a password (including you) is signed out, and people without a linked Microsoft account cannot sign in. Continue?')) return
      confirm = true
    }
    run(() => api.put('/admin/sso/policy', { password_enabled: pol.password_enabled, microsoft_enabled: pol.microsoft_enabled, trusted_ips: lines(pol.trusted), confirm }), 'Sign-in policy saved.')
  }
  if (!d) return <Alert>{err}</Alert>
  const ms = d.microsoft
  return (
    <>
      <Alert>{err}</Alert><Alert kind="ok">{msg}</Alert>
      <div className="grid2">
        <form className="panel" onSubmit={savePolicy}>
          <h2>Sign-in methods</h2>
          {d.platform_organization && <p className="muted small" style={{ marginTop: -6 }}>Unical organization (platform). If you lock yourself out, run <code>python -m testpilot.cli auth-recovery</code> on the server.</p>}
          <label className="row" style={{ gap: 8 }}><input type="checkbox" checked={pol.password_enabled} disabled={!d.allowed.password} onChange={(e) => setPol({ ...pol, password_enabled: e.target.checked })} /> Username / e-mail and password</label>
          {!d.allowed.password && <div className="hint">Not permitted by the platform policy.</div>}
          <label className="row" style={{ gap: 8, marginTop: 8 }}><input type="checkbox" checked={pol.microsoft_enabled} disabled={!d.allowed.microsoft || ms?.status !== 'active'} onChange={(e) => setPol({ ...pol, microsoft_enabled: e.target.checked })} /> Microsoft 365</label>
          {ms?.status !== 'active' && <div className="hint">Configure and test the Microsoft connection first.</div>}
          <label className="f" htmlFor="tips">Trusted IP addresses (skip CAPTCHA on this organization’s sign-in)</label>
          <textarea className="i mono" id="tips" rows={4} placeholder={'203.0.113.10\n198.51.100.0/24'} value={pol.trusted} onChange={(e) => setPol({ ...pol, trusted: e.target.value })} />
          <div className="hint">One address or CIDR range per line. Applies only when the platform’s reCAPTCHA is on.</div>
          <div className="small muted" style={{ marginTop: 8 }}>In effect now: password {d.effective.password ? 'on' : 'off'} · Microsoft {d.effective.microsoft ? 'on' : 'off'}</div>
          <button className="btn primary" style={{ marginTop: 12 }}>Save sign-in policy</button>
        </form>
        <form className="panel" onSubmit={saveMs}>
          <div className="row" style={{ justifyContent: 'space-between' }}><h2 style={{ margin: 0 }}>Microsoft Entra ID</h2>
            {ms && <span className="tag" style={{ color: ms.status === 'active' ? 'var(--ok)' : ms.status === 'error' ? 'var(--bad)' : 'var(--warn)' }}>{ms.status}</span>}</div>
          <p className="muted small">Register an app in <i>Microsoft Entra admin center → App registrations</i> (single tenant), add this <b>Web</b> redirect URI, and create a client secret. No API permissions are needed (sign-in uses <code>openid profile email</code>).</p>
          <input className="i mono small" readOnly value={d.redirect_uri} onFocus={(e) => e.target.select()} aria-label="Redirect URI" />
          <label className="f" htmlFor="etid">Directory (tenant) ID</label><input className="i mono" id="etid" value={f.entra_tenant_id} onChange={(e) => setF({ ...f, entra_tenant_id: e.target.value })} placeholder="00000000-0000-0000-0000-000000000000" required />
          <label className="f" htmlFor="ecid">Application (client) ID</label><input className="i mono" id="ecid" value={f.client_id} onChange={(e) => setF({ ...f, client_id: e.target.value })} required />
          <label className="f" htmlFor="esec">Client secret {ms?.secret_set && <span className="muted small">(saved {ms.secret_hint}, {fmtDate(ms.secret_updated_at)} — leave blank to keep, enter a new one to rotate)</span>}</label>
          <input className="i" id="esec" type="password" autoComplete="off" value={f.client_secret} onChange={(e) => setF({ ...f, client_secret: e.target.value })} required={!ms?.secret_set} />
          <label className="f" htmlFor="ecloud">Authority (cloud)</label>
          <select className="i" id="ecloud" value={f.cloud_host} onChange={(e) => setF({ ...f, cloud_host: e.target.value })}>
            <option value="login.microsoftonline.com">Microsoft Entra global (login.microsoftonline.com)</option>
            <option value="login.microsoftonline.us">US Government (login.microsoftonline.us)</option>
          </select>
          <label className="f" htmlFor="edom">Allowed e-mail domains (optional)</label><textarea className="i mono" id="edom" rows={2} value={f.domains} onChange={(e) => setF({ ...f, domains: e.target.value })} placeholder="example.com" />
          <label className="f" htmlFor="egrp">Allowed Entra group object IDs (optional, needs the "groups" claim)</label><textarea className="i mono" id="egrp" rows={2} value={f.groups} onChange={(e) => setF({ ...f, groups: e.target.value })} />
          <label className="f" htmlFor="eprov">New users</label>
          <select className="i" id="eprov" value={f.provisioning} onChange={(e) => setF({ ...f, provisioning: e.target.value })}>
            <option value="invite_only">Only invited or already linked users can sign in (recommended)</option>
            <option value="auto_domain">Create an account on first sign-in for the allowed domains</option>
          </select>
          {f.provisioning === 'auto_domain' && <><label className="f" htmlFor="erole">Role for new accounts (no project access until assigned)</label>
            <select className="i" id="erole" value={f.auto_role} onChange={(e) => setF({ ...f, auto_role: e.target.value })}>
              <option value="auditor">Auditor / Read-only</option><option value="developer">Developer</option><option value="qa_engineer">QA Engineer</option><option value="project_manager">Project Manager</option>
            </select></>}
          {ms?.last_error && <Alert kind="warn">Last test: {ms.last_error}</Alert>}
          {ms?.last_validated_at && <div className="hint">Last successful test {fmtDate(ms.last_validated_at)}</div>}
          <div className="row" style={{ marginTop: 14 }}>
            <button className="btn primary">Save</button>
            {ms && <button type="button" className="btn" onClick={() => run(() => api.post('/admin/sso/microsoft/test'), 'Connected')}>Test connection</button>}
            {ms && <button type="button" className="btn danger" onClick={() => { if (window.confirm('Disconnect Microsoft sign-in? Microsoft sessions are signed out.')) run(() => api.del('/admin/sso/microsoft'), 'Disconnected') }}>Disconnect</button>}
          </div>
        </form>
      </div>
    </>
  )
}
