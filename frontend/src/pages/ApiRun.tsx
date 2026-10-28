import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, apiBase, type ApiResult, type ApiRun } from '../api'
import { Method } from '../components/ApiTesting'
import { PushToTracker } from '../components/PushToTracker'
import { Alert, Badge, fmtDate, fmtMs } from '../components/ui'

const LIVE = ['queued', 'running']
const ORDER: Record<string, number> = { failed: 0, error: 1, passed: 2, skipped: 3 }

function Excerpt({ title, headers, body }: { title: string; headers?: Record<string, string>; body?: string }) {
  if (!headers && !body) return null
  const text = Object.entries(headers ?? {}).map(([k, v]) => `${k}: ${v}`).join('\n') + (body ? `\n\n${body}` : '')
  return <><div className="small muted" style={{ marginTop: 8 }}>{title}</div><pre className="err" style={{ color: 'var(--ink)', background: '#f8fafd', borderColor: 'var(--line)', maxHeight: 260, overflow: 'auto' }}>{text.trim()}</pre></>
}

function ResultRow({ r }: { r: ApiResult }) {
  const [open, setOpen] = useState(r.status === 'failed' || r.status === 'error')
  return (
    <li className={`item${open ? ' open' : ''}`}>
      <div className="head" onClick={() => setOpen(!open)} role="button" aria-expanded={open} tabIndex={0} onKeyDown={(e) => e.key === 'Enter' && setOpen(!open)}>
        <span>{open ? '▾' : '▸'}</span>
        <div style={{ minWidth: 0 }}>
          <div><Method m={r.method} /> <span className="mono small">{r.path}</span></div>
          <div className="small">{r.name}</div>
        </div>
        <div style={{ textAlign: 'right' }}><Badge s={r.status} /><div className="small muted">{r.http_status ?? '—'} · {fmtMs(r.duration_ms)}</div></div>
      </div>
      {open && (
        <div className="body">
          <div className="mono small" style={{ wordBreak: 'break-all' }}>{r.method} {r.url}</div>
          {r.error && <pre className="err">{r.error}</pre>}
          {r.assertions.length > 0 && <ul className="summary" style={{ marginTop: 8 }}>{r.assertions.map((a, i) => (
            <li key={i} style={{ color: a.ok ? 'var(--ok)' : 'var(--bad)' }}>{a.ok ? '✔' : '✘'} {a.name}{a.detail && <span className="muted"> — {a.detail}</span>}</li>
          ))}</ul>}
          <Excerpt title="Request (credentials masked)" headers={r.request.headers} body={r.request.body} />
          <Excerpt title={`Response${r.response.truncated ? ' (truncated)' : ''}`} headers={r.response.headers} body={r.response.body} />
        </div>
      )}
    </li>
  )
}

