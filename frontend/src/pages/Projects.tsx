import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api, type Project } from '../api'
import { Alert, Badge, Modal, fmtDate, useApp, useAsync } from '../components/ui'

export function ProjectForm({ initial, onSaved, onClose }: { initial?: Project; onSaved: (p: Project) => void; onClose: () => void }) {
  const [name, setName] = useState(initial?.name ?? '')
  const [url, setUrl] = useState(initial?.base_url ?? 'https://')
  const [desc, setDesc] = useState(initial?.description ?? '')
  const [hosts, setHosts] = useState((initial?.extra_hosts ?? []).join('\n'))
  const [err, setErr] = useState('')
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr('')
    const body = { name, base_url: url, description: desc, extra_hosts: hosts.split(/[\n,]/).map((s) => s.trim()).filter(Boolean) }
    try { onSaved(initial ? await api.put<Project>(`/projects/${initial.id}`, body) : await api.post<Project>('/projects', body)) }
    catch (x) { setErr((x as Error).message) }
  }
  return (
    <Modal title={initial ? 'Edit project' : 'New project'} onClose={onClose}>
      <form onSubmit={submit}>
        <Alert>{err}</Alert>
        <label className="f" htmlFor="pn">Name</label>
        <input className="i" id="pn" value={name} onChange={(e) => setName(e.target.value)} required maxLength={120} />
        <label className="f" htmlFor="pu">Application base URL</label>
        <input className="i mono" id="pu" value={url} onChange={(e) => setUrl(e.target.value)} required />
        <div className="hint">The host must be on the authorised target allowlist (Administration → Targets) before tests can run.</div>
        <label className="f" htmlFor="ph">Additional hosts the app loads (optional)</label>
        <textarea className="i mono" id="ph" style={{ minHeight: 60 }} value={hosts} onChange={(e) => setHosts(e.target.value)} placeholder={'cdn.example.com\nauth.example.com:8443'} />
        <div className="hint">One per line. Requests to any host not listed here, and not allowlisted, are blocked.</div>
        <label className="f" htmlFor="pd">Description</label>
        <textarea className="i" id="pd" value={desc} onChange={(e) => setDesc(e.target.value)} />
        <div className="row" style={{ marginTop: 16, justifyContent: 'flex-end' }}>
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary">Save project</button>
        </div>
      </form>
    </Modal>
  )
}

export default function Projects() {
  const { can } = useApp()
  const nav = useNavigate()
  const { data, error } = useAsync(() => api.get<Project[]>('/projects'), [])
  const [creating, setCreating] = useState(false)
  return (
    <>
      <div className="topbar">
        <div><h1>Projects</h1><div className="muted small">Web applications under test</div></div>
        {can('tester') && <button className="btn primary" onClick={() => setCreating(true)}>+ New project</button>}
      </div>
      <Alert>{error}</Alert>
      <div className="panel" style={{ padding: 0 }}>
        {data && data.length === 0 && <div className="empty">No projects yet.{can('tester') && ' Create one to start testing.'}</div>}
        {data && data.length > 0 && (
          <table className="t">
            <thead><tr><th>Project</th><th>Base URL</th><th>Test cases</th><th>Last run</th></tr></thead>
            <tbody>
              {data.map((p) => (
                <tr key={p.id} className="click" onClick={() => nav(`/projects/${p.id}`)}>
                  <td><Link to={`/projects/${p.id}`} onClick={(e) => e.stopPropagation()}><b>{p.name}</b></Link>
                    {p.policy_problems.length > 0 && <div className="small" style={{ color: 'var(--warn)' }}>Target not authorised</div>}</td>
                  <td className="mono">{p.base_url}</td>
                  <td>{p.case_count}</td>
                  <td>{p.last_run ? <><Badge s={p.last_run.status} /> <span className="muted small">{fmtDate(p.last_run.created_at)}</span></> : <span className="muted">—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {creating && <ProjectForm onClose={() => setCreating(false)} onSaved={(p) => nav(`/projects/${p.id}`)} />}
    </>
  )
}
