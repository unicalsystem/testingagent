import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { PROVIDER_LABEL, api, type Run, apiBase } from '../api'
import { Alert, Badge, fmtBytes, fmtDate, fmtMs, useApp, useProjectPerms } from '../components/ui'
import { PushToTracker } from '../components/PushToTracker'

const LIVE = ['queued', 'running']
const KIND_LABEL: Record<string, string> = { trace: 'Playwright trace', video: 'Video recording', log: 'Execution log', report_json: 'Report (JSON)', report_html: 'Report (HTML)', test_case: 'Test-case definition (JSON)', ai_analysis: 'AI analysis' }
interface Details { error: string; ai_analysis: string; steps: { idx: number; error: string }[]; source: string }

export default function RunPage() {
  const { rid } = useParams()
  const { status } = useApp()
  const nav = useNavigate()
  const [run, setRun] = useState<Run | null>(null)
  const [err, setErr] = useState(''), [busy, setBusy] = useState(false), [zoom, setZoom] = useState<string | null>(null)
  const [details, setDetails] = useState<Details | null>(null), [detailsErr, setDetailsErr] = useState('')
  const external = !!run?.details_external
  useEffect(() => {
    if (!external) return
    api.get<Details>(`/runs/${rid}/details`).then(setDetails).catch((e) => setDetailsErr(e.message))
  }, [rid, external])

  useEffect(() => {
    let live = true, t: number | undefined
    const load = () => api.get<Run>(`/runs/${rid}`).then((r) => {
      if (!live) return
      setRun(r); setErr('')
      if (LIVE.includes(r.status)) t = window.setTimeout(load, 2000)
    }).catch((e) => live && setErr(e.message))
    load()
    return () => { live = false; clearTimeout(t) }
  }, [rid])

  const has = useProjectPerms(run?.project_id)
  if (err && !run) return <Alert>{err}</Alert>
  if (!run) return null
  const stepErr = (idx: number, own: string) => own || details?.steps.find((x) => x.idx === idx)?.error || ''
  const runError = external ? (details?.error || run.error) : run.error
  const analysis = run.ai_analysis || details?.ai_analysis || ''
  const where = run.storage_mode === 'external' ? PROVIDER_LABEL[run.storage_provider ?? ''] ?? 'your storage' : 'Unical-managed storage' 
  const dur = run.started_at && run.finished_at ? new Date(run.finished_at).getTime() - new Date(run.started_at).getTime() : null
  const done = !LIVE.includes(run.status)
  const act = async (fn: () => Promise<void>) => { setBusy(true); setErr(''); try { await fn() } catch (x) { setErr((x as Error).message) } finally { setBusy(false) } }

  return (
    <>
      <div className="topbar">
        <div><div className="crumbs"><Link to="/projects">Projects</Link> / <Link to={`/projects/${run.project_id}`}>Project</Link> /</div>
          <h1>Run #{run.id} · {run.case_name} <Badge s={run.status} /></h1></div>
        <div className="row">
          {!done && has('run.cancel') && <button className="btn danger" disabled={busy || run.cancel_requested} onClick={() => act(async () => { setRun({ ...run, ...(await api.post<Run>(`/runs/${run.id}/cancel`)) }) })}>{run.cancel_requested ? 'Cancelling…' : 'Cancel run'}</button>}
          {done && has('run.execute') && run.test_case_id && <button className="btn" disabled={busy} onClick={() => act(async () => { const r = await api.post<Run>(`/runs/${run.id}/rerun`); nav(`/runs/${r.id}`) })}>↻ Re-run</button>}
          {done && <a className="btn primary" href={`${apiBase()}/runs/${run.id}/report?format=html`}>⬇ Download report</a>}
          {done && <a className="btn" href={`${apiBase()}/runs/${run.id}/report?format=json`}>JSON</a>}
{done && <PushToTracker type="browser" id={run.id} canPush={has('run.execute')} />}
        </div>
      </div>
      <Alert>{err}</Alert>
      {runError && <Alert>{runError}</Alert>}
      {run.storage_mode === 'external' && done && (
        <Alert kind={run.upload_state === 'failed' ? 'err' : run.upload_state === 'pending' ? 'warn' : 'ok'}>
          {run.upload_state === 'pending' ? `Evidence is waiting to be uploaded to your ${where}; it is retried automatically.`
            : run.upload_state === 'failed' ? `Some evidence could not be stored in your ${where} and was discarded (see the list below).`
              : `Evidence and reports are stored in your organization's ${where}.`}
          {detailsErr && <> Details could not be read from it: {detailsErr}</>}
        </Alert>)}
      <div className="stats">
        <div className="stat"><div className="k">Started</div><div className="v" style={{ fontSize: 14 }}>{fmtDate(run.started_at)}</div></div>
        <div className="stat"><div className="k">Duration</div><div className="v">{fmtMs(dur)}</div></div>
        <div className="stat"><div className="k">Passed</div><div className="v" style={{ color: 'var(--ok)' }}>{run.steps?.filter((s) => s.status === 'passed').length}</div></div>
        <div className="stat"><div className="k">Failed</div><div className="v" style={{ color: 'var(--bad)' }}>{run.steps?.filter((s) => s.status === 'failed').length}</div></div>
        <div className="stat"><div className="k">Requested by</div><div className="v" style={{ fontSize: 14 }}>{run.requested_by ?? '—'}</div></div>
      </div>

      {(run.status === 'failed' || run.status === 'error') && (
        <div className="panel">
          <div className="row" style={{ justifyContent: 'space-between' }}>
            <h2 style={{ margin: 0 }}>AI failure analysis</h2>
            {has('ai.use') && <button className="btn sm" disabled={busy || !status?.ai.available} title={status?.ai.available ? '' : 'Claude is not available'}
              onClick={() => act(async () => { const r = await api.post<{ ai_analysis: string; saved: boolean }>(`/runs/${run.id}/ai/analyze`); setRun({ ...run, ai_analysis: r.ai_analysis }); if (!r.saved) setErr('The analysis is shown here but could not be saved to your organization\'s storage.') })}>
              {busy ? 'Analysing…' : analysis ? 'Analyse again' : '✦ Analyse with Claude'}</button>}
          </div>
          {analysis ? <div className="ai-box" style={{ marginTop: 12 }}>{analysis}</div>
            : <p className="muted small">Claude reviews the step log and the failure screenshot and suggests a likely cause.</p>}
        </div>
      )}

      <div className="panel">
        <h2>Steps</h2>
        <div className="steps">
          {run.steps?.map((s) => (
            <div key={s.id} className={`stepline ${s.status}`}>
              <div className="n">{s.idx + 1}</div>
              <div style={{ minWidth: 0 }}>
                <div><code>{s.action}</code> {s.description}</div>
                {stepErr(s.idx, s.error) && <pre className="err">{stepErr(s.idx, s.error)}</pre>}
                {s.evidence.some((e) => e.kind === 'screenshot') && (
                  <div className="thumbs">{s.evidence.filter((e) => e.kind === 'screenshot').map((e) => e.available === false
                    ? <div key={e.id} className="small muted">Screenshot not available{e.upload_error ? ` — ${e.upload_error}` : ''}</div>
                    : <img key={e.id} src={e.url} alt={`Step ${s.idx + 1} screenshot`} loading="lazy" onClick={() => setZoom(e.url)} />
                  )}</div>
                )}
              </div>
              <div style={{ textAlign: 'right' }}><Badge s={s.status} />
                <div className="small muted">{s.duration_ms ? fmtMs(s.duration_ms) : ''}{s.attempts > 1 ? ` · ${s.attempts} attempts` : ''}</div></div>
            </div>
          ))}
        </div>
      </div>

      {!!run.evidence?.length && (
        <div className="panel">
          <h2>Evidence</h2>
          <table className="t resp"><tbody>{run.evidence.map((e) => (
            <tr key={e.id}><td>{KIND_LABEL[e.kind] ?? e.kind}</td>
              <td className="mono small">{e.file}{e.location && <div className="muted" title={e.location} style={{ maxWidth: 360, overflow: 'hidden', textOverflow: 'ellipsis' }}>{PROVIDER_LABEL[e.storage ?? ''] ?? e.storage}: {e.location}</div>}</td>
              <td className="small muted">{fmtBytes(e.size)}</td>
              <td className="mono small muted hide-sm" title={e.sha256}>sha256 {e.sha256.slice(0, 12)}…</td>
              <td style={{ textAlign: 'right' }}>{e.available === false ? <span className="pill bad" title={e.upload_error}>{e.upload_status === 'lost' ? 'not stored' : 'deleted'}</span>
                : e.upload_status === 'pending' ? <span className="pill warn">uploading</span>
                  : <a className="btn sm" href={`${e.url}?download=true`}>Download</a>}</td></tr>
          ))}</tbody></table>
          {run.evidence.some((e) => e.kind === 'trace') && <p className="hint">Open the trace with <code>npx playwright show-trace trace.zip</code> or at trace.playwright.dev (runs locally in your browser).</p>}
          {status?.report_store.enabled && run.storage_mode !== 'external' && (
            <p className="hint">MySQL report store: {run.report_synced_at ? `copied ${fmtDate(run.report_synced_at)}` : run.report_sync_error ? `pending — ${run.report_sync_error}` : 'pending'}</p>
          )}
        </div>
      )}
      {zoom && <div className="lightbox" onClick={() => setZoom(null)}><img src={zoom} alt="Screenshot" /></div>}
    </>
  )
}
