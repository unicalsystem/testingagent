import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { AUTH_LABEL, api, type ApiSuite, type AuthIn, type AuthType, type Project } from '../api'
import { Alert, Badge, Modal, fmtDate, useAsync } from './ui'

export const METHOD_COLOR: Record<string, string> = { GET: '#157347', HEAD: '#157347', OPTIONS: '#44546a', POST: '#1f54b3', PUT: '#8a5a00', PATCH: '#8a5a00', DELETE: '#b42318' }
export function Method({ m }: { m: string }) {
  return <span className="mono" style={{ fontWeight: 700, color: METHOD_COLOR[m] ?? 'var(--ink-2)', display: 'inline-block', minWidth: 52 }}>{m}</span>
}
export const pct = (v: number | null | undefined, d = 2) => v == null ? '—' : `${Number(v.toFixed(d))}%`

/* ---------------------------------------------------------------- credentials */
const SECRET_FIELDS: Record<AuthType, [string, string][]> = {
  none: [], api_key: [['api_key', 'API key']], bearer: [['token', 'Token']], basic: [['password', 'Password']], oauth2: [['client_secret', 'Client secret']],
}
export function emptyAuth(type: AuthType = 'none'): AuthIn {
  return { type, config: type === 'api_key' ? { name: 'X-API-Key', location: 'header' } : type === 'oauth2' ? { client_auth: 'body' } : {}, secrets: {} }
}

export function CredentialsFields({ value, onChange, stored }: { value: AuthIn; onChange: (v: AuthIn) => void; stored?: boolean }) {
  const cfg = (k: string, v: string) => onChange({ ...value, config: { ...value.config, [k]: v } })
  const sec = (k: string, v: string) => onChange({ ...value, secrets: { ...value.secrets, [k]: v } })
  const t = value.type
  return (
    <>
      <label className="f" htmlFor="at">Authentication</label>
      <select className="i" id="at" value={t} onChange={(e) => onChange(emptyAuth(e.target.value as AuthType))}>
        {(Object.keys(AUTH_LABEL) as AuthType[]).map((k) => <option key={k} value={k}>{AUTH_LABEL[k]}</option>)}
      </select>
      {t === 'api_key' && (
        <div className="f2">
          <div><label className="f" htmlFor="akn">Key name</label><input className="i mono" id="akn" value={value.config.name ?? ''} onChange={(e) => cfg('name', e.target.value)} placeholder="X-API-Key" /></div>
          <div><label className="f" htmlFor="akl">Sent in</label>
            <select className="i" id="akl" value={value.config.location ?? 'header'} onChange={(e) => cfg('location', e.target.value)}><option value="header">Header</option><option value="query">Query parameter</option></select></div>
        </div>
      )}
      {t === 'basic' && <><label className="f" htmlFor="bu">Username</label><input className="i" id="bu" value={value.config.username ?? ''} onChange={(e) => cfg('username', e.target.value)} autoComplete="off" /></>}
      {t === 'oauth2' && (
        <>
          <label className="f" htmlFor="tu">Token URL</label>
          <input className="i mono" id="tu" value={value.config.token_url ?? ''} onChange={(e) => cfg('token_url', e.target.value)} placeholder="https://login.example.com/oauth2/token" />
          <div className="f2">
            <div><label className="f" htmlFor="ci">Client ID</label><input className="i mono" id="ci" value={value.config.client_id ?? ''} onChange={(e) => cfg('client_id', e.target.value)} autoComplete="off" /></div>
            <div><label className="f" htmlFor="sc">Scope (optional)</label><input className="i mono" id="sc" value={value.config.scope ?? ''} onChange={(e) => cfg('scope', e.target.value)} /></div>
            <div><label className="f" htmlFor="au">Audience (optional)</label><input className="i mono" id="au" value={value.config.audience ?? ''} onChange={(e) => cfg('audience', e.target.value)} /></div>
            <div><label className="f" htmlFor="ca">Client authentication</label>
              <select className="i" id="ca" value={value.config.client_auth ?? 'body'} onChange={(e) => cfg('client_auth', e.target.value)}><option value="body">In the request body</option><option value="basic">HTTP Basic header</option></select></div>
          </div>
          <div className="hint">The token URL's host must also be an authorised target.</div>
        </>
      )}
      {SECRET_FIELDS[t].map(([k, label]) => (
        <div key={k}>
          <label className="f" htmlFor={`s-${k}`}>{label}</label>
          <input className="i" id={`s-${k}`} type="password" autoComplete="new-password" value={value.secrets[k] ?? ''} onChange={(e) => sec(k, e.target.value)}
            placeholder={stored ? 'Stored — leave empty to keep it' : ''} />
        </div>
      ))}
      {t !== 'none' && <div className="hint">Encrypted at rest, never shown again, and masked in every result and report. Use a dedicated test account where possible.</div>}
    </>
  )
}

