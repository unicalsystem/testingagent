import { useState, type FormEvent, type ReactNode } from 'react'
import { abs, money, type Payment, type Plan } from '../api'
import { Alert, Badge, Modal, fmtDate, useAsync } from './ui'

interface AdminPlans { plans: Plan[]; features: Record<string, string>; limits: Record<string, string>; periods: Record<string, string>; currencies: string[] }
interface Coupon { id: number; code: string; description: string; kind: 'extend' | 'percent' | 'amount'; extend_days: number; extend_plan_id: number | null
  percent_off: number; amount_off: number; currency: string; plan_ids: number[]; tenant_slugs: string[]; max_redemptions: number | null
  redemptions: number; valid_until: string | null; is_active: boolean; created_at: string }
interface LicenceRow { tenant_id: number; slug: string; name: string; tenant_status: string; status: string; state: string; plan_id: number | null
  plan_name: string; current_period_end: string | null; trial_used: boolean; note: string; paid: { INR: string; USD: string } }
type Settings = Record<string, unknown> & { webhook_url: string; gateways: { razorpay: { enabled: boolean }; paypal: { enabled: boolean } } }

const STATE_BADGE: Record<string, string> = { unlimited: 'passed', active: 'passed', trial: 'running', none: 'queued', expired: 'failed' }

function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return <div><label className="f">{label}</label>{children}{hint && <div className="hint">{hint}</div>}</div>
}

