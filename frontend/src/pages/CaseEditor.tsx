import { Fragment, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, type CaseMeta, type Run, type Step, type TestCase } from '../api'
import { Alert, useApp, useProjectPerms } from '../components/ui'

const EMPTY_META: CaseMeta = { external_id: '', module: '', sub_module: '', priority: '', scenario_type: '', assigned_to: '', requirement: '', preconditions: '', test_data: '', procedure: '', expected_result: '' }

const TEMPLATE: Step[] = [
  { action: 'goto', url: '/', description: 'Open the home page' },
  { action: 'expect_visible', selector: 'h1' },
  { action: 'screenshot', name: 'home' },
]

const HELP = `Steps are a JSON list. Each step has an "action" plus its fields:
goto {url}            click / hover / check / uncheck {selector}
fill {selector,value} press {selector,key}   select {selector,value}
wait_for {selector,state?}   wait {ms}   screenshot {name?, full_page?}
expect_visible / expect_hidden {selector}
expect_text {selector,text,match?: contains|equals}
expect_url {contains}   expect_title {contains}
Optional on any step: description, timeout_ms, retries (0-5).
Selectors: CSS (#id, [data-testid=x]), text=Sign in, role=button[name="Save"].
Secrets: "value": "{{secret:NAME}}". Script execution is not supported.`

