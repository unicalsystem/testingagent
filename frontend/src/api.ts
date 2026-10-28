// Thin typed client. Cookies carry the session (HttpOnly); every mutating request sends the
// X-Requested-With header the backend requires as CSRF defence.

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message) }
}

// Every tenant-scoped call goes to /api/t/<slug>/...; the server checks the slug against the session.
let BASE = '/api'
export const DEFAULT_TENANT = 'unical'
export function setTenant(slug: string) { BASE = `/api/t/${encodeURIComponent(slug)}` }
export const apiBase = () => BASE

async function req<T>(method: string, path: string, body?: unknown, absolute = false): Promise<T> {
  const res = await fetch(absolute ? path : `${BASE}${path}`, {
    method,
    credentials: 'same-origin',
    headers: {
      'X-Requested-With': 'TestAgent',
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (res.status === 401 && !path.startsWith('/auth/') && !absolute) {
    window.dispatchEvent(new Event('tp:unauthorized'))
  }
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`
    try { const j = await res.json(); if (j.detail) msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail) } catch { /* not json */ }
    throw new ApiError(res.status, msg)
  }
  return res.status === 204 ? (undefined as T) : res.json()
}

async function upload<T>(path: string, form: FormData): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { method: 'POST', credentials: 'same-origin', headers: { 'X-Requested-With': 'TestAgent' }, body: form })
  if (res.status === 401) window.dispatchEvent(new Event('tp:unauthorized'))
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`
    if (res.status === 413) msg = 'The file is too large for the server (max 10 MB)'
    try { const j = await res.json(); if (j.detail) msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail) } catch { /* not json */ }
    throw new ApiError(res.status, msg)
  }
  return res.json()
}

export const api = {
  upload,
  get: <T,>(p: string) => req<T>('GET', p),
  post: <T,>(p: string, b?: unknown) => req<T>('POST', p, b ?? {}),
  put: <T,>(p: string, b: unknown) => req<T>('PUT', p, b),
  patch: <T,>(p: string, b: unknown) => req<T>('PATCH', p, b),
  del: <T,>(p: string) => req<T>('DELETE', p),
}

/** Absolute API paths: /api/public/... (no sign-in) and /api/platform/... (Unical Super Admin). */
export const abs = {
  get: <T,>(p: string) => req<T>('GET', p, undefined, true),
  post: <T,>(p: string, b?: unknown) => req<T>('POST', p, b ?? {}, true),
}

export type Role = 'admin' | 'tester' | 'viewer'  // release-1.0 coarse levels, still used by can()
export type TenantRole = 'tenant_owner' | 'tenant_admin' | 'project_manager' | 'qa_engineer' | 'developer' | 'auditor'
export const ROLE_LABELS: Record<TenantRole, string> = {
  tenant_owner: 'Tenant Owner', tenant_admin: 'Tenant Administrator', project_manager: 'Project Manager',
  qa_engineer: 'QA Engineer', developer: 'Developer', auditor: 'Auditor / Read-only',
}
export interface Me {
  id: number; username: string; display_name: string; role: TenantRole; role_label: string; legacy_role: Role
  permissions: string[]; is_platform_admin: boolean; must_change_password: boolean
  email?: string; microsoft_linked?: boolean; sign_in_methods?: { password: boolean; microsoft: boolean }
  tenant?: { id: number; slug: string; name: string }
  legal_pending?: string[]
}
export interface Branding {
  slug: string; name: string; display_name: string; logo_data_url: string; primary_color: string
  welcome_text: string; support_email: string; support_url: string
}
export interface RunRef { id: number; status: RunStatus; created_at: string }
export type RunStatus = 'queued' | 'running' | 'passed' | 'failed' | 'error' | 'cancelled'
export interface Project {
  id: number; name: string; description: string; base_url: string; extra_hosts: string[]
  policy_problems: string[]; case_count: number; last_run: RunRef | null; created_at: string
  my_permissions?: string[]
  setup_steps?: Step[]; app_notes?: string
}
export type Step = { action: string; description?: string; [k: string]: unknown }
export interface CaseMeta {
  external_id: string; module: string; sub_module: string; priority: string; scenario_type: string; assigned_to: string
  requirement: string; preconditions: string; test_data: string; procedure: string; expected_result: string
}
export interface TestCase extends CaseMeta {
  id: number; project_id: number; name: string; description: string; tags: string[]
  steps: Step[]; source: string; last_run?: RunRef | null; updated_at: string
  attributes: Record<string, string>; import_batch: string; use_setup: boolean
}
export type ItemStatus = 'untested' | 'passed' | 'failed' | 'blocked' | 'retest' | 'skipped'
export const ITEM_STATUSES: ItemStatus[] = ['passed', 'failed', 'blocked', 'retest', 'skipped', 'untested']
export const ITEM_LABEL: Record<ItemStatus, string> = { untested: 'Not run', passed: 'Pass', failed: 'Fail', blocked: 'Blocked', retest: 'Retest', skipped: 'Skipped' }
export interface TestRunSummary {
  id: number; project_id: number; name: string; description: string; status: 'active' | 'closed'; source: string
  group_label: string; import_batch: string; created_at: string; updated_at: string; closed_at: string | null
  created_by: string | null; total: number; counts: Record<ItemStatus, number>; progress: number
}
export interface TestRunItem {
  id: number; test_case_id: number; position: number; status: ItemStatus; actual_result: string; notes: string
  assigned_to: string; executed_by: string; executed_at: string | null; automated_run_id: number | null; automated_status: string | null
  case: Pick<CaseMeta, 'external_id' | 'module' | 'sub_module' | 'priority' | 'scenario_type' | 'preconditions' | 'test_data' | 'procedure' | 'expected_result'> & { name: string; description: string; has_steps: boolean; archived: boolean }
}
export interface TestRunDetail extends TestRunSummary { items: TestRunItem[] }
export interface GroupOption { field: string; label: string; distinct: number }
export interface ImportField { key: string; label: string; target: 'case' | 'attr' | 'result'; help: string; required: boolean; groupable: boolean }
export interface ImportPreview {
  file: string; sheets: string[]; sheet: string; headers: string[]; row_count: number; truncated: boolean; max_rows: number
  mapping: Record<string, string>; suggested: Record<string, string>; sample: Record<string, string>[]
  mapping_error?: string
  valid?: number; errors?: { row: number; error: string }[]; error_count?: number
  warnings?: { row: number; warning: string }[]; warning_count?: number
  existing_matches?: number; new_cases?: number; results?: Record<string, number>
  group_options?: GroupOption[]; groups?: Record<string, { value: string; count: number }[]>; recommended_group_by?: string[]
}
export interface Evidence {
  id: number; kind: string; step_id: number | null; mime: string; size: number; sha256: string; file: string; url: string
  storage?: string; location?: string; upload_status?: string; upload_error?: string; available?: boolean
}
export interface RunStep { id: number; idx: number; action: string; description: string; status: string; attempts: number; duration_ms: number; error: string; evidence: Evidence[] }
export interface Run {
  id: number; project_id: number; test_case_id: number | null; case_name: string; status: RunStatus; error: string
  summary: Record<string, number>; cancel_requested: boolean; created_at: string; started_at: string | null; finished_at: string | null
  requested_by?: string; ai_analysis?: string; steps?: RunStep[]; evidence?: Evidence[]
  report_synced_at?: string | null; report_sync_error?: string
  storage_mode?: 'unical' | 'external'; storage_provider?: string; upload_state?: string; details_external?: boolean
}
export interface SystemStatus {
  version: string; environment: string
  ai: { configured: boolean; available: boolean; model: string; last_error: string }
  workers: { id: string; last_seen: string; current_run_id: number | null }[]
  queue: { queued: number; running: number }
  report_store: { enabled: boolean; pending?: number; last_error?: string }
  storage?: { mode: string; provider: string; status: string; ai_enabled: boolean; run_block_reason: string | null; consent_legacy: boolean }
}
export interface Legal {
  entity_name: string; address: string; contact_email: string; grievance_officer_name: string; grievance_officer_email: string
  grievance_officer_phone: string; dpo_email: string; uk_representative: string; us_contact: string; hosting_location: string
  terms_version: string; privacy_version: string; response_days: number
  request_kinds: { kind: string; label: string }[]
  retention: { audit_retention_days: number; registration_retention_days: number; session_retention_days: number; request_response_days: number }
}
export const PROVIDER_LABEL: Record<string, string> = { local: 'Unical-managed storage', sharepoint: 'SharePoint', gdrive: 'Google Drive' }

export type AutoStatus = 'queued' | 'drafting' | 'repair' | 'repairing' | 'trial' | 'drafted' | 'verified' | 'failed' | 'manual_only' | 'applied' | 'rejected' | 'error' | 'cancelled'
export const AUTO_LABEL: Record<AutoStatus, string> = {
  queued: 'Waiting', drafting: 'Drafting', repair: 'Needs repair', repairing: 'Repairing', trial: 'Trial run',
  drafted: 'Draft ready', verified: 'Verified', failed: 'Could not verify', manual_only: 'Stays manual',
  applied: 'Saved', rejected: 'Rejected', error: 'Error', cancelled: 'Cancelled',
}
export interface AutomationJob {
  id: number; project_id: number; status: 'exploring' | 'mapping' | 'running' | 'done' | 'cancelled'
  options: { verify: boolean; auto_apply: boolean; record_results: boolean; max_attempts: number }
  test_run: { id: number; name: string } | null; created_at: string; finished_at: string | null
  claude_calls: number; total: number; counts: Partial<Record<AutoStatus, number>>; progress: number
  explore_log: string; has_map: boolean
}
export interface AutomationItem {
  id: number; status: AutoStatus; attempts: number; steps: Step[]; use_setup: boolean; reason: string; error: string
  trial_run_id: number | null; trial_status: string | null; updated_at: string
  case: { id: number; external_id: string; name: string; module: string; procedure: string; expected_result: string; has_steps: boolean }
}
export interface AutomationJobDetail extends AutomationJob { app_map: string; items: AutomationItem[] }

// ---- release 1.6: API testing & monitoring ----------------------------------------------------
export type AuthType = 'none' | 'api_key' | 'bearer' | 'basic' | 'oauth2'
export const AUTH_LABEL: Record<AuthType, string> = {
  none: 'No authentication', api_key: 'API key', bearer: 'Bearer token', basic: 'Basic auth (username + password)', oauth2: 'OAuth2 client credentials',
}
export interface AuthIn { type: AuthType; config: Record<string, string>; secrets: Record<string, string> }
export interface ApiRunRef { id: number; status: RunStatus; created_at: string; finished_at: string | null; summary: ApiRunSummary | Record<string, never> }
export interface ApiRunSummary {
  total: number; passed: number; failed: number; error: number; skipped: number; pass_rate: number
  avg_ms: number; p95_ms: number; max_ms: number; endpoints: number; executed: number
  by_category: Record<string, { total: number; passed: number; failed: number; error: number; skipped: number }>
}
export interface ApiSuite {
  id: number; project_id: number; name: string; base_url: string; spec_source: 'url' | 'upload' | 'manual'
  spec_url: string; spec_title: string; has_spec: boolean; endpoints_text: string
  auth_type: AuthType; auth_config: Record<string, string>; auth_hint: string; has_credentials: boolean
  requires_auth: boolean; allow_writes: boolean; verify_tls: boolean; timeout_ms: number; max_response_ms: number
  default_headers: Record<string, string>; import_state: '' | 'pending' | 'running' | 'done' | 'error'; import_error: string
  case_count: number; enabled_count: number; write_count: number; policy_problems: string[]; run_block_reason: string | null
  last_run: ApiRunRef | null; my_permissions: string[]; created_at: string; updated_at: string
  generation?: { generated: number | null; import?: string; run: number | null; run_error?: string }
}
export interface ApiCaseDef {
  method: string; path: string; path_params?: Record<string, string | null>; path_samples?: Record<string, string>
  query?: Record<string, string>; headers?: Record<string, string>; body?: unknown; body_type?: string | null
  auth?: 'suite' | 'none' | 'invalid'
  expect: { status: (number | string)[]; max_ms?: number; content_type?: string; schema?: unknown; body_contains?: string; json_equals?: Record<string, unknown> }
  operation?: string; tags?: string[]
}
export interface ApiCase {
  id: number; suite_id: number; position: number; name: string; category: 'positive' | 'auth' | 'negative' | 'performance'
  method: string; path: string; definition: ApiCaseDef; source: 'spec' | 'auto' | 'ai' | 'manual'; is_write: boolean; enabled: boolean
}
export interface ApiAssertion { name: string; ok: boolean; detail: string }
export interface ApiResult {
  id: number; idx: number; case_id: number | null; name: string; category: string; method: string; path: string; url: string
  status: 'passed' | 'failed' | 'error' | 'skipped'; http_status: number | null; duration_ms: number; assertions: ApiAssertion[]
  request: { headers?: Record<string, string>; body?: string }
  response: { headers?: Record<string, string>; body?: string; size?: number; truncated?: boolean; final_url?: string }
  error: string
}
export interface ApiRun extends ApiRunRef {
  suite_id: number; project_id: number; error: string; options: { include_writes?: boolean }
  suite: { name?: string; base_url?: string; auth_type?: string; spec_title?: string; spec_source?: string }
  cancel_requested: boolean; started_at: string | null; requested_by: string | null; report_sha256: string
  results?: ApiResult[]
}
export type MonitorStatus = 'unknown' | 'up' | 'down' | 'paused'
export type AlertChannel = 'email' | 'webhook' | 'inapp'
export const CHANNEL_LABEL: Record<AlertChannel, string> = { email: 'E-mail', webhook: 'Webhook (Slack / Teams)', inapp: 'In-app (dashboard & banner)' }
export interface MonitorStats {
  checks: number; failed_checks: number; uptime_pct: number | null; downtime_seconds: number; incidents: number
  mttr_seconds: number; avg_ms: number; p95_ms: number
}
export interface MonitorIncident { id: number; started_at: string; ended_at: string | null; cause: string; failed_checks: number; duration_seconds: number; alerts: { channel: string; ok: boolean; at: string; error?: string; to?: string; event?: string }[] }
export interface Monitor {
  id: number; project_id: number; suite_id: number | null; suite_name: string | null; name: string; url: string; method: 'GET' | 'HEAD'
  headers: Record<string, string>; use_suite_auth: boolean; expected_status: (number | string)[]; body_contains: string
  max_response_ms: number; timeout_ms: number; interval_seconds: number; failure_threshold: number; enabled: boolean
  alert_channels: AlertChannel[]; alert_emails: string[]; webhook_kind: 'slack' | 'teams' | 'generic'; webhook_hint: string; has_webhook: boolean
  status: MonitorStatus; consecutive_failures: number; failing_since: string | null; last_check_at: string | null
  next_check_at: string | null; last_ok_at: string | null; last_status_change_at: string | null; last_error: string
  policy_problems: string[]; created_at: string; uptime_24h: number | null
  project_name?: string; uptime_30d?: number | null
  stats?: Record<'24h' | '7d' | '30d', MonitorStats>
  series?: { t: string; ok: number; failed: number; avg_ms: number }[]
  incidents?: MonitorIncident[]
  recent_checks?: { checked_at: string; ok: boolean; http_status: number | null; duration_ms: number; error: string }[]
  my_permissions?: string[]
}
export interface MonitorAlert { id: number; name: string; project_id: number; failing_since: string | null; last_error: string }

// ---- release 1.7: connectors (Azure DevOps, Jira, GitHub, GitLab, webhook) ---------------------
export type IntegrationKind = 'azure_devops' | 'jira' | 'github' | 'gitlab' | 'webhook'
export type RunType = 'api' | 'browser' | 'manual'
export const KIND_LABEL: Record<IntegrationKind, string> = { azure_devops: 'Azure DevOps', jira: 'Jira', github: 'GitHub Issues', gitlab: 'GitLab Issues', webhook: 'Webhook' }
export const RUN_TYPE_LABEL: Record<RunType, string> = { api: 'API test runs', browser: 'Automated browser runs', manual: 'Manual test runs (when closed)' }
export interface IntegrationSync {
  id: number; integration_id: number; source_kind: string; source_type: RunType; source_id: number
  status: 'pending' | 'running' | 'done' | 'error'; attempts: number; error: string
  result: { ado_run_id?: number; ado_run_url?: string; matched?: number; unmatched?: number; issues_created?: { id: string; url: string; case?: string }[]
    issues_updated?: { id: string; url: string }[]; issues_recovered?: { id: string; url: string }[]; issues_skipped?: number; delivered_items?: number; report_attached?: boolean }
  created_at: string; updated_at: string; finished_at: string | null; next_attempt_at: string
}
export interface Integration {
  id: number; project_id: number; kind: IntegrationKind; kind_label: string; name: string; enabled: boolean
  trigger: 'auto' | 'failures' | 'manual'; run_types: RunType[]; config: Record<string, unknown>; secret_hint: string; has_secret: boolean
  status: 'unverified' | 'ok' | 'error'; last_error: string; last_sync_at: string | null; active_since: string; created_at: string
  sync_counts: Record<string, number>; issue_count: number
}
export interface SourceTarget { id: number; name: string; kind: IntegrationKind; kind_label: string; trigger: string; sync: IntegrationSync | null }

// ---- release 1.8: mobile app testing ---------------------------------------------------------
export type MobileKind = 'browserstack' | 'lambdatest' | 'saucelabs' | 'appium' | 'unical_lab' | 'emulator'
export type MPlatform = 'android' | 'ios'
export interface MDevice { name: string; os_version: string; platform: MPlatform }
export interface MStep { action: string; description?: string; target?: string; text?: string; url?: string; direction?: string
  distance?: number; x?: number; y?: number; ms?: number; match?: string; state?: string; clear_first?: boolean
  max_swipes?: number; name?: string; timeout_ms?: number }
export interface MobileOverview {
  providers: number; apps: number; cases: number; run_groups: number
  kinds: { kind: MobileKind; label: string; available: boolean }[]; sauce_regions: string[]; emulated_devices: MDevice[]
  actions: string[]; ai_available: boolean; max_app_mb: number; my_permissions: string[]
}
export interface MobileProvider {
  id: number; project_id: number; kind: MobileKind; label: string; name: string; config: Record<string, string>
  secret_hint: string; has_secret: boolean; status: 'ok' | 'error' | 'unverified'; last_error: string; native: boolean
  created_at: string; updated_at: string
}
export interface MobileApp {
  id: number; name: string; platform: MPlatform; version: string; filename: string; has_file: boolean; source_url: string
  size: number; sha256: string; app_id: string; app_activity: string; created_at: string
}
export interface MobileCase {
  id: number; project_id: number; name: string; description: string; kind: 'native' | 'web'; platform: MPlatform | 'any'
  app_id: number | null; start_url: string; step_count: number; source: 'manual' | 'ai' | 'recorded'; test_case_id: number | null
  tags: string[]; steps?: MStep[]; created_at: string; updated_at: string
}
export interface MobileRunRow {
  id: number; group_id: number; case_id: number | null; case_name: string; device: MDevice & { provider_kind?: string }
  status: RunStatus; error: string; session_url: string; passed_steps: number; total_steps: number
  created_at: string; started_at: string | null; finished_at: string | null
}
export interface MobileStepResult { i: number; action: string; description: string; status: 'passed' | 'failed' | 'skipped'; error: string; note: string; shot: string; ms: number }
export interface MobileRunDetail extends MobileRunRow { steps: MobileStepResult[]; case: { name: string; kind: string; platform: string; start_url: string }; has_video: boolean }
export interface MobileRunGroup {
  id: number; project_id: number; name: string; provider: string; provider_kind: string; devices: MDevice[]
  counts: Record<string, number>; total: number; status: RunStatus; created_at: string; runs?: MobileRunRow[]
  skipped?: { case: string; device: string; reason: string }[]
}
export interface MElement { ref: number; type: string; text: string; desc: string; id: string; interactive: boolean; target: string; box: [number, number, number, number] }
export interface MobileSession {
  id: number; project_id: number; purpose: 'record' | 'ai'; status: 'starting' | 'ready' | 'closed' | 'error'
  phase: '' | 'running' | 'done' | 'failed' | 'stopped'; device: MDevice; target: { kind: 'native' | 'web'; platform: MPlatform; app_id?: number; app_package?: string; start_url?: string }
  goal: string; test_case_id: number | null; session_url: string; error: string; steps: MStep[]; step_text: string[]
  log: { at: string; kind: string; text: string }[]
  screen: { seq: number; ok: boolean; error: string; note: string; has_image: boolean; width: number | null; height: number | null; url: string; elements: MElement[] }
  busy: boolean; created_at: string; closed_at: string | null
}
