import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api, apiBase, type MDevice, type MPlatform, type MobileApp, type MobileCase, type MobileKind, type MobileOverview,
  type MobileProvider, type MobileRunGroup, type MobileSession, type Project } from '../api'
import { Alert, Badge, Modal, Tabs, fmtBytes, fmtDate, useAsync } from './ui'

type Pane = 'cases' | 'runs' | 'devices' | 'sessions'
const SECRET_LABEL: Partial<Record<MobileKind, string>> = { browserstack: 'Access key', lambdatest: 'Access key', saucelabs: 'Access key', appium: 'Password (optional)' }
const KIND_HINT: Record<MobileKind, string> = {
  browserstack: 'Real iOS and Android devices on BrowserStack App Automate. Username and access key: Account → Access keys.',
  lambdatest: 'Real devices on LambdaTest. Username and access key: Account settings → Password & security.',
  saucelabs: 'Real devices on Sauce Labs. Username and access key: User settings → Access key.',
  appium: 'Your own Appium 2 server or device lab. It must be reachable over HTTPS from the internet; TestAgent sends it a short-lived download link for the build.',
  unical_lab: 'Physical devices hosted by Unical. Available once Unical has set up the device host for your organization.',
  emulator: 'TestAgent’s own browser with phone and tablet emulation (screen size, touch, user agent). Mobile web only, no account needed.',
}
export const PLATFORM_LABEL: Record<string, string> = { android: 'Android', ios: 'iOS', any: 'Any' }

function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return <div><label className="f">{label}</label>{children}{hint && <div className="hint">{hint}</div>}</div>
}

// --------------------------------------------------------------------------- providers
function ProviderForm({ pid, ov, initial, onClose, onSaved }: { pid: number; ov: MobileOverview; initial?: MobileProvider; onClose: () => void; onSaved: () => void }) {
  const [kind, setKind] = useState<MobileKind>(initial?.kind ?? 'browserstack')
  const [name, setName] = useState(initial?.name ?? 'BrowserStack')
  const [cfg, setCfg] = useState<Record<string, string>>(initial?.config ?? {})
  const [secret, setSecret] = useState(''), [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const label = (k: MobileKind) => ov.kinds.find((x) => x.kind === k)?.label ?? k
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr(''); setBusy(true)
    try {
      const body = { kind, name, config: cfg, secret }
      if (initial) await api.put(`/mobile/providers/${initial.id}`, body); else await api.post(`/projects/${pid}/mobile/providers`, body)
      onSaved()
    } catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  const cloud = kind === 'browserstack' || kind === 'lambdatest' || kind === 'saucelabs'
  return (
    <Modal title={initial ? `Edit ${initial.name}` : 'Add a device provider'} onClose={onClose}>
      <form onSubmit={submit}>
        <Alert>{err}</Alert>
        {!initial && <div className="chips" style={{ marginBottom: 6 }}>{ov.kinds.map((k) =>
          <button type="button" key={k.kind} disabled={!k.available} title={k.available ? '' : 'Not available on this server yet'} className={`chip${kind === k.kind ? ' on' : ''}`}
            onClick={() => { setKind(k.kind); setName(k.label.replace(/ \(.*\)$/, '')); setCfg({}) }}>{k.label}</button>)}</div>}
        <p className="small muted">{KIND_HINT[kind]}</p>
        <Field label="Name"><input className="i" value={name} onChange={(e) => setName(e.target.value)} required maxLength={120} /></Field>
        {cloud && <Field label="Username"><input className="i mono" value={cfg.username ?? ''} onChange={(e) => setCfg({ ...cfg, username: e.target.value })} required /></Field>}
        {kind === 'saucelabs' && <Field label="Data center"><select className="i" value={cfg.region ?? 'us-west-1'} onChange={(e) => setCfg({ ...cfg, region: e.target.value })}>{ov.sauce_regions.map((r) => <option key={r}>{r}</option>)}</select></Field>}
        {kind === 'appium' && <>
          <Field label="Appium server URL" hint="e.g. https://appium.example.com/wd/hub — must be HTTPS on a public address"><input className="i mono" value={cfg.hub_url ?? ''} onChange={(e) => setCfg({ ...cfg, hub_url: e.target.value })} required placeholder="https://" /></Field>
          <Field label="Username (optional)"><input className="i mono" value={cfg.username ?? ''} onChange={(e) => setCfg({ ...cfg, username: e.target.value })} /></Field>
        </>}
        {SECRET_LABEL[kind] && <Field label={SECRET_LABEL[kind]!} hint={initial?.has_secret ? `Stored (${initial.secret_hint || 'set'}). Leave empty to keep it.` : 'Stored encrypted; never shown again.'}>
          <input className="i mono" type="password" autoComplete="new-password" value={secret} onChange={(e) => setSecret(e.target.value)} required={cloud && !initial?.has_secret} /></Field>}
        <div className="row" style={{ justifyContent: 'flex-end', marginTop: 12 }}>
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy}>{busy ? 'Saving…' : 'Save'}</button>
        </div>
        {!initial && <p className="small muted" style={{ marginTop: 8 }}>Device minutes are billed by the provider on your own account. {label(kind)} credentials stay in this project.</p>}
      </form>
    </Modal>
  )
}