/* ---------------------------------------------------------------- suite fields (shared by create + settings) */
export interface SuiteForm {
  name: string; base_url: string; spec_source: 'url' | 'upload' | 'manual'; spec_url: string; spec_text: string | null; endpoints_text: string
  requires_auth: boolean; allow_writes: boolean; verify_tls: boolean; timeout_ms: number; max_response_ms: number; default_headers: Record<string, string>
}
export function suiteForm(s?: ApiSuite): SuiteForm {
  return {
    name: s?.name ?? '', base_url: s?.base_url ?? '', spec_source: s?.spec_source ?? 'url', spec_url: s?.spec_url ?? '', spec_text: null,
    endpoints_text: s?.endpoints_text ?? '', requires_auth: s?.requires_auth ?? true, allow_writes: s?.allow_writes ?? false,
    verify_tls: s?.verify_tls ?? true, timeout_ms: s?.timeout_ms ?? 15000, max_response_ms: s?.max_response_ms ?? 2000, default_headers: s?.default_headers ?? {},
  }
}
const EXAMPLE_ENDPOINTS = `GET /health
GET /users?limit=10
GET /users/{id}
POST /users {"name": "QA Tester", "email": "qa@example.com"} -> 201
DELETE /users/{id} -> 204`

export function SuiteFields({ f, set, existingSpec }: { f: SuiteForm; set: (f: SuiteForm) => void; existingSpec?: boolean }) {
  const [fileErr, setFileErr] = useState('')
  const readFile = (file: File | undefined) => {
    setFileErr('')
    if (!file) return
    if (file.size > 2_000_000) { setFileErr('The file is larger than 2 MB. Publish it at a URL instead.'); return }
    file.text().then((t) => set({ ...f, spec_text: t })).catch(() => setFileErr('The file could not be read'))
  }
  return (
    <>
      <label className="f" htmlFor="sn">Name</label>
      <input className="i" id="sn" value={f.name} onChange={(e) => set({ ...f, name: e.target.value })} required maxLength={120} placeholder="e.g. Orders API (staging)" />
      <label className="f">Where should the test cases come from?</label>
      {([['url', 'OpenAPI / Swagger URL', 'Recommended. TestAgent downloads the document and generates cases for every operation.'],
        ['upload', 'Upload or paste an OpenAPI / Swagger file', 'JSON or YAML, OpenAPI 3.x or Swagger 2.0.'],
        ['manual', 'List the endpoints myself', 'No specification? Enter one endpoint per line; Claude can suggest more cases afterwards.']] as const).map(([k, title, help]) => (
        <label key={k} className={`opt${f.spec_source === k ? ' on' : ''}`}>
          <input type="radio" checked={f.spec_source === k} onChange={() => set({ ...f, spec_source: k })} />
          <div><b>{title}</b><div className="small muted">{help}</div></div>
        </label>
      ))}
      {f.spec_source === 'url' && (
        <>
          <label className="f" htmlFor="su">Specification URL</label>
          <input className="i mono" id="su" value={f.spec_url} onChange={(e) => set({ ...f, spec_url: e.target.value })} required placeholder="https://api.example.com/openapi.json" />
        </>
      )}
      {f.spec_source === 'upload' && (
        <>
          <label className="f" htmlFor="sf">Specification file</label>
          <input className="i" id="sf" type="file" accept=".json,.yaml,.yml,application/json,text/yaml" onChange={(e) => readFile(e.target.files?.[0])} />
          <Alert>{fileErr}</Alert>
          <textarea className="i code" style={{ minHeight: 140 }} spellCheck={false} value={f.spec_text ?? ''} onChange={(e) => set({ ...f, spec_text: e.target.value })}
            placeholder={existingSpec ? 'A document is stored. Paste or upload a new one to replace it.' : '…or paste the JSON / YAML here'} aria-label="Specification text" />
        </>
      )}
      {f.spec_source === 'manual' && (
        <>
          <label className="f" htmlFor="ep">Endpoints</label>
          <textarea className="i code" id="ep" style={{ minHeight: 140 }} spellCheck={false} value={f.endpoints_text} onChange={(e) => set({ ...f, endpoints_text: e.target.value })} placeholder={EXAMPLE_ENDPOINTS} />
          <div className="hint">One per line: <code>METHOD /path [JSON body] [-&gt; expected status]</code>. <code>{'{id}'}</code> path parameters are filled from ids returned by earlier calls.</div>
          {!f.endpoints_text && <button type="button" className="btn sm" style={{ marginTop: 6 }} onClick={() => set({ ...f, endpoints_text: EXAMPLE_ENDPOINTS })}>Insert example</button>}
        </>
      )}
      <label className="f" htmlFor="bu2">API base URL{f.spec_source !== 'manual' && ' (optional — taken from the document when empty)'}</label>
      <input className="i mono" id="bu2" value={f.base_url} onChange={(e) => set({ ...f, base_url: e.target.value })} required={f.spec_source === 'manual'} placeholder="https://staging-api.example.com/v1" />
      <div className="hint">Prefer a test or staging environment. The host must be an authorised target of your organization.</div>
    </>
  )
}