// --------------------------------------------------------------------------- plans
function PlanForm({ meta, initial, onClose, onSaved }: { meta: AdminPlans; initial?: Plan; onClose: () => void; onSaved: () => void }) {
  const [f, setF] = useState({ code: initial?.code ?? '', name: initial?.name ?? '', tagline: initial?.tagline ?? '', badge: initial?.badge ?? '',
    is_trial: initial?.is_trial ?? false, trial_days: initial?.trial_days ?? 7, is_public: initial?.is_public ?? true, is_active: initial?.is_active ?? true,
    sort_order: initial?.sort_order ?? 10 })
  const [prices, setPrices] = useState<Record<string, Record<string, string>>>(() => {
    const out: Record<string, Record<string, string>> = {}
    for (const c of meta.currencies) { out[c] = {}; for (const m of Object.keys(meta.periods)) { const v = initial?.prices?.[c]?.[m]; out[c][m] = v ? String(v / 100) : '' } }
    return out
  })
  const [features, setFeatures] = useState<Record<string, boolean>>(initial?.features ?? {})
  const [limits, setLimits] = useState<Record<string, string>>(() => Object.fromEntries(Object.keys(meta.limits).map((k) => [k, initial?.limits?.[k] == null ? '' : String(initial.limits[k])])))
  const [highlights, setHighlights] = useState((initial?.highlights ?? []).join('\n'))
  const [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const set = (k: string, v: unknown) => setF({ ...f, [k]: v })
  const autofill = (c: string) => {
    const base = parseFloat(prices[c]['1'])
    if (!base) return
    const disc: Record<string, number> = { '3': 5, '6': 10, '12': 20 }
    setPrices({ ...prices, [c]: { ...prices[c], ...Object.fromEntries(Object.entries(disc).map(([m, d]) => [m, String(Math.round(base * +m * (100 - d) / 100))])) } })
  }
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr(''); setBusy(true)
    const p: Record<string, Record<string, number>> = {}
    for (const [c, per] of Object.entries(prices)) for (const [m, v] of Object.entries(per)) if (v.trim()) { p[c] = p[c] ?? {}; p[c][m] = Math.round(parseFloat(v) * 100) }
    const body = { ...f, trial_days: +f.trial_days, sort_order: +f.sort_order, prices: f.is_trial ? {} : p, features,
      limits: Object.fromEntries(Object.entries(limits).map(([k, v]) => [k, v.trim() === '' ? null : +v])), highlights: highlights.split('\n') }
    try {
      if (initial) await abs.put(`/api/platform/billing/plans/${initial.id}`, body); else await abs.post('/api/platform/billing/plans', body)
      onSaved()
    } catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  return (
    <Modal title={initial ? `Edit plan · ${initial.name}` : 'New plan'} onClose={onClose}>
      <form onSubmit={submit}>
        <Alert>{err}</Alert>
        <div className="f2">
          <Field label="Name"><input className="i" value={f.name} onChange={(e) => set('name', e.target.value)} required maxLength={80} /></Field>
          <Field label="Code" hint="Lower-case id, e.g. professional"><input className="i mono" value={f.code} onChange={(e) => set('code', e.target.value.toLowerCase())} required /></Field>
          <Field label="Tagline"><input className="i" value={f.tagline} onChange={(e) => set('tagline', e.target.value)} maxLength={200} /></Field>
          <Field label="Badge (optional)" hint="e.g. Recommended"><input className="i" value={f.badge} onChange={(e) => set('badge', e.target.value)} maxLength={40} /></Field>
          <Field label="Display order"><input className="i" type="number" value={f.sort_order} onChange={(e) => set('sort_order', e.target.value)} /></Field>
          <div>
            <label className="check"><input type="checkbox" checked={f.is_public} onChange={(e) => set('is_public', e.target.checked)} /><span>Shown on pricing pages</span></label>
            <label className="check"><input type="checkbox" checked={f.is_active} onChange={(e) => set('is_active', e.target.checked)} /><span>Available to buy / start</span></label>
            <label className="check"><input type="checkbox" checked={f.is_trial} onChange={(e) => set('is_trial', e.target.checked)} /><span>Free trial plan (no payment)</span></label>
          </div>
        </div>
        {f.is_trial ? <Field label="Trial length (days)"><input className="i" type="number" min={1} max={365} value={f.trial_days} onChange={(e) => set('trial_days', e.target.value)} style={{ maxWidth: 120 }} /></Field>
          : <>
            <h3 style={{ margin: '14px 0 4px' }}>Prices <span className="small muted">(total per period, in ₹ / $; leave empty to not offer)</span></h3>
            <table className="t"><thead><tr><th>Currency</th>{Object.entries(meta.periods).map(([m, l]) => <th key={m}>{l}</th>)}<th /></tr></thead><tbody>
              {meta.currencies.map((c) => <tr key={c}><td><b>{c}</b></td>
                {Object.keys(meta.periods).map((m) => <td key={m}><input className="i" inputMode="decimal" style={{ minWidth: 80 }} value={prices[c][m]}
                  onChange={(e) => setPrices({ ...prices, [c]: { ...prices[c], [m]: e.target.value.replace(/[^0-9.]/g, '') } })} placeholder="—" /></td>)}
                <td><button type="button" className="btn sm" title="Fill 3/6/12 months from the monthly price with 5/10/20% discounts" onClick={() => autofill(c)}>Auto</button></td></tr>)}
            </tbody></table>
          </>}
        <h3 style={{ margin: '14px 0 4px' }}>Features</h3>
        <div className="f2">{Object.entries(meta.features).map(([k, l]) => <label key={k} className="check"><input type="checkbox" checked={!!features[k]} onChange={(e) => setFeatures({ ...features, [k]: e.target.checked })} /><span>{l}</span></label>)}</div>
        <h3 style={{ margin: '14px 0 4px' }}>Limits <span className="small muted">(empty = unlimited, 0 = none)</span></h3>
        <div className="f2">{Object.entries(meta.limits).map(([k, l]) => <Field key={k} label={l}><input className="i" inputMode="numeric" value={limits[k]} onChange={(e) => setLimits({ ...limits, [k]: e.target.value.replace(/[^0-9]/g, '') })} placeholder="unlimited" /></Field>)}</div>
        <Field label="Extra lines on the plan card (one per line)" hint="e.g. Priority e-mail support"><textarea className="i" rows={3} value={highlights} onChange={(e) => setHighlights(e.target.value)} /></Field>
        <div className="row" style={{ justifyContent: 'flex-end', marginTop: 12 }}><button type="button" className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={busy}>Save plan</button></div>
      </form>
    </Modal>
  )
}

function Plans() {
  const d = useAsync(() => abs.get<AdminPlans>('/api/platform/billing/plans'), [])
  const [edit, setEdit] = useState<Plan | 'new' | null>(null), [msg, setMsg] = useState(''), [err, setErr] = useState('')
  if (!d.data) return <Alert>{d.error}</Alert>
  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: 'space-between' }}><h2>Plans</h2><button className="btn primary sm" onClick={() => setEdit('new')}>+ New plan</button></div>
      <Alert>{err}</Alert><Alert kind="ok">{msg}</Alert>
      <table className="t resp"><thead><tr><th>Plan</th><th>Monthly price</th><th>Features</th><th>Organizations</th><th /></tr></thead><tbody>
        {d.data.plans.map((p) => <tr key={p.id}>
          <td><b>{p.name}</b> {p.badge && <span className="tag">{p.badge}</span>}{!p.is_public && <span className="tag">hidden</span>}{!p.is_active && <span className="tag">retired</span>}
            <div className="small muted mono">{p.code}{p.is_trial ? ` · free trial ${p.trial_days} days` : ''}</div></td>
          <td className="small">{p.is_trial ? 'Free' : Object.entries(p.prices).map(([c, per]) => per['1'] ? money(per['1'], c) : `${c}: —`).join(' · ')}</td>
          <td className="small">{Object.entries(d.data!.features).filter(([k]) => p.features[k]).map(([, l]) => l.split(' (')[0]).join(', ') || '—'}</td>
          <td>{p.organizations ?? 0}</td>
          <td style={{ textAlign: 'right' }} className="row">
            <button className="btn sm" onClick={() => setEdit(p)}>Edit</button>
            <button className="btn sm danger" onClick={async () => { if (!confirm(`Delete or retire plan ${p.name}?`)) return; try { setMsg((await abs.del<{ message: string }>(`/api/platform/billing/plans/${p.id}`)).message); d.reload() } catch (x) { setErr((x as Error).message) } }}>Delete</button>
          </td></tr>)}</tbody></table>
      <p className="small muted">Changes apply immediately to organizations on the plan (features and limits) and to new purchases (prices). Prices exclude tax; tax is added at checkout according to the settings.</p>
      {edit && <PlanForm meta={d.data} initial={edit === 'new' ? undefined : edit} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); d.reload() }} />}
    </div>
  )
}

