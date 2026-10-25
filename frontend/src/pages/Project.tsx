import { useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, type Project, type Run, type Step, type TestCase } from '../api'
import { Alert, Badge, Modal, fmtDate, fmtMs, useApp, useAsync } from '../components/ui'
import { ProjectForm } from './Projects'

interface Generated { test_cases: { name: string; description: string; tags: string[]; steps: Step[] }[]; rejected: { index: number; error: string }[]; model: string }

function GenerateModal({ project, onClose, onSaved }: { project: Project; onClose: () => void; onSaved: () => void }) {
  const [reqText, setReq] = useState(''), [notes, setNotes] = useState(''), [count, setCount] = useState(3)
  const [busy, setBusy] = useState(false), [err, setErr] = useState('')
  const [out, setOut] = useState<Generated | null>(null)
  const [pick, setPick] = useState<Record<number, boolean>>({})
  const gen = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setErr(''); setOut(null)
    try {
      const r = await api.post<Generated>(`/projects/${project.id}/ai/generate`, { requirement: reqText, page_notes: notes, count })
      setOut(r); setPick(Object.fromEntries(r.test_cases.map((_, i) => [i, true])))
    } catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  const save = async () => {
    if (!out) return
    setBusy(true); setErr('')
    try {
      for (const [i, c] of out.test_cases.entries()) if (pick[i]) await api.post(`/projects/${project.id}/cases`, { ...c, source: 'ai' })
      onSaved()
    } catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  return (
    <Modal title="Generate test cases with Claude" onClose={onClose}>
      <Alert>{err}</Alert>
      {!out && (
        <form onSubmit={gen}>
          <label className="f" htmlFor="rq">Requirement or user story</label>
          <textarea className="i" id="rq" value={reqText} onChange={(e) => setReq(e.target.value)} required minLength={10}
            placeholder="e.g. A registered user signs in with username and password. Invalid credentials show an error message." />
          <label className="f" htmlFor="pn">Page and element notes (optional)</label>
          <textarea className="i" id="pn" value={notes} onChange={(e) => setNotes(e.target.value)}
            placeholder="e.g. Login form fields have ids #username and #password; the error has data-testid=login-error" />
          <div className="hint">Do not paste real passwords. Store them as project secrets; Claude only sees the secret names.</div>
          <label className="f" htmlFor="ct">Number of test cases</label>
          <input className="i" id="ct" type="number" min={1} max={10} value={count} onChange={(e) => setCount(+e.target.value)} style={{ width: 100 }} />
          <div className="row" style={{ marginTop: 16, justifyContent: 'flex-end' }}>
            <button type="button" className="btn" onClick={onClose}>Cancel</button>
            <button className="btn primary" disabled={busy}>{busy ? 'Generating…' : 'Generate drafts'}</button>
          </div>
        </form>
      )}
      {out && (
        <>
          <p className="muted small">Review the drafts from {out.model}. Only the ones you select are saved; you can edit them afterwards.</p>
          {out.rejected.length > 0 && <Alert kind="warn">{out.rejected.length} draft(s) were discarded because they used invalid steps.</Alert>}
          {out.test_cases.map((c, i) => (
            <label key={i} className="panel" style={{ display: 'block', cursor: 'pointer', marginBottom: 10 }}>
              <div className="row"><input type="checkbox" checked={!!pick[i]} onChange={(e) => setPick({ ...pick, [i]: e.target.checked })} /><b>{c.name}</b></div>
              <div className="small muted" style={{ margin: '4px 0 6px 22px' }}>{c.description}</div>
              <ol className="small" style={{ margin: '0 0 0 22px' }}>{c.steps.map((s, j) => <li key={j}><code>{s.action}</code> {String(s.description ?? s.selector ?? s.url ?? '')}</li>)}</ol>
            </label>
          ))}
          <div className="row" style={{ justifyContent: 'flex-end' }}>
            <button className="btn" onClick={() => setOut(null)}>Back</button>
            <button className="btn primary" onClick={save} disabled={busy || !Object.values(pick).some(Boolean)}>Save selected</button>
          </div>
        </>
      )}
    </Modal>
  )
}

function Secrets({ pid }: { pid: string }) {
  const { can } = useApp()
  const { data, reload, error } = useAsync(() => api.get<{ name: string; updated_at: string }[]>(`/projects/${pid}/secrets`), [pid])
  const [name, setName] = useState(''), [val, setVal] = useState(''), [err, setErr] = useState('')
  const add = async (e: FormEvent) => {
    e.preventDefault(); setErr('')
    try { await api.put(`/projects/${pid}/secrets`, { name, value: val }); setName(''); setVal(''); reload() } catch (x) { setErr((x as Error).message) }
  }
  return (
    <div className="panel">
      <h2>Secrets</h2>
      <p className="muted small" style={{ marginTop: -6 }}>Encrypted at rest and never shown again. Use in steps as <code>{'{{secret:NAME}}'}</code>. Values are masked in logs and removed from traces.</p>
      <Alert>{error || err}</Alert>
      {data?.length ? (
        <table className="t"><tbody>{data.map((s) => (
          <tr key={s.name}><td className="mono">{s.name}</td><td className="muted small">updated {fmtDate(s.updated_at)}</td>
            <td style={{ textAlign: 'right' }}>{can('tester') && <button className="btn sm danger" onClick={async () => { if (confirm(`Delete secret ${s.name}?`)) { await api.del(`/projects/${pid}/secrets/${s.name}`); reload() } }}>Delete</button>}</td></tr>
        ))}</tbody></table>
      ) : <div className="muted small">No secrets.</div>}
      {can('tester') && (
        <form className="row" onSubmit={add} style={{ marginTop: 12 }}>
          <input className="i mono" style={{ flex: 1, minWidth: 140 }} placeholder="NAME" value={name} onChange={(e) => setName(e.target.value.toUpperCase())} pattern="[A-Za-z_][A-Za-z0-9_]*" required aria-label="Secret name" />
          <input className="i" style={{ flex: 2, minWidth: 160 }} type="password" placeholder="value" value={val} onChange={(e) => setVal(e.target.value)} required autoComplete="off" aria-label="Secret value" />
          <button className="btn">Save secret</button>
        </form>
      )}
    </div>
  )
}

export default function ProjectPage() {
  const { pid = '' } = useParams()
  const { can, status } = useApp()
  const nav = useNavigate()
  const proj = useAsync(() => api.get<Project>(`/projects/${pid}`), [pid])
  const cases = useAsync(() => api.get<TestCase[]>(`/projects/${pid}/cases`), [pid])
  const runs = useAsync(() => api.get<Run[]>(`/projects/${pid}/runs?limit=25`), [pid])
  const [editing, setEditing] = useState(false), [gen, setGen] = useState(false), [err, setErr] = useState('')
  const p = proj.data
  const run = async (cid: number) => {
    setErr('')
    try { const r = await api.post<Run>(`/cases/${cid}/runs`); nav(`/runs/${r.id}`) } catch (x) { setErr((x as Error).message) }
  }
  const runAll = async () => {
    setErr('')
    try { for (const c of cases.data ?? []) await api.post(`/cases/${c.id}/runs`); runs.reload(); cases.reload() } catch (x) { setErr((x as Error).message) }
  }
  if (proj.error) return <Alert>{proj.error}</Alert>
  if (!p) return null
  const aiOk = status?.ai.available
  return (
    <>
      <div className="topbar">
        <div><div className="crumbs"><Link to="/projects">Projects</Link> /</div><h1>{p.name}</h1>
          <div className="mono small muted">{p.base_url}</div></div>
        <div className="row">
          {can('tester') && <button className="btn" onClick={() => setEditing(true)}>Edit</button>}
          {can('tester') && <button className="btn" onClick={() => setGen(true)} disabled={!aiOk} title={aiOk ? '' : 'Claude is not available'}>✦ Generate with AI</button>}
          {can('tester') && <Link className="btn" to={`/projects/${p.id}/cases/new`}>+ Test case</Link>}
          {can('tester') && !!cases.data?.length && <button className="btn primary" onClick={runAll} disabled={p.policy_problems.length > 0}>Run all</button>}
        </div>
      </div>
      {p.policy_problems.length > 0 && <Alert kind="warn">Tests cannot run: {p.policy_problems.join('; ')}. An administrator must authorise the target host.</Alert>}
      <Alert>{err}</Alert>
      {p.description && <p className="muted" style={{ marginTop: -6 }}>{p.description}</p>}
      <div className="panel" style={{ padding: 0 }}>
        <div style={{ padding: '14px 20px 0' }}><h2>Test cases</h2></div>
        {cases.data?.length === 0 && <div className="empty">No test cases yet.</div>}
        {!!cases.data?.length && (
          <table className="t">
            <thead><tr><th style={{ paddingLeft: 20 }}>Name</th><th>Steps</th><th>Last result</th><th /></tr></thead>
            <tbody>{cases.data.map((c) => (
              <tr key={c.id}>
                <td style={{ paddingLeft: 20 }}><Link to={`/cases/${c.id}`}><b>{c.name}</b></Link>
                  <div>{c.source === 'ai' && <span className="tag">AI</span>}{c.tags.map((t) => <span className="tag" key={t}>{t}</span>)}</div></td>
                <td>{c.steps.length}</td>
                <td>{c.last_run ? <Link to={`/runs/${c.last_run.id}`}><Badge s={c.last_run.status} /></Link> : <span className="muted">never run</span>}</td>
                <td style={{ textAlign: 'right', paddingRight: 20 }}>{can('tester') && <button className="btn sm" onClick={() => run(c.id)} disabled={p.policy_problems.length > 0}>▶ Run</button>}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </div>
      <div className="grid2">
        <div className="panel" style={{ padding: 0 }}>
          <div className="row" style={{ padding: '14px 20px 0', justifyContent: 'space-between' }}><h2>Recent runs</h2><button className="btn sm" onClick={runs.reload}>Refresh</button></div>
          {runs.data?.length === 0 && <div className="empty">No runs yet.</div>}
          {!!runs.data?.length && (
            <table className="t"><tbody>{runs.data.map((r) => (
              <tr key={r.id} className="click" onClick={() => nav(`/runs/${r.id}`)}>
                <td style={{ paddingLeft: 20 }}>#{r.id}</td><td>{r.case_name}</td><td><Badge s={r.status} /></td>
                <td className="small muted">{fmtDate(r.created_at)}</td>
                <td className="small muted">{r.started_at && r.finished_at ? fmtMs(new Date(r.finished_at).getTime() - new Date(r.started_at).getTime()) : ''}</td>
              </tr>
            ))}</tbody></table>
          )}
        </div>
        <Secrets pid={pid} />
      </div>
      {editing && <ProjectForm initial={p} onClose={() => setEditing(false)} onSaved={() => { setEditing(false); proj.reload() }} />}
      {gen && <GenerateModal project={p} onClose={() => setGen(false)} onSaved={() => { setGen(false); cases.reload() }} />}
    </>
  )
}
