import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, type AutomationJob } from '../api'
import { Alert, Modal, useApp } from './ui'

/** Starts an AI automation job: options, cost/impact summary and explicit consent. */
export function AutomateModal({ pid, count, caseIds, testRunId, itemIds, allManual, hasSetup, baseUrl, onClose }: {
  pid: number; count: number; caseIds?: number[]; testRunId?: number; itemIds?: number[]; allManual?: boolean
  hasSetup: boolean; baseUrl: string; onClose: () => void
}) {
  const { status } = useApp()
  const nav = useNavigate()
  const [verify, setVerify] = useState(true), [autoApply, setAutoApply] = useState(true)
  const [record, setRecord] = useState(true), [attempts, setAttempts] = useState(2)
  const [consent, setConsent] = useState(false), [busy, setBusy] = useState(false), [err, setErr] = useState('')
  const maxCalls = count * (verify ? attempts : 1)
  const start = async () => {
    setBusy(true); setErr('')
    try {
      const j = await api.post<AutomationJob>(`/projects/${pid}/automation-jobs`, {
        ...(testRunId ? { test_run_id: testRunId, item_ids: itemIds?.length ? itemIds : null } : caseIds?.length ? { case_ids: caseIds } : { all_manual: !!allManual }),
        options: { verify, auto_apply: autoApply, record_results: record, max_attempts: attempts }, confirm: true,
      })
      nav(`/automation-jobs/${j.id}`)
    } catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  return (
    <Modal title="Automate manual test cases with AI" onClose={onClose}>
      <Alert>{err}</Alert>
      {!status?.ai.configured && <Alert kind="warn">The Claude API is not configured on this server, so AI automation cannot run.</Alert>}
      <p className="small" style={{ marginTop: 0 }}>The test agent first explores <span className="mono">{baseUrl}</span>{hasSetup ? ' (signing in with the project setup steps)' : ''} to map its pages, fields and buttons. Claude then turns each manual case's procedure and expected result into browser steps.</p>
      {!hasSetup && <Alert kind="warn">No sign-in setup steps are configured for this project, so only signed-out pages can be explored and automated reliably. Add them under the project's Settings tab first.</Alert>}
      <label className="row" style={{ alignItems: 'flex-start', marginTop: 8 }}><input type="checkbox" checked={verify} onChange={(e) => setVerify(e.target.checked)} style={{ marginTop: 4 }} />
        <span><b>Verify by running each draft</b><span className="hint" style={{ display: 'block' }}>Executes every draft in the browser. If it fails, Claude repairs it using the error and screenshot.</span></span></label>
      {verify && (
        <div style={{ margin: '6px 0 0 24px', maxWidth: 280 }}>
          <label className="f" htmlFor="ma" style={{ marginTop: 0 }}>Attempts per case</label>
          <select className="i" id="ma" value={attempts} onChange={(e) => setAttempts(+e.target.value)}>
            <option value={1}>1 (no repair)</option><option value={2}>2 (one repair, recommended)</option><option value={3}>3</option>
          </select>
        </div>
      )}
      <label className="row" style={{ alignItems: 'flex-start', marginTop: 10 }}><input type="checkbox" checked={autoApply} onChange={(e) => setAutoApply(e.target.checked)} style={{ marginTop: 4 }} />
        <span><b>Save {verify ? 'verified' : ''} drafts to the cases automatically</b><span className="hint" style={{ display: 'block' }}>Untick to review and approve every draft yourself first. Drafts that fail verification always wait for review.</span></span></label>
      {testRunId && verify && <label className="row" style={{ alignItems: 'flex-start', marginTop: 10 }}><input type="checkbox" checked={record} onChange={(e) => setRecord(e.target.checked)} style={{ marginTop: 4 }} />
        <span><b>Record verified passes in this test run</b><span className="hint" style={{ display: 'block' }}>Cases that fail stay “Not run”, because a failure may be the draft's fault rather than a defect.</span></span></label>}
      <div className="consent">
        <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} style={{ marginTop: 3 }} />
        <span>I understand this will process <b>{count}</b> manual case(s) with up to <b>{maxCalls}</b> Claude API calls (billed to the configured API key){verify ? <>, and run the drafts against <span className="mono">{baseUrl}</span>. Steps such as saving, deleting or deactivating will really happen there — use a test environment or test data</> : null}.</span>
      </div>
      <div className="row" style={{ justifyContent: 'flex-end' }}>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" disabled={!consent || busy || !status?.ai.configured || count === 0} onClick={start}>{busy ? 'Starting…' : '✦ Start automation'}</button>
      </div>
    </Modal>
  )
}