// --------------------------------------------------------------------------- coupons
function CouponForm({ plans, initial, onClose, onSaved }: { plans: Plan[]; initial?: Coupon; onClose: () => void; onSaved: () => void }) {
  const [f, setF] = useState({ code: initial?.code ?? '', description: initial?.description ?? '', kind: initial?.kind ?? 'extend',
    extend_days: initial?.extend_days ?? 30, extend_plan_id: initial?.extend_plan_id ?? null as number | null, percent_off: initial?.percent_off ?? 10,
    amount: initial ? String(initial.amount_off / 100) : '', currency: initial?.currency ?? 'INR', plan_ids: initial?.plan_ids ?? [] as number[],
    tenant_slugs: (initial?.tenant_slugs ?? []).join(', '), max_redemptions: initial?.max_redemptions == null ? '' : String(initial.max_redemptions),
    valid_until: initial?.valid_until ? initial.valid_until.slice(0, 10) : '', is_active: initial?.is_active ?? true })
  const [err, setErr] = useState('')
  const set = (k: string, v: unknown) => setF({ ...f, [k]: v })
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr('')
    const body = { code: f.code, description: f.description, kind: f.kind, extend_days: +f.extend_days, extend_plan_id: f.extend_plan_id,
      percent_off: +f.percent_off, amount_off: Math.round(parseFloat(f.amount || '0') * 100), currency: f.kind === 'amount' ? f.currency : '',
      plan_ids: f.plan_ids, tenant_slugs: f.tenant_slugs.split(',').map((s) => s.trim()).filter(Boolean),
      max_redemptions: f.max_redemptions ? +f.max_redemptions : null, valid_until: f.valid_until ? `${f.valid_until}T23:59:59+05:30` : null, is_active: f.is_active }
    try { if (initial) await abs.put(`/api/platform/billing/coupons/${initial.id}`, body); else await abs.post('/api/platform/billing/coupons', body); onSaved() }
    catch (x) { setErr((x as Error).message) }
  }
  return (
    <Modal title={initial ? `Edit coupon ${initial.code}` : 'New coupon'} onClose={onClose}>
      <form onSubmit={submit}>
        <Alert>{err}</Alert>
        <div className="chips" style={{ marginBottom: 8 }}>
          {([['extend', 'Extend licence (free days)'], ['percent', '% discount'], ['amount', 'Fixed discount']] as const).map(([k, l]) =>
            <button type="button" key={k} className={`chip${f.kind === k ? ' on' : ''}`} onClick={() => set('kind', k)}>{l}</button>)}
        </div>
        <div className="f2">
          <Field label="Code" hint="Letters, digits, - and _; customers type it"><input className="i mono" value={f.code} onChange={(e) => set('code', e.target.value.toUpperCase())} required /></Field>
          <Field label="Description (internal)"><input className="i" value={f.description} onChange={(e) => set('description', e.target.value)} /></Field>
          {f.kind === 'extend' && <>
            <Field label="Days added"><input className="i" type="number" min={1} value={f.extend_days} onChange={(e) => set('extend_days', e.target.value)} /></Field>
            <Field label="Plan granted" hint="Empty = keep the organization's current plan (or the trial plan)">
              <select className="i" value={f.extend_plan_id ?? ''} onChange={(e) => set('extend_plan_id', e.target.value ? +e.target.value : null)}>
                <option value="">— current plan —</option>{plans.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></Field>
          </>}
          {f.kind === 'percent' && <Field label="Discount %"><input className="i" type="number" min={1} max={100} value={f.percent_off} onChange={(e) => set('percent_off', e.target.value)} /></Field>}
          {f.kind === 'amount' && <>
            <Field label="Discount amount"><input className="i" inputMode="decimal" value={f.amount} onChange={(e) => set('amount', e.target.value.replace(/[^0-9.]/g, ''))} /></Field>
            <Field label="Currency"><select className="i" value={f.currency} onChange={(e) => set('currency', e.target.value)}><option>INR</option><option>USD</option></select></Field>
          </>}
          <Field label="Valid until (optional)"><input className="i" type="date" value={f.valid_until} onChange={(e) => set('valid_until', e.target.value)} /></Field>
          <Field label="Maximum uses (optional)" hint="Each organization can use a coupon once"><input className="i" inputMode="numeric" value={f.max_redemptions} onChange={(e) => set('max_redemptions', e.target.value.replace(/[^0-9]/g, ''))} placeholder="unlimited" /></Field>
          <Field label="Only for organizations (optional)" hint="Comma-separated addresses, e.g. acme, globex"><input className="i mono" value={f.tenant_slugs} onChange={(e) => set('tenant_slugs', e.target.value)} /></Field>
        </div>
        {f.kind !== 'extend' && <Field label="Applies to plans (none ticked = all)"><div className="chips">{plans.filter((p) => !p.is_trial).map((p) =>
          <button type="button" key={p.id} className={`chip${f.plan_ids.includes(p.id) ? ' on' : ''}`} onClick={() => set('plan_ids', f.plan_ids.includes(p.id) ? f.plan_ids.filter((x) => x !== p.id) : [...f.plan_ids, p.id])}>{p.name}</button>)}</div></Field>}
        <label className="check"><input type="checkbox" checked={f.is_active} onChange={(e) => set('is_active', e.target.checked)} /><span>Active</span></label>
        <div className="row" style={{ justifyContent: 'flex-end', marginTop: 12 }}><button type="button" className="btn" onClick={onClose}>Cancel</button><button className="btn primary">Save coupon</button></div>
      </form>
    </Modal>
  )
}