// --------------------------------------------------------------------------- apps
function uploadWithProgress(url: string, form: FormData, onProgress: (p: number) => void): Promise<MobileApp> {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest()
    x.open('POST', url); x.withCredentials = true; x.setRequestHeader('X-Requested-With', 'TestAgent')
    x.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total)
    x.onload = () => {
      let body: { detail?: unknown } = {}
      try { body = JSON.parse(x.responseText) } catch { /* not JSON */ }
      if (x.status >= 200 && x.status < 300) resolve(body as MobileApp)
      else reject(new Error(typeof body.detail === 'string' ? body.detail : x.status === 413 ? 'The file is too large' : `Upload failed (HTTP ${x.status})`))
    }
    x.onerror = () => reject(new Error('Upload failed: network error'))
    x.send(form)
  })
}

function AppForm({ pid, maxMb, onClose, onSaved }: { pid: number; maxMb: number; onClose: () => void; onSaved: () => void }) {
  const [mode, setMode] = useState<'file' | 'url'>('file')
  const [file, setFile] = useState<File | null>(null), [name, setName] = useState(''), [version, setVersion] = useState('')
  const [platform, setPlatform] = useState<MPlatform>('android'), [url, setUrl] = useState(''), [pkg, setPkg] = useState(''), [activity, setActivity] = useState('')
  const [pct, setPct] = useState<number | null>(null), [err, setErr] = useState('')
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr('')
    try {
      if (mode === 'file') {
        if (!file) return setErr('Choose the build file')
        if (file.size > maxMb * 1048576) return setErr(`The build is larger than ${maxMb} MB`)
        const f = new FormData(); f.append('file', file); f.append('name', name || file.name); f.append('version', version); f.append('app_id', pkg); f.append('app_activity', activity)
        setPct(0); await uploadWithProgress(`${apiBase()}/projects/${pid}/mobile/apps`, f, setPct)
      } else {
        await api.post(`/projects/${pid}/mobile/apps/url`, { name, platform, version, source_url: url, app_id: pkg, app_activity: activity })
      }
      onSaved()
    } catch (x) { setErr((x as Error).message); setPct(null) }
  }
  return (
    <Modal title="Add an app build" onClose={onClose}>
      <form onSubmit={submit}>
        <Alert>{err}</Alert>
        <div className="chips" style={{ marginBottom: 8 }}>
          <button type="button" className={`chip${mode === 'file' ? ' on' : ''}`} onClick={() => setMode('file')}>Upload a build</button>
          <button type="button" className={`chip${mode === 'url' ? ' on' : ''}`} onClick={() => setMode('url')}>Download URL / installed app</button>
        </div>
        {mode === 'file' ? <Field label={`Build (.apk, .aab, .ipa or zipped .app — up to ${maxMb} MB)`} hint="iOS real devices need a build signed for testing (ad hoc / development); simulators need a zipped .app.">
          <input className="i" type="file" accept=".apk,.aab,.ipa,.zip" onChange={(e) => { const f = e.target.files?.[0] ?? null; setFile(f); if (f && !name) setName(f.name.replace(/\.[^.]+$/, '')) }} /></Field>
          : <>
            <Field label="Platform"><select className="i" value={platform} onChange={(e) => setPlatform(e.target.value as MPlatform)}><option value="android">Android</option><option value="ios">iOS</option></select></Field>
            <Field label="Download URL (HTTPS, optional)" hint="A direct link to the build, e.g. from your CI. Leave empty if the app is already installed on the devices (give its package / bundle id below)."><input className="i mono" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://ci.example.com/builds/app-release.apk" /></Field>
          </>}
        <div className="f2">
          <Field label="Name"><input className="i" value={name} onChange={(e) => setName(e.target.value)} required={mode === 'url'} maxLength={160} /></Field>
          <Field label="Version (optional)"><input className="i" value={version} onChange={(e) => setVersion(e.target.value)} maxLength={60} /></Field>
          <Field label="Package name / bundle id (optional)" hint="Lets tests relaunch the app, e.g. com.acme.shop"><input className="i mono" value={pkg} onChange={(e) => setPkg(e.target.value)} /></Field>
          <Field label="Launch activity (Android, optional)"><input className="i mono" value={activity} onChange={(e) => setActivity(e.target.value)} placeholder=".MainActivity" /></Field>
        </div>
        {pct !== null && <div className="small" style={{ marginTop: 8 }}>Uploading… {Math.round(pct * 100)}%<div style={{ height: 6, background: 'var(--line, #e5e7eb)', borderRadius: 3 }}><div style={{ width: `${pct * 100}%`, height: 6, background: 'var(--accent, #2563eb)', borderRadius: 3 }} /></div></div>}
        <div className="row" style={{ justifyContent: 'flex-end', marginTop: 12 }}>
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={pct !== null}>{mode === 'file' ? 'Upload' : 'Add'}</button>
        </div>
      </form>
    </Modal>
  )
}

