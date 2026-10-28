import { useEffect, useRef, useState, type MouseEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, apiBase, type MElement, type MStep, type MobileCase, type MobileSession } from '../api'
import { PLATFORM_LABEL } from '../components/Mobile'
import { MobileStepEditor, describeStep } from '../components/MobileSteps'
import { Alert, Badge, fmtDate } from '../components/ui'

type Mode = 'tap' | 'type' | 'check' | 'inspect'
const MODE_LABEL: Record<Mode, string> = { tap: 'Tap', type: 'Type', check: 'Add check', inspect: 'Inspect' }

export default function MobileSessionPage() {
  const { sid } = useParams()
  const nav = useNavigate()
  const [s, setS] = useState<MobileSession | null>(null), [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const [mode, setMode] = useState<Mode>('tap'), [sel, setSel] = useState<MElement | null>(null), [name, setName] = useState('')
  const [url, setUrl] = useState(''), [q, setQ] = useState('')
  const img = useRef<HTMLImageElement>(null)
  const live = s && (s.status === 'starting' || (s.purpose === 'ai' && s.phase === 'running'))
  useEffect(() => {
    let on = true, t: number | undefined
    const load = () => api.get<MobileSession>(`/mobile/sessions/${sid}`).then((x) => {
      if (!on) return
      setS(x); setErr('')
      if (x.status === 'starting' || (x.purpose === 'ai' && x.phase === 'running')) t = window.setTimeout(load, 1500)
    }).catch((e) => on && setErr(e.message))
    load()
    return () => { on = false; clearTimeout(t) }
  }, [sid, live])
  if (!s) return <Alert>{err}</Alert>
  const ready = s.status === 'ready'
  const recorder = s.purpose === 'record'
  const canAct = ready && !busy && !(s.purpose === 'ai' && s.phase === 'running')
  const web = s.target.kind === 'web'

  const act = async (step: MStep, record = true) => {
    setBusy(true); setErr('')
    try { setS(await api.post<MobileSession>(`/mobile/sessions/${s.id}/actions`, { step, record: record && recorder })); setSel(null) }
    catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  const onElement = (e: MElement, how: Mode) => {
    if (how === 'inspect') return setSel(e)
    if (how === 'tap') return act({ action: 'tap', target: e.target })
    if (how === 'type') {
      const text = prompt(`Text to type into ${e.target}\n(use {{secret:NAME}} for a project secret)`)
      if (text !== null) act({ action: 'type', target: e.target, text })
      return
    }
    act(e.text ? { action: 'expect_text', target: e.target, text: e.text } : { action: 'expect_visible', target: e.target })
  }
  const click = async (ev: MouseEvent<HTMLImageElement>) => {
    if (!canAct || !img.current) return
    const r = img.current.getBoundingClientRect()
    const x = Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width)), y = Math.min(1, Math.max(0, (ev.clientY - r.top) / r.height))
    try {
      const { element } = await api.post<{ element: MElement | null }>(`/mobile/sessions/${s.id}/inspect`, { x, y })
      if (element) onElement(element, mode)
      else if (mode === 'tap') act({ action: 'tap_at', x: +x.toFixed(3), y: +y.toFixed(3) })
      else setErr('No element at that point; try another spot or use Inspect')
    } catch (x2) { setErr((x2 as Error).message) }
  }
  const saveCase = async () => {
    setBusy(true); setErr('')
    try { const c = await api.post<MobileCase>(`/mobile/sessions/${s.id}/save`, { name: name || (s.purpose === 'ai' ? 'AI-built mobile test' : 'Recorded mobile test') }); nav(`/mobile-cases/${c.id}`) }
    catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  const els = s.screen.elements.filter((e) => !q || `${e.text} ${e.desc} ${e.target}`.toLowerCase().includes(q.toLowerCase()))
  const ratio = s.screen.width && s.screen.height ? s.screen.height / s.screen.width : 2
  return (
    <>
      <div className="topbar">
        <div><div className="crumbs"><Link to="/projects">Projects</Link> / <Link to={`/projects/${s.project_id}?tab=mobile`}>Project</Link> / Device sessions /</div>
          <h1>{recorder ? 'Recorder' : 'AI agent'} · {s.device.name} {s.device.os_version} <Badge s={ready ? 'running' : s.status === 'error' ? 'error' : s.status === 'starting' ? 'queued' : 'cancelled'} label={s.status} />
            {s.purpose === 'ai' && s.phase && <> <Badge s={s.phase === 'done' ? 'passed' : s.phase === 'running' ? 'running' : 'failed'} label={s.phase} /></>}</h1>
          <div className="small muted">{PLATFORM_LABEL[s.device.platform]} · {web ? s.target.start_url : 'app'} · started {fmtDate(s.created_at)}{s.session_url && <> · <a href={s.session_url} target="_blank" rel="noopener noreferrer">provider session ↗</a></>}</div></div>
        <div className="row">
          {(ready || s.status === 'starting') && <button className="btn danger" disabled={busy} onClick={async () => { setBusy(true); try { setS(await api.post<MobileSession>(`/mobile/sessions/${s.id}/stop`)) } catch (x) { setErr((x as Error).message) } finally { setBusy(false) } }}>
            {s.purpose === 'ai' && s.phase === 'running' ? 'Stop the agent' : 'Close the device'}</button>}
        </div>
      </div>
      <Alert>{err || (s.error && s.status !== 'ready' ? s.error : '')}</Alert>
      {s.status === 'starting' && <Alert kind="warn">Starting the device… cloud devices usually take 30 seconds to 3 minutes, and the build is installed first.</Alert>}
      {s.purpose === 'ai' && <div className="panel"><h2>Goal</h2><div className="small" style={{ whiteSpace: 'pre-wrap' }}>{s.goal}</div></div>}
      <div className="row" style={{ alignItems: 'flex-start', gap: 16, flexWrap: 'wrap' }}>
        <div className="panel" style={{ flex: '0 0 auto', width: 340 }}>
          {recorder && <div className="chips" style={{ marginBottom: 8 }}>{(Object.keys(MODE_LABEL) as Mode[]).map((m) =>
            <button key={m} type="button" className={`chip${mode === m ? ' on' : ''}`} onClick={() => setMode(m)}>{MODE_LABEL[m]}</button>)}</div>}
          <div style={{ position: 'relative', width: 300, minHeight: 300 * ratio, background: '#111', borderRadius: 18, padding: 0, overflow: 'hidden' }}>
            {s.screen.has_image && <img ref={img} src={`${apiBase()}/mobile/sessions/${s.id}/screen?seq=${s.screen.seq}`} alt="Device screen" onClick={click}
              style={{ width: 300, display: 'block', cursor: canAct && recorder ? 'crosshair' : 'default', opacity: busy ? 0.6 : 1 }} />}
            {sel && <div style={{ position: 'absolute', pointerEvents: 'none', border: '2px solid #f59e0b', borderRadius: 3,
              left: `${sel.box[0] * 100}%`, top: `${sel.box[1] * 100}%`, width: `${sel.box[2] * 100}%`, height: `${sel.box[3] * 100}%` }} />}
            {!s.screen.has_image && <div className="small" style={{ color: '#9ca3af', padding: 20 }}>{s.status === 'starting' ? 'Waiting for the device…' : 'No screen'}</div>}
          </div>
          {s.screen.seq >= 0 && !s.screen.ok && <div className="small" style={{ color: 'var(--bad)', marginTop: 6 }}>{s.screen.error}</div>}
          {recorder && ready && <div className="row" style={{ flexWrap: 'wrap', gap: 4, marginTop: 8 }}>
            {(['up', 'down', 'left', 'right'] as const).map((d) => <button key={d} className="btn sm" disabled={!canAct} onClick={() => act({ action: 'swipe', direction: d })}>Swipe {d}</button>)}
            {!web && <button className="btn sm" disabled={!canAct} onClick={() => act({ action: 'hide_keyboard' })}>Hide keyboard</button>}
            <button className="btn sm" disabled={!canAct} onClick={() => act({ action: 'back' })}>Back</button>
            <button className="btn sm" disabled={!canAct} onClick={() => act({ action: 'wait', ms: 1000 })}>Wait 1 s</button>
            <button className="btn sm" disabled={!canAct} onClick={async () => { setBusy(true); try { setS(await api.post<MobileSession>(`/mobile/sessions/${s.id}/refresh`)) } catch (x) { setErr((x as Error).message) } finally { setBusy(false) } }}>↻ Refresh</button>
          </div>}
          {recorder && ready && web && <div className="row" style={{ marginTop: 6, gap: 4 }}>
            <input className="i mono" style={{ flex: 1 }} placeholder="Open URL…" value={url} onChange={(e) => setUrl(e.target.value)} />
            <button className="btn sm" disabled={!canAct || !url} onClick={() => act({ action: 'open_url', url })}>Go</button></div>}
          {busy && <div className="small muted" style={{ marginTop: 6 }}>Working on the device…</div>}
        </div>

        <div style={{ flex: 1, minWidth: 300 }}>
          {recorder && ready && <div className="panel">
            <div className="row" style={{ justifyContent: 'space-between' }}><h2>Elements on screen</h2>
              <input className="i" style={{ width: 180 }} placeholder="Filter…" value={q} onChange={(e) => setQ(e.target.value)} /></div>
            {sel && <div className="small" style={{ background: 'var(--soft, #f9fafb)', padding: 8, borderRadius: 6, marginBottom: 6 }}>
              <b>{sel.type}</b> {sel.text && `“${sel.text}”`} {sel.desc && <span className="muted">[{sel.desc}]</span>}<div className="mono">{sel.target}</div>
              <div className="row" style={{ marginTop: 4, gap: 4 }}>
                {(['tap', 'type', 'check'] as Mode[]).map((m) => <button key={m} className="btn sm" disabled={!canAct} onClick={() => onElement(sel, m)}>{MODE_LABEL[m]}</button>)}
                <button className="btn sm" disabled={!canAct} onClick={() => act({ action: 'scroll_to', target: sel.target })}>Scroll to</button>
              </div></div>}
            <div style={{ maxHeight: 300, overflow: 'auto' }}>
              <table className="t"><tbody>{els.map((e) => (
                <tr key={e.ref} onMouseEnter={() => setSel(e)} style={{ cursor: 'pointer' }} onClick={() => setSel(e)}>
                  <td className="small muted">{e.ref}</td>
                  <td className="small">{e.type}{!e.interactive && <span className="muted"> (text)</span>}</td>
                  <td className="small">{e.text || e.desc}</td>
                  <td className="small mono" style={{ wordBreak: 'break-all' }}>{e.target}</td>
                </tr>))}</tbody></table>
              {!els.length && <div className="small muted">No elements detected on this screen. Use Tap on the screenshot (tap at point) or swipe.</div>}
            </div>
          </div>}

          <div className="panel">
            <h2>{recorder ? 'Recorded steps' : 'Steps built by the agent'} ({s.steps.length})</h2>
            {recorder && ready ? <MobileStepEditor steps={s.steps} web={web} onChange={async (steps) => { try { setS(await api.put<MobileSession>(`/mobile/sessions/${s.id}/steps`, { steps })) } catch (x) { setErr((x as Error).message) } }} />
              : <ol className="small">{s.steps.map((x, i) => <li key={i}>{describeStep(x)}</li>)}</ol>}
            {s.steps.length > 0 && !(s.purpose === 'ai' && s.phase === 'running') && <div className="row" style={{ marginTop: 10, gap: 6 }}>
              <input className="i" style={{ flex: 1 }} placeholder="Case name" value={name} onChange={(e) => setName(e.target.value)} maxLength={200} />
              <button className="btn primary" disabled={busy} onClick={saveCase}>Save as mobile case</button>
            </div>}
          </div>

          {s.purpose === 'ai' && <div className="panel">
            <h2>Agent log</h2>
            <div style={{ maxHeight: 320, overflow: 'auto' }}>{[...s.log].reverse().map((l, i) =>
              <div key={i} className="small" style={{ padding: '3px 0', color: l.kind === 'error' ? 'var(--bad)' : undefined }}>
                <span className="muted mono">{l.at.slice(11, 19)}</span> {l.text}</div>)}</div>
          </div>}
        </div>
      </div>
    </>
  )
}