export default function CaseEditor() {
  const { cid, pid } = useParams()
  const { status } = useApp()
  const nav = useNavigate()
  const [tc, setTc] = useState<TestCase | null>(null)
  const [name, setName] = useState(''), [desc, setDesc] = useState(''), [tags, setTags] = useState('')
  const [meta, setMeta] = useState<CaseMeta>(EMPTY_META)
  const [json, setJson] = useState(cid ? '[]' : JSON.stringify(TEMPLATE, null, 2))
  const [err, setErr] = useState(''), [ok, setOk] = useState(''), [drafting, setDrafting] = useState(false)
  const [useSetup, setUseSetup] = useState(false)

  useEffect(() => {
    if (!cid) return
    api.get<TestCase>(`/cases/${cid}`).then((c) => {
      setTc(c); setName(c.name); setDesc(c.description); setTags(c.tags.join(', ')); setJson(JSON.stringify(c.steps, null, 2)); setUseSetup(!!c.use_setup)
      setMeta(Object.fromEntries(Object.keys(EMPTY_META).map((k) => [k, (c as unknown as Record<string, string>)[k] ?? ''])) as unknown as CaseMeta)
    }).catch((e) => setErr(e.message))
  }, [cid])

  const projectId = tc?.project_id ?? Number(pid)
  const has = useProjectPerms(projectId || null)
  const readOnly = !has('case.write')
  let parsedSteps: Step[] | null = null
  try { parsedSteps = JSON.parse(json) } catch { parsedSteps = null }
  const hasSteps = !!parsedSteps?.length
  const save = async (thenRun = false) => {
    setErr(''); setOk('')
    let steps: Step[]
    try { steps = JSON.parse(json || '[]') } catch (e) { return setErr('Automated steps are not valid JSON: ' + (e as Error).message) }
    const body = { name, description: desc, tags: tags.split(',').map((t) => t.trim()).filter(Boolean), steps, use_setup: useSetup, ...meta }
    try {
      const saved = tc ? await api.put<TestCase>(`/cases/${tc.id}`, body) : await api.post<TestCase>(`/projects/${projectId}/cases`, body)
      setTc(saved); setJson(JSON.stringify(saved.steps, null, 2)); setOk('Saved')
      if (thenRun) { const r = await api.post<Run>(`/cases/${saved.id}/runs`); nav(`/runs/${r.id}`) }
      else if (!tc) nav(`/cases/${saved.id}`, { replace: true })
    } catch (x) { setErr((x as Error).message) }
  }
  const archive = async () => {
    if (!tc || !confirm('Archive this test case? Its past runs and reports are kept.')) return
    await api.del(`/cases/${tc.id}`); nav(`/projects/${tc.project_id}`)
  }
  const draft = async () => {
    if (!tc) return
    if (hasSteps && !confirm('Replace the current automated steps with an AI draft? (Nothing is saved until you click Save.)')) return
    setDrafting(true); setErr(''); setOk('')
    try {
      const r = await api.post<{ steps: Step[]; model: string }>(`/cases/${tc.id}/ai/automate`)
      setJson(JSON.stringify(r.steps, null, 2)); setOk(`Draft steps from ${r.model}. Check the selectors, then Save.`)
    } catch (x) { setErr((x as Error).message) } finally { setDrafting(false) }
  }
  const m = (k: keyof CaseMeta) => ({ value: meta[k], readOnly, onChange: (e: { target: { value: string } }) => setMeta({ ...meta, [k]: e.target.value }) })

  return (
    <>
      <div className="topbar">
        <div><div className="crumbs"><Link to="/projects">Projects</Link> / <Link to={`/projects/${projectId}`}>Project</Link> /</div>
          <h1>{tc ? (meta.external_id ? `${meta.external_id} · ` : '') + 'Edit test case' : 'New test case'}</h1></div>
        {!readOnly && <div className="row">
          {tc && <button className="btn danger" onClick={archive}>Archive</button>}
          <button className="btn primary" onClick={() => save(false)}>Save</button>
          <button className="btn" onClick={() => save(true)} disabled={!hasSteps} title={hasSteps ? '' : 'Add automated steps to run it in the browser'}>Save &amp; run</button>
        </div>}
      </div>
      <Alert>{err}</Alert><Alert kind="ok">{ok}</Alert>
      <div className="editor-grid">
        <div>
          <div className="panel">
            <h2>Test case</h2>
            <div className="f2">
              <div><label className="f" htmlFor="xid" style={{ marginTop: 0 }}>Test Case ID</label><input className="i mono" id="xid" maxLength={100} {...m('external_id')} /></div>
              <div><label className="f" htmlFor="pr" style={{ marginTop: 0 }}>Priority</label><input className="i" id="pr" list="prios" maxLength={40} {...m('priority')} />
                <datalist id="prios"><option>Critical</option><option>High</option><option>Medium</option><option>Low</option></datalist></div>
            </div>
            <label className="f" htmlFor="n">Title</label>
            <input className="i" id="n" value={name} onChange={(e) => setName(e.target.value)} readOnly={readOnly} maxLength={200} />
            <div className="f2">
              <div><label className="f" htmlFor="mo">Module</label><input className="i" id="mo" maxLength={200} {...m('module')} /></div>
              <div><label className="f" htmlFor="sm">Sub-module</label><input className="i" id="sm" maxLength={200} {...m('sub_module')} /></div>
              <div><label className="f" htmlFor="sc">Scenario type</label><input className="i" id="sc" list="scens" maxLength={80} {...m('scenario_type')} />
                <datalist id="scens"><option>Positive</option><option>Negative</option><option>Boundary</option><option>Security/Access</option></datalist></div>
              <div><label className="f" htmlFor="as">Assigned to</label><input className="i" id="as" maxLength={120} {...m('assigned_to')} /></div>
            </div>
            <label className="f" htmlFor="rq">Requirement</label><input className="i" id="rq" {...m('requirement')} />
            <label className="f" htmlFor="d">Description</label>
            <textarea className="i" id="d" style={{ minHeight: 60 }} value={desc} onChange={(e) => setDesc(e.target.value)} readOnly={readOnly} />
            <label className="f" htmlFor="pc">Pre-conditions</label><textarea className="i" id="pc" style={{ minHeight: 60 }} {...m('preconditions')} />
            <label className="f" htmlFor="td">Test data</label><textarea className="i" id="td" style={{ minHeight: 50 }} {...m('test_data')} />
            <label className="f" htmlFor="pro">Procedure / steps (manual)</label><textarea className="i" id="pro" style={{ minHeight: 110 }} {...m('procedure')} />
            <label className="f" htmlFor="ex">Expected result</label><textarea className="i" id="ex" style={{ minHeight: 70 }} {...m('expected_result')} />
            <label className="f" htmlFor="tg">Tags (comma separated)</label>
            <input className="i" id="tg" value={tags} onChange={(e) => setTags(e.target.value)} readOnly={readOnly} />
            {tc && Object.keys(tc.attributes ?? {}).length > 0 && (
              <><label className="f">Other fields from import</label>
                <dl className="kv">{Object.entries(tc.attributes).map(([k, v]) => <Fragment key={k}><dt>{k}</dt><dd>{v}</dd></Fragment>)}</dl></>
            )}
          </div>
          <div className="panel">
            <div className="row" style={{ justifyContent: 'space-between' }}>
              <h2 style={{ margin: 0 }}>Automated steps <span className="muted small" style={{ fontWeight: 400 }}>(optional)</span></h2>
              {!readOnly && tc && has('ai.use') && status?.storage?.ai_enabled !== false && <button className="btn sm" onClick={draft} disabled={drafting || !status?.ai.available || !(meta.procedure || meta.expected_result || desc)}
                title={status?.ai.available ? 'Claude drafts browser steps from the procedure and expected result' : 'Claude is not available'}>{drafting ? 'Drafting…' : '✦ Draft steps with AI'}</button>}
            </div>
            <p className="hint">Leave empty (<code>[]</code>) for a manual case. With steps, the case can also run automatically in the browser.</p>
            <textarea className="i code" id="s" aria-label="Automated steps JSON" spellCheck={false} value={json} onChange={(e) => setJson(e.target.value)} readOnly={readOnly} />
            {parsedSteps === null && <div className="hint" style={{ color: 'var(--bad)' }}>Not valid JSON yet.</div>}
            <label className="row small" style={{ marginTop: 8 }}><input type="checkbox" checked={useSetup} disabled={readOnly} onChange={(e) => setUseSetup(e.target.checked)} /> Run the project's sign-in setup steps before these steps</label>
          </div>
        </div>
        <div className="panel" style={{ alignSelf: 'start' }}><h2>Step reference</h2><pre className="small mono" style={{ whiteSpace: 'pre-wrap', margin: 0, color: 'var(--ink-2)' }}>{HELP}</pre></div>
      </div>
    </>
  )
}