export function SuiteOptions({ f, set }: { f: SuiteForm; set: (f: SuiteForm) => void }) {
  return (
    <>
      <label className="check"><input type="checkbox" checked={f.allow_writes} onChange={(e) => set({ ...f, allow_writes: e.target.checked })} />
        <span><b>Allow write tests</b> (POST / PUT / PATCH / DELETE)<div className="small muted">Off by default: runs are read-only and write cases are reported as skipped. When allowed, each run still asks before sending them. Only enable this against test data.</div></span></label>
      <label className="check"><input type="checkbox" checked={f.requires_auth} onChange={(e) => set({ ...f, requires_auth: e.target.checked })} />
        <span>Endpoints require authentication<div className="small muted">Adds checks that requests without (or with wrong) credentials are rejected with 401/403.</div></span></label>
      <label className="check"><input type="checkbox" checked={f.verify_tls} onChange={(e) => set({ ...f, verify_tls: e.target.checked })} />
        <span>Verify TLS certificates<div className="small muted">Turn off only for staging servers with self-signed certificates.</div></span></label>
      <div className="f2">
        <div><label className="f" htmlFor="mr">Response time limit (ms)</label><input className="i" id="mr" type="number" min={50} max={120000} value={f.max_response_ms} onChange={(e) => set({ ...f, max_response_ms: +e.target.value })} /></div>
        <div><label className="f" htmlFor="to">Request timeout (ms)</label><input className="i" id="to" type="number" min={1000} max={120000} value={f.timeout_ms} onChange={(e) => set({ ...f, timeout_ms: +e.target.value })} /></div>
      </div>
    </>
  )
}

export function suiteBody(f: SuiteForm) {
  return { ...f, spec_text: f.spec_source === 'upload' ? f.spec_text : null }
}

/* ---------------------------------------------------------------- new suite wizard */
function NewSuiteModal({ p, has, onClose }: { p: Project; has: (perm: string) => boolean; onClose: () => void }) {
  const nav = useNavigate()
  const [step, setStep] = useState(0)
  const [f, setF] = useState<SuiteForm>({ ...suiteForm(), name: `${p.name} API` })
  const [auth, setAuth] = useState<AuthIn>(emptyAuth())
  const [run, setRun] = useState(true)
  const [busy, setBusy] = useState(false), [err, setErr] = useState('')
  const canSecrets = has('secret.write'), canRun = has('run.execute')
  const next = (e: FormEvent) => {
    e.preventDefault(); setErr('')
    if (step === 0 && f.spec_source === 'upload' && !f.spec_text) return setErr('Upload or paste the specification')
    setStep(step + 1)
  }
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr(''); setBusy(true)
    try {
      const s = await api.post<ApiSuite>(`/projects/${p.id}/api-suites`, { ...suiteBody(f), auth: auth.type === 'none' ? null : auth, generate: true, run: run && canRun })
      if (s.generation?.run) nav(`/api-runs/${s.generation.run}`)
      else nav(`/api-suites/${s.id}${s.generation?.run_error ? `?notice=${encodeURIComponent(s.generation.run_error)}` : ''}`)
    } catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  return (
    <Modal title="New API test" onClose={onClose}>
      <div className="wsteps"><div className={step === 0 ? 'on' : 'done'}>1 · API</div><div className={step === 1 ? 'on' : step > 1 ? 'done' : ''}>2 · Credentials</div><div className={step === 2 ? 'on' : ''}>3 · Options</div></div>
      <Alert>{err}</Alert>
      {step === 0 && <form onSubmit={next}><SuiteFields f={f} set={setF} />
        <div className="wnav"><button type="button" className="btn" onClick={onClose}>Cancel</button><button className="btn primary">Next</button></div></form>}
      {step === 1 && <form onSubmit={next}>
        {canSecrets ? <CredentialsFields value={auth} onChange={setAuth} /> : <Alert kind="warn">You cannot store credentials in this project. Ask a QA engineer or project manager to add them afterwards.</Alert>}
        <div className="wnav"><button type="button" className="btn" onClick={() => setStep(0)}>Back</button><button className="btn primary">Next</button></div></form>}
      {step === 2 && <form onSubmit={submit}>
        <SuiteOptions f={f} set={setF} />
        {canRun && <label className="check"><input type="checkbox" checked={run} onChange={(e) => setRun(e.target.checked)} />
          <span><b>Run the generated tests straight away</b><div className="small muted">Read-only: write tests are not sent on this first run.</div></span></label>}
        <div className="consent" style={{ background: '#f4f7fe', borderColor: '#d9e2fa', fontWeight: 400 }}>
          TestAgent generates positive, negative, authentication and response-time cases for every endpoint, {run && canRun ? 'runs them, ' : ''}and produces an HTML / JSON report. You can review, edit, disable or add cases at any time.
        </div>
        <div className="wnav"><button type="button" className="btn" onClick={() => setStep(1)}>Back</button>
          <button className="btn primary" disabled={busy}>{busy ? 'Creating…' : run && canRun ? 'Generate & run' : 'Generate test cases'}</button></div>
      </form>}
    </Modal>
  )
}

