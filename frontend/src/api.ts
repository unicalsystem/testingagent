// Thin typed client. Cookies carry the session (HttpOnly); every mutating request sends the
// X-Requested-With header the backend requires as CSRF defence.

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message) }
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    credentials: 'same-origin',
    headers: {
      'X-Requested-With': 'TestPilot',
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (res.status === 401 && !path.startsWith('/auth/')) {
    window.dispatchEvent(new Event('tp:unauthorized'))
  }
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`
    try { const j = await res.json(); if (j.detail) msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail) } catch { /* not json */ }
    throw new ApiError(res.status, msg)
  }
  return res.status === 204 ? (undefined as T) : res.json()
}

export const api = {
  get: <T,>(p: string) => req<T>('GET', p),
  post: <T,>(p: string, b?: unknown) => req<T>('POST', p, b ?? {}),
  put: <T,>(p: string, b: unknown) => req<T>('PUT', p, b),
  patch: <T,>(p: string, b: unknown) => req<T>('PATCH', p, b),
  del: <T,>(p: string) => req<T>('DELETE', p),
}

export type Role = 'admin' | 'tester' | 'viewer'
export interface Me { id: number; username: string; display_name: string; role: Role; must_change_password: boolean }
export interface RunRef { id: number; status: RunStatus; created_at: string }
export type RunStatus = 'queued' | 'running' | 'passed' | 'failed' | 'error' | 'cancelled'
export interface Project {
  id: number; name: string; description: string; base_url: string; extra_hosts: string[]
  policy_problems: string[]; case_count: number; last_run: RunRef | null; created_at: string
}
export type Step = { action: string; description?: string; [k: string]: unknown }
export interface TestCase {
  id: number; project_id: number; name: string; description: string; tags: string[]
  steps: Step[]; source: string; last_run?: RunRef | null; updated_at: string
}
export interface Evidence { id: number; kind: string; step_id: number | null; mime: string; size: number; sha256: string; file: string; url: string }
export interface RunStep { id: number; idx: number; action: string; description: string; status: string; attempts: number; duration_ms: number; error: string; evidence: Evidence[] }
export interface Run {
  id: number; project_id: number; test_case_id: number | null; case_name: string; status: RunStatus; error: string
  summary: Record<string, number>; cancel_requested: boolean; created_at: string; started_at: string | null; finished_at: string | null
  requested_by?: string; ai_analysis?: string; steps?: RunStep[]; evidence?: Evidence[]
  report_synced_at?: string | null; report_sync_error?: string
}
export interface SystemStatus {
  version: string; environment: string
  ai: { configured: boolean; available: boolean; model: string; last_error: string }
  workers: { id: string; last_seen: string; current_run_id: number | null }[]
  queue: { queued: number; running: number }
  report_store: { enabled: boolean; pending?: number; last_error?: string }
}
