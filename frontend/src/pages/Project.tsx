import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api, AUTO_LABEL, type AutoStatus, type AutomationJob, type GroupOption, type Project, type Run, type Step, type TestCase, type TestRunSummary } from '../api'
import { AutomateModal } from '../components/Automate'
import { Assignments } from '../components/AccessAdmin'
import { Alert, Badge, Counts, Modal, Progress, Tabs, fmtDate, fmtMs, useApp, useAsync } from '../components/ui'
import { ProjectForm } from './Projects'
import { ApiTestingTab } from '../components/ApiTesting'
import { MonitorsTab } from '../components/Monitoring'
import { IntegrationsTab } from '../components/Integrations'
import { MobileTab } from '../components/Mobile'

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

function Secrets({ pid, can }: { pid: string; can: (p: string) => boolean }) {
  const has = can
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
        <table className="t resp"><tbody>{data.map((s) => (
          <tr key={s.name}><td className="mono">{s.name}</td><td className="muted small">updated {fmtDate(s.updated_at)}</td>
            <td style={{ textAlign: 'right' }}>{has('secret.write') && <button className="btn sm danger" onClick={async () => { if (confirm(`Delete secret ${s.name}?`)) { await api.del(`/projects/${pid}/secrets/${s.name}`); reload() } }}>Delete</button>}</td></tr>
        ))}</tbody></table>
      ) : <div className="muted small">No secrets.</div>}
      {has('secret.write') && (
        <form className="row" onSubmit={add} style={{ marginTop: 12 }}>
          <input className="i mono" style={{ flex: 1, minWidth: 140 }} placeholder="NAME" value={name} onChange={(e) => setName(e.target.value.toUpperCase())} pattern="[A-Za-z_][A-Za-z0-9_]*" required aria-label="Secret name" />
          <input className="i" style={{ flex: 2, minWidth: 160 }} type="password" placeholder="value" value={val} onChange={(e) => setVal(e.target.value)} required autoComplete="off" aria-label="Secret value" />
          <button className="btn">Save secret</button>
        </form>
      )}
    </div>
  )
}

type Tab = 'cases' | 'testruns' | 'automation' | 'runs' | 'api' | 'mobile' | 'monitors' | 'integrations' | 'settings'
const PAGE = 100

