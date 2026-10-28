import { useEffect, useState, type FormEvent } from 'react'
import { abs } from '../api'
import { Alert, fmtDate } from './ui'

interface Settings {
  auth: Record<string, string | number | boolean>
  recaptcha: { enabled: boolean; site_key: string; secret_set: boolean; secret_hint: string; min_score: number; actions: string[]; unavailable_policy: string; last_test_at: string | null; last_test_ok: boolean | null; last_test_message: string }
  trusted_ips: { cidrs: string[]; note: string }
  smtp: { enabled: boolean; host: string; port: number; security: string; username: string; password_set: boolean; password_hint: string; from_address: string; from_name: string; last_test_at: string | null; last_test_ok: boolean | null; last_test_message: string }
  smtp_ready: boolean; captcha_actions: string[]; microsoft_redirect_uri: string
}

const ACTION_LABELS: Record<string, string> = { login: 'Sign-in pages (all organizations)', register: 'Organization registration', password_reset: 'Password reset request' }
const put = <T,>(p: string, b: unknown) => fetch(p, { method: 'PUT', credentials: 'same-origin', headers: { 'X-Requested-With': 'TestAgent', 'Content-Type': 'application/json' }, body: JSON.stringify(b) })
  .then(async (r) => { const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(typeof j.detail === 'string' ? j.detail : `${r.status}`); return j as T })

export function useSettings() {
  const [s, setS] = useState<Settings | null>(null), [err, setErr] = useState(''), [msg, setMsg] = useState('')
  useEffect(() => { abs.get<Settings>('/api/platform/settings').then(setS).catch((x) => setErr(x.message)) }, [])
  const run = async (fn: () => Promise<Settings & { ok?: boolean; message?: string }>, ok: string) => {
    setErr(''); setMsg('')
    try { const r = await fn(); setS(r); if (r.ok === false) setErr(r.message ?? 'Failed'); else setMsg(r.message ?? ok) } catch (x) { setErr((x as Error).message) }
  }
  return { s, err, msg, run }
}

