import { useState, type FormEvent, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { KIND_LABEL, RUN_TYPE_LABEL, api, type Integration, type IntegrationKind, type IntegrationSync, type Project, type RunType } from '../api'
import { Alert, Badge, Modal, fmtDate, useAsync } from './ui'

type Cfg = Record<string, string | number | boolean>
const DEFAULTS: Record<IntegrationKind, Cfg> = {
  azure_devops: { org_url: 'https://dev.azure.com/', project: '', publish_results: true, plan_id: '', suite_id: '', case_map: '', use_external_id: false,
    create_bugs: true, work_item_type: 'Bug', area_path: '', iteration_path: '', tags: 'TestAgent', priority: 2, attach_report: true },
  jira: { deployment: 'cloud', base_url: 'https://', email: '', project_key: '', issue_type: 'Bug', labels: 'testagent' },
  github: { api_url: 'https://api.github.com', repo: '', labels: 'bug, testagent' },
  gitlab: { base_url: 'https://gitlab.com', project: '', labels: 'bug, testagent' },
  webhook: { include_passed: false },
}
const COMMON: Cfg = { max_bugs: 20, comment_on_pass: true }
const SECRETS: Record<IntegrationKind, [string, string, string][]> = {
  azure_devops: [['pat', 'Personal access token (PAT)', 'Scopes: Test Management (read & write), Work Items (read & write)']],
  jira: [['token', 'API token / personal access token', 'Cloud: API token of the e-mail above. Data Center: personal access token.']],
  github: [['token', 'Token', 'Fine-grained token with Issues: read & write on the repository']],
  gitlab: [['token', 'Access token', 'Project or personal access token with the api scope']],
  webhook: [['url', 'Webhook URL', 'HTTPS endpoint that accepts a JSON POST'], ['signing_secret', 'Signing secret (optional)', 'Requests carry X-TestAgent-Signature: sha256=HMAC of the body']],
}

function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return <div><label className="f">{label}</label>{children}{hint && <div className="hint">{hint}</div>}</div>
}

