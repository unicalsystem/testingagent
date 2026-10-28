import { useEffect, useState, type FormEvent } from 'react'
import { PROVIDER_LABEL, api } from '../api'
import { Alert, fmtDate, useApp } from './ui'

interface Dest { site_id?: string; site_name?: string; site_url?: string; drive_id?: string; drive_name?: string; folder_id?: string; folder_path?: string }
interface Pending { provider: string; credentials_hint: string; destination: Dest; tested_ok?: boolean; tested_at?: string; test_message?: string }
interface St {
  policy: string; policy_label: string; options: { mode: string; provider: string; label: string }[]
  mode: string; provider: string; unical_consent_at: string | null; unical_consent_source: string
  destination: Dest; folders: Record<string, string>; pending: Pending | null; credentials_set: boolean; credentials_hint: string
  status: string; last_check_at: string | null; last_ok_at: string | null; last_error: string
  needs_reselection: boolean; reselection_reason: string; allow_delete: boolean; retention_days: number; ai_enabled: boolean
  run_block_reason: string | null; max_artifact_mb: number; spool_max_hours: number
  pending_uploads: number; lost_uploads: number; pending_case_versions: number; local_artifacts: number
  ok?: boolean; message?: string; deleted?: number
}
interface Item { id: string; name: string; url?: string }

