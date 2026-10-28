import { useState } from 'react'
import type { MStep } from '../api'

export const ACTION_LABEL: Record<string, string> = {
  launch_app: 'Launch app', open_url: 'Open URL', tap: 'Tap', tap_at: 'Tap at point', type: 'Type text', clear: 'Clear field',
  swipe: 'Swipe', scroll_to: 'Scroll to element', back: 'Back', hide_keyboard: 'Hide keyboard', wait_for: 'Wait for element',
  expect_visible: 'Expect visible', expect_hidden: 'Expect hidden', expect_text: 'Expect text', screenshot: 'Screenshot', wait: 'Wait',
}
const TARGET = new Set(['tap', 'type', 'clear', 'scroll_to', 'wait_for', 'expect_visible', 'expect_hidden', 'expect_text'])
const DEFAULTS: Record<string, Partial<MStep>> = {
  open_url: { url: '' }, tap: { target: '' }, tap_at: { x: 0.5, y: 0.5 }, type: { target: '', text: '', clear_first: true },
  clear: { target: '' }, swipe: { direction: 'up', distance: 0.6 }, scroll_to: { target: '', direction: 'down', max_swipes: 8 },
  wait_for: { target: '', state: 'visible' }, expect_visible: { target: '' }, expect_hidden: { target: '' },
  expect_text: { target: '', text: '', match: 'contains' }, screenshot: { name: 'checkpoint' }, wait: { ms: 1000 },
}
export const TARGET_HELP = '~accessibility id · id=resource-id · text=Exact text · contains=Part · xpath=… · css=… (web)'

export function describeStep(s: MStep): string {
  if (s.description) return s.description
  const a = ACTION_LABEL[s.action] ?? s.action
  if (s.action === 'type') return `Type into ${s.target}`
  if (s.action === 'expect_text') return `Expect ${s.target} ${s.match ?? 'contains'} "${s.text}"`
  if (s.action === 'open_url') return `Open ${s.url}`
  if (s.action === 'swipe') return `Swipe ${s.direction}`
  if (s.action === 'wait') return `Wait ${s.ms} ms`
  if (s.action === 'tap_at') return `Tap at ${Math.round((s.x ?? 0) * 100)}% / ${Math.round((s.y ?? 0) * 100)}%`
  return s.target ? `${a} ${s.target}` : a
}

function StepRow({ s, onChange, web }: { s: MStep; onChange: (s: MStep) => void; web: boolean }) {
  const set = (k: keyof MStep, v: unknown) => onChange({ ...s, [k]: v } as MStep)
  const num = (k: keyof MStep, step: number, min: number, max: number, w = 80) => (
    <input className="i" type="number" style={{ width: w }} step={step} min={min} max={max} value={Number(s[k] ?? 0)} onChange={(e) => set(k, +e.target.value)} />)
  return (
    <div className="row" style={{ flexWrap: 'wrap', gap: 6, flex: 1, minWidth: 0 }}>
      <select className="i" style={{ width: 170 }} value={s.action} onChange={(e) => onChange({ action: e.target.value, ...(DEFAULTS[e.target.value] ?? {}), ...(s.description ? { description: s.description } : {}) } as MStep)}>
        {Object.entries(ACTION_LABEL).filter(([k]) => !web || k !== 'launch_app' && k !== 'hide_keyboard').map(([k, l]) => <option key={k} value={k}>{l}</option>)}
      </select>
      {TARGET.has(s.action) && <input className="i mono" style={{ flex: 2, minWidth: 180 }} placeholder={web ? 'css=#email · text=Sign in · ~Label' : '~login · id=com.app:id/user · text=Sign in'} title={TARGET_HELP} value={s.target ?? ''} onChange={(e) => set('target', e.target.value)} />}
      {s.action === 'open_url' && <input className="i mono" style={{ flex: 2, minWidth: 220 }} placeholder="https://… or /path" value={s.url ?? ''} onChange={(e) => set('url', e.target.value)} />}
      {(s.action === 'type' || s.action === 'expect_text') && <input className="i" style={{ flex: 2, minWidth: 160 }} placeholder={s.action === 'type' ? 'text or {{secret:NAME}}' : 'expected text'} value={s.text ?? ''} onChange={(e) => set('text', e.target.value)} />}
      {s.action === 'type' && <label className="check small"><input type="checkbox" checked={s.clear_first !== false} onChange={(e) => set('clear_first', e.target.checked)} /><span>clear first</span></label>}
      {s.action === 'expect_text' && <select className="i" style={{ width: 110 }} value={s.match ?? 'contains'} onChange={(e) => set('match', e.target.value)}><option value="contains">contains</option><option value="equals">equals</option></select>}
      {(s.action === 'swipe' || s.action === 'scroll_to') && <select className="i" style={{ width: 100 }} value={s.direction ?? (s.action === 'swipe' ? 'up' : 'down')} onChange={(e) => set('direction', e.target.value)}>
        {(s.action === 'swipe' ? ['up', 'down', 'left', 'right'] : ['down', 'up']).map((d) => <option key={d}>{d}</option>)}</select>}
      {s.action === 'swipe' && <>{num('distance', 0.1, 0.1, 0.9)}<span className="small muted">of the screen</span></>}
      {s.action === 'scroll_to' && <>{num('max_swipes', 1, 1, 20, 70)}<span className="small muted">swipes max</span></>}
      {s.action === 'tap_at' && <><span className="small muted">x</span>{num('x', 0.01, 0, 1)}<span className="small muted">y</span>{num('y', 0.01, 0, 1)}</>}
      {s.action === 'wait_for' && <select className="i" style={{ width: 110 }} value={s.state ?? 'visible'} onChange={(e) => set('state', e.target.value)}><option>visible</option><option>hidden</option></select>}
      {s.action === 'wait' && <>{num('ms', 100, 0, 15000, 100)}<span className="small muted">ms</span></>}
      {s.action === 'screenshot' && <input className="i" style={{ width: 180 }} value={s.name ?? ''} onChange={(e) => set('name', e.target.value)} />}
    </div>
  )
}

