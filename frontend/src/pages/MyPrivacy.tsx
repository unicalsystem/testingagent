import { useState, type FormEvent } from 'react'
import { api, apiBase } from '../api'
import { Alert, fmtDate, useAsync } from '../components/ui'

interface Req { id: number; kind: string; kind_label: string; status: string; created_at: string; due_at: string; handled_at: string | null; response: string; details: string; overdue: boolean }
interface Mine { requests: Req[]; needs_acceptance: string[]; versions: { terms: string; privacy: string }; kinds: { kind: string; label: string }[] }

const DOC = { terms: 'Terms & Conditions', privacy: 'Privacy Policy' } as Record<string, string>

/** Shown instead of the app until the current Terms and Privacy Policy are accepted. */
export function LegalGate({ pending, onDone, onSignOut }: { pending: string[]; onDone: () => void; onSignOut: () => void }) {
  const [ok, setOk] = useState(false), [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const accept = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setErr('')
    try { await api.post('/privacy/accept'); onDone() } catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  return (
    <div className="login-wrap">
      <form className="login" style={{ width: 460 }} onSubmit={accept}>
        <h1 style={{ marginBottom: 8 }}>Before you continue</h1>
        <p className="small">Please read and accept the current {pending.map((d) => DOC[d] ?? d).join(' and ')} of TestAgent (Unical Systems).
          They explain how the service may be used, how your organization's test data is handled, and your privacy rights.</p>
        <ul className="small">
          {pending.includes('terms') && <li><a href="/terms" target="_blank" rel="noopener">Terms &amp; Conditions</a> (opens in a new tab)</li>}
          {pending.includes('privacy') && <li><a href="/privacy" target="_blank" rel="noopener">Privacy Policy</a> (opens in a new tab)</li>}
        </ul>
        <Alert>{err}</Alert>
        <label className="check small"><input type="checkbox" checked={ok} onChange={(e) => setOk(e.target.checked)} />
          <span>I have read them, I accept the Terms &amp; Conditions, and I consent to the processing of my personal data as described in the Privacy Policy.</span></label>
        <button className="btn primary" style={{ width: '100%', justifyContent: 'center', marginTop: 16 }} disabled={!ok || busy}>{busy ? 'Saving…' : 'Accept and continue'}</button>
        <p className="small muted" style={{ textAlign: 'center', marginTop: 10 }}>Don't agree? <a href="#" onClick={(e) => { e.preventDefault(); onSignOut() }}>Sign out</a> and contact your administrator.</p>
      </form>
    </div>
  )
}

export default function MyPrivacy() {
  const d = useAsync(() => api.get<Mine>('/privacy/me'), [])
  const [kind, setKind] = useState('access'), [details, setDetails] = useState('')
  const [err, setErr] = useState(''), [msg, setMsg] = useState('')
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr(''); setMsg('')
    try { await api.post('/privacy/requests', { kind, details }); setDetails(''); setMsg('Request sent. You can follow it below.'); d.reload() }
    catch (x) { setErr((x as Error).message) }
  }
  return (
    <>
      <div className="topbar"><div><h1>Privacy &amp; my data</h1>
        <p className="muted small" style={{ margin: '4px 0 0' }}>Your rights under India's DPDP Act, the UK GDPR and US state privacy laws. See the <a href="/privacy" target="_blank" rel="noopener">Privacy Policy</a>.</p></div></div>
      <Alert>{d.error}</Alert>
      <div className="grid2">
        <div className="panel">
          <h2>Download my data</h2>
          <p className="small muted">A JSON file with your account, linked identities, project access, consents, sessions, privacy requests and your own
            activity log. Test cases and results belong to your organization; its administrators can export them.</p>
          <a className="btn primary" href={`${apiBase()}/privacy/me/export`}>⬇ Download my data</a>
          {d.data && <p className="small muted" style={{ marginTop: 12 }}>You accepted the Terms version {d.data.versions.terms} and Privacy Policy version {d.data.versions.privacy}.</p>}
        </div>
        <form className="panel" onSubmit={submit}>
          <h2>Make a request</h2>
          <Alert>{err}</Alert><Alert kind="ok">{msg}</Alert>
          <label className="f" htmlFor="rk">What would you like?</label>
          <select className="i" id="rk" value={kind} onChange={(e) => setKind(e.target.value)}>
            {d.data?.kinds.map((k) => <option key={k.kind} value={k.kind}>{k.label}</option>)}</select>
          <label className="f" htmlFor="rd">Details</label>
          <textarea className="i" id="rd" rows={3} maxLength={4000} value={details} onChange={(e) => setDetails(e.target.value)}
            placeholder={kind === 'nominate' ? 'Name and contact details of the person you nominate' : kind === 'correction' ? 'What should be corrected?' : ''} />
          <div className="hint">Your organization's administrators and the Unical Systems Grievance Officer receive it. Withdrawing consent or deleting your account ends your access to TestAgent.</div>
          <button className="btn" style={{ marginTop: 12 }}>Send request</button>
        </form>
      </div>
      <div className="panel" style={{ padding: 0 }}>
        <div style={{ padding: '14px 20px 0' }}><h2>My requests</h2></div>
        <table className="t"><thead><tr><th style={{ paddingLeft: 20 }}>Request</th><th>Raised</th><th>Due</th><th>Status</th><th>Response</th></tr></thead>
          <tbody>{d.data?.requests.length === 0 && <tr><td colSpan={5} className="muted" style={{ paddingLeft: 20 }}>None.</td></tr>}
            {d.data?.requests.map((r) => (
              <tr key={r.id}><td style={{ paddingLeft: 20 }}>#{r.id} {r.kind_label}{r.details && <div className="muted small">{r.details}</div>}</td>
                <td className="small">{fmtDate(r.created_at)}</td><td className="small">{fmtDate(r.due_at)}</td>
                <td><span className={`pill ${r.status === 'completed' ? 'ok' : r.status === 'rejected' ? 'bad' : r.overdue ? 'warn' : ''}`}>{r.status.replace('_', ' ')}</span></td>
                <td className="small">{r.response || '—'}</td></tr>))}
          </tbody></table>
      </div>
    </>
  )
}
