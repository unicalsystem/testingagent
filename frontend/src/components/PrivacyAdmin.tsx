import { useEffect, useState, type FormEvent } from 'react'
import { abs, api } from '../api'
import { Alert, Modal, fmtDate, useAsync } from './ui'

interface Req {
  id: number; kind: string; kind_label: string; status: string; created_at: string; due_at: string; handled_at: string | null
  response: string; details: string; overdue: boolean; requester_name: string; requester_email: string; user_id: number | null
  organization: { slug: string; name: string } | null
}

const send = <T,>(method: string, url: string, body: unknown) => fetch(url, {
  method, credentials: 'same-origin', headers: { 'X-Requested-With': 'TestAgent', 'Content-Type': 'application/json' }, body: JSON.stringify(body),
}).then(async (r) => { const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(typeof j.detail === 'string' ? j.detail : `${r.status}`); return j as T })

/** Data-subject requests. scope="org": this organization (tenant admins). scope="platform": every request (Unical Super Admin). */
export function PrivacyRequests({ scope }: { scope: 'org' | 'platform' }) {
  const [filter, setFilter] = useState('open')
  const list = useAsync(() => scope === 'org' ? api.get<Req[]>('/admin/privacy-requests') : abs.get<Req[]>(`/api/platform/privacy-requests?status=${filter}`), [filter, scope])
  const [edit, setEdit] = useState<Req | null>(null)
  const rows = (list.data ?? []).filter((r) => scope === 'platform' || filter === 'all' || (filter === 'open' ? ['open', 'in_progress'].includes(r.status) : r.status === filter))
  return (
    <div className="panel" style={{ padding: 0 }}>
      <div className="row" style={{ padding: '14px 20px 0', justifyContent: 'space-between' }}>
        <div><h2 style={{ margin: 0 }}>Privacy requests</h2>
          <p className="muted small" style={{ margin: '4px 0 0' }}>Access, correction, erasure, consent withdrawal, nomination and grievances. Respond before the due date{scope === 'org' ? '; the Unical Systems Grievance Officer also sees these requests' : ''}.</p></div>
        <select className="i" style={{ width: 150 }} value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter">
          <option value="open">Open</option><option value="completed">Completed</option><option value="rejected">Rejected</option><option value="all">All</option></select>
      </div>
      <Alert>{list.error}</Alert>
      <table className="t" style={{ marginTop: 8 }}><thead><tr><th style={{ paddingLeft: 20 }}>Request</th><th>From</th>{scope === 'platform' && <th>Organization</th>}<th>Due</th><th>Status</th><th /></tr></thead>
        <tbody>{rows.length === 0 && <tr><td colSpan={6} className="muted" style={{ paddingLeft: 20 }}>None.</td></tr>}
          {rows.map((r) => (
            <tr key={r.id}><td style={{ paddingLeft: 20 }}>#{r.id} {r.kind_label}{r.details && <div className="muted small" style={{ maxWidth: 380, whiteSpace: 'pre-wrap' }}>{r.details}</div>}</td>
              <td className="small">{r.requester_name || '—'}<div className="muted">{r.requester_email}</div></td>
              {scope === 'platform' && <td className="small">{r.organization?.name ?? <span className="muted">public form</span>}</td>}
              <td className="small">{fmtDate(r.due_at)}</td>
              <td><span className={`pill ${r.status === 'completed' ? 'ok' : r.status === 'rejected' ? 'bad' : r.overdue ? 'warn' : ''}`}>{r.overdue ? 'overdue' : r.status.replace('_', ' ')}</span></td>
              <td style={{ textAlign: 'right', paddingRight: 20 }}><button className="btn sm" onClick={() => setEdit(r)}>Handle</button></td></tr>))}
        </tbody></table>
      {edit && <HandleModal req={edit} scope={scope} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); list.reload() }} />}
    </div>
  )
}

