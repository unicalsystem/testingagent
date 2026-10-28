import { useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ROLE_LABELS, api, type Branding, type TenantRole } from '../api'
import { Alert, Modal, fmtDate, useApp, useAsync } from '../components/ui'
import { Assignments, Groups } from '../components/AccessAdmin'
import SsoSettings from '../components/SsoSettings'
import StorageSettings from '../components/StorageSettings'
import { PrivacyRequests } from '../components/PrivacyAdmin'

interface U { id: number; username: string; display_name: string; email: string; role: TenantRole; role_label: string; is_active: boolean; must_change_password: boolean; last_login_at: string | null }
interface Inv { id: number; email: string; role: TenantRole; state: string; expires_at: string; link?: string }
interface T { id: number; host: string; note: string; created_at: string; private_access: boolean }
interface A { id: number; at: string; user: string | null; action: string; target: string; ip: string }

function RoleSelect({ value, onChange, id }: { value: string; onChange: (r: string) => void; id: string }) {
  const { me } = useApp()
  return (
    <select className="i" id={id} value={value} onChange={(e) => onChange(e.target.value)}>
      {(Object.keys(ROLE_LABELS) as TenantRole[]).filter((r) => r !== 'tenant_owner' || me.role === 'tenant_owner' || value === 'tenant_owner')
        .map((r) => <option key={r} value={r}>{ROLE_LABELS[r]}</option>)}
    </select>
  )
}

const ROLE_HELP = 'Owner/Administrator: users, targets, branding · Project Manager & QA Engineer: projects, secrets, test cases, runs, AI · Developer: test cases, runs, AI · Auditor: read-only plus audit log'

function UserModal({ user, onClose, onSaved }: { user?: U; onClose: () => void; onSaved: () => void }) {
  const [username, setUsername] = useState(user?.username ?? ''), [dn, setDn] = useState(user?.display_name ?? ''), [email, setEmail] = useState(user?.email ?? '')
  const [role, setRole] = useState<string>(user?.role ?? 'qa_engineer'), [pw, setPw] = useState(''), [active, setActive] = useState(user?.is_active ?? true)
  const [err, setErr] = useState('')
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr('')
    try {
      if (user) await api.patch(`/admin/users/${user.id}`, { display_name: dn, email, role, is_active: active, ...(pw ? { password: pw } : {}) })
      else await api.post('/admin/users', { username, display_name: dn, email, role, password: pw })
      onSaved()
    } catch (x) { setErr((x as Error).message) }
  }
  return (
    <Modal title={user ? `Edit ${user.username}` : 'Add user'} onClose={onClose}>
      <form onSubmit={submit}>
        <Alert>{err}</Alert>
        {!user && <><label className="f" htmlFor="un">Username</label><input className="i" id="un" value={username} onChange={(e) => setUsername(e.target.value)} required minLength={3} /></>}
        <label className="f" htmlFor="dn">Display name</label><input className="i" id="dn" value={dn} onChange={(e) => setDn(e.target.value)} />
        <label className="f" htmlFor="em">Email</label><input className="i" id="em" type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        <label className="f" htmlFor="ro">Role</label>
        <RoleSelect id="ro" value={role} onChange={setRole} />
        <div className="hint">{ROLE_HELP}. Except for owners and administrators, give project access on the Project access tab.</div>
        <label className="f" htmlFor="pw">{user ? 'Reset password (optional)' : 'Initial password'}</label>
        <input className="i" id="pw" type="password" autoComplete="new-password" value={pw} onChange={(e) => setPw(e.target.value)} minLength={12} required={!user} />
        <div className="hint">At least 12 characters with letters and digits. The user must change it at first sign-in.</div>
        {user && <label className="row" style={{ marginTop: 12 }}><input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} /> Account active</label>}
        <div className="row" style={{ marginTop: 16, justifyContent: 'space-between' }}>
          <div>{user && <button type="button" className="btn sm danger" title="Anonymise this account (privacy erasure request)" onClick={async () => {
            if (!window.confirm(`Erase ${user.username}? Their name, e-mail and sign-in links are removed permanently and the account is disabled. Their test cases and runs stay with the organization.`)) return
            setErr(''); try { await api.post(`/admin/users/${user.id}/erase`); onSaved() } catch (x) { setErr((x as Error).message) } }}>Erase personal data</button>}</div>
          <div className="row"><button type="button" className="btn" onClick={onClose}>Cancel</button><button className="btn primary">Save</button></div></div>
      </form>
    </Modal>
  )
}

