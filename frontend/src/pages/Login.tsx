import { useEffect, useState, type FormEvent } from 'react'
import { abs, api, type Branding, type Me } from '../api'
import { captchaToken, preloadCaptcha, type CaptchaCfg } from '../recaptcha'
import { Alert } from '../components/ui'
import { CookieNotice } from './Legal'
import { Logo } from '../App'

export interface AuthConfig {
  password: boolean; microsoft: boolean; login_identifier: string; password_reset: boolean
  captcha: Record<string, CaptchaCfg>
}

const SSO_ERRORS: Record<string, string> = {
  not_linked: 'This Microsoft account is not linked to a TestAgent account here. Sign in with your password and link it, or ask your administrator for an invitation.',
  account_exists: 'An account with this e-mail already exists. Sign in with your password, then link Microsoft from the sidebar.',
  domain_not_allowed: 'Your Microsoft account’s e-mail domain is not allowed for this organization.',
  group_not_allowed: 'Your Microsoft account is not in a group allowed to sign in here.',
  groups_overage: 'Your Microsoft account is in too many groups for the group restriction. Ask your administrator.',
  disabled: 'Microsoft sign-in is disabled for this organization.',
  cancelled: 'Microsoft sign-in was cancelled.',
  expired: 'The sign-in took too long. Please try again.',
  invalid_state: 'The sign-in could not be verified (it may have been started in another browser). Please try again.',
  email_mismatch: 'Sign in with the Microsoft account of the invited e-mail address.',
  account_inactive: 'Your account is disabled.',
}

export function MicrosoftButton({ label, onClick, busy }: { label: string; onClick: () => void; busy?: boolean }) {
  return (
    <button type="button" className="btn" style={{ width: '100%', justifyContent: 'center', gap: 10 }} onClick={onClick} disabled={busy}>
      <svg width="16" height="16" viewBox="0 0 21 21" aria-hidden="true"><rect x="1" y="1" width="9" height="9" fill="#f25022" /><rect x="11" y="1" width="9" height="9" fill="#7fba00" /><rect x="1" y="11" width="9" height="9" fill="#00a4ef" /><rect x="11" y="11" width="9" height="9" fill="#ffb900" /></svg>
      {label}
    </button>
  )
}

export async function startMicrosoft(slug: string, purpose: 'login' | 'invite', inviteToken?: string) {
  const r = await abs.post<{ url: string }>('/api/public/oidc/microsoft/start', { tenant: slug, purpose, invite_token: inviteToken })
  window.location.assign(r.url)
}