function DevicesPane({ pid, ov, has, reloadOv }: { pid: number; ov: MobileOverview; has: (p: string) => boolean; reloadOv: () => void }) {
  const provs = useAsync(() => api.get<MobileProvider[]>(`/projects/${pid}/mobile/providers`), [pid])
  const apps = useAsync(() => api.get<MobileApp[]>(`/projects/${pid}/mobile/apps`), [pid])
  const [edit, setEdit] = useState<MobileProvider | 'new' | null>(null), [addApp, setAddApp] = useState(false)
  const [msg, setMsg] = useState<Record<number, { ok: boolean; message: string }>>({}), [err, setErr] = useState('')
  const canCred = has('secret.write'), canCase = has('case.write')
  const test = async (p: MobileProvider) => {
    setMsg((m) => ({ ...m, [p.id]: { ok: true, message: 'Testing…' } }))
    try { const r = await api.post<{ ok: boolean; message: string }>(`/mobile/providers/${p.id}/test`); setMsg((m) => ({ ...m, [p.id]: r })); provs.reload() } catch (x) { setErr((x as Error).message) }
  }
  return (
    <>
      <Alert>{provs.error || apps.error || err}</Alert>
      <div className="panel">
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <h2>Device providers</h2>
          {canCred && <button className="btn primary sm" onClick={() => setEdit('new')}>+ Add provider</button>}
        </div>
        {provs.data?.length === 0 && <p className="small muted">No providers yet. Add a device cloud (BrowserStack, LambdaTest, Sauce Labs), your own Appium server, or the built-in phone emulation for mobile web.</p>}
        <table className="t resp"><tbody>{provs.data?.map((p) => (
          <tr key={p.id}>
            <td><b>{p.name}</b> <span className="tag">{p.label}</span>
              <div className="small muted">{p.config.username ? `user ${p.config.username}` : p.config.hub_url ?? ''}{p.config.region ? ` · ${p.config.region}` : ''}{p.has_secret ? ` · key ${p.secret_hint}` : ''}</div>
              {msg[p.id] && <div className="small" style={{ color: msg[p.id].ok ? 'var(--ok)' : 'var(--bad)' }}>{msg[p.id].message}</div>}
              {!msg[p.id] && p.status === 'error' && <div className="small" style={{ color: 'var(--bad)' }}>{p.last_error}</div>}</td>
            <td><span className={`pill ${p.status === 'ok' ? 'ok' : p.status === 'error' ? 'bad' : ''}`}>{p.status === 'ok' ? 'connected' : p.status}</span></td>
            <td style={{ textAlign: 'right' }} className="row">
              {has('project.update') && <button className="btn sm" onClick={() => test(p)}>Test</button>}
              {canCred && <button className="btn sm" onClick={() => setEdit(p)}>Edit</button>}
              {canCred && <button className="btn sm danger" onClick={async () => { if (confirm(`Remove ${p.name}? Saved runs keep their results.`)) { try { await api.del(`/mobile/providers/${p.id}`); provs.reload(); reloadOv() } catch (x) { setErr((x as Error).message) } } }}>Remove</button>}
            </td>
          </tr>))}</tbody></table>
      </div>
      <div className="panel">
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <h2>App builds</h2>
          {canCase && <button className="btn primary sm" onClick={() => setAddApp(true)}>+ Add build</button>}
        </div>
        {apps.data?.length === 0 && <p className="small muted">Upload an APK / IPA, link a build from your CI, or name an app already installed on the devices. Mobile web tests need no build.</p>}
        <table className="t resp"><tbody>{apps.data?.map((a) => (
          <tr key={a.id}>
            <td><b>{a.name}</b>{a.version && <span className="muted"> {a.version}</span>} <span className="tag">{PLATFORM_LABEL[a.platform]}</span>
              <div className="small muted mono">{a.has_file ? `${a.filename} · ${fmtBytes(a.size)} · sha256 ${a.sha256.slice(0, 12)}…` : a.source_url || `installed app ${a.app_id}`}</div></td>
            <td className="small muted">{a.app_id}{a.app_activity && ` / ${a.app_activity}`}</td>
            <td className="small muted">{fmtDate(a.created_at)}</td>
            <td style={{ textAlign: 'right' }}>{canCase && <button className="btn sm danger" onClick={async () => { if (confirm(`Delete build ${a.name}?`)) { await api.del(`/mobile/apps/${a.id}`); apps.reload(); reloadOv() } }}>Delete</button>}</td>
          </tr>))}</tbody></table>
      </div>
      {edit && <ProviderForm pid={pid} ov={ov} initial={edit === 'new' ? undefined : edit} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); provs.reload(); reloadOv() }} />}
      {addApp && <AppForm pid={pid} maxMb={ov.max_app_mb} onClose={() => setAddApp(false)} onSaved={() => { setAddApp(false); apps.reload(); reloadOv() }} />}
    </>
  )
}

