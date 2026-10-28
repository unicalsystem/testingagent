import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, AUTO_LABEL, type AutoStatus, type AutomationItem, type AutomationJobDetail } from '../api'
import { Alert, Badge, fmtDate, useAsync, useProjectPerms } from '../components/ui'

const BADGE: Record<AutoStatus, string> = {
  queued: 'queued', drafting: 'running', repair: 'running', repairing: 'running', trial: 'running', drafted: 'retest',
  verified: 'passed', failed: 'failed', manual_only: 'skipped', applied: 'passed', rejected: 'cancelled', error: 'error', cancelled: 'cancelled',
}
const ACTIVE: AutoStatus[] = ['queued', 'drafting', 'repair', 'repairing', 'trial']
const REVIEWABLE: AutoStatus[] = ['drafted', 'verified', 'failed', 'applied', 'rejected', 'error', 'manual_only']
const JOB_LABEL = { exploring: 'Waiting for the agent', mapping: 'Exploring the application', running: 'Automating cases', done: 'Finished', cancelled: 'Cancelled' }

function ItemPanel({ it, editable, onChange }: { it: AutomationItem; editable: boolean; onChange: () => void }) {
  const [json, setJson] = useState(JSON.stringify(it.steps, null, 2))
  const [useSetup, setUseSetup] = useState(it.use_setup), [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  useEffect(() => { setJson(JSON.stringify(it.steps, null, 2)); setUseSetup(it.use_setup) }, [it.steps, it.use_setup])
  const act = async (fn: () => Promise<unknown>) => { setBusy(true); setErr(''); try { await fn(); onChange() } catch (x) { setErr((x as Error).message) } finally { setBusy(false) } }
  const save = () => act(async () => {
    let steps
    try { steps = JSON.parse(json) } catch (e) { throw new Error('Steps are not valid JSON: ' + (e as Error).message) }
    await api.post(`/automation-items/${it.id}/approve`, { steps, use_setup: useSetup })
  })
  const active = ACTIVE.includes(it.status)
  return (
    <div className="body">
      <Alert>{err}</Alert>
      <dl className="kv">
        <dt>Procedure</dt><dd>{it.case.procedure || '—'}</dd>
        <dt>Expected result</dt><dd>{it.case.expected_result || '—'}</dd>
        {it.reason && <><dt>Claude's notes</dt><dd>{it.reason}</dd></>}
        {it.error && <><dt>{it.status === 'queued' ? 'Status' : 'Last failure'}</dt><dd><pre className="err" style={{ margin: 0 }}>{it.error}</pre></dd></>}
        {it.trial_run_id && <><dt>Trial run</dt><dd><Link to={`/runs/${it.trial_run_id}`}>#{it.trial_run_id}</Link> {it.trial_status && <Badge s={it.trial_status} />} · attempt {it.attempts}</dd></>}
      </dl>
      {it.status !== 'manual_only' && (it.steps.length > 0 || editable) && !active && (
        <>
          <label className="f" htmlFor={`s${it.id}`} style={{ marginTop: 0 }}>Automated steps {editable && <span className="muted small" style={{ fontWeight: 400 }}>(editable)</span>}</label>
          <textarea className="i code" id={`s${it.id}`} style={{ minHeight: 200 }} spellCheck={false} value={json} onChange={(e) => setJson(e.target.value)} readOnly={!editable} />
          <label className="row small" style={{ marginTop: 6 }}><input type="checkbox" checked={useSetup} disabled={!editable} onChange={(e) => setUseSetup(e.target.checked)} /> Run the project's sign-in setup steps first</label>
        </>
      )}
      {editable && !active && (
        <div className="row" style={{ marginTop: 10 }}>
          {it.status !== 'manual_only' && <button className="btn primary sm" disabled={busy} onClick={save}>{it.status === 'applied' ? 'Save changes to case' : 'Approve & save to case'}</button>}
          {!['rejected', 'applied'].includes(it.status) && <button className="btn sm" disabled={busy} onClick={() => act(() => api.post(`/automation-items/${it.id}/reject`))}>Reject</button>}
          <button className="btn sm" disabled={busy} onClick={() => act(() => api.post(`/automation-items/${it.id}/retry`))}>↻ Try again with AI</button>
          <Link className="btn sm" to={`/cases/${it.case.id}`}>Open case</Link>
        </div>
      )}
    </div>
  )
}

export default function AutomationJobPage() {
  const { jid = '' } = useParams()
  const job = useAsync(() => api.get<AutomationJobDetail>(`/automation-jobs/${jid}`), [jid])
  const has = useProjectPerms(job.data?.project_id)
  const [open, setOpen] = useState<number | null>(null), [filter, setFilter] = useState<AutoStatus | ''>('')
  const [err, setErr] = useState(''), [msg, setMsg] = useState(''), [q, setQ] = useState('')
  const j = job.data
  const live = j && (['exploring', 'mapping'].includes(j.status) || j.items.some((i) => ACTIVE.includes(i.status)))
  useEffect(() => {
    if (!live) return
    const t = setInterval(() => job.reload(), 4000)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live])
  const list = useMemo(() => (j?.items ?? []).filter((i) => (!filter || i.status === filter) &&
    (!q || `${i.case.external_id} ${i.case.name}`.toLowerCase().includes(q.toLowerCase()))), [j, filter, q])
  if (job.error) return <Alert>{job.error}</Alert>
  if (!j) return null
  const editable = has('case.write')  // review, approve, reject, cancel (retry also needs ai.use; enforced by the server)
  const c = j.counts
  const act = async (fn: () => Promise<string>) => { setErr(''); setMsg(''); try { setMsg(await fn()); job.reload() } catch (x) { setErr((x as Error).message) } }
  const statuses = (Object.keys(c) as AutoStatus[]).filter((k) => c[k])

  return (
    <>
      <div className="topbar">
        <div>
          <div className="crumbs"><Link to="/projects">Projects</Link> / <Link to={`/projects/${j.project_id}?tab=automation`}>Project</Link> / AI automation /</div>
          <h1>AI automation #{j.id} <Badge s={j.status === 'done' ? 'passed' : j.status === 'cancelled' ? 'cancelled' : 'running'} label={JOB_LABEL[j.status]} /></h1>
          <div className="sub">{j.total} manual cases · {j.claude_calls} Claude calls · started {fmtDate(j.created_at)}{j.test_run && <> · from test run <Link to={`/test-runs/${j.test_run.id}`}>{j.test_run.name}</Link></>}
            {' · '}{j.options.verify ? `verify by running (${j.options.max_attempts} attempts)` : 'no verification'}{j.options.auto_apply ? ', auto-save' : ', review before saving'}</div>
        </div>
        {editable && (
          <div className="row">
            {!!c.verified && <button className="btn primary" onClick={() => act(async () => `${(await api.post<{ applied: number }>(`/automation-jobs/${j.id}/approve`, { statuses: ['verified'] })).applied} verified case(s) saved`)}>Save all verified ({c.verified})</button>}
            {!!c.drafted && <button className="btn" onClick={() => { if (confirm(`Save ${c.drafted} unverified draft(s) to their cases?`)) act(async () => `${(await api.post<{ applied: number }>(`/automation-jobs/${j.id}/approve`, { statuses: ['drafted'] })).applied} draft(s) saved`) }}>Save all drafts ({c.drafted})</button>}
            {!['done', 'cancelled'].includes(j.status) && <button className="btn danger" onClick={() => act(async () => { await api.post(`/automation-jobs/${j.id}/cancel`); return 'Cancelled. Cases already processed keep their results.' })}>Cancel</button>}
          </div>
        )}
      </div>
      <Alert>{err}</Alert><Alert kind="ok">{msg}</Alert>
      <div className="panel">
        <div className="row" style={{ justifyContent: 'space-between' }}><b>{j.progress}% processed</b><span className="sub">{live ? 'updating live…' : ''}</span></div>
        <div className="prog" style={{ marginTop: 8 }}>
          {(['applied', 'verified', 'drafted', 'failed', 'manual_only', 'error'] as AutoStatus[]).map((k) => !!c[k] &&
            <span key={k} className={`seg ${({ applied: 'passed', verified: 'passed', drafted: 'retest', failed: 'failed', manual_only: 'skipped', error: 'failed' } as Record<string, string>)[k]}`} style={{ width: `${(100 * (c[k] ?? 0)) / Math.max(1, j.total)}%` }} />)}
        </div>
        <div className="counts">{statuses.map((k) => <span key={k} className={`cnt ${BADGE[k]}`}><i />{AUTO_LABEL[k]} {c[k]}</span>)}</div>
        {(j.explore_log || j.has_map) && (
          <details style={{ marginTop: 10 }}><summary className="small">What the agent found while exploring</summary>
            {j.explore_log && <pre className="small mono" style={{ whiteSpace: 'pre-wrap' }}>{j.explore_log}</pre>}
            {j.app_map && <pre className="small mono" style={{ whiteSpace: 'pre-wrap', maxHeight: 360, overflow: 'auto', background: '#f8fafd', padding: 10, borderRadius: 6 }}>{j.app_map}</pre>}
          </details>
        )}
      </div>
      <div className="panel" style={{ padding: 0 }}>
        <div className="toolbar">
          <input className="i" type="search" placeholder="Search ID or title" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search" />
          <div className="chips">
            <button className={`chip${filter === '' ? ' on' : ''}`} onClick={() => setFilter('')}>All {j.total}</button>
            {statuses.map((k) => <button key={k} className={`chip${filter === k ? ' on' : ''}`} onClick={() => setFilter(k)}>{AUTO_LABEL[k]} {c[k]}</button>)}
          </div>
        </div>
        <ul className="items">
          {list.slice(0, 300).map((it) => (
            <li key={it.id} className={`item${open === it.id ? ' open' : ''}`}>
              <div className="head" style={{ gridTemplateColumns: '1fr auto' }} onClick={() => setOpen(open === it.id ? null : it.id)}>
                <div style={{ minWidth: 0 }}>
                  <div style={{ wordBreak: 'break-word' }}>{it.case.external_id && <span className="mono muted">{it.case.external_id} · </span>}<b>{it.case.name}</b></div>
                  <div className="sub">{[it.case.module, it.steps.length ? `${it.steps.length} steps` : '', it.attempts ? `attempt ${it.attempts}` : ''].filter(Boolean).join(' · ')}</div>
                </div>
                <Badge s={BADGE[it.status]} label={AUTO_LABEL[it.status]} />
              </div>
              {open === it.id && <ItemPanel it={it} editable={editable && REVIEWABLE.includes(it.status)} onChange={job.reload} />}
            </li>
          ))}
        </ul>
        {list.length > 300 && <div className="morebar muted small">Showing 300 of {list.length}. Use the filters to narrow down.</div>}
      </div>
    </>
  )
}