export default function Login({ branding, onLogin }: { branding: Branding; onLogin: (m: Me) => void }) {
  const [cfg, setCfg] = useState<AuthConfig | null>(null)
  const [u, setU] = useState(''), [p, setP] = useState(''), [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const [forgot, setForgot] = useState(false)
  useEffect(() => {
    abs.get<AuthConfig>(`/api/public/tenants/${branding.slug}/auth-config`).then((c) => { setCfg(c); preloadCaptcha(c.captcha.login) }).catch(() => setCfg(null))
    const q = new URLSearchParams(window.location.search)
    const e = q.get('sso_error')
    if (e) setErr(SSO_ERRORS[e] ?? 'Microsoft sign-in failed. Please try again.')
  }, [branding.slug])
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setErr('')
    try {
      const token = await captchaToken(cfg?.captcha.login)
      onLogin(await api.post<Me>('/auth/login', { username: u, password: p, captcha_token: token }))
    } catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  const ms = async () => { setErr(''); setBusy(true); try { await startMicrosoft(branding.slug, 'login') } catch (x) { setErr((x as Error).message); setBusy(false) } }
  if (forgot && cfg) return <ForgotPassword branding={branding} cfg={cfg} onBack={() => setForgot(false)} />
  const emailOk = cfg?.login_identifier === 'username_or_email'
  return (
    <div className="login-wrap">
      <form className="login" onSubmit={submit}>
        <div className="brand">
          <Logo branding={branding} size={34} />
          <div>{branding.display_name}<small>TestAgent · Intelligent web application testing</small></div>
        </div>
        {branding.welcome_text && <p className="muted small" style={{ whiteSpace: 'pre-line' }}>{branding.welcome_text}</p>}
        <Alert>{err}</Alert>
        {cfg?.microsoft && <div style={{ margin: '6px 0 4px' }}><MicrosoftButton label="Sign in with Microsoft" onClick={ms} busy={busy} /></div>}
        {cfg?.microsoft && cfg.password && <div className="muted small" style={{ textAlign: 'center', margin: '12px 0 0' }}>or</div>}
        {(cfg === null || cfg.password) && <>
          <label className="f" htmlFor="u">{emailOk ? 'Username or e-mail' : 'Username'}</label>
          <input className="i" id="u" autoComplete="username" value={u} onChange={(e) => setU(e.target.value)} autoFocus required />
          <label className="f" htmlFor="p">Password</label>
          <input className="i" id="p" type="password" autoComplete="current-password" value={p} onChange={(e) => setP(e.target.value)} required />
          <button className="btn primary" style={{ width: '100%', justifyContent: 'center', marginTop: 18 }} disabled={busy}>
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
          {cfg?.password_reset && <p className="small" style={{ textAlign: 'center', marginTop: 10 }}><a href="#" onClick={(e) => { e.preventDefault(); setForgot(true) }}>Forgot password?</a></p>}
          {cfg?.captcha.login.required && <p className="muted" style={{ fontSize: 11, textAlign: 'center', marginTop: 8 }}>Protected by reCAPTCHA. Google <a href="https://policies.google.com/privacy" target="_blank" rel="noopener noreferrer">Privacy</a> and <a href="https://policies.google.com/terms" target="_blank" rel="noopener noreferrer">Terms</a> apply.</p>}
        </>}
        {(branding.support_email || branding.support_url) && (
          <p className="muted small" style={{ marginTop: 14, textAlign: 'center' }}>
            Need help? {branding.support_email && <a href={`mailto:${branding.support_email}`}>{branding.support_email}</a>}
            {branding.support_email && branding.support_url && ' · '}
            {branding.support_url && <a href={branding.support_url} target="_blank" rel="noopener noreferrer">Support</a>}
          </p>
        )}
        <p className="muted small" style={{ marginTop: 8, textAlign: 'center' }}>Operated by Unical Systems · <a href="/privacy">Privacy</a> · <a href="/terms">Terms</a></p>
      </form>
      <CookieNotice />
    </div>
  )
}

function ForgotPassword({ branding, cfg, onBack }: { branding: Branding; cfg: AuthConfig; onBack: () => void }) {
  const [ident, setIdent] = useState(''), [msg, setMsg] = useState(''), [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  useEffect(() => { preloadCaptcha(cfg.captcha.password_reset) }, [cfg])
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr(''); setBusy(true)
    try {
      const token = await captchaToken(cfg.captcha.password_reset)
      const r = await abs.post<{ message: string }>('/api/public/password-reset/request', { tenant: branding.slug, identifier: ident, captcha_token: token })
      setMsg(r.message)
    } catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  return (
    <div className="login-wrap">
      <form className="login" onSubmit={submit}>
        <div className="brand"><Logo branding={branding} size={34} /><div>{branding.display_name}<small>Reset your password</small></div></div>
        <Alert>{err}</Alert><Alert kind="ok">{msg}</Alert>
        {!msg && <>
          <label className="f" htmlFor="fi">Username or e-mail</label>
          <input className="i" id="fi" value={ident} onChange={(e) => setIdent(e.target.value)} autoFocus required />
          <button className="btn primary" style={{ width: '100%', justifyContent: 'center', marginTop: 18 }} disabled={busy}>{busy ? 'Sending…' : 'E-mail me a reset link'}</button>
        </>}
        <p className="small" style={{ textAlign: 'center', marginTop: 12 }}><a href="#" onClick={(e) => { e.preventDefault(); onBack() }}>Back to sign-in</a></p>
      </form>
    </div>
  )
}

export function ResetPassword({ branding }: { branding: Branding }) {
  const token = window.location.hash.slice(1)
  const [p1, setP1] = useState(''), [p2, setP2] = useState(''), [err, setErr] = useState(''), [done, setDone] = useState(false)
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr('')
    if (p1 !== p2) return setErr('The passwords do not match')
    try { await abs.post('/api/public/password-reset/confirm', { token, new_password: p1 }); history.replaceState(null, '', window.location.pathname); setDone(true) }
    catch (x) { setErr((x as Error).message) }
  }
  return (
    <div className="login-wrap">
      <form className="login" onSubmit={submit}>
        <div className="brand"><Logo branding={branding} size={34} /><div>{branding.display_name}<small>Choose a new password</small></div></div>
        <Alert>{err}</Alert>
        {done ? <><Alert kind="ok">Your password has been changed. You have been signed out everywhere.</Alert>
          <a className="btn primary" style={{ width: '100%', justifyContent: 'center' }} href={`/${branding.slug}/`}>Sign in</a></> : <>
          <label className="f" htmlFor="r1">New password</label>
          <input className="i" id="r1" type="password" autoComplete="new-password" minLength={12} value={p1} onChange={(e) => setP1(e.target.value)} required />
          <div className="hint">At least 12 characters with letters and digits.</div>
          <label className="f" htmlFor="r2">Confirm new password</label>
          <input className="i" id="r2" type="password" autoComplete="new-password" value={p2} onChange={(e) => setP2(e.target.value)} required />
          <button className="btn primary" style={{ width: '100%', justifyContent: 'center', marginTop: 18 }}>Save password</button>
        </>}
      </form>
    </div>
  )
}

export function ChangePassword({ me, onDone }: { me: Me; onDone: (m: Me) => void }) {
  const [cur, setCur] = useState(''), [n1, setN1] = useState(''), [n2, setN2] = useState(''), [err, setErr] = useState('')
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr('')
    if (n1 !== n2) return setErr('The new passwords do not match')
    try { onDone(await api.post<Me>('/auth/password', { current_password: cur, new_password: n1 })) }
    catch (x) { setErr((x as Error).message) }
  }
  return (
    <div className="login-wrap">
      <form className="login" onSubmit={submit}>
        <h1>Set a new password</h1>
        <p className="muted small">Hi {me.display_name || me.username}, your administrator requires you to choose a new password (at least 12 characters, with letters and digits).</p>
        <Alert>{err}</Alert>
        <label className="f" htmlFor="c">Current password</label>
        <input className="i" id="c" type="password" autoComplete="current-password" value={cur} onChange={(e) => setCur(e.target.value)} required />
        <label className="f" htmlFor="n1">New password</label>
        <input className="i" id="n1" type="password" autoComplete="new-password" minLength={12} value={n1} onChange={(e) => setN1(e.target.value)} required />
        <label className="f" htmlFor="n2">Confirm new password</label>
        <input className="i" id="n2" type="password" autoComplete="new-password" value={n2} onChange={(e) => setN2(e.target.value)} required />
        <button className="btn primary" style={{ width: '100%', justifyContent: 'center', marginTop: 18 }}>Save password</button>
      </form>
    </div>
  )
}
