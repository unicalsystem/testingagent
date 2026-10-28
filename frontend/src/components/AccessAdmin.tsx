import { useMemo, useState, type FormEvent } from 'react'
import { ROLE_LABELS, api, type TenantRole } from '../api'
import { Alert, Modal, fmtDate, useAsync } from './ui'

interface Person { id: number; username: string; display_name: string; email: string; administrator: boolean }
interface GroupRef { id: number; name: string; is_active: boolean }
interface Directory { users: Person[]; groups: GroupRef[]; projects: { id: number; name: string }[] }
interface Group { id: number; name: string; description: string; is_active: boolean; members: { id: number; username: string; display_name: string; email: string }[] }
interface Assignment {
  id: number; principal_type: 'user' | 'group'; user: Person | null; group: GroupRef | null; scope: 'all' | 'project'
  project: { id: number; name: string } | null; role: TenantRole; role_label: string; starts_at: string | null; ends_at: string | null
  timezone: string; state: 'active' | 'future' | 'expired' | 'revoked'; reason: string; created_by: string | null
}

const PROJECT_ROLES: TenantRole[] = ['project_manager', 'qa_engineer', 'developer', 'auditor']
const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Kolkata'
/** An end at local midnight is shown as the end of the previous day (that is what a date-only end means). */
function fmtEnd(iso: string) {
  const d = new Date(iso)
  if (d.getHours() === 0 && d.getMinutes() === 0) { const prev = new Date(d.getTime() - 60000); return `end of ${prev.toLocaleDateString('en-IN', { dateStyle: 'medium' })}` }
  return fmtDate(iso)
}

const STATE_COLOR: Record<string, string> = { active: 'var(--ok)', future: 'var(--run)', expired: 'var(--ink-3)', revoked: 'var(--bad)' }

// ---------------------------------------------------------------------------------------------- groups
export function Groups() {
  const groups = useAsync(() => api.get<Group[]>('/access/groups'), [])
  const dir = useAsync(() => api.get<Directory>('/access/directory'), [])
  const [edit, setEdit] = useState<Group | 'new' | null>(null)
  const [err, setErr] = useState('')
  const [adding, setAdding] = useState<Record<number, string>>({})
  const act = async (fn: () => Promise<unknown>) => { setErr(''); try { await fn(); groups.reload() } catch (x) { setErr((x as Error).message) } }
  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: 'space-between' }}><h2 style={{ margin: 0 }}>Groups</h2><button className="btn sm primary" onClick={() => setEdit('new')}>+ New group</button></div>
      <p className="muted small">Groups get project access through <b>Project access</b> assignments; every active member receives it. Deactivating a group suspends that access.</p>
      <Alert>{err || groups.error}</Alert>
      {groups.data?.length === 0 && <div className="muted small">No groups yet (for example QA Team, Development Team, Project Auditors).</div>}
      {groups.data?.map((g) => (
        <div key={g.id} className="panel" style={{ marginBottom: 10, background: 'var(--line-2)' }}>
          <div className="row" style={{ justifyContent: 'space-between' }}>
            <div><b>{g.name}</b> {!g.is_active && <span className="tag">inactive</span>}<div className="muted small">{g.description}</div></div>
            <div className="row"><button className="btn sm" onClick={() => setEdit(g)}>Edit</button>
              <button className="btn sm danger" onClick={() => { if (confirm(`Delete group ${g.name}? Its members lose the access it granted.`)) act(() => api.del(`/access/groups/${g.id}`)) }}>Delete</button></div>
          </div>
          <div className="row" style={{ flexWrap: 'wrap', gap: 6, marginTop: 8 }}>
            {g.members.map((m) => <span key={m.id} className="tag">{m.username} <a href="#" aria-label={`Remove ${m.username}`} onClick={(e) => { e.preventDefault(); act(() => api.del(`/access/groups/${g.id}/members/${m.id}`)) }}>✕</a></span>)}
            {g.members.length === 0 && <span className="muted small">No members</span>}
          </div>
          <div className="row" style={{ marginTop: 8 }}>
            <select className="i" style={{ maxWidth: 280 }} value={adding[g.id] ?? ''} onChange={(e) => setAdding({ ...adding, [g.id]: e.target.value })} aria-label="Add member">
              <option value="">Add a member…</option>
              {dir.data?.users.filter((u) => !g.members.some((m) => m.id === u.id)).map((u) => <option key={u.id} value={u.id}>{u.username}{u.display_name ? ` (${u.display_name})` : ''}</option>)}
            </select>
            <button className="btn sm" disabled={!adding[g.id]} onClick={() => act(async () => { await api.post(`/access/groups/${g.id}/members`, { user_ids: [Number(adding[g.id])] }); setAdding({ ...adding, [g.id]: '' }) })}>Add</button>
          </div>
        </div>
      ))}
      {edit && <GroupModal group={edit === 'new' ? undefined : edit} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); groups.reload() }} />}
    </div>
  )
}