/** Create one or many test runs from existing cases (all / filtered / selected, optionally grouped). */
export function NewRunModal({ pid, caseIds, onClose, onDone }: { pid: number; caseIds?: number[]; onClose: () => void; onDone: (runs: TestRunSummary[]) => void }) {
  const [name, setName] = useState(''), [desc, setDesc] = useState('')
  const [mode, setMode] = useState<'single' | 'group'>('single')
  const [scope, setScope] = useState<'all' | 'filter'>('all')
  const [fField, setFField] = useState(''), [fVals, setFVals] = useState<Record<string, boolean>>({})
  const [groupBy, setGroupBy] = useState('')
  const [opts, setOpts] = useState<GroupOption[]>([]), [total, setTotal] = useState(0)
  const [groups, setGroups] = useState<{ value: string; count: number }[]>([])
  const [filterGroups, setFilterGroups] = useState<{ value: string; count: number }[]>([])
  const [picked, setPicked] = useState<Record<string, boolean>>({})
  const [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const sel = () => ({
    ...(caseIds ? { case_ids: caseIds } : {}),
    ...(scope === 'filter' && fField ? { filter_field: fField, filter_values: Object.keys(fVals).filter((k) => fVals[k]) } : {}),
  })
  useEffect(() => {
    api.post<{ total: number; group_options: GroupOption[]; recommended_group_by: string[] }>(`/projects/${pid}/test-runs/plan`, { ...(caseIds ? { case_ids: caseIds } : {}) })
      .then((r) => { setOpts(r.group_options); setTotal(r.total); setGroupBy(r.recommended_group_by[0] ?? r.group_options[0]?.field ?? '') })
      .catch((e) => setErr(e.message))
  }, [pid, caseIds])
  useEffect(() => {
    if (!fField) { setFilterGroups([]); return }
    api.post<{ groups: { value: string; count: number }[] }>(`/projects/${pid}/test-runs/plan`, { ...(caseIds ? { case_ids: caseIds } : {}), plan: { mode: 'group', group_by: [fField] } })
      .then((r) => { setFilterGroups(r.groups); setFVals({}) }).catch((e) => setErr(e.message))
  }, [pid, caseIds, fField])
  const fKey = JSON.stringify(sel())
  useEffect(() => {
    if (mode !== 'group' || !groupBy) { setGroups([]); return }
    api.post<{ total: number; groups: { value: string; count: number }[] }>(`/projects/${pid}/test-runs/plan`, { ...sel(), plan: { mode: 'group', group_by: [groupBy] } })
      .then((r) => { setGroups(r.groups); setPicked(Object.fromEntries(r.groups.map((g) => [g.value, true]))) }).catch((e) => setErr(e.message))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pid, mode, groupBy, fKey])
  const chosen = groups.filter((g) => picked[g.value])
  const count = scope === 'filter' ? filterGroups.filter((g) => fVals[g.value]).reduce((a, g) => a + g.count, 0) : total
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr(''); setBusy(true)
    try {
      const plan = mode === 'group' ? { mode, group_by: [groupBy], groups: chosen.length === groups.length ? null : chosen.map((g) => g.value), name } : { mode, name }
      onDone(await api.post<TestRunSummary[]>(`/projects/${pid}/test-runs`, { ...sel(), name, description: desc, plan }))
    } catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  const runs = mode === 'single' ? 1 : chosen.length
  return (
    <Modal title="New test run" onClose={onClose}>
      <form onSubmit={submit}>
        <Alert>{err}</Alert>
        <label className="f" htmlFor="rn">{mode === 'single' ? 'Name' : 'Name prefix (optional)'}</label>
        <input className="i" id="rn" value={name} onChange={(e) => setName(e.target.value)} required={mode === 'single'} maxLength={150} placeholder="e.g. Release 2.3 regression" />
        {!caseIds && (
          <>
            <label className="f">Which cases</label>
            <div className="row">
              <label className="row"><input type="radio" checked={scope === 'all'} onChange={() => setScope('all')} /> All {total} cases</label>
              <label className="row"><input type="radio" checked={scope === 'filter'} onChange={() => setScope('filter')} disabled={!opts.length} /> Only cases where…</label>
            </div>
            {scope === 'filter' && (
              <>
                <select className="i" value={fField} onChange={(e) => setFField(e.target.value)} style={{ marginTop: 6 }} aria-label="Filter field">
                  <option value="">Choose a field…</option>{opts.map((o) => <option key={o.field} value={o.field}>{o.label}</option>)}
                </select>
                {!!filterGroups.length && <div className="grouplist" style={{ maxHeight: 200 }}>{filterGroups.map((g) => (
                  <label key={g.value}><input type="checkbox" checked={!!fVals[g.value]} onChange={(e) => setFVals({ ...fVals, [g.value]: e.target.checked })} />{g.value}<span className="n">{g.count}</span></label>
                ))}</div>}
              </>
            )}
          </>
        )}
        {caseIds && <p className="small muted">{caseIds.length} selected case(s).</p>}
        <label className="f">Organise</label>
        <label className={`opt${mode === 'single' ? ' on' : ''}`}><input type="radio" checked={mode === 'single'} onChange={() => setMode('single')} /><div><b>One test run</b></div></label>
        <label className={`opt${mode === 'group' ? ' on' : ''}`}>
          <input type="radio" checked={mode === 'group'} onChange={() => setMode('group')} disabled={!opts.length} />
          <div style={{ flex: 1, minWidth: 0 }}><b>One test run per group</b>
            {mode === 'group' && (
              <>
                <select className="i" value={groupBy} onChange={(e) => setGroupBy(e.target.value)} style={{ marginTop: 6 }} aria-label="Group by">
                  {opts.map((o) => <option key={o.field} value={o.field}>{o.label} ({o.distinct})</option>)}
                </select>
                {!!groups.length && <div className="grouplist" style={{ maxHeight: 220 }}>{groups.map((g) => (
                  <label key={g.value}><input type="checkbox" checked={!!picked[g.value]} onChange={(e) => setPicked({ ...picked, [g.value]: e.target.checked })} />{name ? `${name} – ` : ''}{g.value}<span className="n">{g.count}</span></label>
                ))}</div>}
              </>
            )}
          </div>
        </label>
        {mode === 'single' && <><label className="f" htmlFor="rd">Description (optional)</label><textarea className="i" id="rd" style={{ minHeight: 60 }} value={desc} onChange={(e) => setDesc(e.target.value)} /></>}
        <div className="consent" style={{ background: '#f4f7fe', borderColor: '#d9e2fa' }}>
          <span>This creates <b>{runs}</b> test run(s) with {mode === 'group' ? chosen.reduce((a, g) => a + g.count, 0) : caseIds ? caseIds.length : count} case(s), all marked Not run.</span>
        </div>
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy || runs === 0 || (scope === 'filter' && !caseIds && count === 0)}>{busy ? 'Creating…' : `Create ${runs > 1 ? `${runs} runs` : 'run'}`}</button>
        </div>
      </form>
    </Modal>
  )
}