// --------------------------------------------------------------------------- device picker
export function DevicePicker({ ov, provider, value, onChange, single, platform }: { ov: MobileOverview; provider: MobileProvider | undefined; value: MDevice[]
  onChange: (d: MDevice[]) => void; single?: boolean; platform?: MPlatform | 'any' }) {
  const [list, setList] = useState<MDevice[]>([]), [free, setFree] = useState(true), [err, setErr] = useState(''), [q, setQ] = useState('')
  const [custom, setCustom] = useState<MDevice>({ name: '', os_version: '', platform: platform === 'ios' ? 'ios' : 'android' })
  useEffect(() => {
    setErr(''); setList([])
    if (!provider) return
    if (provider.kind === 'emulator') { setList(ov.emulated_devices); setFree(false); return }
    api.get<{ devices: MDevice[]; free_text: boolean }>(`/mobile/providers/${provider.id}/devices`).then((r) => { setList(r.devices); setFree(r.free_text || !r.devices.length) }).catch((e) => setErr((e as Error).message))
  }, [provider, ov.emulated_devices])
  const key = (d: MDevice) => `${d.platform}|${d.name}|${d.os_version}`
  const sel = new Set(value.map(key))
  const toggle = (d: MDevice) => onChange(single ? [d] : sel.has(key(d)) ? value.filter((x) => key(x) !== key(d)) : [...value, d].slice(0, 20))
  const shown = list.filter((d) => (!platform || platform === 'any' || d.platform === platform) && `${d.name} ${d.os_version}`.toLowerCase().includes(q.toLowerCase())).slice(0, 80)
  if (!provider) return <div className="small muted">Choose a provider first.</div>
  return (
    <div>
      {err && <div className="small" style={{ color: 'var(--bad)' }}>{err}</div>}
      {value.length > 0 && <div className="chips" style={{ marginBottom: 6 }}>{value.map((d) => <button type="button" key={key(d)} className="chip on" onClick={() => toggle(d)}>{d.name} {d.os_version} ✕</button>)}</div>}
      {list.length > 8 && <input className="i" placeholder="Search devices…" value={q} onChange={(e) => setQ(e.target.value)} style={{ marginBottom: 6 }} />}
      {list.length > 0 && <div style={{ maxHeight: 200, overflow: 'auto', border: '1px solid var(--line, #e5e7eb)', borderRadius: 6, padding: 6 }}>{shown.map((d) =>
        <label key={key(d)} className="check small" style={{ display: 'flex' }}><input type={single ? 'radio' : 'checkbox'} checked={sel.has(key(d))} onChange={() => toggle(d)} />
          <span>{d.name} <span className="muted">{PLATFORM_LABEL[d.platform]} {d.os_version}</span></span></label>)}</div>}
      {free && <div className="row" style={{ marginTop: 6, gap: 6, flexWrap: 'wrap' }}>
        <input className="i" style={{ flex: 2, minWidth: 160 }} placeholder="Device name, e.g. Galaxy S24 / iPhone 15" value={custom.name} onChange={(e) => setCustom({ ...custom, name: e.target.value })} />
        <input className="i" style={{ width: 90 }} placeholder="OS ver." value={custom.os_version} onChange={(e) => setCustom({ ...custom, os_version: e.target.value })} />
        <select className="i" style={{ width: 110 }} value={custom.platform} onChange={(e) => setCustom({ ...custom, platform: e.target.value as MPlatform })}><option value="android">Android</option><option value="ios">iOS</option></select>
        <button type="button" className="btn sm" disabled={!custom.name.trim()} onClick={() => { toggle({ ...custom, name: custom.name.trim() }); setCustom({ ...custom, name: '' }) }}>Add device</button>
      </div>}
    </div>
  )
}