/* ---------------------------------------------------------------- project tab */
export function ApiTestingTab({ p, has }: { p: Project; has: (perm: string) => boolean }) {
  const suites = useAsync(() => api.get<ApiSuite[]>(`/projects/${p.id}/api-suites`), [p.id])
  const [creating, setCreating] = useState(false)
  return (
    <>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 12 }}>
        <span className="muted small">Give TestAgent an API endpoint (ideally its OpenAPI / Swagger document) and credentials: it generates the test cases, runs them and reports the results.</span>
        {has('case.write') && <button className="btn primary" onClick={() => setCreating(true)}>+ New API test</button>}
      </div>
      <Alert>{suites.error}</Alert>
      {suites.data && suites.data.length === 0 && (
        <div className="panel empty"><p><b>No API tests yet.</b></p>
          <p className="small">Supported: OpenAPI 3.x and Swagger 2.0 (URL or file), or a plain list of endpoints. Authentication: API key, bearer token, basic auth and OAuth2 client credentials.</p>
          {has('case.write') && <button className="btn primary" onClick={() => setCreating(true)}>+ New API test</button>}</div>
      )}
      <div className="cards">
        {suites.data?.map((s) => (
          <Link key={s.id} className="card" to={`/api-suites/${s.id}`}>
            <div className="row" style={{ justifyContent: 'space-between', flexWrap: 'nowrap', alignItems: 'flex-start' }}>
              <div className="ttl">{s.name}</div>{s.last_run ? <Badge s={s.last_run.status} /> : <Badge s="untested" label="not run" />}
            </div>
            <div className="sub mono" style={{ wordBreak: 'break-all' }}>{s.base_url || s.spec_url}</div>
            <div className="sub" style={{ margin: '6px 0' }}>{s.spec_title || (s.spec_source === 'manual' ? 'Endpoint list' : 'OpenAPI')} · {s.enabled_count} case(s) · {AUTH_LABEL[s.auth_type]}</div>
            {s.last_run?.summary && 'pass_rate' in s.last_run.summary && <div className="sub">Last run {fmtDate(s.last_run.created_at)}: {s.last_run.summary.passed}/{s.last_run.summary.total} passed · p95 {s.last_run.summary.p95_ms} ms</div>}
            {s.import_state === 'pending' || s.import_state === 'running' ? <div className="sub" style={{ color: 'var(--run)' }}>Importing specification…</div> : null}
            {s.import_state === 'error' && <div className="sub" style={{ color: 'var(--bad)' }}>Import failed</div>}
            {s.policy_problems.length > 0 && <div className="sub" style={{ color: 'var(--warn)' }}>Host not authorised yet</div>}
          </Link>
        ))}
      </div>
      {creating && <NewSuiteModal p={p} has={has} onClose={() => setCreating(false)} />}
    </>
  )
}