function InviteModal({ onClose, onDone }: { onClose: () => void; onDone: (i: Inv) => void }) {
  const [email, setEmail] = useState(''), [role, setRole] = useState<string>('qa_engineer'), [err, setErr] = useState('')
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr('')
    try { onDone(await api.post<Inv>('/admin/invitations', { email, role })) } catch (x) { setErr((x as Error).message) }
  }
  return (
    <Modal title="Invite a user" onClose={onClose}>
      <form onSubmit={submit}>
        <Alert>{err}</Alert>
        <label className="f" htmlFor="ie">Email</label><input className="i" id="ie" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
        <label className="f" htmlFor="ir">Role</label><RoleSelect id="ir" value={role} onChange={setRole} />
        <div className="hint">You get a one-time activation link to send to this person (valid 72 hours).</div>
        <div className="row" style={{ marginTop: 16, justifyContent: 'flex-end' }}><button type="button" className="btn" onClick={onClose}>Cancel</button><button className="btn primary">Create link</button></div>
      </form>
    </Modal>
  )
}

const DEFAULT_COLOR = '#2a63d4'

function BrandingPanel() {
  const b = useAsync(() => api.get<Branding>('/admin/branding'), [])
  const [f, setF] = useState<Branding | null>(null), [msg, setMsg] = useState(''), [err, setErr] = useState('')
  const v = f ?? b.data
  if (!v) return <div className="panel"><h2>Branding</h2><Alert>{b.error}</Alert></div>
  const set = (k: keyof Branding, val: string) => setF({ ...v, [k]: val })
  const logo = (file?: File) => {
    if (!file) return
    if (file.size > 200 * 1024) return setErr('Logo must be 200 KB or smaller')
    const r = new FileReader(); r.onload = () => set('logo_data_url', String(r.result)); r.readAsDataURL(file)
  }
  const save = async (e: FormEvent) => {
    e.preventDefault(); setErr(''); setMsg('')
    try { await api.put('/admin/branding', { display_name: v.display_name, logo_data_url: v.logo_data_url, primary_color: v.primary_color, welcome_text: v.welcome_text, support_email: v.support_email, support_url: v.support_url }); setMsg('Saved. Your sign-in page now uses this branding.') }
    catch (x) { setErr((x as Error).message) }
  }
  return (
    <form className="panel" onSubmit={save}>
      <h2>Branding</h2>
      <p className="muted small" style={{ marginTop: -6 }}>Shown on your sign-in page at <code>{window.location.host}/{v.slug}/</code> and in the sidebar.</p>
      <Alert>{err}</Alert><Alert kind="ok">{msg}</Alert>
      <label className="f" htmlFor="bdn">Display name</label><input className="i" id="bdn" value={v.display_name} onChange={(e) => set('display_name', e.target.value)} required />
      <label className="f" htmlFor="blogo">Logo (PNG, JPEG or WebP, max 200 KB)</label>
      <div className="row" style={{ alignItems: 'center' }}>
        {v.logo_data_url && <img src={v.logo_data_url} alt="Current logo" style={{ height: 36, background: '#fff', borderRadius: 6 }} />}
        <input id="blogo" type="file" accept="image/png,image/jpeg,image/webp" onChange={(e) => logo(e.target.files?.[0])} />
        {v.logo_data_url && <button type="button" className="btn sm" onClick={() => set('logo_data_url', '')}>Remove</button>}
      </div>
      <label className="f" htmlFor="bcol">Primary colour</label>
      <div className="row" style={{ alignItems: 'center' }}><input id="bcol" type="color" value={v.primary_color} onChange={(e) => set('primary_color', e.target.value)} /><code>{v.primary_color}</code>
        <button type="button" className="icon-btn" title={`Reset to the default colour (${DEFAULT_COLOR})`} aria-label="Reset colour to default"
          disabled={v.primary_color.toLowerCase() === DEFAULT_COLOR} onClick={() => set('primary_color', DEFAULT_COLOR)}>↺</button></div>
      <label className="f" htmlFor="bwel">Welcome text</label><textarea className="i" id="bwel" rows={2} maxLength={1000} value={v.welcome_text} onChange={(e) => set('welcome_text', e.target.value)} />
      <div className="grid2" style={{ gap: 12 }}>
        <div><label className="f" htmlFor="bse">Support email</label><input className="i" id="bse" type="email" value={v.support_email} onChange={(e) => set('support_email', e.target.value)} /></div>
        <div><label className="f" htmlFor="bsu">Support URL (https://)</label><input className="i" id="bsu" value={v.support_url} onChange={(e) => set('support_url', e.target.value)} /></div>
      </div>
      <button className="btn primary" style={{ marginTop: 14 }}>Save branding</button>
    </form>
  )
}

export default function Admin() {
  const { status, has } = useApp()
  const [params] = useSearchParams()
  const [tab, setTab] = useState<string>(params.get('tab') || 'general')
  const canUsers = has('tenant.users'), canTargets = has('tenant.targets')
  const users = useAsync(() => canUsers ? api.get<U[]>('/admin/users') : Promise.resolve([] as U[]), [canUsers])
  const invites = useAsync(() => canUsers ? api.get<Inv[]>('/admin/invitations') : Promise.resolve([] as Inv[]), [canUsers])
  const targets = useAsync(() => api.get<T[]>('/admin/targets'), [])
  const audit = useAsync(() => has('tenant.audit') ? api.get<A[]>('/admin/audit?limit=50') : Promise.resolve([] as A[]), [])
  const [edit, setEdit] = useState<U | 'new' | null>(null)
  const [inviting, setInviting] = useState(false), [newLink, setNewLink] = useState<Inv | null>(null)
  const [host, setHost] = useState(''), [note, setNote] = useState(''), [err, setErr] = useState('')
  const addTarget = async (e: FormEvent) => {
    e.preventDefault(); setErr('')
    try { await api.post('/admin/targets', { host, note }); setHost(''); setNote(''); targets.reload() } catch (x) { setErr((x as Error).message) }
  }
  const tabs = [['general', 'Users, targets & branding', true], ['groups', 'Groups', has('tenant.groups')],
    ['access', 'Project access', has('tenant.assignments')], ['sso', 'Identity & SSO', has('tenant.sso')],
    ['storage', 'Storage & data residency', has('tenant.storage')], ['privacy', 'Privacy requests', has('tenant.users')],
    ['roles', 'Roles & permissions', true]] as const
  const visibleTabs = tabs.filter((t) => t[2])
  const current = visibleTabs.some((t) => t[0] === tab) ? tab : 'general'
  return (
    <>
      <div className="topbar"><h1>Administration</h1></div>
      <div className="tabs" role="tablist" style={{ display: 'flex', gap: 6, marginBottom: 16, flexWrap: 'wrap' }}>
        {visibleTabs.map(([k, label]) => <button key={k} role="tab" aria-selected={current === k} className={`btn sm${current === k ? ' primary' : ''}`} onClick={() => setTab(k)}>{label}</button>)}
      </div>
      {current === 'groups' && <Groups />}
      {current === 'access' && <Assignments />}
      {current === 'sso' && <SsoSettings />}
      {current === 'storage' && <StorageSettings />}
      {current === 'privacy' && <PrivacyRequests scope="org" />}
      {current === 'roles' && <RolesMatrix />}
      {current === 'general' && <>
      {status && (
        <div className="stats">
          <div className="stat"><div className="k">Version</div><div className="v" style={{ fontSize: 15 }}>{status.version} · {status.environment}</div></div>
          <div className="stat"><div className="k">Claude</div><div className="v" style={{ fontSize: 15, color: status.ai.available ? 'var(--ok)' : 'var(--warn)' }}>{status.ai.available ? status.ai.model : status.ai.configured ? 'unavailable' : 'not configured'}</div></div>
          <div className="stat"><div className="k">Workers online</div><div className="v">{status.workers.length}</div></div>
          <div className="stat"><div className="k">Your queue</div><div className="v" style={{ fontSize: 15 }}>{status.queue.running} running · {status.queue.queued} queued</div></div>
          <div className="stat"><div className="k">MySQL reports</div><div className="v" style={{ fontSize: 15, color: !status.report_store.enabled ? 'var(--ink-3)' : status.report_store.last_error && status.report_store.pending ? 'var(--warn)' : 'var(--ok)' }}>
            {!status.report_store.enabled ? 'disabled' : `${status.report_store.pending} pending`}</div></div>
        </div>
      )}
      {status?.report_store.enabled && !!status.report_store.pending && status.report_store.last_error && <Alert kind="warn">MySQL report store: {status.report_store.last_error}</Alert>}
      <div className="grid2">
        {canUsers && <div className="panel">
          <div className="row" style={{ justifyContent: 'space-between' }}><h2 style={{ margin: 0 }}>Users</h2>
            <div className="row"><button className="btn sm" onClick={() => setInviting(true)}>Invite</button><button className="btn sm primary" onClick={() => setEdit('new')}>+ Add user</button></div></div>
          <Alert>{users.error}</Alert>
          <table className="t resp" style={{ marginTop: 8 }}><thead><tr><th>User</th><th>Role</th><th>Last sign-in</th><th /></tr></thead>
            <tbody>{users.data?.map((u) => (
              <tr key={u.id}><td><b>{u.username}</b> <span className="muted small">{u.display_name}</span>{!u.is_active && <span className="tag">disabled</span>}</td>
                <td className="small" data-label="Role">{u.role_label}</td><td className="small muted" data-label="Last sign-in">{fmtDate(u.last_login_at)}</td>
                <td style={{ textAlign: 'right' }}><button className="btn sm" onClick={() => setEdit(u)}>Edit</button></td></tr>
            ))}</tbody></table>
          {!!invites.data?.filter((i) => i.state === 'pending').length && <>
            <h3 style={{ marginTop: 14 }}>Pending invitations</h3>
            <table className="t"><tbody>{invites.data.filter((i) => i.state === 'pending').map((i) => (
              <tr key={i.id}><td className="small">{i.email}</td><td className="small">{ROLE_LABELS[i.role]}</td><td className="small muted">expires {fmtDate(i.expires_at)}</td>
                <td style={{ textAlign: 'right' }}><button className="btn sm danger" onClick={async () => { await api.del(`/admin/invitations/${i.id}`); invites.reload() }}>Revoke</button></td></tr>))}</tbody></table>
          </>}
        </div>}
        <div className="panel">
          <h2>Authorised test targets</h2>
          <p className="muted small" style={{ marginTop: -6 }}>Your organization's browser tests can only reach these hosts (exact hostname match). Approvals are per organization. Private, loopback and cloud-metadata addresses are always blocked unless the operator has listed the host in <code>ALLOWED_PRIVATE_TARGETS</code>.</p>
          <Alert>{err || targets.error}</Alert>
          <table className="t resp"><tbody>{targets.data?.map((t) => (
            <tr key={t.id}><td className="mono">{t.host}{t.private_access && <span className="tag" style={{ marginLeft: 6 }}>private OK</span>}</td><td className="small muted">{t.note}</td>
              <td style={{ textAlign: 'right' }}>{canTargets && <button className="btn sm danger" onClick={async () => { if (confirm(`Remove ${t.host} from the allowlist?`)) { await api.del(`/admin/targets/${t.id}`); targets.reload() } }}>Remove</button>}</td></tr>
          ))}</tbody></table>
          {canTargets && <form className="row" style={{ marginTop: 12 }} onSubmit={addTarget}>
            <input className="i mono" style={{ flex: 2, minWidth: 160 }} placeholder="app.example.com" value={host} onChange={(e) => setHost(e.target.value)} required aria-label="Host" />
            <input className="i" style={{ flex: 2, minWidth: 120 }} placeholder="Note (owner, approval ref)" value={note} onChange={(e) => setNote(e.target.value)} aria-label="Note" />
            <button className="btn">Authorise</button>
          </form>}
        </div>
      </div>
      {has('tenant.settings') && <BrandingPanel />}
      {has('tenant.audit') && <div className="panel" style={{ padding: 0 }}>
        <div style={{ padding: '14px 20px 0' }}><h2>Audit log (latest 50)</h2></div>
        <table className="t resp"><thead><tr><th style={{ paddingLeft: 20 }}>When</th><th>User</th><th>Action</th><th>Target</th><th>IP</th></tr></thead>
          <tbody>{audit.data?.map((a) => (
            <tr key={a.id}><td style={{ paddingLeft: 20 }} className="small">{fmtDate(a.at)}</td><td data-label="User">{a.user ?? '—'}</td><td className="mono small" data-label="Action">{a.action}</td><td className="small" data-label="Target">{a.target}</td><td className="small muted" data-label="IP">{a.ip}</td></tr>
          ))}</tbody></table>
      </div>}
      {edit && <UserModal user={edit === 'new' ? undefined : edit} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); users.reload() }} />}
      </>}
      {inviting && <InviteModal onClose={() => setInviting(false)} onDone={(i) => { setInviting(false); setNewLink(i); invites.reload() }} />}
      {newLink && <Modal title="Activation link" onClose={() => setNewLink(null)}>
        <p className="small">Send this one-time link to <b>{newLink.email}</b>. It expires {fmtDate(newLink.expires_at)} and is not shown again.</p>
        <textarea className="i mono" readOnly rows={3} value={newLink.link} onFocus={(e) => e.target.select()} />
        <div className="row" style={{ justifyContent: 'flex-end', marginTop: 12 }}><button className="btn" onClick={() => navigator.clipboard?.writeText(newLink.link ?? '')}>Copy</button><button className="btn primary" onClick={() => setNewLink(null)}>Done</button></div>
      </Modal>}
    </>
  )
}

