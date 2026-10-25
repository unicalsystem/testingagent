import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, type Run, type Step, type TestCase } from '../api'
import { Alert, useApp } from '../components/ui'

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
  const { can } = useApp()
  const nav = useNavigate()
  const [tc, setTc] = useState<TestCase | null>(null)
  const [name, setName] = useState(''), [desc, setDesc] = useState(''), [tags, setTags] = useState('')
  const [json, setJson] = useState(JSON.stringify(TEMPLATE, null, 2))
  const [err, setErr] = useState(''), [ok, setOk] = useState('')
  const readOnly = !can('tester')

  useEffect(() => {
    if (!cid) return
    api.get<TestCase>(`/cases/${cid}`).then((c) => {
      setTc(c); setName(c.name); setDesc(c.description); setTags(c.tags.join(', ')); setJson(JSON.stringify(c.steps, null, 2))
    }).catch((e) => setErr(e.message))
  }, [cid])

  const projectId = tc?.project_id ?? Number(pid)
  const save = async (thenRun = false) => {
    setErr(''); setOk('')
    let steps: Step[]
    try { steps = JSON.parse(json) } catch (e) { return setErr('Steps are not valid JSON: ' + (e as Error).message) }
    const body = { name, description: desc, tags: tags.split(',').map((t) => t.trim()).filter(Boolean), steps }
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

  return (
    <>
      <div className="topbar">
        <div><div className="crumbs"><Link to="/projects">Projects</Link> / <Link to={`/projects/${projectId}`}>Project</Link> /</div>
          <h1>{tc ? 'Edit test case' : 'New test case'}</h1></div>
        {!readOnly && <div className="row">
          {tc && <button className="btn danger" onClick={archive}>Archive</button>}
          <button className="btn" onClick={() => save(false)}>Save</button>
          <button className="btn primary" onClick={() => save(true)}>Save &amp; run</button>
        </div>}
      </div>
      <Alert>{err}</Alert><Alert kind="ok">{ok}</Alert>
      <div className="grid2" style={{ gridTemplateColumns: 'minmax(0,2fr) minmax(260px,1fr)' }}>
        <div className="panel">
          <label className="f" htmlFor="n" style={{ marginTop: 0 }}>Name</label>
          <input className="i" id="n" value={name} onChange={(e) => setName(e.target.value)} readOnly={readOnly} />
          <label className="f" htmlFor="d">Description</label>
          <textarea className="i" id="d" style={{ minHeight: 60 }} value={desc} onChange={(e) => setDesc(e.target.value)} readOnly={readOnly} />
          <label className="f" htmlFor="tg">Tags (comma separated)</label>
          <input className="i" id="tg" value={tags} onChange={(e) => setTags(e.target.value)} readOnly={readOnly} />
          <label className="f" htmlFor="s">Steps</label>
          <textarea className="i code" id="s" spellCheck={false} value={json} onChange={(e) => setJson(e.target.value)} readOnly={readOnly} />
        </div>
        <div className="panel"><h2>Step reference</h2><pre className="small mono" style={{ whiteSpace: 'pre-wrap', margin: 0, color: 'var(--ink-2)' }}>{HELP}</pre></div>
      </div>
    </>
  )
}
