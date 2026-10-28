import { useEffect, useState, type FormEvent } from 'react'
import { useLocation } from 'react-router-dom'
import { api, type Me } from '../api'
import { Avatar } from '../components/UserMenu'
import { Alert, fmtDate, useApp } from '../components/ui'

interface Sess { id: number; current: boolean; method: string; ip: string; user_agent: string; created_at: string; last_seen_at: string }

function browserOf(ua: string): string {
  const b = /Edg\//.test(ua) ? 'Edge' : /Chrome\//.test(ua) ? 'Chrome' : /Firefox\//.test(ua) ? 'Firefox' : /Safari\//.test(ua) ? 'Safari' : 'Browser'
  const os = /Windows/.test(ua) ? 'Windows' : /Android/.test(ua) ? 'Android' : /iPhone|iPad/.test(ua) ? 'iOS' : /Mac OS/.test(ua) ? 'macOS' : /Linux/.test(ua) ? 'Linux' : ''
  return os ? `${b} on ${os}` : b
}

export default function Profile({ onMe }: { onMe: (m: Me) => void }) {
  const { me } = useApp()
  const loc = useLocation()
  const [name, setName] = useState(me.display_name), [email, setEmail] = useState(me.email ?? ''), [pw, setPw] = useState('')
  const [cur, setCur] = useState(''), [n1, setN1] = useState(''), [n2, setN2] = useState('')
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string; where: string } | null>(null), [busy, setBusy] = useState(false)
  const [sessions, setSessions] = useState<Sess[] | null>(null)
  const loadSessions = () => api.get<Sess[]>('/auth/sessions').then(setSessions).catch(() => setSessions([]))
  useEffect(() => { loadSessions() }, [])
  useEffect(() => { if (new URLSearchParams(loc.search).get('tab') === 'password') document.getElementById('password')?.scrollIntoView({ behavior: 'smooth' }) }, [loc.search])
  const emailChanged = email.trim().toLowerCase() !== (me.email ?? '').toLowerCase()
  const hasPassword = me.has_password !== false
  const run = async (where: string, fn: () => Promise<string>) => {
    setBusy(true); setMsg(null)
    try { setMsg({ kind: 'ok', text: await fn(), where }) } catch (x) { setMsg({ kind: 'err', text: (x as Error).message, where }) } finally { setBusy(false) }
  }
  const saveProfile = (e: FormEvent) => { e.preventDefault(); run('profile', async () => {
    const m = await api.patch<Me>('/auth/me', { display_name: name, email, current_password: pw }); onMe(m); setPw(''); return 'Profile saved' }) }
  const savePassword = (e: FormEvent) => { e.preventDefault(); run('password', async () => {
    if (n1 !== n2) throw new Error('The new passwords do not match')
    const m = await api.post<Me>('/auth/password', { current_password: cur, new_password: n1 }); onMe(m); setCur(''); setN1(''); setN2(''); loadSessions()
    return 'Password changed. You were signed out on other devices.' }) }
  const note = (where: string) => msg && msg.where === where ? <Alert kind={msg.kind}>{msg.text}</Alert> : null
  return (
    <>
      <div className="topbar"><div className="row" style={{ gap: 14 }}><Avatar me={me} size={52} />
        <div><h1 style={{ margin: 0 }}>{me.display_name || me.username}</h1><div className="muted small">@{me.username} · {me.role_label}{me.tenant ? ` · ${me.tenant.name}` : ''}</div></div></div></div>
      <div className="grid2" style={{ alignItems: 'start' }}>
        <form className="panel" onSubmit={saveProfile}>
          <h2>Profile</h2>
          {note('profile')}
          <label className="f" htmlFor="pn">Display name</label>
          <input className="i" id="pn" value={name} onChange={(e) => setName(e.target.value)} maxLength={120} placeholder={me.username} />
          <label className="f" htmlFor="pe">E-mail</label>
          <input className="i" id="pe" type="email" value={email} onChange={(e) => setEmail(e.target.value)} maxLength={254} disabled={!hasPassword} />
          <div className="hint">Used for password reset, invitations and plan reminders{me.sign_in_methods && ' and, where enabled, for signing in'}.</div>
          {emailChanged && <><label className="f" htmlFor="pc">Current password (to confirm the new e-mail)</label>
            <input className="i" id="pc" type="password" value={pw} onChange={(e) => setPw(e.target.value)} autoComplete="current-password" required /></>}
          <label className="f">Username</label><input className="i" value={me.username} disabled />
          <div className="hint">Your username and role are managed by your organization's administrators.</div>
          <div className="row" style={{ justifyContent: 'flex-end', marginTop: 12 }}><button className="btn primary" disabled={busy}>Save profile</button></div>
        </form>
        <div>
          {hasPassword && <form className="panel" id="password" onSubmit={savePassword}>
            <h2>Change password</h2>
            {note('password')}
            <label className="f" htmlFor="c0">Current password</label><input className="i" id="c0" type="password" value={cur} onChange={(e) => setCur(e.target.value)} autoComplete="current-password" required />
            <label className="f" htmlFor="c1">New password</label><input className="i" id="c1" type="password" value={n1} onChange={(e) => setN1(e.target.value)} autoComplete="new-password" minLength={12} required />
            <div className="hint">At least 12 characters with letters and digits</div>
            <label className="f" htmlFor="c2">Confirm new password</label><input className="i" id="c2" type="password" value={n2} onChange={(e) => setN2(e.target.value)} autoComplete="new-password" minLength={12} required />
            <div className="row" style={{ justifyContent: 'flex-end', marginTop: 12 }}><button className="btn primary" disabled={busy}>Change password</button></div>
          </form>}
          <div className="panel">
            <h2>Signed-in devices</h2>
            {note('sessions')}
            {sessions?.map((s) => <div key={s.id} className="row small" style={{ justifyContent: 'space-between', padding: '6px 0', borderBottom: '1px solid var(--line)' }}>
              <span><b>{browserOf(s.user_agent)}</b>{s.current && <span className="tag" style={{ marginLeft: 6 }}>this device</span>}
                <div className="muted">{s.ip || 'unknown IP'} · {s.method} · last active {fmtDate(s.last_seen_at)}</div></span></div>)}
            {sessions && sessions.length > 1 && <div className="row" style={{ justifyContent: 'flex-end', marginTop: 10 }}>
              <button className="btn danger" disabled={busy} onClick={() => run('sessions', async () => { const r = await api.post<{ revoked: number }>('/auth/sessions/revoke-others'); loadSessions(); return `Signed out of ${r.revoked} other device(s)` })}>Sign out other devices</button></div>}
          </div>
        </div>
      </div>
    </>
  )
}