function Coupons() {
  const d = useAsync(() => abs.get<Coupon[]>('/api/platform/billing/coupons'), [])
  const plans = useAsync(() => abs.get<AdminPlans>('/api/platform/billing/plans'), [])
  const [edit, setEdit] = useState<Coupon | 'new' | null>(null), [uses, setUses] = useState<{ c: Coupon; rows: { tenant: string; slug: string; at: string }[] } | null>(null)
  const pname = (id: number | null) => plans.data?.plans.find((p) => p.id === id)?.name ?? 'current plan'
  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: 'space-between' }}><h2>Coupons</h2><button className="btn primary sm" onClick={() => setEdit('new')}>+ New coupon</button></div>
      <p className="small muted" style={{ marginTop: -6 }}>Share licence-extension coupons with customers (pilots, delayed payments, goodwill); they redeem them on their Plans &amp; billing page. Discount coupons are entered at checkout.</p>
      <Alert>{d.error}</Alert>
      <table className="t resp"><thead><tr><th>Code</th><th>Gives</th><th>Used</th><th>Valid until</th><th /></tr></thead><tbody>
        {d.data?.map((c) => <tr key={c.id}>
          <td><b className="mono">{c.code}</b>{!c.is_active && <span className="tag">inactive</span>}<div className="small muted">{c.description}</div></td>
          <td className="small">{c.kind === 'extend' ? `+${c.extend_days} days on ${pname(c.extend_plan_id)}` : c.kind === 'percent' ? `${c.percent_off}% off` : `${money(c.amount_off, c.currency)} off`}
            {c.tenant_slugs.length > 0 && <div className="muted">only {c.tenant_slugs.join(', ')}</div>}</td>
          <td><a href="#" onClick={async (e) => { e.preventDefault(); setUses({ c, rows: await abs.get(`/api/platform/billing/coupons/${c.id}/redemptions`) }) }}>{c.redemptions}{c.max_redemptions ? ` / ${c.max_redemptions}` : ''}</a></td>
          <td className="small">{c.valid_until ? fmtDate(c.valid_until) : '—'}</td>
          <td style={{ textAlign: 'right' }} className="row"><button className="btn sm" onClick={() => setEdit(c)}>Edit</button>
            <button className="btn sm danger" onClick={async () => { if (confirm(`Delete coupon ${c.code}? Used coupons are deactivated instead.`)) { await abs.del(`/api/platform/billing/coupons/${c.id}`); d.reload() } }}>Delete</button></td></tr>)}
      </tbody></table>
      {d.data?.length === 0 && <p className="small muted">No coupons yet.</p>}
      {edit && plans.data && <CouponForm plans={plans.data.plans} initial={edit === 'new' ? undefined : edit} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); d.reload() }} />}
      {uses && <Modal title={`${uses.c.code} — used by`} onClose={() => setUses(null)}>
        {uses.rows.length === 0 ? <p className="small muted">Not used yet.</p> : <table className="t"><tbody>{uses.rows.map((r, i) => <tr key={i}><td>{r.tenant} <span className="muted mono small">/{r.slug}</span></td><td className="small">{fmtDate(r.at)}</td></tr>)}</tbody></table>}
        <div className="row" style={{ justifyContent: 'flex-end' }}><button className="btn" onClick={() => setUses(null)}>Close</button></div></Modal>}
    </div>
  )
}