function HandleModal({ req, scope, onClose, onSaved }: { req: Req; scope: 'org' | 'platform'; onClose: () => void; onSaved: () => void }) {
  const [status, setStatus] = useState(req.status), [response, setResponse] = useState(req.response), [err, setErr] = useState('')
  const save = async (e: FormEvent) => {
    e.preventDefault(); setErr('')
    try {
      if (scope === 'org') await api.patch(`/admin/privacy-requests/${req.id}`, { status, response })
      else await send('PATCH', `/api/platform/privacy-requests/${req.id}`, { status, response })
      onSaved()
    } catch (x) { setErr((x as Error).message) }
  }
  const erase = async () => {
    if (!req.user_id || !window.confirm('Erase this person\'s account now? This cannot be undone.')) return
    setErr(''); try { await api.post(`/admin/users/${req.user_id}/erase`); setStatus('completed'); setResponse((r) => r || 'Your account and personal data were erased.') } catch (x) { setErr((x as Error).message) }
  }
  return (
    <Modal title={`Request #${req.id}: ${req.kind_label}`} onClose={onClose}>
      <form onSubmit={save}>
        <Alert>{err}</Alert>
        <p className="small">From <b>{req.requester_name || '—'}</b> {req.requester_email && <>(<a href={`mailto:${req.requester_email}`}>{req.requester_email}</a>)</>} · received {fmtDate(req.created_at)} · due {fmtDate(req.due_at)}</p>
        {req.details && <pre className="small" style={{ whiteSpace: 'pre-wrap', background: 'var(--bg)', padding: 10, borderRadius: 6 }}>{req.details}</pre>}
        <p className="hint">Verify the person's identity before releasing or deleting data. Signed-in users can download their own data under <i>Privacy &amp; my data</i>.
          {req.kind === 'erasure' && scope === 'org' && req.user_id && <> For an account erasure you can <a href="#" onClick={(e) => { e.preventDefault(); erase() }}>erase the account now</a>.</>}</p>
        <label className="f" htmlFor="rs">Status</label>
        <select className="i" id="rs" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="open">Open</option><option value="in_progress">In progress</option><option value="completed">Completed</option><option value="rejected">Rejected</option></select>
        <label className="f" htmlFor="rr">Response to the person (kept with the request)</label>
        <textarea className="i" id="rr" rows={4} maxLength={4000} value={response} onChange={(e) => setResponse(e.target.value)} />
        <div className="row" style={{ justifyContent: 'flex-end', marginTop: 14 }}><button type="button" className="btn" onClick={onClose}>Cancel</button><button className="btn primary">Save</button></div>
      </form>
    </Modal>
  )
}

interface LegalDoc { [k: string]: string }
interface PrivacyDoc { request_response_days: number; audit_retention_days: number; registration_retention_days: number; session_retention_days: number }

const LEGAL_FIELDS: [string, string, string][] = [
  ['entity_name', 'Legal entity name shown in the documents', ''],
  ['address', 'Registered / postal address', ''],
  ['contact_email', 'General contact e-mail', 'email'],
  ['grievance_officer_name', 'Grievance Officer name (DPDP Act)', ''],
  ['grievance_officer_email', 'Grievance Officer e-mail', 'email'],
  ['grievance_officer_phone', 'Grievance Officer phone', ''],
  ['dpo_email', 'Data protection contact / DPO e-mail (optional)', 'email'],
  ['uk_representative', 'UK representative, Art. 27 UK GDPR (name and address, if appointed)', ''],
  ['us_contact', 'United States contact (toll-free number or address, if required)', ''],
  ['hosting_location', 'Where the servers are located', ''],
]