export default function ApiRunPage() {
  const { rid } = useParams()
  const nav = useNavigate()
  const [run, setRun] = useState<ApiRun | null>(null), [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const [filter, setFilter] = useState<'all' | 'problems'>('all')
  useEffect(() => {
    let live = true, t: number | undefined
    const load = () => api.get<ApiRun>(`/api-runs/${rid}`).then((r) => {
      if (!live) return
      setRun(r); setErr('')
      if (LIVE.includes(r.status)) t = window.setTimeout(load, 2000)
    }).catch((e) => live && setErr(e.message))
    load()
    return () => { live = false; clearTimeout(t) }
  }, [rid])
  const results = useMemo(() => [...(run?.results ?? [])].filter((r) => filter === 'all' || r.status === 'failed' || r.status === 'error')
    .sort((a, b) => (ORDER[a.status] ?? 9) - (ORDER[b.status] ?? 9) || a.idx - b.idx), [run, filter])
  if (err && !run) return <Alert>{err}</Alert>
  if (!run) return null
  const s = 'total' in run.summary ? run.summary : null
  const done = !LIVE.includes(run.status)
  const dur = run.started_at && run.finished_at ? new Date(run.finished_at).getTime() - new Date(run.started_at).getTime() : null
  const act = async (fn: () => Promise<void>) => { setBusy(true); setErr(''); try { await fn() } catch (x) { setErr((x as Error).message) } finally { setBusy(false) } }
  const progress = run.results?.length ?? 0
  return (
    <>
      <div className="topbar">
        <div><div className="crumbs"><Link to="/projects">Projects</Link> / <Link to={`/projects/${run.project_id}?tab=api`}>Project</Link> / <Link to={`/api-suites/${run.suite_id}`}>{run.suite.name ?? 'API suite'}</Link> /</div>
          <h1>API run #{run.id} <Badge s={run.status} /></h1>
          <div className="mono small muted">{run.suite.base_url}</div></div>
        <div className="row">
          {!done && <button className="btn danger" disabled={busy || run.cancel_requested} onClick={() => act(async () => { setRun({ ...run, ...(await api.post<ApiRun>(`/api-runs/${run.id}/cancel`)) }) })}>{run.cancel_requested ? 'Cancelling…' : 'Cancel run'}</button>}
          {done && <button className="btn" disabled={busy} onClick={() => act(async () => { const r = await api.post<ApiRun>(`/api-suites/${run.suite_id}/runs`, { include_writes: !!run.options.include_writes }); nav(`/api-runs/${r.id}`) })}>↻ Run again</button>}
          {done && <a className="btn primary" href={`${apiBase()}/api-runs/${run.id}/report?format=html`}>⬇ Download report</a>}
          {done && <a className="btn" href={`${apiBase()}/api-runs/${run.id}/report?format=json`}>JSON</a>}
          {done && <a className="btn" href={`${apiBase()}/api-runs/${run.id}/report?format=html&download=false`} target="_blank" rel="noopener">Open report</a>}
          {done && <PushToTracker type="api" id={run.id} canPush />}
        </div>
      </div>
      <Alert>{err}</Alert>
      {run.error && <Alert>{run.error}</Alert>}
      {!done && <Alert kind="warn">{run.status === 'queued' ? 'Waiting for a worker…' : `Running — ${progress} request(s) done so far.`}</Alert>}
      <div className="stats">
        <div className="stat"><div className="k">Pass rate</div><div className="v">{s ? `${s.pass_rate}%` : '—'}</div></div>
        <div className="stat"><div className="k">Passed</div><div className="v" style={{ color: 'var(--ok)' }}>{s?.passed ?? run.results?.filter((r) => r.status === 'passed').length ?? 0}</div></div>
        <div className="stat"><div className="k">Failed</div><div className="v" style={{ color: 'var(--bad)' }}>{s ? s.failed + s.error : run.results?.filter((r) => r.status === 'failed' || r.status === 'error').length ?? 0}</div></div>
        <div className="stat"><div className="k">Skipped</div><div className="v">{s?.skipped ?? 0}</div></div>
        <div className="stat"><div className="k">Endpoints</div><div className="v">{s?.endpoints ?? '—'}</div></div>
        <div className="stat"><div className="k">Avg / p95</div><div className="v" style={{ fontSize: 15 }}>{s ? `${s.avg_ms} / ${s.p95_ms} ms` : '—'}</div></div>
        <div className="stat"><div className="k">Duration</div><div className="v" style={{ fontSize: 15 }}>{fmtMs(dur)}</div></div>
        <div className="stat"><div className="k">Started</div><div className="v" style={{ fontSize: 13 }}>{fmtDate(run.started_at)}<div className="small muted">{run.requested_by ?? ''}</div></div></div>
      </div>
      {s && Object.keys(s.by_category).length > 0 && (
        <div className="panel">
          <h2>By category</h2>
          <table className="t resp"><thead><tr><th>Category</th><th>Total</th><th>Passed</th><th>Failed</th><th>Errors</th><th>Skipped</th></tr></thead>
            <tbody>{Object.entries(s.by_category).map(([k, v]) => <tr key={k}><td><b>{k}</b></td><td data-label="Total">{v.total}</td><td data-label="Passed">{v.passed}</td><td data-label="Failed">{v.failed}</td><td data-label="Errors">{v.error}</td><td data-label="Skipped">{v.skipped}</td></tr>)}</tbody></table>
        </div>
      )}
      <div className="panel" style={{ padding: 0 }}>
        <div className="row" style={{ padding: '14px 16px', justifyContent: 'space-between' }}>
          <h2 style={{ margin: 0 }}>Results</h2>
          <div className="chips"><button className={`chip${filter === 'all' ? ' on' : ''}`} onClick={() => setFilter('all')}>All</button><button className={`chip${filter === 'problems' ? ' on' : ''}`} onClick={() => setFilter('problems')}>Failures only</button></div>
        </div>
        {results.length === 0 && <div className="empty">{done ? 'Nothing to show.' : 'Results appear as requests complete.'}</div>}
        <ul className="items">{results.map((r) => <ResultRow key={r.id} r={r} />)}</ul>
      </div>
      {done && run.report_sha256 && <p className="hint">Report fingerprint (SHA-256 of the JSON report): <span className="mono">{run.report_sha256}</span></p>}
    </>
  )
}