function useProviders(pid: number) {
  return useAsync(() => api.get<MobileProvider[]>(`/projects/${pid}/mobile/providers`), [pid])
}

// --------------------------------------------------------------------------- run
export function RunModal({ pid, ov, cases, initial, onClose }: { pid: number; ov: MobileOverview; cases: MobileCase[]; initial: number[]; onClose: () => void }) {
  const provs = useProviders(pid), nav = useNavigate()
  const [prov, setProv] = useState<number | ''>(''), [devices, setDevices] = useState<MDevice[]>([]), [picked, setPicked] = useState<number[]>(initial)
  const [name, setName] = useState(''), [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const provider = provs.data?.find((p) => p.id === prov)
  useEffect(() => { if (prov === '' && provs.data?.length) setProv(provs.data[0].id) }, [provs.data, prov])
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr(''); setBusy(true)
    try {
      const g = await api.post<MobileRunGroup>(`/projects/${pid}/mobile/run-groups`, { name, provider_id: prov, devices, case_ids: picked })
      nav(`/mobile-runs/${g.id}`)
    } catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  return (
    <Modal title="Run mobile tests" onClose={onClose}>
      <form onSubmit={submit}>
        <Alert>{err || provs.error}</Alert>
        <Field label="Cases">
          <div style={{ maxHeight: 160, overflow: 'auto', border: '1px solid var(--line, #e5e7eb)', borderRadius: 6, padding: 6 }}>{cases.map((c) =>
            <label key={c.id} className="check small" style={{ display: 'flex' }}><input type="checkbox" checked={picked.includes(c.id)} onChange={(e) => setPicked(e.target.checked ? [...picked, c.id] : picked.filter((x) => x !== c.id))} />
              <span>{c.name} <span className="muted">{c.kind === 'web' ? 'mobile web' : PLATFORM_LABEL[c.platform]} · {c.step_count} steps</span></span></label>)}</div></Field>
        <Field label="Device provider"><select className="i" value={prov} onChange={(e) => { setProv(+e.target.value); setDevices([]) }}>
          {provs.data?.map((p) => <option key={p.id} value={p.id}>{p.name} ({p.label})</option>)}</select></Field>
        <Field label="Devices" hint="Each case runs on every compatible device (Android cases on Android devices, iOS on iOS, mobile web anywhere)."><DevicePicker ov={ov} provider={provider} value={devices} onChange={setDevices} /></Field>
        <Field label="Run name (optional)"><input className="i" value={name} onChange={(e) => setName(e.target.value)} maxLength={200} /></Field>
        <div className="row" style={{ justifyContent: 'flex-end', marginTop: 12 }}>
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy || !picked.length || !devices.length || prov === ''}>{busy ? 'Starting…' : `Run ${picked.length} case(s) on ${devices.length} device(s)`}</button>
        </div>
      </form>
    </Modal>
  )
}