function CasesTab({ p, cases, reload, onRuns, has }: { p: Project; cases: TestCase[]; reload: () => void; onRuns: () => void; has: (perm: string) => boolean }) {
  const { status } = useApp()
  const canAutomate = has('ai.use') && has('case.write') && status?.storage?.ai_enabled !== false
  const canSelect = has('case.write') || has('run.execute')
  const nav = useNavigate()
  const [q, setQ] = useState(''), [mod, setMod] = useState(''), [kind, setKind] = useState<'' | 'auto' | 'manual'>('')
  const [sel, setSel] = useState<Record<number, boolean>>({}), [shown, setShown] = useState(PAGE)
  const [err, setErr] = useState(''), [newRun, setNewRun] = useState(false), [automate, setAutomate] = useState(false)
  const modules = useMemo(() => [...new Set(cases.map((c) => c.module).filter(Boolean))], [cases])
  const list = useMemo(() => {
    const n = q.trim().toLowerCase()
    return cases.filter((c) => (!mod || c.module === mod) && (!kind || (kind === 'auto' ? c.steps.length > 0 : c.steps.length === 0)) &&
      (!n || `${c.external_id} ${c.name} ${c.sub_module} ${c.tags.join(' ')}`.toLowerCase().includes(n)))
  }, [cases, q, mod, kind])
  useEffect(() => setShown(PAGE), [q, mod, kind])
  const selected = Object.keys(sel).filter((k) => sel[+k]).map(Number)
  const allShown = list.slice(0, shown)
  const run = async (cid: number) => {
    setErr('')
    try { const r = await api.post<Run>(`/cases/${cid}/runs`); nav(`/runs/${r.id}`) } catch (x) { setErr((x as Error).message) }
  }
  if (!cases.length) return (
    <div className="panel empty">
      <p><b>No test cases yet.</b></p>
      {has('case.write') && <div className="row" style={{ justifyContent: 'center' }}>
        <Link className="btn primary" to={`/projects/${p.id}/import`}>⬆ Import from CSV / Excel</Link>
        <Link className="btn" to={`/projects/${p.id}/cases/new`}>+ Write one</Link></div>}
    </div>
  )
  return (
    <>
      <Alert>{err}</Alert>
      <div className="panel" style={{ padding: 0 }}>
        <div className="toolbar">
          <input className="i" type="search" placeholder={`Search ${cases.length} cases`} value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search cases" />
          {modules.length > 1 && <select className="i" value={mod} onChange={(e) => setMod(e.target.value)} aria-label="Module"><option value="">All modules</option>{modules.map((m) => <option key={m}>{m}</option>)}</select>}
          <select className="i" value={kind} onChange={(e) => setKind(e.target.value as typeof kind)} aria-label="Type"><option value="">Manual + automated</option><option value="auto">Automated only</option><option value="manual">Manual only</option></select>
        </div>
        <table className="t resp">
          <thead><tr>
            {canSelect && <th style={{ width: 34, paddingLeft: 16 }}><input type="checkbox" aria-label="Select all shown" checked={allShown.length > 0 && allShown.every((c) => sel[c.id])}
              onChange={(e) => setSel({ ...sel, ...Object.fromEntries(allShown.map((c) => [c.id, e.target.checked])) })} /></th>}
            <th>Test case</th><th>Module</th><th>Priority</th><th>Automation</th><th />
          </tr></thead>
          <tbody>{allShown.map((c) => (
            <tr key={c.id}>
              {canSelect && <td style={{ paddingLeft: 16 }} className="sel"><input type="checkbox" aria-label={`Select ${c.name}`} checked={!!sel[c.id]} onChange={(e) => setSel({ ...sel, [c.id]: e.target.checked })} /></td>}
              <td>
                <Link to={`/cases/${c.id}`}>{c.external_id && <span className="mono muted">{c.external_id} · </span>}<b>{c.name}</b></Link>
                <div>{c.source === 'ai' && <span className="tag">AI</span>}{c.source === 'import' && <span className="tag">imported</span>}{c.tags.map((t) => <span className="tag" key={t}>{t}</span>)}</div>
              </td>
              <td data-label="Module" className="small">{c.module || '—'}{c.sub_module && <div className="sub">{c.sub_module}</div>}</td>
              <td data-label="Priority" className="small">{c.priority || '—'}</td>
              <td data-label="Automation">{c.steps.length ? (c.last_run ? <Link to={`/runs/${c.last_run.id}`}><Badge s={c.last_run.status} /></Link> : <span className="small">{c.steps.length} steps</span>) : <span className="muted small">manual</span>}</td>
              <td style={{ textAlign: 'right', paddingRight: 16 }}>
                <span className="row" style={{ justifyContent: 'flex-end' }}>
                  {has('run.execute') && c.steps.length > 0 && <button className="btn sm" onClick={() => run(c.id)} disabled={p.policy_problems.length > 0}>▶ Run</button>}
                </span>
              </td>
            </tr>
          ))}</tbody>
        </table>
        {list.length === 0 && <div className="empty">No cases match.</div>}
        {list.length > shown && <div className="morebar"><button className="btn" onClick={() => setShown(shown + PAGE)}>Show more ({list.length - shown} remaining)</button></div>}
      </div>
      {selected.length > 0 && (
        <div className="bulkbar">
          <b>{selected.length} selected</b><span style={{ flex: 1 }} />
          {has('run.execute') && <button className="btn sm" onClick={() => setNewRun(true)}>+ Test run from selection</button>}
          {canAutomate && <button className="btn sm" onClick={() => setAutomate(true)}>✦ Automate with AI</button>}
          {has('case.write') && <button className="btn sm" onClick={async () => {
            if (!confirm(`Archive ${selected.length} test case(s)? Past runs and reports are kept.`)) return
            try { await api.post(`/projects/${p.id}/cases/archive`, { case_ids: selected }); setSel({}); reload() } catch (x) { setErr((x as Error).message) }
          }}>Archive</button>}
          <button className="btn sm" onClick={() => setSel({})}>Clear</button>
        </div>
      )}
      {automate && <AutomateModal pid={p.id} count={cases.filter((c) => sel[c.id] && !c.steps.length).length} caseIds={selected.filter((id) => !cases.find((c) => c.id === id)?.steps.length)}
        hasSetup={!!p.setup_steps?.length} baseUrl={p.base_url} onClose={() => setAutomate(false)} />}
      {newRun && <NewRunModal pid={p.id} caseIds={selected} onClose={() => setNewRun(false)} onDone={(r) => { setNewRun(false); setSel({}); if (r.length === 1) nav(`/test-runs/${r[0].id}`); else onRuns() }} />}
    </>
  )
}