export default function StorageSettings() {
  const { me } = useApp()
  const [s, setS] = useState<St | null>(null), [err, setErr] = useState(''), [msg, setMsg] = useState(''), [busy, setBusy] = useState(false)
  const [setup, setSetup] = useState<string | null>(null)
  useEffect(() => { api.get<St>('/admin/storage').then((x) => { setS(x); if (x.pending) setSetup(x.pending.provider) }).catch((x) => setErr(x.message)) }, [])
  const run = async (fn: () => Promise<St>, ok?: string) => {
    setErr(''); setMsg(''); setBusy(true)
    try { const r = await fn(); setS(r); if (r.ok === false) setErr(r.message ?? 'Failed'); else if (r.message || ok) setMsg(r.message || ok || '') ; return r }
    catch (x) { setErr((x as Error).message); return null } finally { setBusy(false) }
  }
  if (!s) return <Alert>{err}</Alert>
  const current = s.mode === 'unical' ? PROVIDER_LABEL.local : s.mode === 'external' ? PROVIDER_LABEL[s.provider] : 'Not chosen'
  const canUnical = s.options.some((o) => o.provider === 'local')
  const ext = s.options.filter((o) => o.mode === 'external')
  return (
    <>
      <Alert>{err}</Alert><Alert kind="ok">{msg}</Alert>
      {s.needs_reselection && <Alert kind="warn">{s.reselection_reason || 'Unical changed the storage policy.'} Choose the storage again below; tests cannot run until you do.</Alert>}
      {!s.needs_reselection && s.run_block_reason && <Alert kind="warn">{s.run_block_reason}</Alert>}
      <div className="stats">
        <div className="stat"><div className="k">Current storage</div><div className="v" style={{ fontSize: 15 }}>{current}</div></div>
        <div className="stat"><div className="k">Status</div><div className="v" style={{ fontSize: 15 }}>
          <span className={`pill ${s.status === 'ok' ? 'ok' : s.status === 'error' ? 'bad' : 'warn'}`}>{s.status}</span></div></div>
        <div className="stat"><div className="k">Last successful check</div><div className="v" style={{ fontSize: 13 }}>{fmtDate(s.last_ok_at)}</div></div>
        <div className="stat"><div className="k">Waiting to upload</div><div className="v" style={{ fontSize: 15 }}>{s.pending_uploads + s.pending_case_versions}{s.lost_uploads ? <span className="small" style={{ color: 'var(--bad)' }}> · {s.lost_uploads} lost</span> : null}</div></div>
        <div className="stat"><div className="k">Unical policy</div><div className="v" style={{ fontSize: 13 }}>{s.policy_label}</div></div>
      </div>
      {s.mode === 'external' && <div className="panel">
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <div><h2 style={{ margin: 0 }}>{PROVIDER_LABEL[s.provider]} destination</h2>
            <div className="small">{s.destination.site_name && <>{s.destination.site_name} · </>}{s.destination.drive_name} · <b>{s.destination.folder_path || s.destination.folder_id}</b></div>
            <div className="muted small">Credentials: {s.credentials_hint}{s.last_error && <> · last error: <span style={{ color: 'var(--bad)' }}>{s.last_error}</span></>}</div></div>
          <button className="btn sm" disabled={busy} onClick={() => run(() => api.post<St>('/admin/storage/test'))}>Test connection</button>
        </div>
      </div>}

      <div className="grid2">
        {canUnical && <UnicalOption s={s} busy={busy} onAgree={() => run(() => api.post<St>('/admin/storage/unical', { agree: true }), 'Unical-managed storage selected.')} />}
        <div className="panel">
          <h2>Your own storage</h2>
          <p className="small muted" style={{ marginTop: -6 }}>Test cases (as versioned JSON), screenshots, recordings, traces, logs and HTML/JSON reports are written to a folder you choose and removed from Unical servers once each upload is verified.
            If your storage is unavailable when a run starts, the run does not start; if it fails during upload, files wait up to {s.spool_max_hours} hours on the server and are then deleted — never moved to Unical storage.</p>
          {ext.length === 0 && <p className="small">Unical's policy for your organization does not allow customer-owned storage.</p>}
          <div className="row">{ext.map((o) => <button key={o.provider} className={`btn${setup === o.provider ? ' primary' : ''}`} onClick={() => setSetup(o.provider)}>
            {s.mode === 'external' && s.provider === o.provider ? `Change ${o.label} destination` : `Set up ${o.label}`}</button>)}</div>
        </div>
      </div>

      {setup && <Wizard provider={setup} s={s} busy={busy} run={run} onClose={async () => {
        if (s.pending) await run(() => api.del<St>('/admin/storage/pending')); setSetup(null) }} onActivated={() => setSetup(null)} />}

      <FolderAndData s={s} busy={busy} run={run} />

      <div className="panel">
        <h2>What still runs on Unical infrastructure</h2>
        <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
          <li>Tests run in a browser on Unical's server; pages and screenshots exist there while a run is in progress (in a temporary area excluded from backups).</li>
          <li>The TestAgent database keeps operational records: projects, test-case definitions, run status, timings, step names and the location of each file. Step error texts and AI analyses are removed once your report is uploaded.</li>
          <li>AI features send step text, errors and the failure screenshot to Anthropic (Claude) — you can switch them off below.</li>
          <li>If you need test execution to stay entirely inside your own network, contact Unical about customer-hosted execution.</li>
        </ul>
      </div>

      <DangerZone s={s} slug={me.tenant?.slug ?? ''} busy={busy} run={run} />
    </>
  )
}

function UnicalOption({ s, busy, onAgree }: { s: St; busy: boolean; onAgree: () => void }) {
  const [agree, setAgree] = useState(false)
  const active = s.mode === 'unical' && !s.needs_reselection
  const legacy = active && s.unical_consent_source === 'legacy'
  return (
    <div className="panel">
      <h2>Unical-managed storage {active && <span className="pill ok">in use</span>}</h2>
      <p className="small muted" style={{ marginTop: -6 }}>Artifacts are stored on Unical Systems' servers, isolated per organization, included in Unical's encrypted backups,
        and deleted according to your retention setting below. A copy of each report may also be kept in Unical's reporting database.</p>
      {active && !legacy && <p className="small">Agreed {fmtDate(s.unical_consent_at)}.</p>}
      {(!active || legacy) && <>
        {legacy && <Alert kind="warn">Your organization used Unical storage before release 1.3. Please confirm this choice explicitly.</Alert>}
        <label className="check small"><input type="checkbox" checked={agree} onChange={(e) => setAgree(e.target.checked)} />
          <span>On behalf of my organization I agree that TestAgent stores our test artifacts on Unical-managed infrastructure, as described in the <a href="/privacy" target="_blank" rel="noopener">Privacy Policy</a>.</span></label>
        <button className="btn primary" style={{ marginTop: 10 }} disabled={!agree || busy} onClick={onAgree}>{legacy ? 'Confirm Unical-managed storage' : 'Use Unical-managed storage'}</button>
      </>}
    </div>
  )
}

type Runner = (fn: () => Promise<St>, ok?: string) => Promise<St | null>

function Wizard({ provider, s, busy, run, onClose, onActivated }: { provider: string; s: St; busy: boolean; run: Runner; onClose: () => void; onActivated: () => void }) {
  const p = s.pending && s.pending.provider === provider ? s.pending : null
  const label = PROVIDER_LABEL[provider]
  return (
    <div className="panel" style={{ borderColor: 'var(--accent)' }}>
      <div className="row" style={{ justifyContent: 'space-between' }}><h2 style={{ margin: 0 }}>Set up {label}</h2>
        <button className="btn sm" onClick={onClose}>Cancel setup</button></div>
      <p className="small muted">Your current storage keeps working until you activate this one.</p>
      <h3 style={{ marginTop: 14 }}>1. Connect</h3>
      <Credentials provider={provider} pending={p} busy={busy} run={run} />
      {p && <><h3 style={{ marginTop: 18 }}>2. Choose the destination folder</h3>
        <Destination provider={provider} pending={p} run={run} /></>}
      {p?.destination.folder_id && <>
        <h3 style={{ marginTop: 18 }}>3. Test and activate</h3>
        <p className="small">Destination: <b>{p.destination.site_name ? `${p.destination.site_name} / ` : ''}{p.destination.drive_name} / {p.destination.folder_path}</b></p>
        {p.test_message && <Alert kind={p.tested_ok ? 'ok' : 'err'}>{p.test_message}</Alert>}
        <div className="row">
          <button className="btn" disabled={busy} onClick={() => run(() => api.post<St>('/admin/storage/pending/test'))}>Test (write, verify, delete a file)</button>
          <button className="btn primary" disabled={busy || !p.tested_ok} onClick={async () => {
            if (!window.confirm(`Store all new test artifacts in ${label} from now on? Existing evidence stays where it is.`)) return
            const r = await run(() => api.post<St>('/admin/storage/activate')); if (r) onActivated() }}>Activate {label}</button>
        </div>
      </>}
    </div>
  )
}

function Credentials({ provider, pending, busy, run }: { provider: string; pending: Pending | null; busy: boolean; run: Runner }) {
  const [f, setF] = useState({ source: 'own', entra_tenant_id: '', client_id: '', client_secret: '', service_account_json: '' })
  const save = (e: FormEvent) => { e.preventDefault(); run(() => api.put<St>('/admin/storage/pending', { provider, ...f }), 'Connected. Now choose the destination folder.') }
  const key = (file?: File) => { if (!file) return; const r = new FileReader(); r.onload = () => setF({ ...f, service_account_json: String(r.result) }); r.readAsText(file) }
  if (provider === 'sharepoint') return (
    <form onSubmit={save}>
      <details className="small" style={{ marginBottom: 8 }}><summary>How to prepare Microsoft 365 (least privilege)</summary>
        <ol style={{ paddingLeft: 18 }}>
          <li>In Microsoft Entra admin center → App registrations, create (or reuse) an app and add a client secret.</li>
          <li>API permissions → Microsoft Graph → <b>Application</b> permission <code>Sites.Selected</code> → Grant admin consent.</li>
          <li>Give the app write access to just your site, e.g. PnP PowerShell: <code>Grant-PnPAzureADAppSitePermission -AppId &lt;client id&gt; -DisplayName TestAgent -Site &lt;site url&gt; -Permissions Write</code>.</li>
          <li>With <code>Sites.Selected</code>, site search cannot list sites: paste the site URL (e.g. https://contoso.sharepoint.com/sites/QA) in step 2.</li>
        </ol></details>
      {pending && <p className="small">Connected with {pending.credentials_hint}. Enter new details to replace them.</p>}
      <label className="row small"><input type="radio" checked={f.source === 'sso'} onChange={() => setF({ ...f, source: 'sso' })} /> Use the app registration from Identity &amp; SSO</label>
      <label className="row small"><input type="radio" checked={f.source === 'own'} onChange={() => setF({ ...f, source: 'own' })} /> Use a separate app registration (recommended)</label>
      {f.source === 'own' && <div className="grid2" style={{ gap: 12 }}>
        <div><label className="f" htmlFor="sp1">Directory (tenant) ID</label><input className="i mono" id="sp1" value={f.entra_tenant_id} onChange={(e) => setF({ ...f, entra_tenant_id: e.target.value })} required /></div>
        <div><label className="f" htmlFor="sp2">Application (client) ID</label><input className="i mono" id="sp2" value={f.client_id} onChange={(e) => setF({ ...f, client_id: e.target.value })} required /></div>
        <div><label className="f" htmlFor="sp3">Client secret</label><input className="i" id="sp3" type="password" autoComplete="off" value={f.client_secret} onChange={(e) => setF({ ...f, client_secret: e.target.value })} required /></div>
      </div>}
      <div className="hint">Stored encrypted; never shown again or available to other users.</div>
      <button className="btn" style={{ marginTop: 10 }} disabled={busy}>{busy ? 'Connecting…' : 'Connect'}</button>
    </form>
  )
  return (
    <form onSubmit={save}>
      <details className="small" style={{ marginBottom: 8 }}><summary>How to prepare Google Drive</summary>
        <ol style={{ paddingLeft: 18 }}>
          <li>In Google Cloud console, enable the <b>Google Drive API</b> and create a <b>service account</b>; create a JSON key for it.</li>
          <li>In Google Drive, open the <b>Shared Drive</b> to use and add the service account's e-mail as <b>Content manager</b>. (Service accounts cannot store files in a personal My Drive.)</li>
          <li>Upload the JSON key here. Only the e-mail, key id and private key are kept, encrypted.</li>
        </ol></details>
      {pending && <p className="small">Connected as {pending.credentials_hint}. Upload a new key to replace it.</p>}
      <label className="f" htmlFor="gk">Service-account key (JSON)</label>
      <input id="gk" type="file" accept="application/json,.json" onChange={(e) => key(e.target.files?.[0])} />
      {f.service_account_json && <div className="hint">Key loaded ({f.service_account_json.length} characters).</div>}
      <button className="btn" style={{ marginTop: 10, display: 'block' }} disabled={busy || !f.service_account_json}>{busy ? 'Connecting…' : 'Connect'}</button>
    </form>
  )
}

function Destination({ provider, pending, run }: { provider: string; pending: Pending; run: Runner }) {
  const [q, setQ] = useState(''), [sites, setSites] = useState<Item[]>([]), [site, setSite] = useState<Item | null>(null)
  const [drives, setDrives] = useState<Item[]>([]), [drive, setDrive] = useState<Item | null>(null)
  const [trail, setTrail] = useState<Item[]>([]), [folders, setFolders] = useState<Item[]>([]), [newName, setNewName] = useState('')
  const [err, setErr] = useState(''), [loading, setLoading] = useState(false)
  const browse = async (body: Record<string, string>) => {
    setErr(''); setLoading(true)
    try { return (await api.post<{ items: Item[] }>('/admin/storage/pending/browse', body)).items } catch (x) { setErr((x as Error).message); return [] } finally { setLoading(false) }
  }
  useEffect(() => { if (provider === 'gdrive') browse({ what: 'drives' }).then(setDrives) }, [provider])
  const openDrive = async (d: Item) => { setDrive(d); setTrail([]); setFolders(await browse({ what: 'folders', drive_id: d.id })) }
  const openFolder = async (f: Item, i?: number) => {
    const t = i === undefined ? [...trail, f] : trail.slice(0, i + 1)
    setTrail(t); setFolders(await browse({ what: 'folders', drive_id: drive!.id, parent_id: t[t.length - 1]?.id ?? '' }))
  }
  const up = async () => { const t = trail.slice(0, -1); setTrail(t); setFolders(await browse({ what: 'folders', drive_id: drive!.id, parent_id: t[t.length - 1]?.id ?? '' })) }
  const create = async () => {
    if (!newName.trim() || !drive) return
    setErr('')
    try { await api.post('/admin/storage/pending/folder', { drive_id: drive.id, parent_id: trail[trail.length - 1]?.id ?? '', name: newName.trim() }); setNewName('')
      setFolders(await browse({ what: 'folders', drive_id: drive.id, parent_id: trail[trail.length - 1]?.id ?? '' })) } catch (x) { setErr((x as Error).message) }
  }
  const choose = () => {
    if (!drive) return
    const here = trail[trail.length - 1]
    const folder_id = here?.id ?? (provider === 'gdrive' ? drive.id : '')
    if (!folder_id) { setErr('Open a folder (or create one) to store TestAgent files in — not the library root.'); return }
    run(() => api.put<St>('/admin/storage/pending/destination', {
      site_id: site?.id ?? '', site_name: site?.name ?? '', site_url: site?.url ?? '', drive_id: drive.id, drive_name: drive.name,
      folder_id, folder_path: [drive.name, ...trail.map((t) => t.name)].join(' / ') }), 'Destination selected. Test it, then activate.')
  }
  return (
    <div>
      <Alert>{err}</Alert>
      {pending.destination.folder_id && <p className="small">Selected: <b>{pending.destination.folder_path}</b>. Pick another below to change it.</p>}
      {provider === 'sharepoint' && <>
        <form className="row" onSubmit={async (e) => { e.preventDefault(); setSite(null); setDrive(null); setSites(await browse({ what: 'sites', q })) }}>
          <input className="i" style={{ flex: 1, minWidth: 220 }} placeholder="Site URL (https://contoso.sharepoint.com/sites/QA) or a search term" value={q} onChange={(e) => setQ(e.target.value)} />
          <button className="btn" disabled={loading}>Find site</button></form>
        {sites.length > 0 && !site && <div className="picker">{sites.map((x) => <button key={x.id} type="button" onClick={async () => { setSite(x); setDrives(await browse({ what: 'libraries', site_id: x.id })) }}>{x.name}<div className="muted small">{x.url}</div></button>)}</div>}
        {site && <p className="small" style={{ marginTop: 8 }}>Site: <b>{site.name}</b> <a href="#" onClick={(e) => { e.preventDefault(); setSite(null); setDrive(null) }}>change</a></p>}
      </>}
      {(provider === 'gdrive' || site) && !drive && <>
        <div className="small" style={{ marginTop: 8 }}>{provider === 'gdrive' ? 'Shared Drives the service account can use:' : 'Document libraries:'}</div>
        <div className="picker">{drives.length === 0 && <div className="muted small" style={{ padding: 10 }}>{loading ? 'Loading…' : provider === 'gdrive' ? 'None — add the service account to a Shared Drive first.' : 'None found.'}</div>}
          {drives.map((d) => <button key={d.id} type="button" onClick={() => openDrive(d)}>{d.name}</button>)}</div>
      </>}
      {drive && <>
        <div className="small" style={{ marginTop: 8 }}>
          <a href="#" onClick={(e) => { e.preventDefault(); setDrive(null) }}>{provider === 'gdrive' ? 'Shared Drives' : 'Libraries'}</a> / <a href="#" onClick={(e) => { e.preventDefault(); openDrive(drive) }}>{drive.name}</a>
          {trail.map((t, i) => <span key={t.id}> / <a href="#" onClick={(e) => { e.preventDefault(); openFolder(t, i) }}>{t.name}</a></span>)}
        </div>
        <div className="picker">
          {trail.length > 0 && <button type="button" onClick={up}>⬑ Up</button>}
          {folders.length === 0 && <div className="muted small" style={{ padding: 10 }}>{loading ? 'Loading…' : 'No sub-folders.'}</div>}
          {folders.map((f) => <button key={f.id} type="button" onClick={() => openFolder(f)}>📁 {f.name}</button>)}
        </div>
        <div className="row" style={{ marginTop: 8 }}>
          <input className="i" style={{ flex: 1, minWidth: 160 }} placeholder="New folder name" value={newName} onChange={(e) => setNewName(e.target.value)} />
          <button type="button" className="btn sm" onClick={create} disabled={!newName.trim()}>Create folder here</button>
          <button type="button" className="btn sm primary" onClick={choose}>Use {trail.length ? `“${trail[trail.length - 1].name}”` : provider === 'gdrive' ? 'the Shared Drive root' : 'this folder'}</button>
        </div>
      </>}
    </div>
  )
}

const FOLDER_LABELS: [string, string][] = [['base_folder', 'Top folder'], ['test_cases_folder', 'Test cases sub-folder'],
  ['evidence_folder', 'Evidence sub-folder'], ['reports_folder', 'Reports sub-folder'], ['run_prefix', 'Execution folder prefix']]

function FolderAndData({ s, busy, run }: { s: St; busy: boolean; run: Runner }) {
  const [f, setF] = useState(s.folders), [ret, setRet] = useState(s.retention_days), [del, setDel] = useState(s.allow_delete), [ai, setAi] = useState(s.ai_enabled)
  useEffect(() => { setF(s.folders); setRet(s.retention_days); setDel(s.allow_delete); setAi(s.ai_enabled) }, [s])
  const save = (e: FormEvent) => { e.preventDefault(); run(() => api.put<St>('/admin/storage/settings', { folders: f, retention_days: ret, allow_delete: del, ai_enabled: ai }), 'Saved.') }
  const example = `${f.base_folder}/${f.project_folder === 'id' ? 'project-12' : f.project_folder === 'name' ? 'Checkout' : 'Checkout (12)'}/${f.run_prefix}345/{${f.test_cases_folder}, ${f.evidence_folder}, ${f.reports_folder}}`
  return (
    <form className="panel" onSubmit={save}>
      <h2>Folders, retention and AI</h2>
      <div className="grid2" style={{ gap: 12 }}>
        <div>
          {FOLDER_LABELS.map(([k, label]) => <div key={k}><label className="f" htmlFor={`fd-${k}`}>{label}</label>
            <input className="i" id={`fd-${k}`} value={f[k] ?? ''} onChange={(e) => setF({ ...f, [k]: e.target.value })} maxLength={k === 'run_prefix' ? 20 : 100} /></div>)}
          <label className="f" htmlFor="fd-p">Project folder name</label>
          <select className="i" id="fd-p" value={f.project_folder} onChange={(e) => setF({ ...f, project_folder: e.target.value })}>
            <option value="name_id">Name and id — “Checkout (12)”</option><option value="name">Name — “Checkout”</option><option value="id">Id — “project-12”</option></select>
          <div className="hint mono">{example}</div>
        </div>
        <div>
          <label className="f" htmlFor="rt">Delete test artifacts after (days)</label>
          <input className="i" id="rt" type="number" min={0} max={3650} value={ret} onChange={(e) => setRet(Number(e.target.value))} />
          <div className="hint">0 keeps them until you delete them. Applies to Unical-managed storage, and to your own storage only if the next option is on.</div>
          <label className="check small"><input type="checkbox" checked={del} onChange={(e) => setDel(e.target.checked)} />
            <span>Allow TestAgent to delete the files it created in our SharePoint / Google Drive (retention; Google Drive files go to the trash).</span></label>
          <label className="check small"><input type="checkbox" checked={ai} onChange={(e) => setAi(e.target.checked)} />
            <span>Allow AI features (Claude). Drafting tests and analysing failures send step text, errors and the failure screenshot to Anthropic.</span></label>
        </div>
      </div>
      <button className="btn primary" style={{ marginTop: 14 }} disabled={busy}>Save</button>
    </form>
  )
}

function DangerZone({ s, slug, busy, run }: { s: St; slug: string; busy: boolean; run: Runner }) {
  const [c, setC] = useState('')
  if (!s.local_artifacts && s.mode === 'external') return null
  return (
    <div className="panel" style={{ borderColor: '#f5c2bd' }}>
      <h2>Delete data held on Unical servers</h2>
      <p className="small">Permanently deletes all {s.local_artifacts} artifact file(s) this organization has on Unical-managed storage, their copies in Unical's reporting database,
        and the step error texts and AI analyses kept with runs. Run summaries remain. Files already in your own storage are not touched. Backups expire on their normal schedule.</p>
      <div className="row"><input className="i mono" style={{ width: 220 }} placeholder={`type ${slug} to confirm`} value={c} onChange={(e) => setC(e.target.value)} />
        <button className="btn danger" disabled={busy || c !== slug} onClick={async () => { const r = await run(() => api.post<St>('/admin/storage/delete-local', { confirm: c })); if (r) { setC(''); } }}>Delete</button></div>
    </div>
  )
}