// --------------------------------------------------------------------------- live session (recorder / AI)
interface ManualCase { id: number; name: string; external_id?: string }
export function SessionModal({ pid, ov, purpose, onClose }: { pid: number; ov: MobileOverview; purpose: 'record' | 'ai'; onClose: () => void }) {
  const provs = useProviders(pid), apps = useAsync(() => api.get<MobileApp[]>(`/projects/${pid}/mobile/apps`), [pid]), nav = useNavigate()
  const manual = useAsync(async () => {
    if (purpose !== 'ai') return [] as ManualCase[]
    const r = await api.get<ManualCase[] | { items: ManualCase[] }>(`/projects/${pid}/cases`)
    return Array.isArray(r) ? r : r.items ?? []
  }, [pid, purpose])
  const [prov, setProv] = useState<number | ''>(''), [devices, setDevices] = useState<MDevice[]>([]), [kind, setKind] = useState<'native' | 'web'>('native')
  const [app, setApp] = useState<number | ''>(''), [url, setUrl] = useState(''), [goal, setGoal] = useState(''), [tc, setTc] = useState<number | ''>('')
  const [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const usable = (provs.data ?? []).filter((p) => purpose === 'ai' || p.kind !== 'emulator')
  const provider = usable.find((p) => p.id === prov)
  useEffect(() => { if (prov === '' && usable.length) setProv(usable[0].id) }, [usable, prov])
  useEffect(() => { if (provider?.kind === 'emulator') setKind('web') }, [provider])
  const appObj = apps.data?.find((a) => a.id === app)
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr(''); setBusy(true)
    try {
      const s = await api.post<MobileSession>(`/projects/${pid}/mobile/sessions`, { purpose, provider_id: prov, device: devices[0], kind,
        app_id: kind === 'native' && app !== '' ? app : null, start_url: kind === 'web' ? url : '', goal, test_case_id: tc === '' ? null : tc })
      nav(`/mobile-sessions/${s.id}`)
    } catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  return (
    <Modal title={purpose === 'ai' ? 'Generate a mobile test with AI' : 'Record a mobile test on a device'} onClose={onClose}>
      <form onSubmit={submit}>
        <Alert>{err || provs.error}</Alert>
        {purpose === 'ai' && !ov.ai_available && <Alert kind="warn">Claude is not configured or temporarily unavailable on this server.</Alert>}
        <p className="small muted">{purpose === 'ai'
          ? 'Claude operates a live device: it reads the screen, taps and types toward your goal, and writes the steps that worked — with checks for the expected result. You review and save them as a case.'
          : 'A live device opens in your browser. Tap on the screenshot or pick elements; each action runs on the device and is recorded as a step.'}</p>
        {usable.length === 0 && provs.data && <Alert kind="warn">{purpose === 'record' ? 'Recording needs a real device provider (device cloud, Appium or the Unical lab).' : 'Add a device provider first.'}</Alert>}
        <Field label="Device provider"><select className="i" value={prov} onChange={(e) => { setProv(+e.target.value); setDevices([]) }}>
          {usable.map((p) => <option key={p.id} value={p.id}>{p.name} ({p.label})</option>)}</select></Field>
        <div className="chips" style={{ margin: '6px 0' }}>
          <button type="button" disabled={provider?.kind === 'emulator'} className={`chip${kind === 'native' ? ' on' : ''}`} onClick={() => setKind('native')}>Native / hybrid app</button>
          <button type="button" className={`chip${kind === 'web' ? ' on' : ''}`} onClick={() => setKind('web')}>Mobile web</button>
        </div>
        {kind === 'native' ? <Field label="App build"><select className="i" value={app} onChange={(e) => { setApp(e.target.value ? +e.target.value : ''); setDevices([]) }}>
          <option value="">— choose —</option>{apps.data?.map((a) => <option key={a.id} value={a.id}>{a.name} {a.version} ({PLATFORM_LABEL[a.platform]})</option>)}</select></Field>
          : <Field label="Start URL" hint="Must be on one of the project's authorised hosts"><input className="i mono" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://" required /></Field>}
        <Field label="Device"><DevicePicker ov={ov} provider={provider} value={devices} onChange={setDevices} single platform={kind === 'native' ? appObj?.platform : 'any'} /></Field>
        {purpose === 'ai' && <>
          <Field label="From a manual test case (optional)"><select className="i" value={tc} onChange={(e) => setTc(e.target.value ? +e.target.value : '')}>
            <option value="">— none: describe the goal below —</option>{manual.data?.map((c) => <option key={c.id} value={c.id}>{c.external_id ? `${c.external_id} · ` : ''}{c.name}</option>)}</select></Field>
          <Field label={tc === '' ? 'What should the test do?' : 'Additional instructions (optional)'} hint="Use project secrets for passwords, e.g. “sign in as qa@acme.com with secret APP_PASSWORD”.">
            <textarea className="i" style={{ minHeight: 90 }} value={goal} onChange={(e) => setGoal(e.target.value)} maxLength={8000} placeholder="Sign in, add the first product to the cart and check the cart shows 1 item" /></Field>
        </>}
        <div className="row" style={{ justifyContent: 'flex-end', marginTop: 12 }}>
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy || !devices.length || prov === '' || (kind === 'native' && app === '') || (purpose === 'ai' && !ov.ai_available)}>
            {busy ? 'Starting…' : purpose === 'ai' ? 'Start the AI agent' : 'Open the device'}</button>
        </div>
      </form>
    </Modal>
  )
}

// --------------------------------------------------------------------------- panes
function CasesPane({ pid, ov, has }: { pid: number; ov: MobileOverview; has: (p: string) => boolean }) {
  const cases = useAsync(() => api.get<MobileCase[]>(`/projects/${pid}/mobile/cases`), [pid])
  const [sel, setSel] = useState<number[]>([]), [run, setRun] = useState(false), [session, setSession] = useState<'record' | 'ai' | null>(null), [err, setErr] = useState('')
  const nav = useNavigate()
  const create = async () => {
    try { const c = await api.post<MobileCase>(`/projects/${pid}/mobile/cases`, { name: 'New mobile case', kind: 'native', platform: 'android', steps: [] }); nav(`/mobile-cases/${c.id}`) } catch (x) { setErr((x as Error).message) }
  }
  return (
    <>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 10, flexWrap: 'wrap' }}>
        <div className="row">
          {has('case.write') && <button className="btn" onClick={create}>+ New case</button>}
          {has('run.execute') && <button className="btn" onClick={() => setSession('record')} disabled={!ov.providers}>● Record on a device</button>}
          {has('ai.use') && has('run.execute') && <button className="btn" onClick={() => setSession('ai')} disabled={!ov.providers}>✦ Generate with AI</button>}
        </div>
        {has('run.execute') && <button className="btn primary" disabled={!cases.data?.length || !ov.providers} onClick={() => setRun(true)}>▶ Run {sel.length ? `${sel.length} selected` : 'cases'}</button>}
      </div>
      <Alert>{cases.error || err}</Alert>
      {!ov.providers && <Alert kind="warn">Add a device provider under <b>Devices &amp; apps</b> to record and run mobile tests.</Alert>}
      {cases.data?.length === 0 && <div className="panel empty"><p><b>No mobile cases yet.</b></p><p className="small">Write steps in the editor, record them on a real device, or describe the test and let the AI agent build it.</p></div>}
      {!!cases.data?.length && <table className="t resp"><thead><tr><th style={{ width: 28 }}><input type="checkbox" aria-label="Select all" checked={sel.length === cases.data.length} onChange={(e) => setSel(e.target.checked ? cases.data!.map((c) => c.id) : [])} /></th>
        <th>Case</th><th>Type</th><th>Steps</th><th>Source</th><th>Updated</th></tr></thead><tbody>
        {cases.data.map((c) => <tr key={c.id}>
          <td><input type="checkbox" aria-label={`Select ${c.name}`} checked={sel.includes(c.id)} onChange={(e) => setSel(e.target.checked ? [...sel, c.id] : sel.filter((x) => x !== c.id))} /></td>
          <td><Link to={`/mobile-cases/${c.id}`}>{c.name}</Link>{c.tags.map((t) => <span key={t} className="tag">{t}</span>)}</td>
          <td className="small">{c.kind === 'web' ? 'Mobile web' : `${PLATFORM_LABEL[c.platform]} app`}</td>
          <td className="small">{c.step_count}</td>
          <td className="small">{c.source === 'ai' ? 'AI agent' : c.source === 'recorded' ? 'Recorded' : 'Manual'}</td>
          <td className="small muted">{fmtDate(c.updated_at)}</td>
        </tr>)}</tbody></table>}
      {run && cases.data && <RunModal pid={pid} ov={ov} cases={cases.data} initial={sel.length ? sel : cases.data.map((c) => c.id)} onClose={() => setRun(false)} />}
      {session && <SessionModal pid={pid} ov={ov} purpose={session} onClose={() => setSession(null)} />}
    </>
  )
}

