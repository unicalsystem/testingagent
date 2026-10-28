import { useMemo, useState } from 'react'
import { money, type Catalog, type Plan } from '../api'

const PERIOD_ORDER = ['1', '3', '6', '12']

function limitLine(label: string, v: number | null | undefined): string | null {
  if (v === 0) return null
  const l = /^[A-Z][a-z]/.test(label) ? label.charAt(0).toLowerCase() + label.slice(1) : label
  return v === null || v === undefined ? `Unlimited ${l}` : `${v.toLocaleString('en-IN')} ${v === 1 ? l.replace(/s\b/, '') : l}`
}

export function savingPct(p: Plan, cur: string, months: string): number {
  const one = p.prices[cur]?.['1'], m = p.prices[cur]?.[months]
  if (!one || !m || months === '1') return 0
  return Math.round((1 - m / (one * +months)) * 100)
}

export function PlanCard({ p, cat, cur, months, current, cta, onChoose, busy }: {
  p: Plan; cat: Catalog; cur: string; months: string; current?: boolean; cta?: string; onChoose?: (p: Plan) => void; busy?: boolean
}) {
  const price = p.prices[cur]?.[months]
  const perMonth = price ? Math.round(price / +months) : null
  const save = savingPct(p, cur, months)
  const lines = [
    ...Object.entries(cat.limits).map(([k, label]) => limitLine(label, p.limits[k])).filter(Boolean) as string[],
    ...Object.entries(cat.features).filter(([k]) => p.features[k]).map(([, l]) => l),
    ...p.highlights,
  ]
  const featured = !!p.badge && !p.is_trial
  return (
    <div className="plan-card" style={{
      border: `${featured ? 2 : 1}px solid ${featured ? 'var(--accent, #2a63d4)' : 'var(--line, #e5e7eb)'}`, borderRadius: 14, padding: '22px 20px',
      background: 'var(--panel, #fff)', position: 'relative', display: 'flex', flexDirection: 'column', minWidth: 0, boxShadow: featured ? '0 6px 24px rgba(20,48,92,.10)' : 'none' }}>
      {p.badge && <span style={{ position: 'absolute', top: -11, left: 18, fontSize: 11, fontWeight: 700, letterSpacing: '.04em', textTransform: 'uppercase',
        background: p.is_trial ? 'var(--panel, #fff)' : 'var(--accent, #2a63d4)', color: p.is_trial ? 'var(--accent, #2a63d4)' : '#fff',
        border: '1px solid var(--accent, #2a63d4)', borderRadius: 20, padding: '2px 10px' }}>{p.badge}</span>}
      <h3 style={{ margin: '4px 0 4px', fontSize: 19 }}>{p.name}{current && <span className="tag" style={{ marginLeft: 8 }}>current</span>}</h3>
      <div className="muted small" style={{ minHeight: 38 }}>{p.tagline}</div>
      <div style={{ margin: '14px 0 4px', minHeight: 58 }}>
        {p.is_trial ? <><div style={{ fontSize: 30, fontWeight: 800 }}>Free</div><div className="small muted">for {p.trial_days} days · no payment details needed</div></>
          : perMonth ? <><div style={{ fontSize: 30, fontWeight: 800 }}>{money(perMonth, cur)}<span className="muted" style={{ fontSize: 14, fontWeight: 500 }}> / month</span></div>
            <div className="small muted">{months === '1' ? `billed monthly${cat.tax[cur as 'INR' | 'USD'] ? ` + ${cat.tax.label}` : ''}` : `${money(price!, cur)} billed every ${months} months`}{save > 0 && <b style={{ color: 'var(--ok, #15803d)' }}> · save {save}%</b>}</div></>
            : <div className="small muted">Not offered for this period / currency</div>}
      </div>
      <ul style={{ listStyle: 'none', padding: 0, margin: '12px 0 18px', flex: 1 }}>
        {lines.map((l, i) => <li key={i} className="small" style={{ display: 'flex', gap: 8, padding: '4px 0' }}><span style={{ color: 'var(--ok, #15803d)' }}>✓</span><span>{l}</span></li>)}
      </ul>
      {onChoose && <button className={`btn${featured ? ' primary' : ''}`} style={{ width: '100%' }} disabled={busy || (!p.is_trial && !price)} onClick={() => onChoose(p)}>{cta ?? (p.is_trial ? 'Start free trial' : `Choose ${p.name}`)}</button>}
    </div>
  )
}

export function PlanGrid({ cat, onChoose, currentPlanId, ctaFor, busy, hideTrial, defaultCurrency }: {
  cat: Catalog; onChoose?: (p: Plan, months: string, currency: string) => void; currentPlanId?: number | null
  ctaFor?: (p: Plan) => string | undefined; busy?: boolean; hideTrial?: boolean; defaultCurrency?: string
}) {
  const periods = useMemo(() => PERIOD_ORDER.filter((m) => cat.plans.some((p) => !p.is_trial && Object.values(p.prices).some((x) => x[m]))), [cat])
  const currencies = cat.currencies.length ? cat.currencies : ['INR']
  const [months, setMonths] = useState(periods[0] ?? '1')
  const [cur, setCur] = useState(defaultCurrency && currencies.includes(defaultCurrency) ? defaultCurrency : currencies[0])
  const plans = cat.plans.filter((p) => !(hideTrial && p.is_trial))
  const bestSave = Math.max(0, ...cat.plans.map((p) => savingPct(p, cur, '12')))
  return (
    <div>
      <div className="row" style={{ justifyContent: 'center', gap: 12, flexWrap: 'wrap', margin: '6px 0 26px' }}>
        <div role="tablist" aria-label="Billing period" style={{ display: 'inline-flex', border: '1px solid var(--line, #e5e7eb)', borderRadius: 10, padding: 3 }}>
          {periods.map((m) => <button key={m} role="tab" aria-selected={months === m} className={`btn sm${months === m ? ' primary' : ''}`} style={{ border: 0 }} onClick={() => setMonths(m)}>
            {cat.periods[m]}{m === '12' && bestSave > 0 ? ` (save up to ${bestSave}%)` : ''}</button>)}
        </div>
        {currencies.length > 1 && <div role="tablist" aria-label="Currency" style={{ display: 'inline-flex', border: '1px solid var(--line, #e5e7eb)', borderRadius: 10, padding: 3 }}>
          {currencies.map((c) => <button key={c} role="tab" aria-selected={cur === c} className={`btn sm${cur === c ? ' primary' : ''}`} style={{ border: 0 }} onClick={() => setCur(c)}>{c === 'INR' ? '₹ INR' : c === 'USD' ? '$ USD' : c}</button>)}
        </div>}
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: `repeat(auto-fit, minmax(230px, 1fr))`, gap: 18, alignItems: 'stretch' }}>
        {plans.map((p) => <PlanCard key={p.id} p={p} cat={cat} cur={cur} months={months} current={currentPlanId === p.id} busy={busy}
          cta={ctaFor?.(p)} onChoose={onChoose ? (x) => onChoose(x, months, cur) : undefined} />)}
      </div>
      <p className="muted small" style={{ textAlign: 'center', marginTop: 18 }}>
        Prices {cat.tax[cur as 'INR' | 'USD'] ? `exclude ${cat.tax.label} (${cat.tax[cur as 'INR' | 'USD']}%), added at checkout` : 'are final'}.
        {' '}Payments are processed by {[cat.gateways.razorpay.enabled && 'Razorpay', cat.gateways.paypal.enabled && 'PayPal'].filter(Boolean).join(' and ') || 'our payment partners'}; TestAgent never sees or stores card details.
      </p>
    </div>
  )
}
