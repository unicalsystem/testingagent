import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, apiBase, type MobileRunDetail, type MobileRunGroup, type MobileRunRow } from '../api'
import { GroupCounts, PLATFORM_LABEL } from '../components/Mobile'
import { Alert, Badge, Modal, fmtDate, fmtMs } from '../components/ui'

const LIVE = ['queued', 'running']
const dev = (r: MobileRunRow) => `${r.device.name}${r.device.os_version ? ` ${r.device.os_version}` : ''}`

function RunDetail({ id, onClose }: { id: number; onClose: () => void }) {
  const [r, setR] = useState<MobileRunDetail | null>(null), [err, setErr] = useState(''), [zoom, setZoom] = useState<string | null>(null)
  useEffect(() => {
    let live = true, t: number | undefined
    const load = () => api.get<MobileRunDetail>(`/mobile/runs/${id}`).then((x) => { if (!live) return; setR(x); if (LIVE.includes(x.status)) t = window.setTimeout(load, 2500) }).catch((e) => live && setErr(e.message))
    load()
    return () => { live = false; clearTimeout(t) }
  }, [id])
  const file = (n: string) => `${apiBase()}/mobile/runs/${id}/files/${n}`
  return (
    <Modal title={r ? `${r.case_name} on ${dev(r)}` : 'Run'} onClose={onClose}>
      <Alert>{err}</Alert>
      {r && <>
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <div><Badge s={r.status} /> <span className="small muted">{PLATFORM_LABEL[r.device.platform]} · run #{r.id} · {fmtDate(r.started_at)}</span></div>
          <div className="row">{r.session_url && <a className="btn sm" href={r.session_url} target="_blank" rel="noopener noreferrer">Device session (video, logs) ↗</a>}
            <button className="btn sm" onClick={onClose}>Close</button></div>
        </div>
        {r.error && <Alert>{r.error}</Alert>}
        {r.has_video && <video src={file('video.webm')} controls style={{ maxWidth: '100%', maxHeight: 420, marginTop: 8 }} />}
        <table className="t" style={{ marginTop: 8 }}><tbody>{r.steps.map((s) => (
          <tr key={s.i}>
            <td className="small muted" style={{ width: 24 }}>{s.i + 1}</td>
            <td><div className="small">{s.description}</div>{s.error && <div className="small" style={{ color: 'var(--bad)' }}>{s.error}</div>}{s.note && !s.error && <div className="small muted">{s.note}</div>}</td>
            <td><Badge s={s.status} /></td>
            <td className="small muted">{s.status !== 'skipped' ? fmtMs(s.ms) : ''}</td>
            <td style={{ width: 70 }}>{s.shot && <img src={file(s.shot)} alt={`Screen after step ${s.i + 1}`} style={{ width: 56, borderRadius: 4, cursor: 'zoom-in', border: '1px solid var(--line, #e5e7eb)' }} onClick={() => setZoom(file(s.shot))} />}</td>
          </tr>))}</tbody></table>
        {!r.steps.length && LIVE.includes(r.status) && <p className="small muted">{r.status === 'queued' ? 'Waiting for a device slot…' : 'Starting the device session… cloud devices can take a minute or two.'}</p>}
        {zoom && <div className="modal-bg" onClick={() => setZoom(null)} style={{ zIndex: 1000 }}><img src={zoom} alt="Screen" style={{ maxHeight: '90vh', maxWidth: '90vw', margin: 'auto', display: 'block', marginTop: '5vh', borderRadius: 8 }} /></div>}
      </>}
    </Modal>
  )
}

export default function MobileRunPage() {
  const { gid } = useParams()
  const nav = useNavigate()
  const [g, setG] = useState<MobileRunGroup | null>(null), [err, setErr] = useState(''), [open, setOpen] = useState<number | null>(null), [busy, setBusy] = useState(false)
  useEffect(() => {
    let live = true, t: number | undefined
    const load = () => api.get<MobileRunGroup>(`/mobile/run-groups/${gid}`).then((x) => { if (!live) return; setG(x); setErr(''); if (LIVE.includes(x.status)) t = window.setTimeout(load, 3000) }).catch((e) => live && setErr(e.message))
    load()
    return () => { live = false; clearTimeout(t) }
  }, [gid])
  const matrix = useMemo(() => {
    const devices: string[] = [], cases: string[] = [], cell: Record<string, MobileRunRow> = {}
    for (const r of g?.runs ?? []) {
      if (!devices.includes(dev(r))) devices.push(dev(r))
      if (!cases.includes(r.case_name)) cases.push(r.case_name)
      cell[`${r.case_name}|${dev(r)}`] = r
    }
    return { devices, cases, cell }
  }, [g])
  if (!g) return <Alert>{err}</Alert>
  const live = LIVE.includes(g.status)
  return (
    <>
      <div className="topbar">
        <div><div className="crumbs"><Link to="/projects">Projects</Link> / <Link to={`/projects/${g.project_id}?tab=mobile`}>Project</Link> / Mobile runs /</div>
          <h1>Mobile run #{g.id} <Badge s={g.status} /></h1>
          <div className="small muted">{g.name} · {g.provider} · {fmtDate(g.created_at)}</div></div>
        <div className="row">
          {live && <button className="btn danger" disabled={busy} onClick={async () => { setBusy(true); try { await api.post(`/mobile/run-groups/${g.id}/cancel`) } catch (x) { setErr((x as Error).message) } finally { setBusy(false) } }}>Cancel</button>}
          {!live && <button className="btn" disabled={busy} onClick={async () => { setBusy(true); try { const n = await api.post<MobileRunGroup>(`/mobile/run-groups/${g.id}/rerun`); nav(`/mobile-runs/${n.id}`) } catch (x) { setErr((x as Error).message) } finally { setBusy(false) } }}>↻ Run again</button>}
          <a className="btn" href={`${apiBase()}/mobile/run-groups/${g.id}/report.html`}>Download report</a>
        </div>
      </div>
      <Alert>{err}</Alert>
      {g.skipped && g.skipped.length > 0 && <Alert kind="warn">Skipped: {g.skipped.map((s) => `${s.case}${s.device ? ` on ${s.device}` : ''} (${s.reason})`).join('; ')}</Alert>}
      <div className="panel">
        <div className="row" style={{ justifyContent: 'space-between' }}><h2>Cross-device results</h2><GroupCounts g={g} /></div>
        <div style={{ overflowX: 'auto' }}>
          <table className="t"><thead><tr><th>Case</th>{matrix.devices.map((d) => <th key={d} className="small">{d}</th>)}</tr></thead>
            <tbody>{matrix.cases.map((c) => <tr key={c}><td>{c}</td>{matrix.devices.map((d) => {
              const r = matrix.cell[`${c}|${d}`]
              return <td key={d}>{r ? <button className="btn sm" style={{ padding: '2px 6px' }} onClick={() => setOpen(r.id)} title={r.error || ''}>
                <Badge s={r.status} /> <span className="small muted">{r.passed_steps}/{r.total_steps}</span></button> : <span className="muted">—</span>}</td>
            })}</tr>)}</tbody></table>
        </div>
        {live && <p className="small muted">Runs start as device slots free up; several devices run in parallel.</p>}
      </div>
      {open !== null && <RunDetail id={open} onClose={() => setOpen(null)} />}
    </>
  )
}
