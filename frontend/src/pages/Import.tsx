import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, apiBase, ITEM_LABEL, type ImportField, type ImportPreview, type ItemStatus, type Project } from '../api'
import { Alert, useAsync } from '../components/ui'

type Step = 0 | 1 | 2 | 3 | 4
type Mode = 'group' | 'single' | 'none'
interface CommitResult { batch: string; created: number; updated: number; skipped: number; error_count: number; errors: { row: number; error: string }[]; test_runs: { id: number; name: string }[] }

const STEPS = ['1 · Upload', '2 · Match columns', '3 · Organise test runs', '4 · Review & confirm']
const IGNORE = ''

function form(file: File, extra: Record<string, string>) {
  const f = new FormData()
  f.append('file', file)
  for (const [k, v] of Object.entries(extra)) f.append(k, v)
  return f
}

export default function ImportWizard() {
  const { pid = '' } = useParams()
  const nav = useNavigate()
  const proj = useAsync(() => api.get<Project>(`/projects/${pid}`), [pid])
  const fields = useAsync(() => api.get<ImportField[]>('/imports/fields'), [])
  const [step, setStep] = useState<Step>(0)
  const [file, setFile] = useState<File | null>(null)
  const [pv, setPv] = useState<ImportPreview | null>(null)
  const [mapping, setMapping] = useState<Record<string, string>>({})
  const [sheet, setSheet] = useState('')
  const [busy, setBusy] = useState(false), [err, setErr] = useState('')
  const [over, setOver] = useState(false)
  // run plan
  const [mode, setMode] = useState<Mode>('group')
  const [groupBy, setGroupBy] = useState('')
  const [picked, setPicked] = useState<Record<string, boolean>>({})
  const [prefix, setPrefix] = useState('')
  const [singleName, setSingleName] = useState('')
  const [applyResults, setApplyResults] = useState(true)
  const [dups, setDups] = useState<'update' | 'skip' | 'create'>('update')
  const [consent, setConsent] = useState(false)
  const [done, setDone] = useState<CommitResult | null>(null)
  const [undone, setUndone] = useState('')
  const input = useRef<HTMLInputElement>(null)

  const runPreview = async (f: File, map?: Record<string, string>, sh?: string) => {
    setBusy(true); setErr('')
    try {
      const extra: Record<string, string> = {}
      if (sh) extra.sheet = sh
      if (map) extra.mapping = JSON.stringify(map)
      const r = await api.upload<ImportPreview>(`/projects/${pid}/imports/preview`, form(f, extra))
      setPv(r); setMapping(r.mapping); setSheet(r.sheet)
      return r
    } catch (x) { setErr((x as Error).message); return null } finally { setBusy(false) }
  }

  const choose = async (f: File | undefined) => {
    if (!f) return
    if (f.size > 10 * 1024 * 1024) return setErr('The file is larger than 10 MB')
    setFile(f); setPv(null)
    const r = await runPreview(f)
    if (r) {
      setStep(1)
      const stem = f.name.replace(/\.(csv|xlsx|xlsm)$/i, '').replace(/[_]+/g, ' ').trim()
      setPrefix(''); setSingleName(stem)
    }
  }

  // re-validate when the mapping changes (debounced)
  const first = useRef(true)
  useEffect(() => {
    if (!file || step !== 1) return
    if (first.current) { first.current = false; return }
    const t = setTimeout(() => runPreview(file, mapping, sheet), 400)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mapping])

  // initialise grouping from the server's recommendation
  useEffect(() => {
    if (!pv?.group_options) return
    const valid = pv.group_options.map((o) => o.field)
    if (!groupBy || !valid.includes(groupBy)) {
      const rec = pv.recommended_group_by?.[0] ?? ''
      setGroupBy(rec)
      if (!rec) setMode(pv.group_options.length ? 'group' : 'single')
    }
    const anyResults = Object.entries(pv.results ?? {}).some(([k, n]) => k !== 'untested' && n > 0)
    setApplyResults(anyResults)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pv])

  const groups = useMemo(() => (groupBy && pv?.groups?.[groupBy]) || [], [groupBy, pv])
  useEffect(() => { setPicked(Object.fromEntries(groups.map((g) => [g.value, true]))) }, [groups])

  const groupLabel = pv?.group_options?.find((o) => o.field === groupBy)?.label ?? ''
  const tooMany = groupBy && (pv?.group_options?.find((o) => o.field === groupBy)?.distinct ?? 0) > 200
  const selectedGroups = groups.filter((g) => picked[g.value])
  const casesInRuns = mode === 'none' ? 0 : mode === 'single' ? pv?.valid ?? 0 : selectedGroups.reduce((a, g) => a + g.count, 0)
  const runCount = mode === 'none' ? 0 : mode === 'single' ? 1 : selectedGroups.length
  const fieldOpts = fields.data ?? []
  const used = new Set(Object.values(mapping).filter((v) => v && !v.startsWith('attr:')))
  const canNext1 = pv && !pv.mapping_error && (pv.valid ?? 0) > 0 && !busy
  const canNext2 = mode === 'none' || (mode === 'single' ? singleName.trim() : groupBy && selectedGroups.length > 0 && !tooMany)
  const hasResultCol = Object.values(mapping).includes('result')

  const commit = async () => {
    if (!file || !pv) return
    setBusy(true); setErr('')
    const options = {
      sheet, mapping, duplicates: dups, confirm: consent,
      run_plan: {
        mode, group_by: mode === 'group' ? [groupBy] : [],
        groups: mode === 'group' && selectedGroups.length !== groups.length ? selectedGroups.map((g) => g.value) : null,
        name: mode === 'single' ? singleName : prefix, apply_results: applyResults,
      },
    }
    try {
      setDone(await api.upload<CommitResult>(`/projects/${pid}/imports/commit`, form(file, { options: JSON.stringify(options) })))
      setStep(4)
    } catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }

  const undo = async () => {
    if (!done || !confirm('Undo this import? New cases are archived and the test runs it created are deleted. Cases it updated keep their new values.')) return
    try {
      const r = await api.post<{ archived_cases: number; deleted_test_runs: number }>(`/projects/${pid}/imports/${done.batch}/undo`)
      setUndone(`Undone: ${r.archived_cases} case(s) archived, ${r.deleted_test_runs} test run(s) deleted.`)
    } catch (x) { setErr((x as Error).message) }
  }

  const sampleOf = (h: string) => pv?.sample.map((r) => r[h]).find((v) => v) ?? ''

  return (
    <>
      <div className="topbar">
        <div><div className="crumbs"><Link to="/projects">Projects</Link> / <Link to={`/projects/${pid}`}>{proj.data?.name ?? 'Project'}</Link> /</div>
          <h1>Import test cases</h1></div>
      </div>
      {step < 4 && <div className="wsteps">{STEPS.map((s, i) => <div key={s} className={i === step ? 'on' : i < step ? 'done' : ''}>{s}</div>)}</div>}
      <Alert>{err}</Alert>

      {step === 0 && (
        <div className="panel">
          <h2>Upload a CSV or Excel file</h2>
          <div className={`drop${over ? ' over' : ''}`} onClick={() => input.current?.click()}
            onDragOver={(e) => { e.preventDefault(); setOver(true) }} onDragLeave={() => setOver(false)}
            onDrop={(e) => { e.preventDefault(); setOver(false); choose(e.dataTransfer.files[0]) }}>
            <div style={{ fontSize: 28 }}>⬆</div>
            <b>{busy ? 'Reading file…' : 'Tap to choose a file, or drop it here'}</b>
            <div className="hint">.xlsx or .csv · up to 10 MB · up to 5,000 test cases</div>
            <input ref={input} type="file" accept=".csv,.xlsx,.xlsm,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" hidden
              onChange={(e) => choose(e.target.files?.[0])} />
          </div>
          <h3 style={{ marginTop: 18 }}>Your own format works</h3>
          <p className="muted small" style={{ marginTop: 0 }}>You don't need to rename your columns. After upload, TestAgent matches them to its fields automatically and you can change any match. Nothing is saved until you confirm in step 4.</p>
          <h3>Or start from the template</h3>
          <div className="row">
            <a className="btn" href={`${apiBase()}/imports/template?format=xlsx`}>⬇ Excel template</a>
            <a className="btn" href={`${apiBase()}/imports/template?format=csv`}>⬇ CSV template</a>
          </div>
          <p className="hint">The Excel template includes an Instructions sheet and drop-down lists for Priority, Scenario Type and Result.</p>
        </div>
      )}

      {step === 1 && pv && (
        <div className="panel">
          <div className="row" style={{ justifyContent: 'space-between' }}>
            <h2 style={{ margin: 0 }}>Match your columns</h2>
            <span className="muted small">{pv.file} · {pv.row_count} rows{pv.truncated ? ` (only the first ${pv.max_rows} are imported)` : ''}</span>
          </div>
          {pv.sheets.length > 1 && (
            <div style={{ maxWidth: 360 }}>
              <label className="f" htmlFor="sh">Sheet</label>
              <select className="i" id="sh" value={sheet} onChange={(e) => { setSheet(e.target.value); first.current = true; if (file) runPreview(file, undefined, e.target.value) }}>
                {pv.sheets.map((s) => <option key={s}>{s}</option>)}
              </select>
            </div>
          )}
          <p className="muted small">We matched {Object.values(mapping).filter((v) => v && !v.startsWith('attr:')).length} of {pv.headers.length} columns automatically. Unrecognised columns are kept as custom fields (you can group runs by them) unless you choose “Ignore”.</p>
          <div>
            {pv.headers.map((h) => {
              const v = mapping[h] ?? ''
              return (
                <div className="maprow" key={h}>
                  <div><b style={{ wordBreak: 'break-word' }}>{h}</b>{pv.suggested[h] === v && v && !v.startsWith('attr:') && <span className="tag" style={{ marginLeft: 6 }}>auto</span>}</div>
                  <select className="i" aria-label={`Field for column ${h}`} value={v} onChange={(e) => setMapping({ ...mapping, [h]: e.target.value })}>
                    <option value={IGNORE}>— Ignore this column —</option>
                    <option value={`attr:${h}`}>Keep as custom field “{h}”</option>
                    <optgroup label="Test case">
                      {fieldOpts.filter((f) => f.target !== 'result').map((f) => <option key={f.key} value={f.key} disabled={used.has(f.key) && v !== f.key}>{f.label}{f.required ? ' *' : ''}</option>)}
                    </optgroup>
                    <optgroup label="Existing result (goes into the test run)">
                      {fieldOpts.filter((f) => f.target === 'result').map((f) => <option key={f.key} value={f.key} disabled={used.has(f.key) && v !== f.key}>{f.label}</option>)}
                    </optgroup>
                  </select>
                  <div className="ex" title={sampleOf(h)}>{sampleOf(h) ? `e.g. ${sampleOf(h).split('\n')[0]}` : <i>empty in the first rows</i>}</div>
                </div>
              )
            })}
          </div>
          <div style={{ marginTop: 14 }}>
            {pv.mapping_error ? <Alert>{pv.mapping_error}</Alert> : (
              <Alert kind={pv.error_count ? 'warn' : 'ok'}>
                {busy ? 'Checking…' : <>{pv.valid} test case(s) ready to import{pv.existing_matches ? ` (${pv.existing_matches} already exist in this project by ID)` : ''}.
                  {!!pv.error_count && <> {pv.error_count} row(s) will be skipped.</>}
                  {!!pv.warning_count && <> {pv.warning_count} row(s) have warnings.</>}</>}
              </Alert>
            )}
            {!!pv.errors?.length && (
              <details><summary className="small">Show skipped rows</summary>
                <ul className="small">{pv.errors.map((e) => <li key={e.row}>Row {e.row}: {e.error}</li>)}</ul></details>
            )}
            {!!pv.warnings?.length && (
              <details><summary className="small">Show warnings</summary>
                <ul className="small">{pv.warnings.map((e) => <li key={e.row}>Row {e.row}: {e.warning}</li>)}</ul></details>
            )}
          </div>
          <div className="wnav">
            <button className="btn" onClick={() => { setStep(0); setFile(null); setPv(null); first.current = true }}>← Choose another file</button>
            <button className="btn primary" disabled={!canNext1} onClick={() => setStep(2)}>Next: organise test runs →</button>
          </div>
        </div>
      )}

      {step === 2 && pv && (
        <div className="panel">
          <h2>How should we organise the test runs?</h2>
          <p className="muted small" style={{ marginTop: -4 }}>A test run is a checklist of cases that testers execute and mark Pass / Fail / Blocked. You can create more runs from these cases later.</p>
          <label className={`opt${mode === 'group' ? ' on' : ''}`}>
            <input type="radio" name="mode" checked={mode === 'group'} onChange={() => setMode('group')} disabled={!pv.group_options?.length} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <b>One test run per group</b>
              <div className="small muted">e.g. one run per Module, so each area can be assigned and tracked separately.</div>
              {mode === 'group' && (
                <>
                  <div className="f2">
                    <div>
                      <label className="f" htmlFor="gb">Group by</label>
                      <select className="i" id="gb" value={groupBy} onChange={(e) => setGroupBy(e.target.value)}>
                        <option value="">Choose a field…</option>
                        {pv.group_options?.map((o) => <option key={o.field} value={o.field}>{o.label} ({o.distinct} value{o.distinct === 1 ? '' : 's'}){pv.recommended_group_by?.[0] === o.field ? ' — recommended' : ''}</option>)}
                      </select>
                    </div>
                    <div>
                      <label className="f" htmlFor="px">Run name prefix (optional)</label>
                      <input className="i" id="px" value={prefix} onChange={(e) => setPrefix(e.target.value)} maxLength={150} />
                    </div>
                  </div>
                  {tooMany && <Alert kind="warn">That field has too many distinct values to make one run each. Pick a coarser field.</Alert>}
                  {!!groups.length && (
                    <>
                      <div className="row" style={{ justifyContent: 'space-between', marginTop: 10 }}>
                        <span className="small"><b>{selectedGroups.length}</b> of {groups.length} runs selected</span>
                        <span className="row"><button type="button" className="btn sm" onClick={() => setPicked(Object.fromEntries(groups.map((g) => [g.value, true])))}>All</button>
                          <button type="button" className="btn sm" onClick={() => setPicked({})}>None</button></span>
                      </div>
                      <div className="grouplist">
                        {groups.map((g) => (
                          <label key={g.value}><input type="checkbox" checked={!!picked[g.value]} onChange={(e) => setPicked({ ...picked, [g.value]: e.target.checked })} />
                            <span style={{ minWidth: 0, wordBreak: 'break-word' }}>{prefix ? `${prefix} – ` : ''}{g.value}</span><span className="n">{g.count} case{g.count === 1 ? '' : 's'}</span></label>
                        ))}
                      </div>
                      <div className="hint">Unticked groups are still imported as test cases, just not put into a run.</div>
                    </>
                  )}
                </>
              )}
            </div>
          </label>
          <label className={`opt${mode === 'single' ? ' on' : ''}`}>
            <input type="radio" name="mode" checked={mode === 'single'} onChange={() => setMode('single')} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <b>One test run with all {pv.valid} cases</b>
              <div className="small muted">Best for a single release or sprint cycle.</div>
              {mode === 'single' && <><label className="f" htmlFor="sn">Run name</label><input className="i" id="sn" value={singleName} onChange={(e) => setSingleName(e.target.value)} maxLength={150} /></>}
            </div>
          </label>
          <label className={`opt${mode === 'none' ? ' on' : ''}`}>
            <input type="radio" name="mode" checked={mode === 'none'} onChange={() => setMode('none')} />
            <div><b>Don't create test runs now</b><div className="small muted">Only import the cases. Create runs later from the Test runs tab.</div></div>
          </label>

          {(!!pv.existing_matches || hasResultCol) && <h3 style={{ marginTop: 16 }}>Options</h3>}
          {!!pv.existing_matches && (
            <div style={{ maxWidth: 520 }}>
              <label className="f" htmlFor="dp">{pv.existing_matches} case(s) already exist with the same Test Case ID</label>
              <select className="i" id="dp" value={dups} onChange={(e) => setDups(e.target.value as typeof dups)}>
                <option value="update">Update them with the file's values (recommended)</option>
                <option value="skip">Keep them unchanged</option>
                <option value="create">Create duplicates anyway</option>
              </select>
              <div className="hint">Automated steps on existing cases are never touched.</div>
            </div>
          )}
          {hasResultCol && mode !== 'none' && (
            <label className="row" style={{ marginTop: 12, alignItems: 'flex-start' }}>
              <input type="checkbox" checked={applyResults} onChange={(e) => setApplyResults(e.target.checked)} style={{ marginTop: 4 }} />
              <span>Copy the file's Result / Actual Result / Executed By / Executed On into the new run(s)
                <span className="hint" style={{ display: 'block' }}>{Object.entries(pv.results ?? {}).map(([k, n]) => `${ITEM_LABEL[k as ItemStatus] ?? k}: ${n}`).join(' · ')}</span></span>
            </label>
          )}
          <div className="wnav">
            <button className="btn" onClick={() => setStep(1)}>← Back</button>
            <button className="btn primary" disabled={!canNext2} onClick={() => { setConsent(false); setStep(3) }}>Next: review →</button>
          </div>
        </div>
      )}

      {step === 3 && pv && (
        <div className="panel">
          <h2>Review and confirm</h2>
          <ul className="summary">
            <li>Import into project <b>{proj.data?.name}</b> from <b>{pv.file}</b>{pv.sheets.length > 1 ? <> (sheet “{sheet}”)</> : null}.</li>
            <li><b>{pv.new_cases}</b> new test case(s) will be created{pv.existing_matches ? <>; <b>{pv.existing_matches}</b> existing case(s) will be {dups === 'update' ? 'updated' : dups === 'skip' ? 'left unchanged' : 'duplicated'}</> : null}.</li>
            {!!pv.error_count && <li><b>{pv.error_count}</b> row(s) will be skipped (no title/ID or duplicate ID).</li>}
            {mode === 'none' ? <li>No test runs will be created.</li> : (
              <li><b>{runCount}</b> test run(s) containing <b>{casesInRuns}</b> case(s){mode === 'group' ? <>, one per <b>{groupLabel}</b></> : <>: “{singleName}”</>}{applyResults && hasResultCol ? ', with the results from the file' : ', all marked Not run'}.</li>
            )}
          </ul>
          {mode === 'group' && runCount > 0 && (
            <details style={{ marginTop: 8 }}><summary className="small">Show the {runCount} run names</summary>
              <ul className="small">{selectedGroups.map((g) => <li key={g.value}>{prefix ? `${prefix} – ` : ''}{g.value} ({g.count})</li>)}</ul></details>
          )}
          <label className="consent">
            <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} style={{ marginTop: 3 }} />
            <span>I have reviewed this summary and want to import these test cases{runCount ? ` and create ${runCount} test run(s)` : ''}.</span>
          </label>
          <div className="wnav">
            <button className="btn" onClick={() => setStep(2)} disabled={busy}>← Back</button>
            <button className="btn primary" disabled={!consent || busy} onClick={commit}>{busy ? 'Importing…' : 'Import now'}</button>
          </div>
        </div>
      )}

      {step === 4 && done && (
        <div className="panel">
          <h2>Import complete</h2>
          <Alert kind="ok">{done.created} created · {done.updated} updated · {done.skipped} unchanged{done.error_count ? ` · ${done.error_count} rows skipped` : ''}</Alert>
          {undone && <Alert kind="warn">{undone}</Alert>}
          {!!done.test_runs.length && !undone && (
            <>
              <h3>{done.test_runs.length} test run(s) created</h3>
              <div className="cards" style={{ marginBottom: 12 }}>
                {done.test_runs.map((t) => <Link className="card" key={t.id} to={`/test-runs/${t.id}`}><div className="ttl">{t.name}</div><div className="sub">Open to start testing →</div></Link>)}
              </div>
            </>
          )}
          <div className="wnav">
            {!undone && <button className="btn danger" onClick={undo}>Undo this import</button>}
            <div className="row">
              <button className="btn" onClick={() => { setStep(0); setFile(null); setPv(null); setDone(null); setUndone(''); first.current = true }}>Import another file</button>
              <button className="btn primary" onClick={() => nav(`/projects/${pid}?tab=${done.test_runs.length && !undone ? 'testruns' : 'cases'}`)}>Go to project</button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