function RolesMatrix() {
  interface R { role: string; label: string; permissions: string[] }
  const d = useAsync(() => api.get<{ tenant_roles: R[]; project_roles: R[]; administrators: string }>('/access/roles'), [])
  if (!d.data) return <Alert>{d.error}</Alert>
  const perms = Array.from(new Set(d.data.project_roles.flatMap((r) => r.permissions))).sort()
  const tperms = Array.from(new Set(d.data.tenant_roles.flatMap((r) => r.permissions))).sort()
  return (
    <div className="grid2">
      <div className="panel" style={{ padding: 0 }}><div style={{ padding: '14px 20px 0' }}><h2>Organization roles</h2><p className="muted small">The role on the user's account. {d.data.administrators}</p></div>
        <table className="t"><thead><tr><th style={{ paddingLeft: 20 }}>Permission</th>{d.data.tenant_roles.map((r) => <th key={r.role} className="small">{r.label}</th>)}</tr></thead>
          <tbody>{tperms.map((p) => <tr key={p}><td className="mono small" style={{ paddingLeft: 20 }}>{p}</td>{d.data!.tenant_roles.map((r) => <td key={r.role}>{r.permissions.includes(p) ? '✓' : ''}</td>)}</tr>)}</tbody></table></div>
      <div className="panel" style={{ padding: 0 }}><div style={{ padding: '14px 20px 0' }}><h2>Project roles</h2><p className="muted small">Granted per project (or all projects) by assignments, directly or through groups. Effective permissions are the union of all active assignments. Custom roles are not available yet.</p></div>
        <table className="t"><thead><tr><th style={{ paddingLeft: 20 }}>Permission</th>{d.data.project_roles.map((r) => <th key={r.role} className="small">{r.label}</th>)}</tr></thead>
          <tbody>{perms.map((p) => <tr key={p}><td className="mono small" style={{ paddingLeft: 20 }}>{p}</td>{d.data!.project_roles.map((r) => <td key={r.role}>{r.permissions.includes(p) ? '✓' : ''}</td>)}</tr>)}</tbody></table></div>
    </div>
  )
}
