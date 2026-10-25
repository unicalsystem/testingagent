import { useState, type FormEvent } from 'react'
import { api } from '../api'
import { Alert, Modal, fmtDate, useApp, useAsync } from '../components/ui'

interface U { id: number; username: string; display_name: string; role: string; is_active: boolean; must_change_password: boolean; last_login_at: string | null }
interface T { id: number; host: string; note: string; created_at: string; private_access: boolean }
interface A { id: number; at: string; user: string | null; action: string; target: string; ip: string }

function UserModal({ user, onClose, onSaved }: { user?: U; onClose: () => void; onSaved: () => void }) {
  const [username, setUsername] = useState(user?.username ?? ''), [dn, setDn] = useState(user?.display_name ?? '')
  const [role, setRole] = useState(user?.role ?? 'tester'), [pw, setPw] = useState(''), [active, setActive] = useState(user?.is_active ?? true)
  const [err, setErr] = useState('')
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr('')
    try {
      if (user) await api.patch(`/admin/users/${user.id}`, { display_name: dn, role, is_active: active, ...(pw ? { password: pw } : {}) })
      else await api.post('/admin/users', { username, display_name: dn, role, password: pw })
      onSaved()
    } catch (x) { setErr((x as Error).message) }
  }
  return (
    <Modal title={user ? `Edit ${user.username}` : 'Add user'} onClose={onClose}>
      <form onSubmit={submit}>
        <Alert>{err}</Alert>
        {!user && <><label className="f" htmlFor="un">Username</label><input className="i" id="un" value={username} onChange={(e) => setUsername(e.target.value)} required minLength={3} /></>}
        <label className="f" htmlFor="dn">Display name</label><input className="i" id="dn" value={dn} onChange={(e) => setDn(e.target.value)} />
        <label className="f" htmlFor="ro">Role</label>
        <select className="i" id="ro" value={role} onChange={(e) => setRole(e.target.value)}>
          <option value="viewer">Viewer: read projects, runs and reports</option>
          <option value="tester">Tester: also create and run tests, use AI</option>
          <option value="admin">Admin: also manage users and targets</option>
        </select>
        <label className="f" htmlFor="pw">{user ? 'Reset password (optional)' : 'Initial password'}</label>
        <input className="i" id="pw" type="password" autoComplete="new-password" value={pw} onChange={(e) => setPw(e.target.value)} minLength={12} required={!user} />
        <div className="hint">At least 12 characters with letters and digits. The user must change it at first sign-in.</div>
        {user && <label className="row" style={{ marginTop: 12 }}><input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} /> Account active</label>}
        <div className="row" style={{ marginTop: 16, justifyContent: 'flex-end' }}><button type="button" className="btn" onClick={onClose}>Cancel</button><button className="btn primary">Save</button></div>
      </form>
    </Modal>
  )
}

