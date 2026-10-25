import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, type Run } from '../api'
import { Alert, Badge, fmtBytes, fmtDate, fmtMs, useApp } from '../components/ui'

const LIVE = ['queued', 'running']
const KIND_LABEL: Record<string, string> = { trace: 'Playwright trace', video: 'Video recording', log: 'Execution log', report_json: 'Report (JSON)', report_html: 'Report (HTML)' }

export default function RunPage() {
  const { rid } = useParams()
  const { can, status } = useApp()
  const nav = useNavigate()
  const [run, setRun] = useState<Run | null>(null)
  const [err, setErr] = useState(''), [busy, setBusy] = useState(false), [zoom, setZoom] = useState<string | null>(null)

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

  if (err && !run) return <Alert>{err}</Alert>
  if (!run) return null
  const dur = run.started_at && run.finished_at ? new Date(run.finished_at).getTime() - new Date(run.started_at).getTime() : null
  const done = !LIVE.includes(run.status)
  const act = async (fn: () => Promise<void>) => { setBusy(true); setErr(''); try { await fn() } catch (x) { setErr((x as Error).message) } finally { setBusy(false) } }

  return (
    <>
      <div className="topbar">
        <div><div className="crumbs"><Link to="/projects">Projects</Link> / <Link to={`/projects/${run.project_id}`}>Project</Link> /</div>
          <h1>Run #{run.id} · {run.case_name} <Badge s={run.status} /></h1></div>
        <div className="row">
          {!done && can('tester') && <button className="btn danger" disabled={busy || run.cancel_requested} onClick={() => act(async () => { setRun({ ...run, ...(await api.post<Run>(`/runs/${run.id}/cancel`)) }) })}>{run.cancel_requested ? 'Cancelling…' : 'Cancel run'}</button>}
          {done && can('tester') && run.test_case_id && <button className="btn" disabled={busy} onClick={() => act(async () => { const r = await api.post<Run>(`/runs/${run.id}/rerun`); nav(`/runs/${r.id}`) })}>↻ Re-run</button>}
          {done && <a className="btn primary" href={`/api/runs/${run.id}/report?format=html`}>⬇ Download report</a>}
          {done && <a className="btn" href={`/api/runs/${run.id}/report?format=json`}>JSON</a>}
        </div>
      </div>
      <Alert>{err}</Alert>
      {run.error && <Alert>{run.error}</Alert>}
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
            {can('tester') && <button className="btn sm" disabled={busy || !status?.ai.available} title={status?.ai.available ? '' : 'Claude is not available'}
              onClick={() => act(async () => { const r = await api.post<{ ai_analysis: string }>(`/runs/${run.id}/ai/analyze`); setRun({ ...run, ai_analysis: r.ai_analysis }) })}>
              {busy ? 'Analysing…' : run.ai_analysis ? 'Analyse again' : '✦ Analyse with Claude'}</button>}
          </div>
          {run.ai_analysis ? <div className="ai-box" style={{ marginTop: 12 }}>{run.ai_analysis}</div>
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
                {s.error && <pre className="err">{s.error}</pre>}
                {s.evidence.some((e) => e.kind === 'screenshot') && (
                  <div className="thumbs">{s.evidence.filter((e) => e.kind === 'screenshot').map((e) => (
                    <img key={e.id} src={e.url} alt={`Step ${s.idx + 1} screenshot`} loading="lazy" onClick={() => setZoom(e.url)} />
                  ))}</div>
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
          <table className="t"><tbody>{run.evidence.map((e) => (
            <tr key={e.id}><td>{KIND_LABEL[e.kind] ?? e.kind}</td><td className="mono small">{e.file}</td><td className="small muted">{fmtBytes(e.size)}</td>
              <td className="mono small muted" title={e.sha256}>sha256 {e.sha256.slice(0, 12)}…</td>
              <td style={{ textAlign: 'right' }}><a className="btn sm" href={`${e.url}?download=true`}>Download</a></td></tr>
          ))}</tbody></table>
          {run.evidence.some((e) => e.kind === 'trace') && <p className="hint">Open the trace with <code>npx playwright show-trace trace.zip</code> or at trace.playwright.dev (runs locally in your browser).</p>}
          {status?.report_store.enabled && (
            <p className="hint">MySQL report store: {run.report_synced_at ? `copied ${fmtDate(run.report_synced_at)}` : run.report_sync_error ? `pending — ${run.report_sync_error}` : 'pending'}</p>
          )}
        </div>
      )}
      {zoom && <div className="lightbox" onClick={() => setZoom(null)}><img src={zoom} alt="Screenshot" /></div>}
    </>
  )
}