function GroupModal({ group, onClose, onSaved }: { group?: Group; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState(group?.name ?? ''), [desc, setDesc] = useState(group?.description ?? ''), [active, setActive] = useState(group?.is_active ?? true), [err, setErr] = useState('')
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr('')
    try { if (group) await api.put(`/access/groups/${group.id}`, { name, description: desc, is_active: active }); else await api.post('/access/groups', { name, description: desc, is_active: active }); onSaved() }
    catch (x) { setErr((x as Error).message) }
  }
  return (
    <Modal title={group ? `Edit ${group.name}` : 'New group'} onClose={onClose}>
      <form onSubmit={submit}>
        <Alert>{err}</Alert>
        <label className="f" htmlFor="gn">Name</label><input className="i" id="gn" value={name} onChange={(e) => setName(e.target.value)} required />
        <label className="f" htmlFor="gd">Description</label><input className="i" id="gd" value={desc} onChange={(e) => setDesc(e.target.value)} />
        <label className="row" style={{ marginTop: 12, gap: 8 }}><input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} /> Active</label>
        <div className="row" style={{ marginTop: 16, justifyContent: 'flex-end' }}><button type="button" className="btn" onClick={onClose}>Cancel</button><button className="btn primary">Save</button></div>
      </form>
    </Modal>
  )
}

// ----------------------------------------------------------------------------------------- assignments
export function Assignments({ projectId, canAllProjects = true }: { projectId?: number; canAllProjects?: boolean }) {
  const [state, setState] = useState('all'), [q, setQ] = useState('')
  const list = useAsync(() => api.get<Assignment[]>(`/access/assignments?state=${state}${projectId ? `&project_id=${projectId}` : ''}&q=${encodeURIComponent(q)}`), [state, q, projectId])
  const dir = useAsync(() => api.get<Directory>(`/access/directory${projectId ? `?project_id=${projectId}` : ''}`), [projectId])
  const [adding, setAdding] = useState(false), [extend, setExtend] = useState<Assignment | null>(null), [err, setErr] = useState('')
  const act = async (fn: () => Promise<unknown>) => { setErr(''); try { await fn(); list.reload() } catch (x) { setErr((x as Error).message) } }
  return (
    <div className="panel" style={{ padding: 0 }}>
      <div className="row" style={{ padding: '14px 20px 0', justifyContent: 'space-between' }}>
        <h2 style={{ margin: 0 }}>{projectId ? 'Members & access' : 'Project access assignments'}</h2>
        <div className="row">
          <input className="i" style={{ width: 200 }} placeholder="Search user, group, project" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search" />
          <select className="i" style={{ width: 130 }} value={state} onChange={(e) => setState(e.target.value)} aria-label="Status">
            <option value="all">All</option><option value="active">Active</option><option value="future">Future</option><option value="expired">Expired</option><option value="revoked">Revoked</option>
          </select>
          <button className="btn sm primary" onClick={() => setAdding(true)}>+ Grant access</button>
        </div>
      </div>
      <p className="muted small" style={{ padding: '6px 20px 0' }}>Owners and administrators always have access to every project. Everyone else needs an active assignment (direct or through a group). Access is checked on every request: when an assignment ends or is revoked, it stops immediately.</p>
      <div style={{ padding: '0 20px' }}><Alert>{err || list.error}</Alert></div>
      <table className="t"><thead><tr><th style={{ paddingLeft: 20 }}>Who</th><th>Project</th><th>Role</th><th>From</th><th>Until</th><th>Status</th><th /></tr></thead>
        <tbody>{list.data?.length === 0 && <tr><td colSpan={7} className="muted" style={{ paddingLeft: 20 }}>No assignments.</td></tr>}
          {list.data?.map((a) => (
            <tr key={a.id}>
              <td style={{ paddingLeft: 20 }}>{a.user ? <><b>{a.user.username}</b><div className="muted small">{a.user.email}</div></> : <><span className="tag">group</span> <b>{a.group?.name}</b>{a.group && !a.group.is_active && <span className="muted small"> (inactive)</span>}</>}</td>
              <td>{a.scope === 'all' ? <b>All projects</b> : a.project?.name}</td>
              <td className="small">{a.role_label}</td>
              <td className="small">{a.starts_at ? fmtDate(a.starts_at) : '—'}</td>
              <td className="small">{a.ends_at ? fmtEnd(a.ends_at) : 'no end'}</td>
              <td><span className="small" style={{ color: STATE_COLOR[a.state] }}>{a.state}</span>{a.reason && <div className="muted small">{a.reason}</div>}</td>
              <td style={{ textAlign: 'right', paddingRight: 20, whiteSpace: 'nowrap' }}>{a.state !== 'revoked' && <>
                <button className="btn sm" onClick={() => setExtend(a)}>Change</button>{' '}
                <button className="btn sm danger" onClick={() => { if (confirm('Revoke this access now?')) act(() => api.post(`/access/assignments/${a.id}/revoke`)) }}>Revoke</button></>}</td>
            </tr>))}
        </tbody></table>
      {adding && dir.data && <GrantModal dir={dir.data} projectId={projectId} canAllProjects={canAllProjects && !projectId} onClose={() => setAdding(false)} onSaved={() => { setAdding(false); list.reload() }} />}
      {extend && <ChangeModal a={extend} onClose={() => setExtend(null)} onSaved={() => { setExtend(null); list.reload() }} />}
    </div>
  )
}

