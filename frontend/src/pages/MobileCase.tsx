import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, type MStep, type MobileApp, type MobileCase, type MobileOverview } from '../api'
import { PLATFORM_LABEL, RunModal, SessionModal } from '../components/Mobile'
import { MobileStepEditor } from '../components/MobileSteps'
import { Alert, fmtDate } from '../components/ui'

export default function MobileCasePage() {
  const { cid } = useParams()
  const nav = useNavigate()
  const [c, setC] = useState<MobileCase | null>(null), [ov, setOv] = useState<MobileOverview | null>(null), [apps, setApps] = useState<MobileApp[]>([])
  const [err, setErr] = useState(''), [ok, setOk] = useState(''), [busy, setBusy] = useState(false), [dirty, setDirty] = useState(false)
  const [run, setRun] = useState(false), [record, setRecord] = useState<'record' | 'ai' | null>(null)
  useEffect(() => {
    api.get<MobileCase>(`/mobile/cases/${cid}`).then((x) => {
      setC(x)
      api.get<MobileOverview>(`/projects/${x.project_id}/mobile`).then(setOv).catch(() => undefined)
      api.get<MobileApp[]>(`/projects/${x.project_id}/mobile/apps`).then(setApps).catch(() => undefined)
    }).catch((e) => setErr(e.message))
  }, [cid])
  if (!c) return <Alert>{err}</Alert>
  const perms = ov?.my_permissions ?? []
  const can = (p: string) => perms.includes(p)
  const edit = (patch: Partial<MobileCase>) => { setC({ ...c, ...patch }); setDirty(true); setOk('') }
  const save = async () => {
    setBusy(true); setErr('')
    try {
      const r = await api.put<MobileCase>(`/mobile/cases/${c.id}`, { name: c.name, description: c.description, kind: c.kind, platform: c.kind === 'web' ? 'any' : c.platform,
        app_id: c.kind === 'native' ? c.app_id : null, start_url: c.start_url, steps: c.steps ?? [], tags: c.tags, source: c.source, test_case_id: c.test_case_id })
      setC(r); setDirty(false); setOk('Saved')
    } catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  const web = c.kind === 'web'
  const appsFor = apps.filter((a) => a.platform === c.platform)
  return (
    <>
      <div className="topbar">
        <div><div className="crumbs"><Link to="/projects">Projects</Link> / <Link to={`/projects/${c.project_id}?tab=mobile`}>Project</Link> / Mobile cases /</div>
          <h1>{c.name}</h1>
          <div className="small muted">{web ? 'Mobile web' : `${PLATFORM_LABEL[c.platform]} app`} · {c.source === 'ai' ? 'built by the AI agent' : c.source === 'recorded' ? 'recorded on a device' : 'written by hand'} · updated {fmtDate(c.updated_at)}</div></div>
        <div className="row">
          {can('run.execute') && ov && <button className="btn primary" disabled={dirty || !c.steps?.length} title={dirty ? 'Save first' : ''} onClick={() => setRun(true)}>▶ Run on devices</button>}
          {can('case.write') && <button className="btn danger" onClick={async () => { if (confirm('Delete this mobile case?')) { await api.del(`/mobile/cases/${c.id}`); nav(`/projects/${c.project_id}?tab=mobile`) } }}>Delete</button>}
        </div>
      </div>
      <Alert>{err}</Alert><Alert kind="ok">{ok}</Alert>
      <div className="panel">
        <div className="f2">
          <div><label className="f">Name</label><input className="i" value={c.name} disabled={!can('case.write')} onChange={(e) => edit({ name: e.target.value })} maxLength={200} /></div>
          <div><label className="f">Tags</label><input className="i" value={c.tags.join(', ')} disabled={!can('case.write')} onChange={(e) => edit({ tags: e.target.value.split(',').map((t) => t.trim()).filter(Boolean) })} placeholder="smoke, login" /></div>
          <div><label className="f">Type</label><select className="i" value={c.kind} disabled={!can('case.write')} onChange={(e) => edit({ kind: e.target.value as 'native' | 'web', platform: e.target.value === 'web' ? 'any' : 'android' })}>
            <option value="native">Native / hybrid app</option><option value="web">Mobile web</option></select></div>
          {web ? <div><label className="f">Start URL</label><input className="i mono" value={c.start_url} disabled={!can('case.write')} onChange={(e) => edit({ start_url: e.target.value })} placeholder="https://" /></div>
            : <div className="row" style={{ gap: 8 }}>
              <div style={{ flex: 1 }}><label className="f">Platform</label><select className="i" value={c.platform} disabled={!can('case.write')} onChange={(e) => edit({ platform: e.target.value as 'android' | 'ios', app_id: null })}><option value="android">Android</option><option value="ios">iOS</option></select></div>
              <div style={{ flex: 2 }}><label className="f">App build</label><select className="i" value={c.app_id ?? ''} disabled={!can('case.write')} onChange={(e) => edit({ app_id: e.target.value ? +e.target.value : null })}>
                <option value="">— choose —</option>{appsFor.map((a) => <option key={a.id} value={a.id}>{a.name} {a.version}</option>)}</select></div>
            </div>}
        </div>
        <label className="f">Description</label>
        <textarea className="i" style={{ minHeight: 60 }} value={c.description} disabled={!can('case.write')} onChange={(e) => edit({ description: e.target.value })} />
      </div>
      <div className="panel">
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <h2>Steps</h2>
          <div className="row">
            {can('run.execute') && ov && <button className="btn sm" onClick={() => setRecord('record')}>● Record new steps</button>}
            {can('run.execute') && can('ai.use') && ov && <button className="btn sm" onClick={() => setRecord('ai')}>✦ Build with AI</button>}
          </div>
        </div>
        <MobileStepEditor steps={c.steps ?? []} web={web} readOnly={!can('case.write')} onChange={(s: MStep[]) => edit({ steps: s })} />
        <p className="small muted">Project secrets can be typed with <code>{'{{secret:NAME}}'}</code> in a “Type text” step; they never appear in results or reports.</p>
        {can('case.write') && <div className="row" style={{ justifyContent: 'flex-end' }}><button className="btn primary" disabled={busy || !dirty} onClick={save}>{busy ? 'Saving…' : 'Save case'}</button></div>}
      </div>
      {run && ov && <RunModal pid={c.project_id} ov={ov} cases={[c]} initial={[c.id]} onClose={() => setRun(false)} />}
      {record && ov && <SessionModal pid={c.project_id} ov={ov} purpose={record} onClose={() => setRecord(null)} />}
    </>
  )
}