export function GroupCounts({ g }: { g: MobileRunGroup }) {
  return <span className="small">{Object.entries(g.counts).map(([k, v]) => <span key={k} style={{ marginRight: 8 }}><Badge s={k} /> {v}</span>)}</span>
}

function RunsPane({ pid }: { pid: number }) {
  const groups = useAsync(() => api.get<MobileRunGroup[]>(`/projects/${pid}/mobile/run-groups`), [pid])
  const running = useMemo(() => groups.data?.some((g) => g.status === 'running' || g.status === 'queued'), [groups.data])
  useEffect(() => { if (!running) return; const t = setInterval(groups.reload, 4000); return () => clearInterval(t) }, [running]) // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <>
      <Alert>{groups.error}</Alert>
      {groups.data?.length === 0 && <div className="panel empty"><p>No mobile runs yet.</p></div>}
      {!!groups.data?.length && <table className="t resp"><thead><tr><th>Run</th><th>Status</th><th>Devices</th><th>Results</th><th>Started</th></tr></thead><tbody>
        {groups.data.map((g) => <tr key={g.id}>
          <td><Link to={`/mobile-runs/${g.id}`}>#{g.id} {g.name}</Link><div className="small muted">{g.provider}</div></td>
          <td><Badge s={g.status} /></td>
          <td className="small">{g.devices.map((d) => d.name).join(', ')}</td>
          <td><GroupCounts g={g} /></td>
          <td className="small muted">{fmtDate(g.created_at)}</td>
        </tr>)}</tbody></table>}
    </>
  )
}