function DateFields({ v, set }: { v: Record<string, string>; set: (k: string, x: string) => void }) {
  return (
    <div className="grid2" style={{ gap: 10 }}>
      <div><label className="f">Start date (optional)</label><div className="row"><input className="i" type="date" value={v.start_date} onChange={(e) => set('start_date', e.target.value)} /><input className="i" type="time" value={v.start_time} onChange={(e) => set('start_time', e.target.value)} aria-label="Start time" /></div></div>
      <div><label className="f">End date (optional)</label><div className="row"><input className="i" type="date" value={v.end_date} onChange={(e) => set('end_date', e.target.value)} /><input className="i" type="time" value={v.end_time} onChange={(e) => set('end_time', e.target.value)} aria-label="End time" /></div>
        <div className="hint">Date without a time: access lasts until the end of that day.</div></div>
    </div>
  )
}

function dates(v: Record<string, string>) {
  return { start_date: v.start_date || undefined, start_time: v.start_date && v.start_time ? v.start_time : undefined,
    end_date: v.end_date || undefined, end_time: v.end_date && v.end_time ? v.end_time : undefined, timezone: v.timezone }
}

function GrantModal({ dir, projectId, canAllProjects, onClose, onSaved }: { dir: Directory; projectId?: number; canAllProjects: boolean; onClose: () => void; onSaved: () => void }) {
  const [v, setV] = useState<Record<string, string>>({ who: 'user', user_id: '', group_id: '', scope: 'project', project_id: projectId ? String(projectId) : '', role: 'qa_engineer',
    start_date: '', start_time: '', end_date: '', end_time: '', timezone: TZ, reason: '' })
  const [err, setErr] = useState('')
  const set = (k: string, x: string) => setV((o) => ({ ...o, [k]: x }))
  const people = useMemo(() => dir.users.filter((u) => !u.administrator), [dir])
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr('')
    let confirm_all_projects = false
    if (v.scope === 'all') {
      if (!window.confirm('All projects: access to EVERY current and future project of this organization (never other organizations). Continue?')) return
      confirm_all_projects = true
    }
    try {
      await api.post('/access/assignments', { user_id: v.who === 'user' ? Number(v.user_id) : undefined, group_id: v.who === 'group' ? Number(v.group_id) : undefined,
        scope: v.scope, project_id: v.scope === 'project' ? Number(v.project_id) : undefined, role: v.role, reason: v.reason, confirm_all_projects, ...dates(v) })
      onSaved()
    } catch (x) { setErr((x as Error).message) }
  }
  return (
    <Modal title="Grant project access" onClose={onClose}>
      <form onSubmit={submit}>
        <Alert>{err}</Alert>
        <div className="row"><label className="row" style={{ gap: 6 }}><input type="radio" checked={v.who === 'user'} onChange={() => set('who', 'user')} /> User</label>
          <label className="row" style={{ gap: 6 }}><input type="radio" checked={v.who === 'group'} onChange={() => set('who', 'group')} /> Group</label></div>
        {v.who === 'user'
          ? <select className="i" value={v.user_id} onChange={(e) => set('user_id', e.target.value)} required aria-label="User"><option value="">Choose a user…</option>{people.map((u) => <option key={u.id} value={u.id}>{u.username}{u.email ? ` · ${u.email}` : ''}</option>)}</select>
          : <select className="i" value={v.group_id} onChange={(e) => set('group_id', e.target.value)} required aria-label="Group"><option value="">Choose a group…</option>{dir.groups.map((g) => <option key={g.id} value={g.id}>{g.name}{g.is_active ? '' : ' (inactive)'}</option>)}</select>}
        <label className="f">Projects</label>
        {canAllProjects && <div className="row"><label className="row" style={{ gap: 6 }}><input type="radio" checked={v.scope === 'project'} onChange={() => set('scope', 'project')} /> A specific project</label>
          <label className="row" style={{ gap: 6 }}><input type="radio" checked={v.scope === 'all'} onChange={() => set('scope', 'all')} /> <b>All projects</b> (current and future, this organization only)</label></div>}
        {v.scope === 'project' && <select className="i" value={v.project_id} onChange={(e) => set('project_id', e.target.value)} required disabled={!!projectId} aria-label="Project"><option value="">Choose a project…</option>{dir.projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select>}
        <label className="f" htmlFor="ar">Role</label>
        <select className="i" id="ar" value={v.role} onChange={(e) => set('role', e.target.value)}>{PROJECT_ROLES.map((r) => <option key={r} value={r}>{ROLE_LABELS[r]}</option>)}</select>
        <DateFields v={v} set={set} />
        <label className="f" htmlFor="atz">Time zone</label><input className="i" id="atz" value={v.timezone} onChange={(e) => set('timezone', e.target.value)} />
        <label className="f" htmlFor="are">Reason / ticket (optional)</label><input className="i" id="are" value={v.reason} onChange={(e) => set('reason', e.target.value)} maxLength={300} />
        <div className="row" style={{ marginTop: 16, justifyContent: 'flex-end' }}><button type="button" className="btn" onClick={onClose}>Cancel</button><button className="btn primary">Grant</button></div>
      </form>
    </Modal>
  )
}