// --------------------------------------------------------------------------- gateways & settings
function Gateways() {
  const d = useAsync(() => abs.get<Settings>('/api/platform/billing/settings'), [])
  const [f, setF] = useState<Record<string, unknown>>({}), [msg, setMsg] = useState<Record<string, { ok: boolean; message: string }>>({}), [err, setErr] = useState(''), [ok, setOk] = useState('')
  if (!d.data) return <Alert>{d.error}</Alert>
  const s = { ...d.data, ...f } as Record<string, unknown>
  const v = (k: string) => (s[k] ?? '') as string
  const set = (k: string, val: unknown) => setF({ ...f, [k]: val })
  const save = async (e?: FormEvent) => {
    e?.preventDefault(); setErr(''); setOk('')
    const body = { ...f }
    if ('reminder_days' in body && typeof body.reminder_days === 'string') body.reminder_days = (body.reminder_days as string).split(',').map((x) => +x.trim()).filter((x) => !isNaN(x))
    for (const k of ['tax_percent_inr', 'tax_percent_usd']) if (k in body) body[k] = +(body[k] as string)
    try { await abs.put('/api/platform/billing/settings', body); setF({}); setOk('Saved'); d.reload() } catch (x) { setErr((x as Error).message) }
  }
  const test = async (gw: string) => { await save(); try { setMsg({ ...msg, [gw]: await abs.post(`/api/platform/billing/gateways/${gw}/test`) }) } catch (x) { setErr((x as Error).message) } }
  const chk = (k: string, label: ReactNode) => <label className="check"><input type="checkbox" checked={!!s[k]} onChange={(e) => set(k, e.target.checked)} /><span>{label}</span></label>
  const secret = (k: string, label: string) => <Field label={label} hint={s[`${k}_set`] ? `Stored (${v(`${k}_hint`)}). Leave empty to keep.` : 'Stored encrypted; never shown again.'}>
    <input className="i mono" type="password" autoComplete="new-password" value={(f[k] as string) ?? ''} onChange={(e) => set(k, e.target.value)} /></Field>
  return (
    <form onSubmit={save}>
      <Alert>{err}</Alert><Alert kind="ok">{ok}</Alert>
      <div className="panel">
        <h2>Licensing</h2>
        {chk('enforce', <><b>Enforce plans and limits</b> <span className="small muted">— organizations without a valid plan become read-only. Organizations that existed before plans were introduced keep an unlimited licence; the Unical organization is always unlimited.</span></>)}
        {chk('self_signup', <><b>Self-service sign-up</b> <span className="small muted">— registration creates the organization at once and takes the owner to the Plans page. Off: registrations wait for your approval as before.</span></>)}
        <Field label="Reminder e-mails before expiry (days, comma separated)" hint="Sent to owners and administrators; an e-mail is also sent when a licence expires. Needs SMTP.">
          <input className="i" style={{ maxWidth: 200 }} value={Array.isArray(s.reminder_days) ? (s.reminder_days as number[]).join(', ') : v('reminder_days')} onChange={(e) => set('reminder_days', e.target.value)} /></Field>
      </div>
      <div className="panel">
        <div className="row" style={{ justifyContent: 'space-between' }}><h2>Razorpay <span className="small muted">(INR — UPI, cards, net banking, wallets)</span></h2>
          <span className={`pill ${d.data.gateways.razorpay.enabled ? 'ok' : ''}`}>{d.data.gateways.razorpay.enabled ? 'enabled' : 'off'}</span></div>
        {chk('razorpay_enabled', 'Accept payments with Razorpay')}
        <div className="f2">
          <Field label="Key ID" hint="Razorpay Dashboard → Account & Settings → API keys (rzp_test_… for testing, rzp_live_… for real payments)"><input className="i mono" value={v('razorpay_key_id')} onChange={(e) => set('razorpay_key_id', e.target.value)} placeholder="rzp_live_…" /></Field>
          {secret('razorpay_key_secret', 'Key secret')}
          {secret('razorpay_webhook_secret', 'Webhook secret')}
          <Field label="Webhook URL" hint="Razorpay → Webhooks → Add: this URL, the secret above, events payment.captured and order.paid"><input className="i mono" readOnly value={d.data.webhook_url} onFocus={(e) => e.target.select()} /></Field>
        </div>
        <div className="row" style={{ marginTop: 8 }}><button type="button" className="btn sm" onClick={() => test('razorpay')}>Save &amp; test Razorpay</button>
          {msg.razorpay && <span className="small" style={{ color: msg.razorpay.ok ? 'var(--ok)' : 'var(--bad)' }}>{msg.razorpay.message}</span>}</div>
      </div>
      <div className="panel">
        <div className="row" style={{ justifyContent: 'space-between' }}><h2>PayPal <span className="small muted">(USD — international customers)</span></h2>
          <span className={`pill ${d.data.gateways.paypal.enabled ? 'ok' : ''}`}>{d.data.gateways.paypal.enabled ? 'enabled' : 'off'}</span></div>
        {chk('paypal_enabled', 'Accept payments with PayPal')}
        <div className="f2">
          <Field label="Environment"><select className="i" value={v('paypal_mode')} onChange={(e) => set('paypal_mode', e.target.value)}><option value="sandbox">Sandbox (testing)</option><option value="live">Live</option></select></Field>
          <Field label="Client ID" hint="developer.paypal.com → Apps & Credentials → your REST app"><input className="i mono" value={v('paypal_client_id')} onChange={(e) => set('paypal_client_id', e.target.value)} /></Field>
          {secret('paypal_client_secret', 'Client secret')}
        </div>
        <div className="row" style={{ marginTop: 8 }}><button type="button" className="btn sm" onClick={() => test('paypal')}>Save &amp; test PayPal</button>
          {msg.paypal && <span className="small" style={{ color: msg.paypal.ok ? 'var(--ok)' : 'var(--bad)' }}>{msg.paypal.message}</span>}</div>
      </div>
      <div className="panel">
        <h2>Invoices &amp; tax</h2>
        <div className="f2">
          <Field label="Seller (legal name)"><input className="i" value={v('seller_name')} onChange={(e) => set('seller_name', e.target.value)} /></Field>
          <Field label="Seller GSTIN"><input className="i mono" value={v('seller_tax_id')} onChange={(e) => set('seller_tax_id', e.target.value.toUpperCase())} /></Field>
          <Field label="Seller address"><input className="i" value={v('seller_address')} onChange={(e) => set('seller_address', e.target.value)} /></Field>
          <Field label="Billing e-mail"><input className="i" value={v('seller_email')} onChange={(e) => set('seller_email', e.target.value)} /></Field>
          <Field label="Tax label"><input className="i" value={v('tax_label')} onChange={(e) => set('tax_label', e.target.value)} /></Field>
          <Field label="Invoice number prefix"><input className="i mono" value={v('invoice_prefix')} onChange={(e) => set('invoice_prefix', e.target.value.toUpperCase())} /></Field>
          <Field label="Tax % on INR prices" hint="Added on top at checkout (GST 18%)"><input className="i" inputMode="decimal" value={String(s.tax_percent_inr ?? '')} onChange={(e) => set('tax_percent_inr', e.target.value)} /></Field>
          <Field label="Tax % on USD prices" hint="Usually 0 for exports of services (LUT)"><input className="i" inputMode="decimal" value={String(s.tax_percent_usd ?? '')} onChange={(e) => set('tax_percent_usd', e.target.value)} /></Field>
        </div>
      </div>
      <div className="row" style={{ justifyContent: 'flex-end' }}><button className="btn primary">Save settings</button></div>
    </form>
  )
}