/** Platform → Legal & privacy: the details printed in the Privacy Policy and Terms, retention periods, all requests. */
export function LegalSettings() {
  const [l, setL] = useState<LegalDoc | null>(null), [p, setP] = useState<PrivacyDoc | null>(null)
  const [publish, setPublish] = useState(false), [err, setErr] = useState(''), [msg, setMsg] = useState('')
  useEffect(() => { abs.get<{ legal: LegalDoc; privacy: PrivacyDoc }>('/api/platform/legal').then((d) => { setL(d.legal); setP(d.privacy) }).catch((x) => setErr(x.message)) }, [])
  if (!l || !p) return <Alert>{err}</Alert>
  const saveLegal = async (e: FormEvent) => {
    e.preventDefault(); setErr(''); setMsg('')
    if (publish && !window.confirm('Publish a new version? Every signed-in user must accept the Terms and Privacy Policy again.')) return
    try {
      const body = Object.fromEntries(LEGAL_FIELDS.map(([k]) => [k, l[k] ?? '']))
      const d = await send<{ legal: LegalDoc }>('PUT', '/api/platform/legal', { ...body, publish_new_version: publish })
      setL(d.legal); setPublish(false); setMsg(publish ? `Published version ${d.legal.terms_version}.` : 'Saved.')
    } catch (x) { setErr((x as Error).message) }
  }
  const savePrivacy = async (e: FormEvent) => {
    e.preventDefault(); setErr(''); setMsg('')
    try { const d = await send<{ privacy: PrivacyDoc }>('PUT', '/api/platform/privacy', p); setP(d.privacy); setMsg('Retention settings saved.') } catch (x) { setErr((x as Error).message) }
  }
  const num = (k: keyof PrivacyDoc) => (e: { target: { value: string } }) => setP({ ...p, [k]: Number(e.target.value) })
  return (
    <>
      <Alert>{err}</Alert><Alert kind="ok">{msg}</Alert>
      <Alert kind="warn">The Privacy Policy and Terms are templates aligned with India's DPDP Act 2023, the UK GDPR / DPA 2018 and US state privacy laws (CCPA/CPRA). Have them reviewed by your legal counsel before relying on them.</Alert>
      <div className="grid2">
        <form className="panel" onSubmit={saveLegal}>
          <h2>Legal details</h2>
          <p className="muted small" style={{ marginTop: -6 }}>Printed in the public <a href="/privacy" target="_blank" rel="noopener">Privacy Policy</a> and <a href="/terms" target="_blank" rel="noopener">Terms</a>. Current version: <b>{l.terms_version}</b>.</p>
          {LEGAL_FIELDS.map(([k, label, type]) => (
            <div key={k}><label className="f" htmlFor={`lg-${k}`}>{label}</label>
              <input className="i" id={`lg-${k}`} type={type || 'text'} value={l[k] ?? ''} onChange={(e) => setL({ ...l, [k]: e.target.value })} required={k === 'entity_name'} /></div>))}
          <label className="check small"><input type="checkbox" checked={publish} onChange={(e) => setPublish(e.target.checked)} />
            <span>Publish as a new version — use when the obligations change; users accept again at their next visit.</span></label>
          <button className="btn primary" style={{ marginTop: 14 }}>Save</button>
        </form>
        <form className="panel" onSubmit={savePrivacy}>
          <h2>Retention &amp; deadlines</h2>
          <label className="f" htmlFor="pr1">Respond to privacy requests within (days)</label>
          <input className="i" id="pr1" type="number" min={1} max={90} value={p.request_response_days} onChange={num('request_response_days')} />
          <div className="hint">30 days meets the UK GDPR one-month limit; the DPDP Rules and CCPA allow longer.</div>
          <label className="f" htmlFor="pr2">Keep audit and security logs (days, 0 = forever)</label>
          <input className="i" id="pr2" type="number" min={0} max={3650} value={p.audit_retention_days} onChange={num('audit_retention_days')} />
          <label className="f" htmlFor="pr3">Keep rejected registrations (days, 0 = forever)</label>
          <input className="i" id="pr3" type="number" min={0} max={3650} value={p.registration_retention_days} onChange={num('registration_retention_days')} />
          <label className="f" htmlFor="pr4">Keep expired sign-in sessions (days)</label>
          <input className="i" id="pr4" type="number" min={1} max={365} value={p.session_retention_days} onChange={num('session_retention_days')} />
          <div className="hint">Deletion runs automatically every few hours on the worker.</div>
          <button className="btn primary" style={{ marginTop: 14 }}>Save</button>
        </form>
      </div>
      <PrivacyRequests scope="platform" />
    </>
  )
}
