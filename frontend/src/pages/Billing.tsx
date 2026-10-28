import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { api, apiBase, money, type BillingOverview, type Licence, type Plan, type Quote } from '../api'
import { PlanGrid } from '../components/Pricing'
import { Alert, Badge, Modal, fmtDate, useApp } from '../components/ui'

declare global { interface Window { Razorpay?: new (o: Record<string, unknown>) => { open: () => void; on: (e: string, f: (r: { error?: { description?: string } }) => void) => void } } }

function loadRazorpay(): Promise<void> {
  if (window.Razorpay) return Promise.resolve()
  return new Promise((resolve, reject) => {
    const s = document.createElement('script')
    s.src = 'https://checkout.razorpay.com/v1/checkout.js'
    s.onload = () => resolve()
    s.onerror = () => reject(new Error('Could not load the Razorpay checkout. Check your connection or ad blocker.'))
    document.body.appendChild(s)
  })
}

const STATE_TEXT: Record<string, string> = { unlimited: 'Unlimited licence', none: 'No plan yet', trial: 'Free trial', active: 'Active', expired: 'Expired' }

export function LicenceBadge({ l }: { l: Licence | { state: string } }) {
  const map: Record<string, string> = { unlimited: 'passed', active: 'passed', trial: 'running', none: 'queued', expired: 'failed' }
  return <Badge s={map[l.state] ?? 'queued'} label={STATE_TEXT[l.state] ?? l.state} />
}

function Meter({ label, used, limit }: { label: string; used: number; limit: number | null }) {
  if (limit === 0) return null
  const pct = limit ? Math.min(100, Math.round((used / limit) * 100)) : 0
  return (
    <div style={{ minWidth: 0 }}>
      <div className="row small" style={{ justifyContent: 'space-between' }}><span>{label}</span><b>{used.toLocaleString('en-IN')}{limit !== null ? ` / ${limit.toLocaleString('en-IN')}` : ' · unlimited'}</b></div>
      {limit !== null && <div style={{ height: 6, background: 'var(--line, #e5e7eb)', borderRadius: 4, marginTop: 4 }}>
        <div style={{ width: `${pct}%`, height: 6, borderRadius: 4, background: pct >= 90 ? 'var(--bad, #b91c1c)' : 'var(--accent, #2a63d4)' }} /></div>}
    </div>
  )
}

