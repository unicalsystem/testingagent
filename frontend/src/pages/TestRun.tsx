import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, apiBase, ITEM_LABEL, ITEM_STATUSES, type ItemStatus, type TestRunDetail, type TestRunItem } from '../api'
import { Alert, Badge, Counts, Progress, fmtDate, useApp, useAsync, useProjectPerms } from '../components/ui'
import { AutomateModal } from '../components/Automate'
import type { Project } from '../api'
import { PushToTracker } from '../components/PushToTracker'

const PAGE = 100

function ItemBody({ it, editable, onSave, onNext }: {
  it: TestRunItem; editable: boolean
  onSave: (patch: Partial<Pick<TestRunItem, 'status' | 'actual_result' | 'notes'>>) => Promise<void>; onNext: () => void
}) {
  const [actual, setActual] = useState(it.actual_result), [notes, setNotes] = useState(it.notes), [saving, setSaving] = useState(false)
  useEffect(() => { setActual(it.actual_result); setNotes(it.notes) }, [it.id, it.actual_result, it.notes])
  const c = it.case
  const dirty = actual !== it.actual_result || notes !== it.notes
  const save = async (status?: ItemStatus) => {
    setSaving(true)
    try { await onSave({ ...(status ? { status } : {}), ...(dirty ? { actual_result: actual, notes } : {}) }); if (status && status !== 'untested') onNext() }
    finally { setSaving(false) }
  }
  return (
    <div className="body">
      <dl className="kv">
        {c.description && <><dt>Description</dt><dd>{c.description}</dd></>}
        {c.preconditions && <><dt>Pre-conditions</dt><dd>{c.preconditions}</dd></>}
        {c.test_data && <><dt>Test data</dt><dd>{c.test_data}</dd></>}
        <dt>Procedure</dt><dd>{c.procedure || <span className="muted">—</span>}</dd>
        <dt>Expected result</dt><dd>{c.expected_result || <span className="muted">—</span>}</dd>
        {(c.scenario_type || c.priority) && <><dt>Type · Priority</dt><dd>{[c.scenario_type, c.priority].filter(Boolean).join(' · ')}</dd></>}
        {it.assigned_to && <><dt>Assigned to</dt><dd>{it.assigned_to}</dd></>}
        {it.executed_at && <><dt>Last recorded</dt><dd>{fmtDate(it.executed_at)}{it.executed_by ? ` by ${it.executed_by}` : ''}</dd></>}
        {it.automated_run_id && <><dt>Automated run</dt><dd><Link to={`/runs/${it.automated_run_id}`}>#{it.automated_run_id}</Link> {it.automated_status && <Badge s={it.automated_status} />}</dd></>}
      </dl>
      {editable ? (
        <>
          <label className="f" htmlFor={`a${it.id}`} style={{ marginTop: 0 }}>Actual result</label>
          <textarea className="i" id={`a${it.id}`} style={{ minHeight: 64 }} value={actual} onChange={(e) => setActual(e.target.value)} placeholder="What actually happened (required for a fail is good practice)" />
          <label className="f" htmlFor={`n${it.id}`}>Notes / defect ID</label>
          <input className="i" id={`n${it.id}`} value={notes} onChange={(e) => setNotes(e.target.value)} />
          <div className="verdict" role="group" aria-label="Record result">
            {(['passed', 'failed', 'blocked', 'retest', 'skipped'] as ItemStatus[]).map((s) => (
              <button key={s} className={`btn ${s}${it.status === s ? ' on' : ''}`} disabled={saving} onClick={() => save(s)}>{ITEM_LABEL[s]}</button>
            ))}
          </div>
          <div className="row">
            {dirty && <button className="btn sm" disabled={saving} onClick={() => save()}>Save text only</button>}
            {it.status !== 'untested' && <button className="btn sm" disabled={saving} onClick={() => save('untested')}>Reset to Not run</button>}
            <Link className="btn sm" to={`/cases/${it.test_case_id}`}>Open test case</Link>
          </div>
        </>
      ) : (
        <>{it.actual_result && <dl className="kv"><dt>Actual result</dt><dd>{it.actual_result}</dd></dl>}
          {it.notes && <dl className="kv"><dt>Notes</dt><dd>{it.notes}</dd></dl>}</>
      )}
    </div>
  )
}

export default function TestRunPage() {
  const { tid = '' } = useParams()
  const nav = useNavigate()
  const tr = useAsync(() => api.get<TestRunDetail>(`/test-runs/${tid}`), [tid])
  const has = useProjectPerms(tr.data?.project_id)
  const canRecord = has('run.execute')
  const { status } = useApp()
  const canAutomate = has('ai.use') && has('case.write') && status?.storage?.ai_enabled !== false
  const [err, setErr] = useState(''), [msg, setMsg] = useState('')
  const [q, setQ] = useState(''), [st, setSt] = useState<ItemStatus | ''>(''), [mod, setMod] = useState('')
  const [open, setOpen] = useState<number | null>(null)
  const [sel, setSel] = useState<Record<number, boolean>>({})
  const [shown, setShown] = useState(PAGE)
  const [renaming, setRenaming] = useState(false), [newName, setNewName] = useState('')
  const [automate, setAutomate] = useState(false)
  const d = tr.data
  const proj = useAsync(() => (d ? api.get<Project>(`/projects/${d.project_id}`) : Promise.resolve(null)), [d?.project_id])

  // keep polling while automated runs are in flight
  const pending = d?.items.some((i) => i.automated_status === 'queued' || i.automated_status === 'running')
  useEffect(() => {
    if (!pending) return
    const t = setInterval(tr.reload, 4000)
    return () => clearInterval(t)
  }, [pending, tr.reload])

  const modules = useMemo(() => [...new Set((d?.items ?? []).map((i) => i.case.module).filter(Boolean))], [d])
  const list = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return (d?.items ?? []).filter((i) => (!st || i.status === st) && (!mod || i.case.module === mod) &&
      (!needle || `${i.case.external_id} ${i.case.name} ${i.case.sub_module}`.toLowerCase().includes(needle)))
  }, [d, q, st, mod])
  useEffect(() => setShown(PAGE), [q, st, mod])

  if (tr.error) return <Alert>{tr.error}</Alert>
  if (!d) return null
  const editable = canRecord && d.status === 'active'
  const selected = Object.keys(sel).filter((k) => sel[+k]).map(Number)
  const automatable = d.items.filter((i) => i.case.has_steps).length
  const manualSel = (selected.length ? d.items.filter((i) => sel[i.id]) : d.items).filter((i) => !i.case.has_steps)

  const act = async (fn: () => Promise<unknown>, ok?: string) => {
    setErr(''); setMsg('')
    try { await fn(); if (ok) setMsg(ok); tr.reload() } catch (x) { setErr((x as Error).message) }
  }
  const patch = async (it: TestRunItem, p: Partial<TestRunItem>) => {
    setErr('')
    try {
      const upd = await api.patch<TestRunItem>(`/test-runs/${d.id}/items/${it.id}`, p)
      const items = d.items.map((x) => (x.id === it.id ? { ...x, ...upd, case: x.case, automated_status: x.automated_status } : x))
      const counts = Object.fromEntries(ITEM_STATUSES.map((s) => [s, items.filter((x) => x.status === s).length])) as TestRunDetail['counts']
      tr.setData({ ...d, items, counts, progress: Math.round((100 * (items.length - counts.untested)) / Math.max(1, items.length)) })
    } catch (x) { setErr((x as Error).message); throw x }
  }
  const nextUntested = (from: TestRunItem) => {
    const idx = list.findIndex((i) => i.id === from.id)
    const nxt = list.slice(idx + 1).find((i) => i.status === 'untested')
    setOpen(nxt ? nxt.id : null)
    if (nxt) setTimeout(() => document.getElementById(`it${nxt.id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 50)
  }

  return (
    <>
      <div className="topbar">
        <div>
          <div className="crumbs"><Link to="/projects">Projects</Link> / <Link to={`/projects/${d.project_id}?tab=testruns`}>Project</Link> / Test runs /</div>
          {renaming ? (
            <form className="row" onSubmit={(e) => { e.preventDefault(); act(() => api.patch(`/test-runs/${d.id}`, { name: newName })).then(() => setRenaming(false)) }}>
              <input className="i" value={newName} onChange={(e) => setNewName(e.target.value)} maxLength={200} required style={{ flex: 1, minWidth: 200 }} autoFocus />
              <button className="btn primary">Save</button><button type="button" className="btn" onClick={() => setRenaming(false)}>Cancel</button>
            </form>
          ) : <h1>{d.name} {d.status === 'closed' && <Badge s="cancelled" label="closed" />}</h1>}
          <div className="sub">{d.group_label && <>{d.group_label} · </>}{d.total} cases · created {fmtDate(d.created_at)}{d.created_by ? ` by ${d.created_by}` : ''}</div>
        </div>
        <div className="row">
          <a className="btn" href={`${apiBase()}/test-runs/${d.id}/export?format=xlsx`}>⬇ Excel</a>
          <a className="btn" href={`${apiBase()}/test-runs/${d.id}/export?format=csv`}>CSV</a>
<PushToTracker type="manual" id={d.id} canPush={canRecord} />
          {canRecord && d.status === 'active' && automatable > 0 && (
            <button className="btn" onClick={() => act(async () => {
              const r = await api.post<{ queued: number; manual: number; problems: string[] }>(`/test-runs/${d.id}/automate`, selected.length ? { item_ids: selected } : {})
              if (r.problems.length) setErr(r.problems.join('\n'))
              setMsg(`${r.queued} automated run(s) queued; ${r.manual} case(s) are manual-only.`)
            })}>▶ Run automated ({selected.length ? 'selected' : automatable})</button>
          )}
          {canAutomate && d.status === 'active' && manualSel.length > 0 && <button className="btn" onClick={() => setAutomate(true)}>✦ Automate {selected.length ? 'selected' : 'manual'} with AI ({manualSel.length})</button>}
          {canRecord && <button className="btn" onClick={() => { setNewName(d.name); setRenaming(true) }}>Rename</button>}
          {canRecord && <button className="btn" onClick={() => act(() => api.patch(`/test-runs/${d.id}`, { status: d.status === 'active' ? 'closed' : 'active' }))}>{d.status === 'active' ? 'Close run' : 'Reopen'}</button>}
          {canRecord && <button className="btn danger" onClick={() => { if (confirm(`Delete test run “${d.name}” and its recorded results? The test cases are kept.`)) act(async () => { await api.del(`/test-runs/${d.id}`); nav(`/projects/${d.project_id}?tab=testruns`) }) }}>Delete</button>}
        </div>
      </div>
      <Alert>{err}</Alert><Alert kind="ok">{msg}</Alert>
      {d.status === 'closed' && <Alert kind="warn">This run is closed. Reopen it to record more results.</Alert>}

      <div className="panel">
        <div className="row" style={{ justifyContent: 'space-between' }}><b>{d.progress}% executed</b><span className="sub">{d.total - d.counts.untested} of {d.total}</span></div>
        <div style={{ marginTop: 8 }}><Progress counts={d.counts} total={d.total} /></div>
        <Counts counts={d.counts} />
      </div>

      <div className="panel" style={{ padding: 0 }}>
        <div className="toolbar">
          <input className="i" type="search" placeholder="Search ID or title" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search" />
          {modules.length > 1 && (
            <select className="i" value={mod} onChange={(e) => setMod(e.target.value)} aria-label="Module">
              <option value="">All modules</option>{modules.map((m) => <option key={m}>{m}</option>)}
            </select>
          )}
          <div className="chips">
            <button className={`chip${st === '' ? ' on' : ''}`} onClick={() => setSt('')}>All {d.total}</button>
            {ITEM_STATUSES.filter((s) => d.counts[s]).map((s) => <button key={s} className={`chip${st === s ? ' on' : ''}`} onClick={() => setSt(s)}>{ITEM_LABEL[s]} {d.counts[s]}</button>)}
          </div>
        </div>
        {list.length === 0 && <div className="empty">No cases match.</div>}
        <ul className="items">
          {list.slice(0, shown).map((it) => (
            <li key={it.id} id={`it${it.id}`} className={`item${open === it.id ? ' open' : ''}`}>
              <div className="head" onClick={() => setOpen(open === it.id ? null : it.id)}>
                {editable ? <input type="checkbox" aria-label="Select" checked={!!sel[it.id]} onClick={(e) => e.stopPropagation()} onChange={(e) => setSel({ ...sel, [it.id]: e.target.checked })} style={{ marginTop: 3 }} /> : <span />}
                <div style={{ minWidth: 0 }}>
                  <div style={{ wordBreak: 'break-word' }}>{it.case.external_id && <span className="mono muted">{it.case.external_id} · </span>}<b>{it.case.name}</b></div>
                  <div className="sub">{[it.case.module, it.case.sub_module, it.case.priority].filter(Boolean).join(' · ')}{it.case.has_steps ? ' · automated' : ''}</div>
                </div>
                <Badge s={it.status} label={ITEM_LABEL[it.status]} />
              </div>
              {open === it.id && <ItemBody it={it} editable={editable} onSave={(p) => patch(it, p)} onNext={() => nextUntested(it)} />}
            </li>
          ))}
        </ul>
        {list.length > shown && <div className="morebar"><button className="btn" onClick={() => setShown(shown + PAGE)}>Show more ({list.length - shown} remaining)</button></div>}
      </div>

      {automate && proj.data && <AutomateModal pid={d.project_id} count={manualSel.length} testRunId={d.id} itemIds={selected.length ? manualSel.map((i) => i.id) : undefined}
        hasSetup={!!proj.data.setup_steps?.length} baseUrl={proj.data.base_url} onClose={() => setAutomate(false)} />}
      {editable && selected.length > 0 && (
        <div className="bulkbar">
          <b>{selected.length} selected</b>
          <span style={{ flex: 1 }} />
          {(['passed', 'failed', 'blocked', 'untested'] as ItemStatus[]).map((s) => (
            <button key={s} className="btn sm" onClick={() => act(() => api.post(`/test-runs/${d.id}/items/bulk`, { item_ids: selected, status: s }), `${selected.length} marked ${ITEM_LABEL[s]}`).then(() => setSel({}))}>Mark {ITEM_LABEL[s]}</button>
          ))}
          <button className="btn sm" onClick={() => { if (confirm(`Remove ${selected.length} case(s) from this run?`)) act(async () => { for (const id of selected) await api.del(`/test-runs/${d.id}/items/${id}`) }).then(() => setSel({})) }}>Remove</button>
          <button className="btn sm" onClick={() => setSel({})}>Clear</button>
        </div>
      )}
    </>
  )
}
