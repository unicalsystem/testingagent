import { useState } from 'react'
import { abs } from '../api'
import { Alert, Modal, fmtDate, useApp, useAsync } from '../components/ui'
import { AuthSecurity, EmailSettings } from '../components/PlatformSettings'
import SsoSettings from '../components/SsoSettings'
import PlatformStorage from '../components/PlatformStorage'
import { LegalSettings } from '../components/PrivacyAdmin'

interface Reg { id: number; org_name: string; requested_slug: string; contact_name: string; contact_email: string; contact_phone: string; country: string; message: string; status: string; decision_note: string; created_at: string }
interface Tn { id: number; slug: string; name: string; status: string; status_reason: string; created_at: string; usage: { users: number; projects: number; runs: number } }
interface Ev { id: number; at: string; user: string | null; action: string; target: string; ip: string }
interface Link { email: string; expires_at: string; link: string }

function LinkBox({ title, link, onClose }: { title: string; link: Link; onClose: () => void }) {
  return (
    <Modal title={title} onClose={onClose}>
      <p className="small">Send this one-time activation link to <b>{link.email}</b>. It expires {fmtDate(link.expires_at)} and works once. It is not shown again.</p>
      <textarea className="i mono" readOnly rows={3} value={link.link} onFocus={(e) => e.target.select()} />
      <div className="row" style={{ justifyContent: 'flex-end', marginTop: 12 }}>
        <button className="btn" onClick={() => navigator.clipboard?.writeText(link.link)}>Copy</button>
        <button className="btn primary" onClick={onClose}>Done</button>
      </div>
    </Modal>
  )
}

function ApproveModal({ reg, onClose, onDone }: { reg: Reg; onClose: () => void; onDone: (l: Link) => void }) {
  const [slug, setSlug] = useState(reg.requested_slug), [name, setName] = useState(reg.org_name), [note, setNote] = useState(''), [err, setErr] = useState('')
  const go = async () => {
    setErr('')
    try { const r = await abs.post<{ owner_invitation: Link }>(`/api/platform/registrations/${reg.id}/approve`, { slug, org_name: name, note }); onDone(r.owner_invitation) }
    catch (x) { setErr((x as Error).message) }
  }
  return (
    <Modal title={`Approve ${reg.org_name}`} onClose={onClose}>
      <Alert>{err}</Alert>
      <label className="f">Organization name</label><input className="i" value={name} onChange={(e) => setName(e.target.value)} />
      <label className="f">Address (slug)</label><input className="i mono" value={slug} onChange={(e) => setSlug(e.target.value.toLowerCase())} />
      <label className="f">Internal note</label><input className="i" value={note} onChange={(e) => setNote(e.target.value)} />
      <p className="muted small">Approving creates the organization and an owner activation link for {reg.contact_email}.</p>
      <div className="row" style={{ justifyContent: 'flex-end' }}><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" onClick={go}>Approve</button></div>
    </Modal>
  )
}

const PLATFORM_TABS = [['orgs', 'Registrations & organizations'], ['auth', 'Authentication & Security'],
  ['microsoft', 'Microsoft 365 (Unical)'], ['email', 'E-mail (SMTP)'], ['storage', 'Storage infrastructure'],
  ['legal', 'Legal & privacy']] as const

export default function Platform() {
  const [tab, setTab] = useState<string>('orgs')
  return (
    <>
      <div className="topbar"><h1>Platform administration</h1></div>
      <div className="tabs" role="tablist" style={{ display: 'flex', gap: 6, marginBottom: 16, flexWrap: 'wrap' }}>
        {PLATFORM_TABS.map(([k, label]) => <button key={k} role="tab" aria-selected={tab === k} className={`btn sm${tab === k ? ' primary' : ''}`} onClick={() => setTab(k)}>{label}</button>)}
      </div>
      {tab === 'orgs' && <Organizations />}
      {tab === 'auth' && <AuthSecurity />}
      {tab === 'microsoft' && <><p className="muted small">Microsoft 365 sign-in for the <b>Unical</b> organization (platform administration). Customer organizations configure their own Microsoft tenants separately under their Identity &amp; SSO settings.</p><SsoSettings /></>}
      {tab === 'email' && <EmailSettings />}
      {tab === 'storage' && <PlatformStorage />}
      {tab === 'legal' && <LegalSettings />}
    </>
  )
}