function Checkout({ ov, plan, months, currency, onClose, onPaid }: { ov: BillingOverview; plan: Plan; months: string; currency: string; onClose: () => void; onPaid: (msg: string) => void }) {
  const [coupon, setCoupon] = useState(''), [applied, setApplied] = useState('')
  const [q, setQ] = useState<Quote | null>(null), [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const [name, setName] = useState(ov.billing_name), [gstin, setGstin] = useState(ov.billing_tax_id)
  const gateway = currency === 'USD' ? 'paypal' : 'razorpay'
  useEffect(() => {
    api.post<Quote>('/billing/quote', { plan_id: plan.id, months: +months, currency, coupon: applied }).then((x) => { setQ(x); if (x.coupon_problem) setErr(x.coupon_problem) }).catch((e) => setErr(e.message))
  }, [plan.id, months, currency, applied])
  const pay = async (e: FormEvent) => {
    e.preventDefault(); setErr(''); setBusy(true)
    try {
      const co = await api.post<Record<string, unknown>>('/billing/checkout', { plan_id: plan.id, months: +months, currency, coupon: q?.coupon ?? '', gateway, billing_name: name, billing_tax_id: gstin })
      if (co.gateway === 'paypal') { window.location.assign(co.approve_url as string); return }
      await loadRazorpay()
      const rz = new window.Razorpay!({
        key: co.key_id, order_id: co.order_id, amount: co.amount, currency: co.currency, name: co.name, description: co.description,
        prefill: co.prefill, theme: { color: getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() || '#14305c' },
        modal: { ondismiss: () => { setBusy(false); api.post(`/billing/payments/${co.payment_id}/cancel`).catch(() => {}) } },
        handler: async (r: { razorpay_order_id: string; razorpay_payment_id: string; razorpay_signature: string }) => {
          try {
            const res = await api.post<{ licence: Licence }>('/billing/razorpay/verify', { payment_id: co.payment_id, ...r })
            onPaid(`Payment received. ${res.licence.plan?.name} is active until ${fmtDate(res.licence.ends_at)}.`)
          } catch (x) { setErr((x as Error).message); setBusy(false) }
        },
      })
      rz.on('payment.failed', (r) => setErr(r.error?.description || 'The payment failed'))
      rz.open()
    } catch (x) { setErr((x as Error).message); setBusy(false) }
  }
  return (
    <Modal title={`${plan.name} · ${ov.periods[months]}`} onClose={onClose}>
      <form onSubmit={pay}>
        <Alert>{err}</Alert>
        {q && <table className="t" style={{ marginBottom: 10 }}><tbody>
          <tr><td>{plan.name} — {ov.periods[months].toLowerCase()} ({months} month{months === '1' ? '' : 's'})</td><td style={{ textAlign: 'right' }}>{q.display.subtotal}</td></tr>
          {q.discount > 0 && <tr><td>Coupon {q.coupon}</td><td style={{ textAlign: 'right', color: 'var(--ok)' }}>−{q.display.discount}</td></tr>}
          {q.tax > 0 && <tr><td>{q.tax_label}</td><td style={{ textAlign: 'right' }}>{q.display.tax}</td></tr>}
          <tr><td><b>Total</b></td><td style={{ textAlign: 'right' }}><b>{q.display.total}</b></td></tr>
        </tbody></table>}
        {q?.renews_same_plan && <p className="small muted">This adds {months} month(s) after your current period ends ({fmtDate(ov.licence.ends_at)}).</p>}
        {q && q.carry_over_days > 0 && <p className="small muted">Includes {q.carry_over_days} extra day(s) converted from the time left on your current plan.</p>}
        <div className="row" style={{ gap: 6, alignItems: 'flex-end' }}>
          <div style={{ flex: 1 }}><label className="f">Coupon code (optional)</label><input className="i mono" value={coupon} onChange={(e) => setCoupon(e.target.value.toUpperCase())} placeholder="LAUNCH20" /></div>
          <button type="button" className="btn" onClick={() => { setErr(''); setApplied(coupon.trim()) }} disabled={!coupon.trim()}>Apply</button>
        </div>
        <div className="f2" style={{ marginTop: 6 }}>
          <div><label className="f">Billed to (company name)</label><input className="i" value={name} onChange={(e) => setName(e.target.value)} maxLength={200} /></div>
          <div><label className="f">{currency === 'INR' ? 'GSTIN (optional)' : 'Tax / VAT ID (optional)'}</label><input className="i mono" value={gstin} onChange={(e) => setGstin(e.target.value.toUpperCase())} maxLength={40} /></div>
        </div>
        <div className="row" style={{ justifyContent: 'space-between', marginTop: 14, alignItems: 'center' }}>
          <span className="small muted">{gateway === 'razorpay' ? 'Pay with UPI, cards, net banking or wallets via Razorpay.' : 'You will be taken to PayPal to pay, then brought back here.'}</span>
          <div className="row"><button type="button" className="btn" onClick={onClose}>Cancel</button>
            <button className="btn primary" disabled={busy || !q || q.total <= 0}>{busy ? 'Opening…' : `Pay ${q ? money(q.total, currency) : ''}`}</button></div>
        </div>
      </form>
    </Modal>
  )
}

export default function BillingPage({ onLicence }: { onLicence?: () => void }) {
  const { me } = useApp()
  const loc = useLocation(), nav = useNavigate()
  const [ov, setOv] = useState<BillingOverview | null>(null), [err, setErr] = useState(''), [ok, setOk] = useState('')
  const [choose, setChoose] = useState<{ plan: Plan; months: string; currency: string } | null>(null)
  const [code, setCode] = useState(''), [busy, setBusy] = useState(false)
  const handled = useRef(false)
  const load = () => api.get<BillingOverview>('/billing').then((x) => { setOv(x); onLicence?.() }).catch((e) => setErr(e.message))
  useEffect(() => { load() }, []) // eslint-disable-line react-hooks/exhaustive-deps
  // Plan chosen on the public pricing page before signing up: open its checkout once.
  useEffect(() => {
    const code = new URLSearchParams(loc.search).get('plan')
    if (!ov || !code || handled.current) return
    handled.current = true
    const p = ov.plans.find((x) => x.code === code && !x.is_trial)
    if (p && ov.can_manage && (ov.gateways.razorpay.enabled || ov.gateways.paypal.enabled)) setChoose({ plan: p, months: '1', currency: ov.currencies[0] })
  }, [ov]) // eslint-disable-line react-hooks/exhaustive-deps
  // Back from PayPal: ?paypal=return&token=<order id>
  useEffect(() => {
    const sp = new URLSearchParams(loc.search)
    if (handled.current || !sp.get('paypal')) return
    handled.current = true
    const token = sp.get('token')
    if (sp.get('paypal') === 'return' && token) {
      setBusy(true)
      api.post<{ licence: Licence }>('/billing/paypal/capture', { order_id: token })
        .then((r) => { setOk(`Payment received. ${r.licence.plan?.name} is active until ${fmtDate(r.licence.ends_at)}.`); load() })
        .catch((e) => setErr(e.message)).finally(() => setBusy(false))
    } else setErr('The PayPal payment was cancelled.')
    nav('/billing', { replace: true })
  }, [loc.search]) // eslint-disable-line react-hooks/exhaustive-deps
  const act = async (fn: () => Promise<string>) => { setErr(''); setOk(''); setBusy(true); try { setOk(await fn()); await load() } catch (x) { setErr((x as Error).message) } finally { setBusy(false) } }
  if (!ov) return <Alert>{err}</Alert>
  const l = ov.licence
  const welcome = new URLSearchParams(loc.search).get('welcome')
  return (
    <>
      <div className="topbar"><div><h1>Plans &amp; billing</h1><div className="muted small">{ov.tenant_name}</div></div></div>
      {welcome && l.state === 'none' && <Alert kind="ok">Welcome to TestAgent! Your organization is ready. Start the {ov.trial.days}-day free trial or choose a plan to begin.</Alert>}
      <Alert>{err}</Alert><Alert kind="ok">{ok}</Alert>
      <div className="panel">
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: 12 }}>
          <div>
            <h2 style={{ marginBottom: 4 }}>{l.plan ? l.plan.name : l.state === 'unlimited' ? 'Unlimited licence' : 'No plan selected'} <LicenceBadge l={l} /></h2>
            <div className="small muted">
              {l.state === 'unlimited' && 'Your organization has an unlimited licence; no plan is needed.'}
              {l.state === 'none' && 'TestAgent is read-only until you start the free trial or choose a plan.'}
              {(l.state === 'trial' || l.state === 'active') && <>Valid until <b>{fmtDate(l.ends_at)}</b> · {l.days_left} day(s) left</>}
              {l.state === 'expired' && <>Expired on {fmtDate(l.ends_at)}. Your data is safe and visible, but tests cannot run until you renew.</>}
            </div>
          </div>
          {ov.trial.available && ov.can_manage && (l.state === 'none' || l.state === 'expired') &&
            <button className="btn primary" disabled={busy} onClick={() => act(async () => { const r = await api.post<Licence>('/billing/trial'); return `Your free trial is active until ${fmtDate(r.ends_at)}.` })}>Start {ov.trial.days}-day free trial</button>}
        </div>
        {l.state !== 'unlimited' && l.state !== 'none' && <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))', gap: 14, marginTop: 14 }}>
          {Object.entries(ov.limits).map(([k, label]) => <Meter key={k} label={label} used={ov.usage[k] ?? 0} limit={l.limits[k] ?? null} />)}
        </div>}
      </div>
      {!ov.can_manage && l.state !== 'unlimited' && <Alert kind="warn">Only your organization's owner or administrators can choose a plan, pay or apply coupons.</Alert>}
      {l.state !== 'unlimited' && <>
        <h2 style={{ textAlign: 'center', margin: '26px 0 4px' }}>{l.state === 'active' ? 'Renew or change your plan' : 'Choose your plan'}</h2>
        <p className="muted small" style={{ textAlign: 'center', marginTop: 0 }}>Prepaid licence for the period you choose. Renew any time; time is added to your current period.</p>
        <PlanGrid cat={ov} busy={busy || !ov.can_manage} currentPlanId={l.plan?.id} hideTrial={!ov.trial.available}
          ctaFor={(p) => p.is_trial ? `Start ${p.trial_days}-day free trial` : l.plan?.id === p.id && l.state === 'active' ? `Renew ${p.name}` : undefined}
          onChoose={(p, months, currency) => {
            if (p.is_trial) { act(async () => { const r = await api.post<Licence>('/billing/trial'); return `Your free trial is active until ${fmtDate(r.ends_at)}.` }); return }
            if (!ov.gateways.razorpay.enabled && !ov.gateways.paypal.enabled) { setErr('Online payment is not available yet. Contact Unical Systems for a licence.'); return }
            setChoose({ plan: p, months, currency })
          }} />
        {ov.can_manage && <div className="panel" style={{ marginTop: 22 }}>
          <h2>Have a licence coupon?</h2>
          <p className="small muted" style={{ marginTop: -6 }}>Coupons from Unical Systems extend your licence without payment. Discount coupons are entered at checkout.</p>
          <form className="row" style={{ gap: 6 }} onSubmit={(e) => { e.preventDefault(); act(async () => (await api.post<{ message: string }>('/billing/redeem', { code })).message) }}>
            <input className="i mono" style={{ maxWidth: 260 }} value={code} onChange={(e) => setCode(e.target.value.toUpperCase())} placeholder="COUPON-CODE" aria-label="Coupon code" />
            <button className="btn" disabled={busy || code.trim().length < 3}>Redeem coupon</button>
          </form>
        </div>}
      </>}
      {ov.payments.length > 0 && <div className="panel" style={{ marginTop: 22 }}>
        <h2>Payments &amp; invoices</h2>
        <table className="t resp"><thead><tr><th>Date</th><th>Plan</th><th>Amount</th><th>Status</th><th>Invoice</th></tr></thead><tbody>
          {ov.payments.map((p) => <tr key={p.id}>
            <td className="small">{fmtDate(p.paid_at || p.created_at)}</td>
            <td>{p.plan_name} <span className="muted small">· {p.period}{p.coupon_code ? ` · ${p.coupon_code}` : ''}</span>
              {p.period_end && <div className="small muted">{fmtDate(p.period_start)} – {fmtDate(p.period_end)}</div>}</td>
            <td>{p.amount_display} <span className="muted small">via {p.gateway}</span></td>
            <td><Badge s={p.status === 'paid' ? 'passed' : p.status === 'failed' ? 'failed' : 'queued'} label={p.status} />{p.error && <div className="small" style={{ color: 'var(--bad)' }}>{p.error}</div>}</td>
            <td>{p.invoice_no ? <a href={`${apiBase()}/billing/payments/${p.id}/invoice`} target="_blank" rel="noopener">{p.invoice_no}</a> : '—'}</td>
          </tr>)}</tbody></table>
      </div>}
      {choose && <Checkout ov={ov} plan={choose.plan} months={choose.months} currency={choose.currency} onClose={() => setChoose(null)}
        onPaid={(m) => { setChoose(null); setOk(m); load() }} />}
      {me.is_platform_admin && <p className="small muted" style={{ marginTop: 16 }}>Plans, prices, coupons and payment gateways are managed under Platform (Unical) → Plans &amp; billing.</p>}
    </>
  )
}