function TestRunsTab({ p, hasCases, has }: { p: Project; hasCases: boolean; has: (perm: string) => boolean }) {
  const nav = useNavigate()
  const runs = useAsync(() => api.get<TestRunSummary[]>(`/projects/${p.id}/test-runs`), [p.id])
  const [creating, setCreating] = useState(false), [showClosed, setShowClosed] = useState(false)
  const list = (runs.data ?? []).filter((r) => showClosed || r.status === 'active')
  const closed = (runs.data ?? []).filter((r) => r.status === 'closed').length
  return (
    <>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 12 }}>
        <span className="muted small">A test run is a checklist of cases executed for a release, sprint or module.</span>
        <span className="row">
          {closed > 0 && <label className="row small"><input type="checkbox" checked={showClosed} onChange={(e) => setShowClosed(e.target.checked)} /> Show {closed} closed</label>}
          {has('run.execute') && hasCases && <button className="btn primary" onClick={() => setCreating(true)}>+ New test run</button>}
        </span>
      </div>
      <Alert>{runs.error}</Alert>
      {runs.data && list.length === 0 && (
        <div className="panel empty">No test runs yet.{has('run.execute') && hasCases && ' Create one.'}{has('case.write') && <> You can also <Link to={`/projects/${p.id}/import`}>import test cases</Link> and organise them into runs.</>}</div>
      )}
      <div className="cards">
        {list.map((r) => (
          <Link key={r.id} className="card" to={`/test-runs/${r.id}`}>
            <div className="row" style={{ justifyContent: 'space-between', flexWrap: 'nowrap', alignItems: 'flex-start' }}>
              <div className="ttl">{r.name}</div>{r.status === 'closed' && <Badge s="cancelled" label="closed" />}
            </div>
            <div className="sub" style={{ marginBottom: 8 }}>{r.total} cases · {r.progress}% executed · {fmtDate(r.created_at)}</div>
            <Progress counts={r.counts} total={r.total} />
            <Counts counts={r.counts} />
          </Link>
        ))}
      </div>
      {creating && <NewRunModal pid={p.id} onClose={() => setCreating(false)} onDone={(r) => { setCreating(false); if (r.length === 1) nav(`/test-runs/${r[0].id}`); else runs.reload() }} />}
    </>
  )
}

