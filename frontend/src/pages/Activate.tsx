import { useEffect, useState, type FormEvent } from 'react'
import { abs, type Branding, type Me } from '../api'
import { MicrosoftButton, startMicrosoft } from './Login'
import { Alert } from '../components/ui'
import { Logo } from '../App'

interface Info { tenant: { slug: string; name: string }; email: string; role: string; role_label: string; expires_at: string }

/** Single-use activation link: /<slug>/activate#<token>. The token stays in the URL fragment,
 *  so it is never sent in a request line or written to server logs. */
export default function Activate({ branding, onDone }: { branding: Branding; onDone: (m: Me) => void }) {
  const token = window.location.hash.slice(1)
  const [info, setInfo] = useState<Info | null>(null), [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const [u, setU] = useState(''), [dn, setDn] = useState(''), [p1, setP1] = useState(''), [p2, setP2] = useState('')
  const [methods, setMethods] = useState<{ password: boolean; microsoft: boolean } | null>(null)
  useEffect(() => { abs.get<{ password: boolean; microsoft: boolean }>(`/api/public/tenants/${branding.slug}/auth-config`).then(setMethods).catch(() => setMethods(null)) }, [branding.slug])
  useEffect(() => {
    if (!token) { setErr('This activation link is incomplete.'); return }
    abs.post<Info>('/api/public/invitations/inspect', { token }).then((i) => {
      if (i.tenant.slug !== branding.slug) setErr('This activation link belongs to a different organization.')
      else { setInfo(i); setU(i.email.split('@')[0].replace(/[^a-zA-Z0-9._-]/g, '').toLowerCase()) }
    }).catch((x) => setErr((x as Error).message))
  }, [token, branding.slug])
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr('')
    if (p1 !== p2) return setErr('The passwords do not match')
    setBusy(true)
    try {
      const m = await abs.post<Me>('/api/public/invitations/accept', { token, username: u, display_name: dn, password: p1 })
      history.replaceState(null, '', window.location.pathname)  // drop the token from the address bar
      onDone(m)
    } catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  return (
    <div className="login-wrap">
      <form className="login" onSubmit={submit}>
        <div className="brand"><Logo branding={branding} size={34} /><div>{branding.display_name}<small>Activate your TestAgent account</small></div></div>
        <Alert>{err}</Alert>
        {info && <>
          <p className="muted small">You have been invited as <b>{info.role_label}</b> ({info.email}).</p>
          {methods?.microsoft && <><MicrosoftButton label="Activate with Microsoft" busy={busy} onClick={async () => { setErr(''); setBusy(true); try { await startMicrosoft(branding.slug, 'invite', token) } catch (x) { setErr((x as Error).message); setBusy(false) } }} />
            <p className="muted small" style={{ textAlign: 'center' }}>Use the Microsoft account of {info.email}.{methods.password ? ' Or choose a username and password:' : ''}</p></>}
          {methods?.password !== false && <>
          <label className="f" htmlFor="au">Username</label>
          <input className="i" id="au" autoComplete="username" value={u} onChange={(e) => setU(e.target.value)} required minLength={3} pattern="[a-zA-Z0-9._@-]+" />
          <label className="f" htmlFor="adn">Your name</label>
          <input className="i" id="adn" value={dn} onChange={(e) => setDn(e.target.value)} />
          <label className="f" htmlFor="ap1">Password</label>
          <input className="i" id="ap1" type="password" autoComplete="new-password" minLength={12} value={p1} onChange={(e) => setP1(e.target.value)} required />
          <div className="hint">At least 12 characters with letters and digits.</div>
          <label className="f" htmlFor="ap2">Confirm password</label>
          <input className="i" id="ap2" type="password" autoComplete="new-password" value={p2} onChange={(e) => setP2(e.target.value)} required />
          <button className="btn primary" style={{ width: '100%', justifyContent: 'center', marginTop: 18 }} disabled={busy}>{busy ? 'Activating…' : 'Activate account'}</button>
          </>}
        </>}
      </form>
    </div>
  )
}