function ChangeModal({ a, onClose, onSaved }: { a: Assignment; onClose: () => void; onSaved: () => void }) {
  const [v, setV] = useState<Record<string, string>>({ role: a.role, start_date: '', start_time: '', end_date: '', end_time: '', timezone: a.timezone || TZ, clear: '' })
  const [err, setErr] = useState('')
  const set = (k: string, x: string) => setV((o) => ({ ...o, [k]: x }))
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr('')
    try { await api.patch(`/access/assignments/${a.id}`, { role: v.role, clear_end: v.clear === '1', ...dates(v) }); onSaved() } catch (x) { setErr((x as Error).message) }
  }
  return (
    <Modal title="Change access" onClose={onClose}>
      <form onSubmit={submit}>
        <Alert>{err}</Alert>
        <p className="small muted">{a.user ? a.user.username : `Group ${a.group?.name}`} · {a.scope === 'all' ? 'All projects' : a.project?.name} · now until {a.ends_at ? fmtEnd(a.ends_at) : 'no end'}</p>
        <label className="f">Role</label>
        <select className="i" value={v.role} onChange={(e) => set('role', e.target.value)}>{PROJECT_ROLES.map((r) => <option key={r} value={r}>{ROLE_LABELS[r]}</option>)}</select>
        <DateFields v={v} set={set} />
        <label className="row" style={{ gap: 6, marginTop: 10 }}><input type="checkbox" checked={v.clear === '1'} onChange={(e) => set('clear', e.target.checked ? '1' : '')} /> Remove the end date (no expiry)</label>
        <div className="hint">Leave dates empty to keep them. Extending an expired assignment renews it.</div>
        <div className="row" style={{ marginTop: 16, justifyContent: 'flex-end' }}><button type="button" className="btn" onClick={onClose}>Cancel</button><button className="btn primary">Save</button></div>
      </form>
    </Modal>
  )
}