export function AuthSecurity() {
  const { s, err, msg, run } = useSettings()
  const [a, setA] = useState<Record<string, string | number | boolean> | null>(null)
  const [rc, setRc] = useState({ enabled: false, site_key: '', secret: '', min_score: 0.5, actions: [] as string[], unavailable_policy: 'deny' })
  const [tips, setTips] = useState(''), [note, setNote] = useState('')
  useEffect(() => {
    if (!s) return
    setA(s.auth)
    setRc({ enabled: s.recaptcha.enabled, site_key: s.recaptcha.site_key, secret: '', min_score: s.recaptcha.min_score, actions: s.recaptcha.actions, unavailable_policy: s.recaptcha.unavailable_policy })
    setTips(s.trusted_ips.cidrs.join('\n')); setNote(s.trusted_ips.note)
  }, [s])
  if (!s || !a) return <Alert>{err}</Alert>
  const policy = a.platform_password_enabled && a.platform_microsoft_enabled ? 'both' : a.platform_microsoft_enabled ? 'microsoft' : 'password'
  const setPolicy = (v: string) => setA({ ...a, platform_password_enabled: v !== 'microsoft', platform_microsoft_enabled: v !== 'password' })
  const saveAuth = (e: FormEvent) => {
    e.preventDefault()
    let confirm = false
    if (!a.platform_password_enabled) {
      if (!window.confirm('Unical users will only be able to sign in with Microsoft. Make sure your own account is linked first (sidebar → Link Microsoft account). Recovery: python -m testpilot.cli auth-recovery on the server. Continue?')) return
      confirm = true
    }
    run(() => put('/api/platform/settings/auth', { ...a, confirm }), 'Authentication settings saved')
  }
  const num = (k: string) => (e: { target: { value: string } }) => setA({ ...a, [k]: Number(e.target.value) })
  const bool = (k: string) => (e: { target: { checked: boolean } }) => setA({ ...a, [k]: e.target.checked })
  const r = s.recaptcha
  return (
    <>
      <Alert>{err}</Alert><Alert kind="ok">{msg}</Alert>
      <div className="grid2">
        <form className="panel" onSubmit={saveAuth}>
          <h2>Sign-in policy</h2>
          <label className="f">Unical organization (platform administration)</label>
          <select className="i" value={policy} onChange={(e) => setPolicy(e.target.value)}>
            <option value="password">Username / password only</option><option value="microsoft">Microsoft 365 only</option><option value="both">Both methods</option>
          </select>
          <div className="hint">Microsoft requires Unical's Microsoft 365 configuration to pass its connection test first.</div>
          <label className="f">Customer organizations may use</label>
          <label className="row" style={{ gap: 8 }}><input type="checkbox" checked={!!a.tenant_password_allowed} onChange={bool('tenant_password_allowed')} /> Username / password</label>
          <label className="row" style={{ gap: 8 }}><input type="checkbox" checked={!!a.tenant_microsoft_allowed} onChange={bool('tenant_microsoft_allowed')} /> Their own Microsoft 365 (configured by each organization)</label>
          <label className="f">Users sign in with</label>
          <select className="i" value={String(a.login_identifier)} onChange={(e) => setA({ ...a, login_identifier: e.target.value })}>
            <option value="username_or_email">Username or e-mail</option><option value="username">Username only</option>
          </select>
          <div className="grid2" style={{ gap: 10 }}>
            <div><label className="f">Failed attempts before lockout</label><input className="i" type="number" min={3} max={50} value={Number(a.max_failed_logins)} onChange={num('max_failed_logins')} /></div>
            <div><label className="f">Lockout (minutes)</label><input className="i" type="number" min={1} max={1440} value={Number(a.lockout_minutes)} onChange={num('lockout_minutes')} /></div>
            <div><label className="f">Session length (hours)</label><input className="i" type="number" min={1} max={720} value={Number(a.session_ttl_hours)} onChange={num('session_ttl_hours')} /></div>
            <div><label className="f">Idle timeout (minutes)</label><input className="i" type="number" min={5} max={10080} value={Number(a.session_idle_minutes)} onChange={num('session_idle_minutes')} /></div>
          </div>
          <label className="row" style={{ gap: 8, marginTop: 12 }}><input type="checkbox" checked={!!a.password_reset_enabled} onChange={bool('password_reset_enabled')} disabled={!s.smtp_ready && !a.password_reset_enabled} /> Allow password reset by e-mail</label>
          {!s.smtp_ready && <div className="hint">Configure and enable e-mail (SMTP) first.</div>}
          <div><label className="f">Reset link valid for (minutes)</label><input className="i" type="number" min={5} max={1440} value={Number(a.password_reset_ttl_minutes)} onChange={num('password_reset_ttl_minutes')} style={{ maxWidth: 140 }} /></div>
          <button className="btn primary" style={{ marginTop: 14 }}>Save</button>
        </form>
        <div>
          <form className="panel" onSubmit={(e) => { e.preventDefault(); run(() => put('/api/platform/settings/recaptcha', { ...rc, secret: rc.secret || undefined }), 'reCAPTCHA settings saved') }}>
            <div className="row" style={{ justifyContent: 'space-between' }}><h2 style={{ margin: 0 }}>Google reCAPTCHA v3</h2>
              <span className="tag" style={{ color: r.enabled ? 'var(--ok)' : 'var(--ink-3)' }}>{r.enabled ? 'on' : 'off'}</span></div>
            <p className="muted small">Create a <b>v3</b> key at google.com/recaptcha/admin for the domain <code>{window.location.hostname}</code>. The secret is encrypted and never shown again.</p>
            <label className="row" style={{ gap: 8 }}><input type="checkbox" checked={rc.enabled} onChange={(e) => setRc({ ...rc, enabled: e.target.checked })} /> Enable reCAPTCHA</label>
            <label className="f">Site key</label><input className="i mono" value={rc.site_key} onChange={(e) => setRc({ ...rc, site_key: e.target.value })} />
            <label className="f">Secret key {r.secret_set && <span className="muted small">(saved {r.secret_hint} — leave blank to keep, enter a new one to rotate)</span>}</label>
            <input className="i" type="password" autoComplete="off" value={rc.secret} onChange={(e) => setRc({ ...rc, secret: e.target.value })} />
            <label className="f">Minimum score: <b>{rc.min_score.toFixed(1)}</b> <span className="muted small">(0.0 = allow everyone, 1.0 = strictest; Google recommends 0.5)</span></label>
            <input type="range" min={0} max={1} step={0.1} value={rc.min_score} onChange={(e) => setRc({ ...rc, min_score: Number(e.target.value) })} style={{ width: '100%' }} />
            <label className="f">Protect</label>
            {s.captcha_actions.map((x) => <label key={x} className="row" style={{ gap: 8 }}><input type="checkbox" checked={rc.actions.includes(x)} onChange={(e) => setRc({ ...rc, actions: e.target.checked ? [...rc.actions, x] : rc.actions.filter((y) => y !== x) })} /> {ACTION_LABELS[x] ?? x}</label>)}
            <label className="f">If Google cannot be reached</label>
            <select className="i" value={rc.unavailable_policy} onChange={(e) => setRc({ ...rc, unavailable_policy: e.target.value })}>
              <option value="deny">Refuse the request (safest)</option><option value="allow_and_audit">Allow it and record an audit event</option>
            </select>
            {r.last_test_at && <Alert kind={r.last_test_ok ? 'ok' : 'warn'}>Last check {fmtDate(r.last_test_at)}: {r.last_test_message}</Alert>}
            <div className="row" style={{ marginTop: 12 }}><button className="btn primary">Save</button>
              {r.secret_set && <button type="button" className="btn" onClick={() => run(() => abs.post('/api/platform/settings/recaptcha/test'), 'Checked')}>Check secret with Google</button>}</div>
          </form>
          <form className="panel" onSubmit={(e) => { e.preventDefault(); run(() => put('/api/platform/settings/trusted-ips', { cidrs: tips.split(/[\n,]+/).map((x) => x.trim()).filter(Boolean), note }), 'Trusted IPs saved') }}>
            <h2>Trusted IP addresses</h2>
            <p className="muted small" style={{ marginTop: -6 }}>Requests from these addresses skip the CAPTCHA on every protected page (sign-in for all organizations, registration, password reset). Each organization can add its own list for its sign-in page under Identity &amp; SSO. Password checks, lockout and permissions still apply.</p>
            <textarea className="i mono" rows={5} placeholder={'203.0.113.10\n198.51.100.0/24\n2001:db8::/48'} value={tips} onChange={(e) => setTips(e.target.value)} aria-label="Trusted IPs" />
            <label className="f">Note</label><input className="i" value={note} onChange={(e) => setNote(e.target.value)} placeholder="e.g. Unical Hyderabad office" />
            <button className="btn primary" style={{ marginTop: 12 }}>Save</button>
          </form>
        </div>
      </div>
    </>
  )
}