export function MobileStepEditor({ steps, onChange, web, readOnly }: { steps: MStep[]; onChange: (s: MStep[]) => void; web: boolean; readOnly?: boolean }) {
  const [json, setJson] = useState<string | null>(null), [jerr, setJerr] = useState('')
  const move = (i: number, d: number) => { const n = [...steps]; const [x] = n.splice(i, 1); n.splice(i + d, 0, x); onChange(n) }
  if (readOnly) return <ol className="small">{steps.map((s, i) => <li key={i} className="mono">{describeStep(s)}</li>)}</ol>
  if (json !== null) return (
    <div>
      {jerr && <div className="small" style={{ color: 'var(--bad)' }}>{jerr}</div>}
      <textarea className="i code" style={{ minHeight: 260 }} spellCheck={false} value={json} onChange={(e) => setJson(e.target.value)} />
      <div className="row" style={{ marginTop: 6 }}>
        <button type="button" className="btn sm primary" onClick={() => { try { const v = JSON.parse(json); if (!Array.isArray(v)) throw new Error('Steps must be a JSON list'); onChange(v); setJson(null); setJerr('') } catch (e) { setJerr((e as Error).message) } }}>Apply JSON</button>
        <button type="button" className="btn sm" onClick={() => { setJson(null); setJerr('') }}>Cancel</button>
      </div>
    </div>)
  return (
    <div>
      {steps.length === 0 && <div className="small muted" style={{ margin: '6px 0' }}>No steps yet. Add them here, record them on a device, or let the AI agent build them.</div>}
      {steps.map((s, i) => (
        <div key={i} className="row" style={{ alignItems: 'flex-start', gap: 6, padding: '6px 0', borderBottom: '1px solid var(--line, #e5e7eb)' }}>
          <span className="small muted" style={{ width: 22, paddingTop: 8 }}>{i + 1}</span>
          <StepRow s={s} web={web} onChange={(n) => onChange(steps.map((x, j) => (j === i ? n : x)))} />
          <div className="row" style={{ gap: 2 }}>
            <button type="button" className="btn sm" disabled={i === 0} onClick={() => move(i, -1)} aria-label="Move up">↑</button>
            <button type="button" className="btn sm" disabled={i === steps.length - 1} onClick={() => move(i, 1)} aria-label="Move down">↓</button>
            <button type="button" className="btn sm danger" onClick={() => onChange(steps.filter((_, j) => j !== i))} aria-label="Delete step">✕</button>
          </div>
        </div>))}
      <div className="row" style={{ marginTop: 8 }}>
        <button type="button" className="btn sm" onClick={() => onChange([...steps, { action: 'tap', target: '' }])}>+ Add step</button>
        <button type="button" className="btn sm" onClick={() => onChange([...steps, { action: 'expect_visible', target: '' }])}>+ Add check</button>
        <button type="button" className="btn sm" onClick={() => setJson(JSON.stringify(steps, null, 2))}>Edit as JSON</button>
        <span className="small muted">{TARGET_HELP}</span>
      </div>
    </div>
  )
}