function AutomationTab({ p, manualCount, has }: { p: Project; manualCount: number; has: (perm: string) => boolean }) {
  const { status } = useApp()
  const canAutomate = has('ai.use') && has('case.write') && status?.storage?.ai_enabled !== false
  const jobs = useAsync(() => api.get<AutomationJob[]>(`/projects/${p.id}/automation-jobs`), [p.id])
  const [start, setStart] = useState(false)
  return (
    <>
      <div className="panel">
        <h2>Turn manual cases into automated tests</h2>
        <p className="small muted" style={{ marginTop: -4 }}>The test agent explores the application, Claude drafts browser steps from each case's procedure and expected result, and every draft is run once to prove it works (repairing it if it fails). You can start this for all manual cases here, for selected cases on the Test cases tab, or from inside a test run.</p>
        <ol className="small" style={{ paddingLeft: 18, margin: '0 0 12px' }}>
          <li>Under <b>Settings</b>, add sign-in <b>setup steps</b> and the password as a secret, so the agent can reach pages behind the login.</li>
          <li>Ask an administrator to authorise <span className="mono">{new URL(p.base_url).host}</span> as a test target.</li>
          <li>Prefer a test/staging copy of the application: automated steps really click, save and delete.</li>
        </ol>
        {canAutomate && <button className="btn primary" disabled={!manualCount} onClick={() => setStart(true)}>✦ Automate all {manualCount} manual cases</button>}
      </div>
      <Alert>{jobs.error}</Alert>
      <div className="cards">
        {jobs.data?.map((j) => (
          <Link key={j.id} className="card" to={`/automation-jobs/${j.id}`}>
            <div className="row" style={{ justifyContent: 'space-between' }}><div className="ttl">Job #{j.id}{j.test_run ? ` · ${j.test_run.name}` : ''}</div><Badge s={j.status === 'done' ? 'passed' : j.status === 'cancelled' ? 'cancelled' : 'running'} label={j.status} /></div>
            <div className="sub" style={{ marginBottom: 8 }}>{j.total} cases · {j.progress}% processed · {fmtDate(j.created_at)}</div>
            <div className="counts">{(Object.keys(j.counts) as AutoStatus[]).map((k) => <span key={k} className="cnt"><i />{AUTO_LABEL[k]} {j.counts[k]}</span>)}</div>
          </Link>
        ))}
      </div>
      {start && <AutomateModal pid={p.id} count={manualCount} allManual hasSetup={!!p.setup_steps?.length} baseUrl={p.base_url} onClose={() => setStart(false)} />}
    </>
  )
}