export function EmailSettings() {
  const { s, err, msg, run } = useSettings()
  const [f, setF] = useState({ enabled: false, host: '', port: 587, security: 'starttls', username: '', password: '', from_address: '', from_name: 'TestAgent' })
  const [to, setTo] = useState('')
  useEffect(() => { if (s) setF({ ...s.smtp, password: '' }) }, [s])
  if (!s) return <Alert>{err}</Alert>
  const m = s.smtp
  return (
    <>
      <Alert>{err}</Alert><Alert kind="ok">{msg}</Alert>
      <form className="panel" style={{ maxWidth: 640 }} onSubmit={(e) => { e.preventDefault(); run(() => put('/api/platform/settings/smtp', { ...f, password: f.password || undefined }), 'E-mail settings saved') }}>
        <h2>Outgoing e-mail (SMTP)</h2>
        <p className="muted small" style={{ marginTop: -6 }}>Used for password-reset links, invitations and organization approvals. The password is encrypted and never shown again. For Microsoft 365 use <code>smtp.office365.com</code>, port 587, STARTTLS.</p>
        <label className="row" style={{ gap: 8 }}><input type="checkbox" checked={f.enabled} onChange={(e) => setF({ ...f, enabled: e.target.checked })} /> Send e-mail</label>
        <div className="grid2" style={{ gap: 10 }}>
          <div><label className="f">Server</label><input className="i" value={f.host} onChange={(e) => setF({ ...f, host: e.target.value })} placeholder="smtp.office365.com" /></div>
          <div><label className="f">Port</label><input className="i" type="number" value={f.port} onChange={(e) => setF({ ...f, port: Number(e.target.value) })} /></div>
          <div><label className="f">Security</label><select className="i" value={f.security} onChange={(e) => setF({ ...f, security: e.target.value })}><option value="starttls">STARTTLS (587)</option><option value="ssl">SSL/TLS (465)</option><option value="none">None (not recommended)</option></select></div>
          <div><label className="f">Username</label><input className="i" value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} autoComplete="off" /></div>
        </div>
        <label className="f">Password {m.password_set && <span className="muted small">(saved {m.password_hint} — leave blank to keep)</span>}</label>
        <input className="i" type="password" autoComplete="off" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} />
        <div className="grid2" style={{ gap: 10 }}>
          <div><label className="f">Sender address</label><input className="i" value={f.from_address} onChange={(e) => setF({ ...f, from_address: e.target.value })} placeholder="noreply@unical.in" /></div>
          <div><label className="f">Sender name</label><input className="i" value={f.from_name} onChange={(e) => setF({ ...f, from_name: e.target.value })} /></div>
        </div>
        {m.last_test_at && <Alert kind={m.last_test_ok ? 'ok' : 'warn'}>Last test {fmtDate(m.last_test_at)}: {m.last_test_message}</Alert>}
        <button className="btn primary" style={{ marginTop: 12 }}>Save</button>
        <div className="row" style={{ marginTop: 14 }}>
          <input className="i" style={{ flex: 1 }} type="email" placeholder="Send a test e-mail to…" value={to} onChange={(e) => setTo(e.target.value)} aria-label="Test recipient" />
          <button type="button" className="btn" disabled={!to} onClick={() => run(() => abs.post('/api/platform/settings/smtp/test', { to }), 'Sent')}>Send test</button>
        </div>
      </form>
    </>
  )
}