function Organizations() {
  const { me } = useApp()
  const [filter, setFilter] = useState('pending')
  const regs = useAsync(() => abs.get<Reg[]>(`/api/platform/registrations?status=${filter}`), [filter])
  const tenants = useAsync(() => abs.get<Tn[]>('/api/platform/tenants'), [])
  const audit = useAsync(() => abs.get<Ev[]>('/api/platform/audit?limit=50'), [])
  const [approving, setApproving] = useState<Reg | null>(null)
  const [link, setLink] = useState<Link | null>(null)
  const [err, setErr] = useState('')
  const reload = () => { regs.reload(); tenants.reload(); audit.reload() }
  const act = async (fn: () => Promise<unknown>) => { setErr(''); try { await fn(); reload() } catch (x) { setErr((x as Error).message) } }

  return (
    <>
      <Alert kind="warn">Unical Super Admin view: organization metadata and lifecycle only. Customer projects, test cases, runs and evidence are not accessible from here.</Alert>
      <Alert>{err || regs.error || tenants.error}</Alert>
      <div className="panel" style={{ padding: 0 }}>
        <div className="row" style={{ padding: '14px 20px 0', justifyContent: 'space-between' }}>
          <h2 style={{ margin: 0 }}>Registrations</h2>
          <select className="i" style={{ width: 160 }} value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter">
            <option value="pending">Pending</option><option value="approved">Approved</option><option value="rejected">Rejected</option><option value="all">All</option>
          </select>
        </div>
        <table className="t"><thead><tr><th style={{ paddingLeft: 20 }}>Organization</th><th>Address</th><th>Contact</th><th>Received</th><th>Status</th><th /></tr></thead>
          <tbody>{regs.data?.length === 0 && <tr><td colSpan={6} className="muted" style={{ paddingLeft: 20 }}>None.</td></tr>}
            {regs.data?.map((r) => (
              <tr key={r.id}>
                <td style={{ paddingLeft: 20 }}><b>{r.org_name}</b>{r.message && <div className="muted small" style={{ maxWidth: 360 }}>{r.message}</div>}</td>
                <td className="mono small">/{r.requested_slug}</td>
                <td className="small">{r.contact_name}<br /><span className="muted">{r.contact_email}{r.contact_phone && ` · ${r.contact_phone}`}{r.country && ` · ${r.country}`}</span></td>
                <td className="small muted">{fmtDate(r.created_at)}</td>
                <td><span className="tag">{r.status}</span>{r.decision_note && <div className="muted small">{r.decision_note}</div>}</td>
                <td style={{ textAlign: 'right', paddingRight: 20, whiteSpace: 'nowrap' }}>{r.status === 'pending' && <>
                  <button className="btn sm primary" onClick={() => setApproving(r)}>Approve</button>{' '}
                  <button className="btn sm danger" onClick={() => { const note = prompt('Reason for rejection (internal):') ?? null; if (note !== null) act(() => abs.post(`/api/platform/registrations/${r.id}/reject`, { note })) }}>Reject</button>
                </>}</td>
              </tr>))}
          </tbody></table>
      </div>
      <div className="panel" style={{ padding: 0 }}>
        <div style={{ padding: '14px 20px 0' }}><h2>Organizations</h2></div>
        <table className="t"><thead><tr><th style={{ paddingLeft: 20 }}>Organization</th><th>Address</th><th>Status</th><th>Users</th><th>Projects</th><th>Runs</th><th /></tr></thead>
          <tbody>{tenants.data?.map((t) => (
            <tr key={t.id}>
              <td style={{ paddingLeft: 20 }}><b>{t.name}</b><div className="muted small">since {fmtDate(t.created_at)}</div></td>
              <td className="mono small"><a href={`/${t.slug}/`}>/{t.slug}</a></td>
              <td><span className="tag">{t.status}</span>{t.status_reason && <div className="muted small">{t.status_reason}</div>}</td>
              <td>{t.usage.users}</td><td>{t.usage.projects}</td><td>{t.usage.runs}</td>
              <td style={{ textAlign: 'right', paddingRight: 20, whiteSpace: 'nowrap' }}>
                {t.status === 'active' && t.usage.users === 0 && <button className="btn sm" onClick={() => act(async () => setLink(await abs.post<Link>(`/api/platform/tenants/${t.id}/owner-invitation`)))}>New owner link</button>}{' '}
                {t.id === me.tenant?.id ? <span className="muted small">platform organization</span> : t.status === 'active'
                  ? <button className="btn sm danger" onClick={() => { const reason = prompt(`Suspend ${t.name}? All its users are signed out and its queued runs wait. Reason:`); if (reason !== null) act(() => abs.post(`/api/platform/tenants/${t.id}/suspend`, { reason })) }}>Suspend</button>
                  : <button className="btn sm" onClick={() => act(() => abs.post(`/api/platform/tenants/${t.id}/reactivate`, { reason: '' }))}>Reactivate</button>}
              </td>
            </tr>))}
          </tbody></table>
      </div>
      <div className="panel" style={{ padding: 0 }}>
        <div style={{ padding: '14px 20px 0' }}><h2>Platform audit (latest 50)</h2></div>
        <table className="t"><thead><tr><th style={{ paddingLeft: 20 }}>When</th><th>By</th><th>Action</th><th>Target</th><th>IP</th></tr></thead>
          <tbody>{audit.data?.map((a) => (
            <tr key={a.id}><td style={{ paddingLeft: 20 }} className="small">{fmtDate(a.at)}</td><td>{a.user ?? '—'}</td><td className="mono small">{a.action}</td><td className="small">{a.target}</td><td className="small muted">{a.ip}</td></tr>))}
          </tbody></table>
      </div>
      {approving && <ApproveModal reg={approving} onClose={() => setApproving(null)} onDone={(l) => { setApproving(null); setLink(l); reload() }} />}
      {link && <LinkBox title="Owner activation link" link={link} onClose={() => setLink(null)} />}
    </>
  )
}