// --------------------------------------------------------------------------- licences & payments
function LicenceForm({ row, plans, onClose, onSaved }: { row: LicenceRow; plans: Plan[]; onClose: () => void; onSaved: () => void }) {
  const [status, setStatus] = useState(row.status === 'legacy' ? 'legacy' : row.status === 'none' ? 'active' : row.status)
  const [plan, setPlan] = useState<number | ''>(row.plan_id ?? '')
  const [end, setEnd] = useState(row.current_period_end ? row.current_period_end.slice(0, 10) : new Date(Date.now() + 30 * 864e5).toISOString().slice(0, 10))
  const [note, setNote] = useState(row.note), [err, setErr] = useState('')
  const save = async () => {
    setErr('')
    try { await abs.put(`/api/platform/billing/licences/${row.tenant_id}`, { status, plan_id: plan === '' ? null : plan, current_period_end: status === 'active' || status === 'trialing' ? `${end}T23:59:59+05:30` : null, note }); onSaved() }
    catch (x) { setErr((x as Error).message) }
  }
  return (
    <Modal title={`Licence · ${row.name}`} onClose={onClose}>
      <Alert>{err}</Alert>
      <p className="small muted">Grant or correct a licence by hand, e.g. after an offline payment (bank transfer, purchase order). The organization's owners see the change at once.</p>
      <Field label="Licence"><select className="i" value={status} onChange={(e) => setStatus(e.target.value)}>
        <option value="active">Active plan until a date</option><option value="trialing">Free trial until a date</option>
        <option value="legacy">Unlimited (no plan, no expiry)</option><option value="none">No plan (read-only)</option></select></Field>
      {(status === 'active' || status === 'trialing') && <div className="f2">
        <Field label="Plan"><select className="i" value={plan} onChange={(e) => setPlan(e.target.value ? +e.target.value : '')}><option value="">— choose —</option>{plans.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></Field>
        <Field label="Valid until"><input className="i" type="date" value={end} onChange={(e) => setEnd(e.target.value)} /></Field></div>}
      <Field label="Internal note"><input className="i" value={note} onChange={(e) => setNote(e.target.value)} placeholder="PO 4512 paid by NEFT" /></Field>
      <div className="row" style={{ justifyContent: 'flex-end', marginTop: 12 }}><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" onClick={save}>Save licence</button></div>
    </Modal>
  )
}

function Licences() {
  const d = useAsync(() => abs.get<LicenceRow[]>('/api/platform/billing/licences'), [])
  const plans = useAsync(() => abs.get<AdminPlans>('/api/platform/billing/plans'), [])
  const [edit, setEdit] = useState<LicenceRow | null>(null), [q, setQ] = useState('')
  const rows = (d.data ?? []).filter((r) => !q || `${r.name} ${r.slug} ${r.plan_name}`.toLowerCase().includes(q.toLowerCase()))
  const count = (s: string) => (d.data ?? []).filter((r) => r.state === s).length
  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: 'space-between', flexWrap: 'wrap' }}><h2>Organization licences</h2>
        <input className="i" style={{ maxWidth: 240 }} placeholder="Search…" value={q} onChange={(e) => setQ(e.target.value)} /></div>
      <div className="small" style={{ marginBottom: 8 }}>{['active', 'trial', 'expired', 'none', 'unlimited'].map((s) => <span key={s} style={{ marginRight: 14 }}><Badge s={STATE_BADGE[s]} label={s} /> {count(s)}</span>)}</div>
      <Alert>{d.error}</Alert>
      <table className="t resp"><thead><tr><th>Organization</th><th>Licence</th><th>Valid until</th><th>Paid</th><th /></tr></thead><tbody>
        {rows.map((r) => <tr key={r.tenant_id}>
          <td><b>{r.name}</b> <span className="muted mono small">/{r.slug}</span>{r.tenant_status !== 'active' && <span className="tag">{r.tenant_status}</span>}{r.note && <div className="small muted">{r.note}</div>}</td>
          <td><Badge s={STATE_BADGE[r.state] ?? 'queued'} label={r.state} /> <span className="small">{r.plan_name}</span></td>
          <td className="small">{r.current_period_end ? fmtDate(r.current_period_end) : '—'}</td>
          <td className="small">{[r.paid.INR !== '₹0' && r.paid.INR, r.paid.USD !== '$0.00' && r.paid.USD].filter(Boolean).join(' · ') || '—'}</td>
          <td style={{ textAlign: 'right' }}><button className="btn sm" onClick={() => setEdit(r)}>Change</button></td></tr>)}</tbody></table>
      {edit && plans.data && <LicenceForm row={edit} plans={plans.data.plans} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); d.reload() }} />}
    </div>
  )
}

