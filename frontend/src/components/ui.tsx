import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import type { Branding, Me, Role, SystemStatus } from '../api'

export const fmtDate = (s?: string | null) =>
  s ? new Date(s).toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' }) : '—'
export const fmtMs = (ms?: number | null) =>
  ms == null ? '—' : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`
export const fmtBytes = (n: number) =>
  n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`

export function Badge({ s, label }: { s: string; label?: string }) { return <span className={`badge ${s}`}>{label ?? s}</span> }

const ORDER: [string, string][] = [['passed', 'Pass'], ['failed', 'Fail'], ['blocked', 'Blocked'], ['retest', 'Retest'], ['skipped', 'Skipped'], ['untested', 'Not run']]
export function Progress({ counts, total }: { counts: Record<string, number>; total: number }) {
  return (
    <div className="prog" role="img" aria-label={ORDER.map(([k, l]) => `${l} ${counts[k] ?? 0}`).join(', ')}>
      {total > 0 && ORDER.map(([k]) => (counts[k] ?? 0) > 0 && <span key={k} className={`seg ${k}`} style={{ width: `${(100 * counts[k]) / total}%` }} />)}
    </div>
  )
}
export function Counts({ counts }: { counts: Record<string, number> }) {
  return <div className="counts">{ORDER.map(([k, l]) => (counts[k] ?? 0) > 0 && <span key={k} className={`cnt ${k}`}><i />{l} {counts[k]}</span>)}</div>
}

export function Tabs<T extends string>({ value, onChange, tabs }: { value: T; onChange: (v: T) => void; tabs: [T, string][] }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map(([k, l]) => <button key={k} role="tab" aria-selected={value === k} className={value === k ? 'on' : ''} onClick={() => onChange(k)}>{l}</button>)}
    </div>
  )
}

export function Alert({ kind = 'err', children }: { kind?: 'err' | 'warn' | 'ok'; children: ReactNode }) {
  return children ? <div className={`alert ${kind}`} role={kind === 'err' ? 'alert' : 'status'}>{children}</div> : null
}

export function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', k); return () => window.removeEventListener('keydown', k)
  }, [onClose])
  return (
    <div className="modal-bg" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}>
        <div className="row" style={{ justifyContent: 'space-between', marginBottom: 6 }}>
          <h2 style={{ margin: 0 }}>{title}</h2>
          <button className="btn sm" onClick={onClose} aria-label="Close">✕</button>
        </div>
        {children}
      </div>
    </div>
  )
}

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState('')
  const [tick, setTick] = useState(0)
  useEffect(() => {
    let live = true
    fn().then((d) => { if (live) { setData(d); setError('') } })
      .catch((e) => live && setError(e.message))
    return () => { live = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick])
  return { data, error, reload: () => setTick((t) => t + 1), setData }
}

interface Ctx {
  me: Me; status: SystemStatus | null; branding: Branding
  can: (r: Role) => boolean        // release-1.0 levels (viewer/tester/admin)
  has: (permission: string) => boolean
  refreshStatus: () => void
}
export const AppCtx = createContext<Ctx>(null as unknown as Ctx)
export const useApp = () => useContext(AppCtx)

/** Permissions the signed-in user has on one project right now (from the server; enforced there too). */
export function useProjectPerms(pid: number | string | undefined | null) {
  const [perms, setPerms] = useState<string[]>([])
  useEffect(() => {
    if (!pid) { setPerms([]); return }
    let live = true
    import('../api').then(({ api }) => api.get<{ my_permissions: string[] }>(`/projects/${pid}`))
      .then((p) => { if (live) setPerms(p.my_permissions ?? []) }).catch(() => live && setPerms([]))
    return () => { live = false }
  }, [pid])
  return (perm: string) => perms.includes(perm)
}
