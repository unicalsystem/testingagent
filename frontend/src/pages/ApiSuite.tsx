import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { AUTH_LABEL, api, type ApiCase, type ApiCaseDef, type ApiRun, type ApiSuite, type AuthIn } from '../api'
import { CredentialsFields, Method, SuiteFields, SuiteOptions, emptyAuth, suiteBody, suiteForm, type SuiteForm } from '../components/ApiTesting'
import { Alert, Badge, Modal, Tabs, fmtDate, useApp, useAsync } from '../components/ui'

type Tab = 'cases' | 'runs' | 'settings'
const CAT_LABEL: Record<string, string> = { positive: 'Positive', auth: 'Auth', negative: 'Negative', performance: 'Performance' }
const SRC_LABEL: Record<string, string> = { spec: 'from spec', auto: 'generated', ai: 'AI', manual: 'manual' }

function CaseModal({ suite, initial, onClose, onSaved }: { suite: ApiSuite; initial?: ApiCase; onClose: () => void; onSaved: () => void }) {
  const blank: ApiCaseDef = { method: 'GET', path: '/', query: {}, headers: {}, body: null, auth: 'suite', expect: { status: [200], max_ms: suite.max_response_ms } }
  const [name, setName] = useState(initial?.name ?? ''), [cat, setCat] = useState<string>(initial?.category ?? 'positive')
  const [json, setJson] = useState(JSON.stringify(initial?.definition ?? blank, null, 2))
  const [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const save = async (e: FormEvent) => {
    e.preventDefault(); setErr('')
    let definition: ApiCaseDef
    try { definition = JSON.parse(json) } catch (x) { return setErr('The definition is not valid JSON: ' + (x as Error).message) }
    setBusy(true)
    try {
      if (initial) await api.put(`/api-cases/${initial.id}`, { name, category: cat, definition })
      else await api.post(`/api-suites/${suite.id}/cases`, { name, category: cat, definition })
      onSaved()
    } catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  return (
    <Modal title={initial ? 'Edit API test case' : 'New API test case'} onClose={onClose}>
      <form onSubmit={save}>
        <Alert>{err}</Alert>
        <label className="f" htmlFor="cn">Name</label>
        <input className="i" id="cn" value={name} onChange={(e) => setName(e.target.value)} required maxLength={300} />
        <label className="f" htmlFor="cc">Category</label>
        <select className="i" id="cc" value={cat} onChange={(e) => setCat(e.target.value)}>{Object.entries(CAT_LABEL).map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select>
        <label className="f" htmlFor="cd">Definition</label>
        <textarea className="i code" id="cd" spellCheck={false} value={json} onChange={(e) => setJson(e.target.value)} />
        <div className="hint">
          <code>auth</code>: suite (valid credentials), none or invalid. <code>expect.status</code>: e.g. <code>[200]</code> or <code>["2xx"]</code>.
          Optional: <code>max_ms</code>, <code>content_type</code>, <code>body_contains</code>, <code>json_equals</code> (e.g. <code>{'{"$.status": "ok"}'}</code>), <code>schema</code>.
          A <code>null</code> path parameter reuses an id returned by an earlier call. Edited generated cases are kept when the suite is regenerated.
        </div>
        <div className="row" style={{ justifyContent: 'flex-end', marginTop: 14 }}>
          <button type="button" className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={busy}>{busy ? 'Saving…' : 'Save case'}</button>
        </div>
      </form>
    </Modal>
  )
}

interface Suggested { cases: { name: string; category: string; definition: ApiCaseDef; is_write: boolean }[]; rejected: { index: number; error: string }[]; model: string }
function SuggestModal({ suite, onClose, onSaved }: { suite: ApiSuite; onClose: () => void; onSaved: () => void }) {
  const [instr, setInstr] = useState(''), [count, setCount] = useState(8)
  const [out, setOut] = useState<Suggested | null>(null), [pick, setPick] = useState<Record<number, boolean>>({})
  const [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const gen = async (e: FormEvent) => {
    e.preventDefault(); setErr(''); setBusy(true)
    try { const r = await api.post<Suggested>(`/api-suites/${suite.id}/ai/suggest`, { instructions: instr, count }); setOut(r); setPick(Object.fromEntries(r.cases.map((c, i) => [i, !c.is_write]))) }
    catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  const save = async () => {
    if (!out) return
    setBusy(true); setErr('')
    try { await api.post(`/api-suites/${suite.id}/cases/bulk`, { source: 'ai', cases: out.cases.filter((_, i) => pick[i]).map((c) => ({ name: c.name, category: c.category, definition: c.definition })) }); onSaved() }
    catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  return (
    <Modal title="Suggest more cases with Claude" onClose={onClose}>
      <Alert>{err}</Alert>
      {!out && <form onSubmit={gen}>
        <p className="small muted" style={{ marginTop: 0 }}>Claude sees the endpoint list and the names of the existing cases — never your credentials or response data — and drafts extra edge cases. You choose which to keep.</p>
        <label className="f" htmlFor="in">Focus (optional)</label>
        <textarea className="i" id="in" value={instr} onChange={(e) => setInstr(e.target.value)} placeholder="e.g. Pagination limits on /orders, and that cancelled orders cannot be updated" />
        <label className="f" htmlFor="ct">Number of cases</label>
        <input className="i" id="ct" type="number" min={1} max={25} value={count} onChange={(e) => setCount(+e.target.value)} style={{ width: 100 }} />
        <div className="row" style={{ justifyContent: 'flex-end', marginTop: 14 }}><button type="button" className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={busy}>{busy ? 'Asking Claude…' : 'Suggest cases'}</button></div>
      </form>}
      {out && <>
        {out.rejected.length > 0 && <Alert kind="warn">{out.rejected.length} suggestion(s) were discarded because they were invalid.</Alert>}
        {out.cases.map((c, i) => (
          <label key={i} className="opt" style={{ display: 'flex' }}>
            <input type="checkbox" checked={!!pick[i]} onChange={(e) => setPick({ ...pick, [i]: e.target.checked })} />
            <div style={{ minWidth: 0 }}><div><Method m={c.definition.method} /> <span className="mono small">{c.definition.path}</span></div>
              <b>{c.name}</b> <span className="tag">{CAT_LABEL[c.category] ?? c.category}</span>{c.is_write && <span className="tag">write</span>}
              <div className="small muted">expects {c.definition.expect.status.join(' or ')}{c.definition.auth && c.definition.auth !== 'suite' ? ` · auth: ${c.definition.auth}` : ''}</div></div>
          </label>
        ))}
        <div className="row" style={{ justifyContent: 'flex-end' }}><button className="btn" onClick={() => setOut(null)}>Back</button>
          <button className="btn primary" disabled={busy || !Object.values(pick).some(Boolean)} onClick={save}>Save selected</button></div>
      </>}
    </Modal>
  )
}

function RunModal({ suite, onClose }: { suite: ApiSuite; onClose: () => void }) {
  const nav = useNavigate()
  const [writes, setWrites] = useState(false), [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const go = async () => {
    setBusy(true); setErr('')
    try { const r = await api.post<ApiRun>(`/api-suites/${suite.id}/runs`, { include_writes: writes }); nav(`/api-runs/${r.id}`) }
    catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  return (
    <Modal title="Run API tests" onClose={onClose}>
      <Alert>{err}</Alert>
      <p>{suite.enabled_count} enabled case(s) against <span className="mono">{suite.base_url}</span>.</p>
      {suite.write_count > 0 && (suite.allow_writes
        ? <label className="check"><input type="checkbox" checked={writes} onChange={(e) => setWrites(e.target.checked)} />
          <span><b>Include {suite.write_count} write test(s)</b> (POST / PUT / PATCH / DELETE)<div className="small muted">They create, change or delete data on this server. Leave unticked for a read-only run.</div></span></label>
        : <p className="small muted">{suite.write_count} write test(s) will be skipped (write tests are not allowed for this suite).</p>)}
      <div className="row" style={{ justifyContent: 'flex-end', marginTop: 14 }}><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={busy} onClick={go}>{busy ? 'Starting…' : '▶ Run now'}</button></div>
    </Modal>
  )
}

function Settings({ suite, has, onSaved }: { suite: ApiSuite; has: (p: string) => boolean; onSaved: (s: ApiSuite) => void }) {
  const [f, setF] = useState<SuiteForm>(suiteForm(suite))
  const [auth, setAuth] = useState<AuthIn>({ ...emptyAuth(suite.auth_type), config: { ...emptyAuth(suite.auth_type).config, ...suite.auth_config } })
  const [err, setErr] = useState(''), [ok, setOk] = useState(''), [aerr, setAerr] = useState(''), [aok, setAok] = useState('')
  const save = async (e: FormEvent) => {
    e.preventDefault(); setErr(''); setOk('')
    try { onSaved(await api.put<ApiSuite>(`/api-suites/${suite.id}`, suiteBody(f))); setOk('Saved. Regenerate the cases if you changed the specification or endpoints.') } catch (x) { setErr((x as Error).message) }
  }
  const saveAuth = async (e: FormEvent) => {
    e.preventDefault(); setAerr(''); setAok('')
    try { onSaved(await api.put<ApiSuite>(`/api-suites/${suite.id}/credentials`, auth)); setAuth({ ...auth, secrets: {} }); setAok('Credentials saved') } catch (x) { setAerr((x as Error).message) }
  }
  return (
    <div className="grid2">
      <form className="panel" onSubmit={save}>
        <h2>API &amp; options</h2>
        <Alert>{err}</Alert><Alert kind="ok">{ok}</Alert>
        <fieldset disabled={!has('case.write')} style={{ border: 0, padding: 0, margin: 0 }}>
          <SuiteFields f={f} set={setF} existingSpec={suite.has_spec} />
          <SuiteOptions f={f} set={setF} />
          {has('case.write') && <div className="row" style={{ justifyContent: 'flex-end', marginTop: 14 }}><button className="btn primary">Save</button></div>}
        </fieldset>
      </form>
      <form className="panel" onSubmit={saveAuth}>
        <h2>Credentials</h2>
        <p className="small muted" style={{ marginTop: -6 }}>Current: {suite.auth_hint || AUTH_LABEL[suite.auth_type]}</p>
        <Alert>{aerr}</Alert><Alert kind="ok">{aok}</Alert>
        {has('secret.write') ? <>
          <CredentialsFields value={auth} onChange={setAuth} stored={suite.has_credentials && auth.type === suite.auth_type} />
          <div className="row" style={{ justifyContent: 'flex-end', marginTop: 14 }}><button className="btn primary">Save credentials</button></div>
        </> : <p className="small muted">You do not have permission to change credentials.</p>}
      </form>
    </div>
  )
}

export default function ApiSuitePage() {
  const { sid = '' } = useParams()
  const nav = useNavigate()
  const { status } = useApp()
  const [params, setParams] = useSearchParams()
  const tab = (params.get('tab') as Tab) || 'cases'
  const setTab = (t: Tab) => setParams(t === 'cases' ? {} : { tab: t }, { replace: true })
  const [suite, setSuite] = useState<ApiSuite | null>(null), [err, setErr] = useState(''), [msg, setMsg] = useState(params.get('notice') ?? '')
  const cases = useAsync(() => api.get<ApiCase[]>(`/api-suites/${sid}/cases`), [sid])
  const runs = useAsync(() => api.get<ApiRun[]>(`/api-suites/${sid}/runs`), [sid])
  const [q, setQ] = useState(''), [cat, setCat] = useState('')
  const [edit, setEdit] = useState<ApiCase | 'new' | null>(null), [suggest, setSuggest] = useState(false), [running, setRunning] = useState(false), [busy, setBusy] = useState(false)

  useEffect(() => {
    let live = true, t: number | undefined
    const load = () => api.get<ApiSuite>(`/api-suites/${sid}`).then((s) => {
      if (!live) return
      setSuite((old) => { if (old && ['pending', 'running'].includes(old.import_state) && s.import_state === 'done') { cases.reload(); runs.reload() } return s })
      if (['pending', 'running'].includes(s.import_state)) t = window.setTimeout(load, 2500)
    }).catch((e) => live && setErr(e.message))
    load()
    return () => { live = false; clearTimeout(t) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sid])

  const list = useMemo(() => {
    const n = q.trim().toLowerCase()
    return (cases.data ?? []).filter((c) => (!cat || c.category === cat) && (!n || `${c.method} ${c.path} ${c.name}`.toLowerCase().includes(n)))
  }, [cases.data, q, cat])
  if (err && !suite) return <Alert>{err}</Alert>
  if (!suite) return null
  const has = (perm: string) => suite.my_permissions.includes(perm)
  const importing = ['pending', 'running'].includes(suite.import_state)
  const act = async (fn: () => Promise<void>) => { setBusy(true); setErr(''); setMsg(''); try { await fn() } catch (x) { setErr((x as Error).message) } finally { setBusy(false) } }
  const regenerate = (run: boolean) => act(async () => {
    if (!confirm('Regenerate the cases from the specification / endpoint list? Cases you added or edited are kept; generated ones are replaced.')) return
    const s = await api.post<ApiSuite>(`/api-suites/${suite.id}/generate`, { run })
    setSuite(s); cases.reload()
    if (s.generation?.run) nav(`/api-runs/${s.generation.run}`)
    else setMsg(s.generation?.generated != null ? `${s.generation.generated} case(s) generated.` : 'Importing the specification…')
  })
  const toggle = (c: ApiCase) => act(async () => { await api.put(`/api-cases/${c.id}`, { enabled: !c.enabled }); cases.reload(); setSuite(await api.get<ApiSuite>(`/api-suites/${sid}`)) })
  const aiOk = status?.ai.available && status?.storage?.ai_enabled !== false
  return (
    <>
      <div className="topbar">
        <div><div className="crumbs"><Link to="/projects">Projects</Link> / <Link to={`/projects/${suite.project_id}?tab=api`}>Project</Link> / API tests /</div>
          <h1>{suite.name}</h1>
          <div className="mono small muted">{suite.base_url || suite.spec_url}</div></div>
        <div className="row">
          {has('run.execute') && <button className="btn primary" disabled={busy || importing || !!suite.run_block_reason} title={suite.run_block_reason ?? ''} onClick={() => setRunning(true)}>▶ Run tests</button>}
          {has('case.write') && <button className="btn" disabled={busy || importing} onClick={() => regenerate(false)}>↻ Regenerate cases</button>}
          {has('ai.use') && <button className="btn" disabled={!aiOk || importing} title={aiOk ? '' : 'Claude is not available'} onClick={() => setSuggest(true)}>✦ Suggest cases with AI</button>}
          {has('case.write') && <button className="btn" onClick={() => setEdit('new')}>+ Case</button>}
        </div>
      </div>
      {importing && <Alert kind="warn">Downloading the specification and generating test cases…{suite.import_state === 'pending' ? ' (waiting for a worker)' : ''}</Alert>}
      {suite.import_state === 'error' && <Alert>Specification import failed: {suite.import_error}</Alert>}
      {suite.import_state === 'done' && suite.import_error && <Alert kind="warn">{suite.import_error}</Alert>}
      {suite.policy_problems.length > 0 && <Alert kind="warn">Tests cannot run yet: {suite.policy_problems.join('; ')}. An administrator must authorise the host under Administration → Targets.</Alert>}
      {!importing && suite.run_block_reason && !suite.policy_problems.length && <Alert kind="warn">{suite.run_block_reason}</Alert>}
      <Alert>{err}</Alert><Alert kind="ok">{msg}</Alert>
      <div className="stats">
        <div className="stat"><div className="k">Source</div><div className="v" style={{ fontSize: 14 }}>{suite.spec_title || (suite.spec_source === 'manual' ? 'Endpoint list' : suite.spec_source === 'url' ? 'OpenAPI URL' : 'OpenAPI file')}</div></div>
        <div className="stat"><div className="k">Cases</div><div className="v">{suite.enabled_count}<span className="small muted"> / {suite.case_count}</span></div></div>
        <div className="stat"><div className="k">Authentication</div><div className="v" style={{ fontSize: 14 }}>{AUTH_LABEL[suite.auth_type]}</div></div>
        <div className="stat"><div className="k">Write tests</div><div className="v" style={{ fontSize: 14 }}>{suite.allow_writes ? `allowed (${suite.write_count})` : `read-only (${suite.write_count} skipped)`}</div></div>
        <div className="stat"><div className="k">Last run</div><div className="v" style={{ fontSize: 14 }}>{suite.last_run ? <Link to={`/api-runs/${suite.last_run.id}`}><Badge s={suite.last_run.status} /></Link> : '—'}</div></div>
      </div>
      <Tabs<Tab> value={tab} onChange={setTab} tabs={[['cases', `Test cases (${suite.case_count})`], ['runs', 'Runs'], ['settings', 'Settings & credentials']]} />
      {tab === 'cases' && (
        <div className="panel" style={{ padding: 0 }}>
          <div className="toolbar">
            <input className="i" type="search" placeholder="Search method, path or name" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search cases" />
            <select className="i" value={cat} onChange={(e) => setCat(e.target.value)} aria-label="Category"><option value="">All categories</option>{Object.entries(CAT_LABEL).map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select>
          </div>
          <Alert>{cases.error}</Alert>
          {cases.data && list.length === 0 && <div className="empty">{importing ? 'Cases appear here when the import finishes.' : 'No cases.'}</div>}
          <table className="t resp"><tbody>{list.map((c) => (
            <tr key={c.id} style={{ opacity: c.enabled ? 1 : 0.55 }}>
              <td style={{ paddingLeft: 16, width: 34 }} className="sel">{has('case.write') && <input type="checkbox" checked={c.enabled} onChange={() => toggle(c)} aria-label={`Enable ${c.name}`} title="Enabled" />}</td>
              <td><div><Method m={c.method} /> <span className="mono small">{c.path}</span></div>
                <div className="small">{c.name}</div>
                <div><span className="tag">{CAT_LABEL[c.category] ?? c.category}</span><span className="tag">{SRC_LABEL[c.source] ?? c.source}</span>{c.is_write && <span className="tag" style={{ background: 'var(--warn-bg)', color: 'var(--warn)' }}>write</span>}
                  {c.definition.auth && c.definition.auth !== 'suite' && <span className="tag">auth: {c.definition.auth}</span>}</div></td>
              <td data-label="Expects" className="small">{c.definition.expect.status.join(' / ')}{c.definition.expect.schema ? ' · schema' : ''}{c.definition.expect.max_ms ? ` · ≤${c.definition.expect.max_ms} ms` : ''}</td>
              <td style={{ textAlign: 'right', paddingRight: 16 }}>
                <span className="row" style={{ justifyContent: 'flex-end' }}>
                  {has('case.write') && <button className="btn sm" onClick={() => setEdit(c)}>Edit</button>}
                  {has('case.write') && <button className="btn sm danger" onClick={() => act(async () => { if (confirm(`Delete "${c.name}"?`)) { await api.del(`/api-cases/${c.id}`); cases.reload() } })}>Delete</button>}
                </span></td>
            </tr>
          ))}</tbody></table>
        </div>
      )}
      {tab === 'runs' && (
        <div className="panel" style={{ padding: 0 }}>
          <div className="row" style={{ padding: '14px 16px 0', justifyContent: 'space-between' }}><h2>Runs</h2><button className="btn sm" onClick={runs.reload}>Refresh</button></div>
          {runs.data?.length === 0 && <div className="empty">No runs yet.</div>}
          <table className="t resp"><tbody>{runs.data?.map((r) => (
            <tr key={r.id} className="click" onClick={() => nav(`/api-runs/${r.id}`)}>
              <td style={{ paddingLeft: 16 }}><b>#{r.id}</b> <Badge s={r.status} /></td>
              <td data-label="Result" className="small">{'total' in r.summary ? `${r.summary.passed}/${r.summary.total} passed · ${r.summary.failed} failed · ${r.summary.skipped} skipped` : '—'}</td>
              <td data-label="p95" className="small muted">{'p95_ms' in r.summary ? `p95 ${r.summary.p95_ms} ms` : ''}</td>
              <td className="small muted">{fmtDate(r.created_at)}{r.requested_by ? ` · ${r.requested_by}` : ''}</td>
            </tr>
          ))}</tbody></table>
        </div>
      )}
      {tab === 'settings' && <Settings suite={suite} has={has} onSaved={setSuite} />}
      {has('case.write') && tab === 'settings' && (
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button className="btn danger" onClick={() => act(async () => { if (confirm('Archive this API suite? Its runs and reports are kept.')) { await api.del(`/api-suites/${suite.id}`); nav(`/projects/${suite.project_id}?tab=api`) } })}>Archive suite</button>
        </div>
      )}
      {edit && <CaseModal suite={suite} initial={edit === 'new' ? undefined : edit} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); cases.reload(); api.get<ApiSuite>(`/api-suites/${sid}`).then(setSuite) }} />}
      {suggest && <SuggestModal suite={suite} onClose={() => setSuggest(false)} onSaved={() => { setSuggest(false); cases.reload(); api.get<ApiSuite>(`/api-suites/${sid}`).then(setSuite) }} />}
      {running && <RunModal suite={suite} onClose={() => setRunning(false)} />}
    </>
  )
}