function AutomationSettings({ p, onSaved, has }: { p: Project; onSaved: () => void; has: (perm: string) => boolean }) {
  const canEdit = has('project.update')
  const [json, setJson] = useState(JSON.stringify(p.setup_steps ?? [], null, 2)), [notes, setNotes] = useState(p.app_notes ?? '')
  const [err, setErr] = useState(''), [ok, setOk] = useState('')
  const example = JSON.stringify([
    { action: 'goto', url: '/' }, { action: 'fill', selector: '#username', value: 'qa.tester' },
    { action: 'fill', selector: '#password', value: '{{secret:APP_PASSWORD}}' }, { action: 'click', selector: 'role=button[name="Sign in"]' },
    { action: 'expect_url', contains: '/dashboard' }], null, 2)
  const save = async () => {
    setErr(''); setOk('')
    let steps
    try { steps = JSON.parse(json || '[]') } catch (e) { return setErr('Setup steps are not valid JSON: ' + (e as Error).message) }
    try { await api.put(`/projects/${p.id}/automation-settings`, { setup_steps: steps, app_notes: notes }); setOk('Saved'); onSaved() } catch (x) { setErr((x as Error).message) }
  }
  return (
    <div className="panel">
      <h2>Automation setup</h2>
      <Alert>{err}</Alert><Alert kind="ok">{ok}</Alert>
      <label className="f" htmlFor="ss" style={{ marginTop: 0 }}>Sign-in setup steps</label>
      <p className="hint" style={{ marginTop: 0 }}>Run before every automated case that needs a signed-in user, and used by the agent to explore pages behind the login. Put the password in a secret above and reference it as <code>{'{{secret:NAME}}'}</code>.</p>
      <textarea className="i code" id="ss" style={{ minHeight: 180 }} spellCheck={false} value={json} onChange={(e) => setJson(e.target.value)} readOnly={!canEdit} placeholder={example} />
      {canEdit && json.trim() === '[]' && <button className="btn sm" style={{ marginTop: 6 }} onClick={() => setJson(example)}>Insert example</button>}
      <label className="f" htmlFor="an">Notes for the AI (optional)</label>
      <textarea className="i" id="an" value={notes} onChange={(e) => setNotes(e.target.value)} readOnly={!canEdit} placeholder="e.g. Use the test company 'ACME QA' for any create/edit cases. The main menu is under the ☰ icon (#menu-toggle). Never delete real users." />
      {canEdit && <div className="row" style={{ marginTop: 12, justifyContent: 'flex-end' }}><button className="btn primary" onClick={save}>Save automation setup</button></div>}
    </div>
  )
}

