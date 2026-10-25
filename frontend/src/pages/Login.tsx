import { useState, type FormEvent } from 'react'
import { api, type Me } from '../api'
import { Alert } from '../components/ui'

export default function Login({ onLogin }: { onLogin: (m: Me) => void }) {
  const [u, setU] = useState(''), [p, setP] = useState(''), [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setErr('')
    try { onLogin(await api.post<Me>('/auth/login', { username: u, password: p })) }
    catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  return (
    <div className="login-wrap">
      <form className="login" onSubmit={submit}>
        <div className="brand">
          <svg width="34" height="34" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="7" fill="#14305c" /><path d="M8 17l5 5 11-12" fill="none" stroke="#7fe0b0" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round" /></svg>
          <div>TestPilot AI<small>Unical Systems · Intelligent web application testing</small></div>
        </div>
        <Alert>{err}</Alert>
        <label className="f" htmlFor="u">Username</label>
        <input className="i" id="u" autoComplete="username" value={u} onChange={(e) => setU(e.target.value)} autoFocus required />
        <label className="f" htmlFor="p">Password</label>
        <input className="i" id="p" type="password" autoComplete="current-password" value={p} onChange={(e) => setP(e.target.value)} required />
        <button className="btn primary" style={{ width: '100%', justifyContent: 'center', marginTop: 18 }} disabled={busy}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
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