export default function Admin() {
  const { status } = useApp()
  const users = useAsync(() => api.get<U[]>('/admin/users'), [])
  const targets = useAsync(() => api.get<T[]>('/admin/targets'), [])
  const audit = useAsync(() => api.get<A[]>('/admin/audit?limit=50'), [])
  const [edit, setEdit] = useState<U | 'new' | null>(null)
  const [host, setHost] = useState(''), [note, setNote] = useState(''), [err, setErr] = useState('')
  const addTarget = async (e: FormEvent) => {
    e.preventDefault(); setErr('')
    try { await api.post('/admin/targets', { host, note }); setHost(''); setNote(''); targets.reload() } catch (x) { setErr((x as Error).message) }
  }
  return (
    <>
      <div className="topbar"><h1>Administration</h1></div>
      {status && (
        <div className="stats">
          <div className="stat"><div className="k">Version</div><div className="v" style={{ fontSize: 15 }}>{status.version} · {status.environment}</div></div>
          <div className="stat"><div className="k">Claude</div><div className="v" style={{ fontSize: 15, color: status.ai.available ? 'var(--ok)' : 'var(--warn)' }}>{status.ai.available ? status.ai.model : status.ai.configured ? 'unavailable' : 'not configured'}</div></div>
          <div className="stat"><div className="k">Workers online</div><div className="v">{status.workers.length}</div></div>
          <div className="stat"><div className="k">Queue</div><div className="v" style={{ fontSize: 15 }}>{status.queue.running} running · {status.queue.queued} queued</div></div>
          <div className="stat"><div className="k">MySQL reports</div><div className="v" style={{ fontSize: 15, color: !status.report_store.enabled ? 'var(--ink-3)' : status.report_store.last_error && status.report_store.pending ? 'var(--warn)' : 'var(--ok)' }}>
            {!status.report_store.enabled ? 'disabled' : `${status.report_store.pending} pending`}</div></div>
        </div>
      )}
      {status?.report_store.enabled && !!status.report_store.pending && status.report_store.last_error && <Alert kind="warn">MySQL report store: {status.report_store.last_error}</Alert>}
      <div className="grid2">
        <div className="panel">
          <div className="row" style={{ justifyContent: 'space-between' }}><h2 style={{ margin: 0 }}>Users</h2><button className="btn sm primary" onClick={() => setEdit('new')}>+ Add user</button></div>
          <Alert>{users.error}</Alert>
          <table className="t" style={{ marginTop: 8 }}><thead><tr><th>User</th><th>Role</th><th>Last sign-in</th><th /></tr></thead>
            <tbody>{users.data?.map((u) => (
              <tr key={u.id}><td><b>{u.username}</b> <span className="muted small">{u.display_name}</span>{!u.is_active && <span className="tag">disabled</span>}</td>
                <td>{u.role}</td><td className="small muted">{fmtDate(u.last_login_at)}</td>
                <td style={{ textAlign: 'right' }}><button className="btn sm" onClick={() => setEdit(u)}>Edit</button></td></tr>
            ))}</tbody></table>
        </div>
        <div className="panel">
          <h2>Authorised test targets</h2>
          <p className="muted small" style={{ marginTop: -6 }}>The browser worker can only reach these hosts (exact hostname match). Private, loopback and cloud-metadata addresses are always blocked unless the operator has listed the host in <code>ALLOWED_PRIVATE_TARGETS</code>.</p>
          <Alert>{err || targets.error}</Alert>
          <table className="t"><tbody>{targets.data?.map((t) => (
            <tr key={t.id}><td className="mono">{t.host}{t.private_access && <span className="tag" style={{ marginLeft: 6 }}>private OK</span>}</td><td className="small muted">{t.note}</td>
              <td style={{ textAlign: 'right' }}><button className="btn sm danger" onClick={async () => { if (confirm(`Remove ${t.host} from the allowlist?`)) { await api.del(`/admin/targets/${t.id}`); targets.reload() } }}>Remove</button></td></tr>
          ))}</tbody></table>
          <form className="row" style={{ marginTop: 12 }} onSubmit={addTarget}>
            <input className="i mono" style={{ flex: 2, minWidth: 160 }} placeholder="app.example.com" value={host} onChange={(e) => setHost(e.target.value)} required aria-label="Host" />
            <input className="i" style={{ flex: 2, minWidth: 120 }} placeholder="Note (owner, approval ref)" value={note} onChange={(e) => setNote(e.target.value)} aria-label="Note" />
            <button className="btn">Authorise</button>
          </form>
        </div>
      </div>
      <div className="panel" style={{ padding: 0 }}>
        <div style={{ padding: '14px 20px 0' }}><h2>Audit log (latest 50)</h2></div>
        <table className="t"><thead><tr><th style={{ paddingLeft: 20 }}>When</th><th>User</th><th>Action</th><th>Target</th><th>IP</th></tr></thead>
          <tbody>{audit.data?.map((a) => (
            <tr key={a.id}><td style={{ paddingLeft: 20 }} className="small">{fmtDate(a.at)}</td><td>{a.user ?? '—'}</td><td className="mono small">{a.action}</td><td className="small">{a.target}</td><td className="small muted">{a.ip}</td></tr>
          ))}</tbody></table>
      </div>
      {edit && <UserModal user={edit === 'new' ? undefined : edit} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); users.reload() }} />}
    </>
  )
}