function IntegrationForm({ pid, initial, onClose, onSaved }: { pid: number; initial?: Integration; onClose: () => void; onSaved: () => void }) {
  const [kind, setKind] = useState<IntegrationKind>(initial?.kind ?? 'azure_devops')
  const [name, setName] = useState(initial?.name ?? 'Azure DevOps')
  const [enabled, setEnabled] = useState(initial?.enabled ?? true)
  const [trigger, setTrigger] = useState(initial?.trigger ?? 'auto')
  const [types, setTypes] = useState<RunType[]>(initial?.run_types ?? ['api', 'browser', 'manual'])
  const [cfg, setCfg] = useState<Cfg>({ ...DEFAULTS[initial?.kind ?? 'azure_devops'], ...COMMON, ...((initial?.config ?? {}) as Cfg) })
  const [sec, setSec] = useState<Record<string, string>>({})
  const [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const set = (k: string, v: string | number | boolean) => setCfg({ ...cfg, [k]: v })
  const txt = (k: string, label: string, ph = '', hint?: string, mono = true) => (
    <Field label={label} hint={hint}><input className={`i${mono ? ' mono' : ''}`} value={String(cfg[k] ?? '')} placeholder={ph} onChange={(e) => set(k, e.target.value)} /></Field>)
  const chk = (k: string, label: ReactNode) => (
    <label className="check"><input type="checkbox" checked={!!cfg[k]} onChange={(e) => set(k, e.target.checked)} /><span>{label}</span></label>)
  const pickKind = (k: IntegrationKind) => { setKind(k); setCfg({ ...DEFAULTS[k], ...COMMON }); setSec({}); if (!initial) setName(KIND_LABEL[k]) }
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr(''); setBusy(true)
    const config = { ...cfg, tags: cfg.tags, labels: cfg.labels }
    try {
      const body = { kind, name, enabled, trigger, run_types: types, config, secrets: sec }
      if (initial) await api.put(`/integrations/${initial.id}`, body); else await api.post(`/projects/${pid}/integrations`, body)
      onSaved()
    } catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  return (
    <Modal title={initial ? `Edit ${initial.name}` : 'New connector'} onClose={onClose}>
      <form onSubmit={submit}>
        <Alert>{err}</Alert>
        {!initial && <div className="chips" style={{ marginBottom: 6 }}>{(Object.keys(KIND_LABEL) as IntegrationKind[]).map((k) =>
          <button type="button" key={k} className={`chip${kind === k ? ' on' : ''}`} onClick={() => pickKind(k)}>{KIND_LABEL[k]}</button>)}</div>}
        <Field label="Name"><input className="i" value={name} onChange={(e) => setName(e.target.value)} required maxLength={120} /></Field>

        {kind === 'azure_devops' && <>
          <div className="f2">{txt('org_url', 'Organization URL', 'https://dev.azure.com/your-org', 'Azure DevOps Services, or your Azure DevOps Server collection URL')}{txt('project', 'Project', 'e.g. Avionics')}</div>
          {chk('publish_results', <><b>Publish test results to Azure Test Plans</b><div className="small muted">Each run becomes an ADO test run with one result per case, and the HTML report attached.</div></>)}
          {cfg.publish_results && <>
            <div className="f2">{txt('plan_id', 'Test plan ID (optional)', 'e.g. 1203')}{txt('suite_id', 'Test suite ID', 'required with a plan')}</div>
            <Field label="Map TestAgent cases to ADO test cases (optional)" hint="One per line: TestAgent case (imported ID such as TC-001, case:45 or api_case:12), ADO test case ID. Mapped cases are recorded against the plan's test points; others are added as extra results.">
              <textarea className="i code" style={{ minHeight: 90 }} spellCheck={false} value={String(cfg.case_map ?? '')} onChange={(e) => set('case_map', e.target.value)} placeholder={'TC-001, 4512\nTC-002, 4513'} /></Field>
            {chk('use_external_id', 'Imported case IDs are ADO test case IDs (e.g. "4512" or "TC-4512")')}
            {chk('attach_report', 'Attach the HTML report to the ADO test run')}
          </>}
          {chk('create_bugs', <><b>Create bugs for failures</b><div className="small muted">One work item per failing case, with the failure screenshot when there is one. Repeat failures comment on it.</div></>)}
          {cfg.create_bugs && <div className="f2">
            {txt('work_item_type', 'Work item type', 'Bug', 'Bug (Agile/Scrum/CMMI) or Issue (Basic)', false)}
            <Field label="Priority"><select className="i" value={Number(cfg.priority)} onChange={(e) => set('priority', +e.target.value)}>{[1, 2, 3, 4].map((n) => <option key={n}>{n}</option>)}</select></Field>
            {txt('area_path', 'Area path (optional)', 'Avionics\\QA', undefined, false)}{txt('iteration_path', 'Iteration path (optional)', 'Avionics\\Sprint 9', undefined, false)}
            {txt('tags', 'Tags', 'TestAgent', 'Comma separated', false)}
          </div>}
        </>}
        {kind === 'jira' && <>
          <div className="f2">
            <Field label="Jira"><select className="i" value={String(cfg.deployment)} onChange={(e) => set('deployment', e.target.value)}><option value="cloud">Jira Cloud</option><option value="datacenter">Jira Data Center / Server</option></select></Field>
            {txt('base_url', 'Jira URL', 'https://your-company.atlassian.net')}
            {cfg.deployment === 'cloud' && txt('email', 'Account e-mail', 'qa-bot@your-company.com', undefined, false)}
            {txt('project_key', 'Project key', 'QA')}{txt('issue_type', 'Issue type', 'Bug', undefined, false)}{txt('labels', 'Labels', 'testagent', 'Comma separated, no spaces in a label', false)}
          </div>
        </>}
        {kind === 'github' && <div className="f2">{txt('repo', 'Repository', 'owner/name')}{txt('api_url', 'API URL', 'https://api.github.com', 'GitHub Enterprise: https://github.your-company.com/api/v3')}{txt('labels', 'Labels', 'bug, testagent', undefined, false)}</div>}
        {kind === 'gitlab' && <div className="f2">{txt('base_url', 'GitLab URL', 'https://gitlab.com')}{txt('project', 'Project', 'group/project or numeric ID')}{txt('labels', 'Labels', 'bug, testagent', undefined, false)}</div>}
        {kind === 'webhook' && chk('include_passed', 'Include passed cases in the payload (default: failures only)')}

        {SECRETS[kind].map(([k, label, hint]) => (
          <Field key={k} label={label} hint={hint}><input className="i mono" type="password" autoComplete="new-password" value={sec[k] ?? ''} onChange={(e) => setSec({ ...sec, [k]: e.target.value })}
            placeholder={initial?.has_secret ? `Stored (${initial.secret_hint}) — leave empty to keep` : ''} required={!initial && k !== 'signing_secret'} /></Field>
        ))}

        <h3 style={{ marginTop: 18 }}>What and when</h3>
        <label className="f">Push</label>
        {([['auto', 'After every finished run'], ['failures', 'Only runs with failures'], ['manual', 'Only when someone presses "Push"']] as const).map(([k, l]) => (
          <label key={k} className="row" style={{ marginBottom: 4 }}><input type="radio" checked={trigger === k} onChange={() => setTrigger(k)} /> {l}</label>))}
        <label className="f">Run types</label>
        {(Object.keys(RUN_TYPE_LABEL) as RunType[]).map((t) => (
          <label key={t} className="row" style={{ marginBottom: 4 }}><input type="checkbox" checked={types.includes(t)} onChange={(e) => setTypes(e.target.checked ? [...types, t] : types.filter((x) => x !== t))} /> {RUN_TYPE_LABEL[t]}</label>))}
        {kind !== 'webhook' && <div className="f2">
          <Field label="Max new issues per run" hint="Protects the tracker from floods when many cases fail at once"><input className="i" type="number" min={1} max={100} value={Number(cfg.max_bugs)} onChange={(e) => set('max_bugs', +e.target.value)} /></Field>
          <div style={{ alignSelf: 'end' }}>{chk('comment_on_pass', 'Comment when a failing case passes again')}</div>
        </div>}
        <label className="check"><input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> Connector active</label>
        <div className="hint">Runs that finished before the connector was created (or re-enabled) are not pushed automatically; use "Push" on a run page for those.</div>
        <div className="row" style={{ justifyContent: 'flex-end', marginTop: 14 }}><button type="button" className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={busy}>{busy ? 'Saving…' : 'Save connector'}</button></div>
      </form>
    </Modal>
  )
}

export function syncSummary(s: IntegrationSync): ReactNode {
  const r = s.result || {}
  const parts: ReactNode[] = []
  if (r.ado_run_id) parts.push(<a key="r" href={r.ado_run_url} target="_blank" rel="noopener noreferrer">ADO run {r.ado_run_id}</a>)
  if (r.issues_created?.length) parts.push(<span key="c">{r.issues_created.length} new issue(s): {r.issues_created.slice(0, 5).map((i) => <a key={i.id} href={i.url} target="_blank" rel="noopener noreferrer" style={{ marginRight: 6 }}>#{i.id}</a>)}</span>)
  if (r.issues_updated?.length) parts.push(<span key="u">{r.issues_updated.length} updated</span>)
  if (r.issues_recovered?.length) parts.push(<span key="v">{r.issues_recovered.length} passing again</span>)
  if (r.issues_skipped) parts.push(<span key="s">{r.issues_skipped} skipped (limit)</span>)
  if (r.delivered_items != null) parts.push(<span key="d">{r.delivered_items} item(s) delivered</span>)
  return parts.length ? parts.reduce<ReactNode[]>((a, p, i) => (i ? [...a, ' · ', p] : [p]), []) : null
}
const SYNC_BADGE: Record<string, string> = { done: 'passed', error: 'failed', pending: 'queued', running: 'running' }
const SRC_PATH: Record<string, string> = { api: '/api-runs/', browser: '/runs/', manual: '/test-runs/' }

function SyncLog({ i }: { i: Integration }) {
  const syncs = useAsync(() => api.get<IntegrationSync[]>(`/integrations/${i.id}/syncs?limit=30`), [i.id])
  const [err, setErr] = useState('')
  return (
    <>
      <Alert>{syncs.error || err}</Alert>
      {syncs.data?.length === 0 && <div className="small muted">Nothing pushed yet.</div>}
      <table className="t resp"><tbody>{syncs.data?.map((s) => (
        <tr key={s.id}>
          <td><Link to={`${SRC_PATH[s.source_type]}${s.source_id}`}>{RUN_TYPE_LABEL[s.source_type]?.split(' ')[0]} #{s.source_id}</Link><div className="small muted">{fmtDate(s.updated_at)}</div></td>
          <td><Badge s={SYNC_BADGE[s.status] ?? s.status} label={s.status} />{s.attempts > 0 && s.status !== 'done' && <div className="small muted">attempt {s.attempts}</div>}</td>
          <td className="small">{syncSummary(s)}{s.error && <div style={{ color: 'var(--bad)' }}>{s.error}</div>}</td>
          <td style={{ textAlign: 'right' }}>{s.status === 'error' && <button className="btn sm" onClick={async () => { try { await api.post(`/integrations/${i.id}/syncs/${s.id}/retry`); syncs.reload() } catch (x) { setErr((x as Error).message) } }}>Retry</button>}</td>
        </tr>))}</tbody></table>
    </>
  )
}

export function IntegrationsTab({ p, has }: { p: Project; has: (perm: string) => boolean }) {
  const list = useAsync(() => api.get<Integration[]>(`/projects/${p.id}/integrations`), [p.id])
  const [edit, setEdit] = useState<Integration | 'new' | null>(null), [open, setOpen] = useState<number | null>(null)
  const [msg, setMsg] = useState<Record<number, { ok: boolean; message: string }>>({}), [err, setErr] = useState('')
  const canEdit = has('project.update')
  const test = async (i: Integration) => {
    setMsg({ ...msg, [i.id]: { ok: true, message: 'Testing…' } })
    try { const r = await api.post<{ ok: boolean; message: string }>(`/integrations/${i.id}/test`); setMsg({ ...msg, [i.id]: r }); list.reload() } catch (x) { setErr((x as Error).message) }
  }
  return (
    <>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 12 }}>
        <span className="muted small">Send results to Azure DevOps Test Plans and open bugs in Azure DevOps, Jira, GitHub or GitLab — or POST them to any tool with a webhook.</span>
        {canEdit && <button className="btn primary" onClick={() => setEdit('new')}>+ New connector</button>}
      </div>
      <Alert>{list.error || err}</Alert>
      {list.data?.length === 0 && <div className="panel empty"><p><b>No connectors.</b></p><p className="small">Results stay in TestAgent until you add one.</p>
        {canEdit && <button className="btn primary" onClick={() => setEdit('new')}>+ New connector</button>}</div>}
      {list.data?.map((i) => (
        <div key={i.id} className="panel">
          <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
            <div style={{ minWidth: 0 }}>
              <h2 style={{ marginBottom: 2 }}>{i.name} <span className="tag">{i.kind_label}</span>{!i.enabled && <span className="tag">off</span>}</h2>
              <div className="small muted">
                {i.trigger === 'auto' ? 'Every finished run' : i.trigger === 'failures' ? 'Runs with failures' : 'Manual push only'} · {i.run_types.map((t) => RUN_TYPE_LABEL[t].split(' (')[0]).join(', ')}
                {i.last_sync_at && <> · last push {fmtDate(i.last_sync_at)}</>} · {i.issue_count} tracked issue(s)
              </div>
              {i.status === 'error' && i.last_error && <div className="small" style={{ color: 'var(--bad)', marginTop: 4 }}>{i.last_error}</div>}
              {msg[i.id] && <div className="small" style={{ color: msg[i.id].ok ? 'var(--ok)' : 'var(--bad)', marginTop: 4 }}>{msg[i.id].message}</div>}
            </div>
            <div className="row">
              <span className={`pill ${i.status === 'ok' ? 'ok' : i.status === 'error' ? 'bad' : ''}`}>{i.status === 'ok' ? 'connected' : i.status}</span>
              {canEdit && <button className="btn sm" onClick={() => test(i)}>Test connection</button>}
              <button className="btn sm" onClick={() => setOpen(open === i.id ? null : i.id)}>{open === i.id ? 'Hide pushes' : 'Pushes'}</button>
              {canEdit && <button className="btn sm" onClick={() => setEdit(i)}>Edit</button>}
              {canEdit && <button className="btn sm danger" onClick={async () => { if (confirm(`Delete connector "${i.name}"? Issues already created in ${i.kind_label} are not touched.`)) { await api.del(`/integrations/${i.id}`); list.reload() } }}>Delete</button>}
            </div>
          </div>
          {open === i.id && <div style={{ marginTop: 12 }}><SyncLog i={i} /></div>}
        </div>
      ))}
      {edit && <IntegrationForm pid={p.id} initial={edit === 'new' ? undefined : edit} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); list.reload() }} />}
    </>
  )
}