function SessionsPane({ pid }: { pid: number }) {
  const ss = useAsync(() => api.get<MobileSession[]>(`/projects/${pid}/mobile/sessions`), [pid])
  return (
    <>
      <Alert>{ss.error}</Alert>
      {ss.data?.length === 0 && <div className="panel empty"><p>No recorder or AI sessions yet.</p></div>}
      {!!ss.data?.length && <table className="t resp"><thead><tr><th>Session</th><th>Device</th><th>Status</th><th>Started</th></tr></thead><tbody>
        {ss.data.map((s) => <tr key={s.id}>
          <td><Link to={`/mobile-sessions/${s.id}`}>#{s.id} {s.purpose === 'ai' ? 'AI agent' : 'Recorder'}</Link><div className="small muted">{s.target.kind === 'web' ? s.target.start_url : 'app'}</div></td>
          <td className="small">{s.device.name} {s.device.os_version}</td>
          <td><Badge s={s.status === 'ready' ? 'running' : s.status === 'error' ? 'error' : 'cancelled'} label={s.purpose === 'ai' && s.phase ? s.phase : s.status} /></td>
          <td className="small muted">{fmtDate(s.created_at)}</td>
        </tr>)}</tbody></table>}
    </>
  )
}

export function MobileTab({ p, has }: { p: Project; has: (perm: string) => boolean }) {
  const ov = useAsync(() => api.get<MobileOverview>(`/projects/${p.id}/mobile`), [p.id])
  const [pane, setPane] = useState<Pane>('cases')
  useEffect(() => { if (ov.data && !ov.data.providers && pane === 'cases') setPane('devices') }, [ov.data]) // eslint-disable-line react-hooks/exhaustive-deps
  if (ov.error) return <Alert>{ov.error}</Alert>
  if (!ov.data) return <div className="muted">Loading…</div>
  return (
    <>
      <p className="small muted" style={{ marginTop: 0 }}>Test native and hybrid apps (APK / IPA) and mobile web on real devices — BrowserStack, LambdaTest, Sauce Labs, your own Appium server or the Unical device lab — or on built-in phone emulation. Run each case across many devices at once.</p>
      <Tabs value={pane} onChange={setPane} tabs={[['cases', `Cases (${ov.data.cases})`], ['runs', `Runs (${ov.data.run_groups})`], ['sessions', 'Recorder & AI sessions'], ['devices', `Devices & apps (${ov.data.providers})`]]} />
      <div style={{ marginTop: 12 }}>
        {pane === 'cases' && <CasesPane pid={p.id} ov={ov.data} has={has} />}
        {pane === 'runs' && <RunsPane pid={p.id} />}
        {pane === 'sessions' && <SessionsPane pid={p.id} />}
        {pane === 'devices' && <DevicesPane pid={p.id} ov={ov.data} has={has} reloadOv={ov.reload} />}
      </div>
    </>
  )
}