function Payments() {
  const d = useAsync(() => abs.get<Payment[]>('/api/platform/billing/payments'), [])
  const [err, setErr] = useState('')
  const paid = (d.data ?? []).filter((p) => p.status === 'paid')
  const sum = (c: string) => paid.filter((p) => p.currency === c).reduce((a, p) => a + p.amount, 0)
  return (
    <div className="panel">
      <h2>Payments</h2>
      <div className="small" style={{ marginBottom: 8 }}>Received (latest 300): <b>{money(sum('INR'), 'INR')}</b> · <b>{money(sum('USD'), 'USD')}</b></div>
      <Alert>{d.error || err}</Alert>
      <table className="t resp"><thead><tr><th>Date</th><th>Organization</th><th>Plan</th><th>Amount</th><th>Status</th><th>Invoice</th></tr></thead><tbody>
        {d.data?.map((p) => <tr key={p.id}>
          <td className="small">{fmtDate(p.paid_at || p.created_at)}</td>
          <td>{p.tenant} <span className="muted mono small">/{p.slug}</span>{p.billing_tax_id && <div className="small muted">GSTIN {p.billing_tax_id}</div>}</td>
          <td className="small">{p.plan_name} · {p.period}{p.coupon_code && ` · ${p.coupon_code}`}</td>
          <td>{p.amount_display} <span className="muted small">{p.gateway}</span><div className="muted small mono">{p.gateway_payment_id || p.gateway_order_id}</div></td>
          <td><Badge s={p.status === 'paid' ? 'passed' : p.status === 'failed' ? 'failed' : 'queued'} label={p.status} />{p.error && <div className="small" style={{ color: 'var(--bad)' }}>{p.error}</div>}
            {(p.status === 'created' || p.status === 'failed') && <div><a href="#" className="small" onClick={async (e) => { e.preventDefault(); if (!confirm('Mark this payment as paid? Do this only after confirming it in the Razorpay / PayPal dashboard.')) return; try { await abs.post(`/api/platform/billing/payments/${p.id}/mark-paid`); d.reload() } catch (x) { setErr((x as Error).message) } }}>Mark paid…</a></div>}</td>
          <td>{p.invoice_no ? <a href={`/api/platform/billing/payments/${p.id}/invoice`} target="_blank" rel="noopener">{p.invoice_no}</a> : '—'}</td></tr>)}</tbody></table>
    </div>
  )
}

export default function PlatformBilling() {
  const [tab, setTab] = useState<'plans' | 'coupons' | 'gateways' | 'licences' | 'payments'>('plans')
  return (
    <>
      <div className="chips" style={{ marginBottom: 12 }}>
        {([['plans', 'Plans'], ['coupons', 'Coupons'], ['gateways', 'Payment gateways & settings'], ['licences', 'Licences'], ['payments', 'Payments']] as const).map(([k, l]) =>
          <button key={k} className={`chip${tab === k ? ' on' : ''}`} onClick={() => setTab(k)}>{l}</button>)}
      </div>
      {tab === 'plans' && <Plans />}
      {tab === 'coupons' && <Coupons />}
      {tab === 'gateways' && <Gateways />}
      {tab === 'licences' && <Licences />}
      {tab === 'payments' && <Payments />}
    </>
  )
}
