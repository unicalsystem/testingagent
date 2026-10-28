import { useEffect, useState } from 'react'
import { api, type IntegrationSync, type RunType, type SourceTarget } from '../api'
import { syncSummary } from './Integrations'
import { Modal } from './ui'

/** "Push" button on a run page: shows each applicable connector, its push state, and pushes on demand. */
export function PushToTracker({ type, id, canPush }: { type: RunType; id: number; canPush: boolean }) {
  const [targets, setTargets] = useState<SourceTarget[] | null>(null)
  const [open, setOpen] = useState(false), [busy, setBusy] = useState(false), [err, setErr] = useState('')
  const load = () => api.get<SourceTarget[]>(`/integrations/for-source?source_type=${type}&source_id=${id}`).then(setTargets).catch(() => setTargets([]))
  useEffect(() => { load() // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [type, id])
  useEffect(() => {
    if (!open || !targets?.some((t) => t.sync && ['pending', 'running'].includes(t.sync.status))) return
    const tm = window.setTimeout(load, 3000)
    return () => clearTimeout(tm)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, targets])
  if (!targets?.length) return null
  const done = targets.filter((t) => t.sync?.status === 'done').length
  const push = async (ids?: number[]) => {
    setBusy(true); setErr('')
    try { await api.post<IntegrationSync[]>('/integrations/push', { source_type: type, source_id: id, integration_ids: ids }); await load() }
    catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  return (
    <>
      <button className="btn" onClick={() => setOpen(true)} title="Azure DevOps / Jira / GitHub / GitLab / webhook">⇪ Push{done ? ` (${done}/${targets.length})` : ''}</button>
      {open && (
        <Modal title="Push results to connected tools" onClose={() => setOpen(false)}>
          {err && <div className="alert err">{err}</div>}
          <table className="t resp"><tbody>{targets.map((t) => (
            <tr key={t.id}>
              <td><b>{t.name}</b><div className="small muted">{t.kind_label} · {t.trigger === 'manual' ? 'manual push' : t.trigger === 'failures' ? 'automatic on failures' : 'automatic'}</div></td>
              <td className="small">{t.sync ? <><b>{t.sync.status}</b>{t.sync.status === 'done' && <> · {syncSummary(t.sync)}</>}{t.sync.error && <div style={{ color: 'var(--bad)' }}>{t.sync.error}</div>}</> : <span className="muted">not pushed</span>}</td>
              <td style={{ textAlign: 'right' }}>{canPush && <button className="btn sm" disabled={busy || ['pending', 'running'].includes(t.sync?.status ?? '')} onClick={() => push([t.id])}>{t.sync ? 'Push again' : 'Push'}</button>}</td>
            </tr>))}</tbody></table>
          <div className="hint">Pushing again updates the open issues instead of creating duplicates.</div>
          <div className="row" style={{ justifyContent: 'flex-end', marginTop: 12 }}>
            {canPush && <button className="btn primary" disabled={busy} onClick={() => push()}>Push to all</button>}
            <button className="btn" onClick={() => setOpen(false)}>Close</button>
          </div>
        </Modal>
      )}
    </>
  )
}