export default function ProjectPage() {
  const { pid = '' } = useParams()
  const { status } = useApp()
  const nav = useNavigate()
  const [params, setParams] = useSearchParams()
  const tab = (params.get('tab') as Tab) || 'cases'
  const setTab = (t: Tab) => setParams(t === 'cases' ? {} : { tab: t }, { replace: true })
  const proj = useAsync(() => api.get<Project>(`/projects/${pid}`), [pid])
  const cases = useAsync(() => api.get<TestCase[]>(`/projects/${pid}/cases`), [pid])
  const runs = useAsync(() => api.get<Run[]>(`/projects/${pid}/runs?limit=25`), [pid])
  const [editing, setEditing] = useState(false), [gen, setGen] = useState(false), [err, setErr] = useState(''), [msg, setMsg] = useState('')
  const p = proj.data
  const automated = (cases.data ?? []).filter((c) => c.steps.length > 0)
  const runAll = async () => {
    setErr(''); setMsg('')
    if (!confirm(`Queue ${automated.length} automated run(s)?`)) return
    try { for (const c of automated) await api.post(`/cases/${c.id}/runs`); runs.reload(); cases.reload(); setMsg(`${automated.length} run(s) queued.`) } catch (x) { setErr((x as Error).message) }
  }
  if (proj.error) return <Alert>{proj.error}</Alert>
  if (!p) return null
  const aiOk = status?.ai.available
  const has = (perm: string) => (p.my_permissions ?? []).includes(perm)
  const canAutomate = has('ai.use') && has('case.write') && status?.storage?.ai_enabled !== false
  return (
    <>
      <div className="topbar">
        <div><div className="crumbs"><Link to="/projects">Projects</Link> /</div><h1>{p.name}</h1>
          <div className="mono small muted">{p.base_url}</div></div>
        <div className="row">
          {has('case.write') && <Link className="btn primary" to={`/projects/${p.id}/import`}>⬆ Import</Link>}
          {has('case.write') && <Link className="btn" to={`/projects/${p.id}/cases/new`}>+ Test case</Link>}
          {has('ai.use') && <button className="btn" onClick={() => setGen(true)} disabled={!aiOk} title={aiOk ? '' : 'Claude is not available'}>✦ Generate with AI</button>}
          {canAutomate && (cases.data?.length ?? 0) > automated.length && <button className="btn" onClick={() => setTab('automation')}>✦ Automate manual cases</button>}
          {has('run.execute') && automated.length > 0 && <button className="btn" onClick={runAll} disabled={p.policy_problems.length > 0}>▶ Run automated ({automated.length})</button>}
          {has('project.update') && <button className="btn" onClick={() => setEditing(true)}>Edit</button>}
        </div>
      </div>
      {p.policy_problems.length > 0 && <Alert kind="warn">Automated tests cannot run: {p.policy_problems.join('; ')}. An administrator must authorise the target host. Manual test runs work normally.</Alert>}
      <Alert>{err}</Alert><Alert kind="ok">{msg}</Alert>
      {p.description && <p className="muted" style={{ marginTop: -6 }}>{p.description}</p>}
      <Tabs<Tab> value={tab} onChange={setTab} tabs={[['cases', `Test cases${cases.data ? ` (${cases.data.length})` : ''}`], ['testruns', 'Test runs'], ['automation', 'AI automation'], ['runs', 'Automated runs'], ['api', 'API testing'], ['mobile', 'Mobile'], ['monitors', 'Monitoring'], ['integrations', 'Integrations'], ['settings', 'Settings']]} />
      {tab === 'cases' && cases.data && <CasesTab p={p} cases={cases.data} reload={cases.reload} onRuns={() => setTab('testruns')} has={has} />}
      {tab === 'testruns' && <TestRunsTab p={p} hasCases={!!cases.data?.length} has={has} />}
      {tab === 'runs' && (
        <div className="panel" style={{ padding: 0 }}>
          <div className="row" style={{ padding: '14px 16px 0', justifyContent: 'space-between' }}><h2>Recent automated runs</h2><button className="btn sm" onClick={runs.reload}>Refresh</button></div>
          {runs.data?.length === 0 && <div className="empty">No automated runs yet.</div>}
          {!!runs.data?.length && (
            <table className="t resp"><tbody>{runs.data.map((r) => (
              <tr key={r.id} className="click" onClick={() => nav(`/runs/${r.id}`)}>
                <td style={{ paddingLeft: 16 }}><b>#{r.id}</b> {r.case_name}</td><td><Badge s={r.status} /></td>
                <td className="small muted">{fmtDate(r.created_at)}</td>
                <td className="small muted">{r.started_at && r.finished_at ? fmtMs(new Date(r.finished_at).getTime() - new Date(r.started_at).getTime()) : ''}</td>
              </tr>
            ))}</tbody></table>
          )}
        </div>
      )}
      {tab === 'automation' && <AutomationTab p={p} manualCount={(cases.data ?? []).length - automated.length} has={has} />}
      {tab === 'api' && <ApiTestingTab p={p} has={has} />}
      {tab === 'monitors' && <MonitorsTab p={p} has={has} />}
      {tab === 'integrations' && <IntegrationsTab p={p} has={has} />}
      {tab === 'mobile' && <MobileTab p={p} has={has} />}
      {tab === 'settings' && <><div className="grid2"><Secrets pid={pid} can={has} /><AutomationSettings p={p} onSaved={proj.reload} has={has} /></div>{has('project.members') && <Assignments projectId={p.id} canAllProjects={false} />}</>}
      {editing && <ProjectForm initial={p} onClose={() => setEditing(false)} onSaved={() => { setEditing(false); proj.reload() }} />}
      {gen && <GenerateModal project={p} onClose={() => setGen(false)} onSaved={() => { setGen(false); cases.reload() }} />}
    </>
  )
}
