import { useEffect, useState, type FormEvent } from 'react'
import { abs, type Catalog, type Plan } from '../api'
import { PlanGrid } from '../components/Pricing'
import { captchaToken, preloadCaptcha, type CaptchaCfg } from '../recaptcha'
import { Alert } from '../components/ui'
import { COUNTRIES, countryName } from '../countries'
import { CookieNotice, LegalFooter } from './Legal'

const slugify = (s: string) => s.toLowerCase().normalize('NFKD').replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40)

function Mark() {
  return (
    <svg width="34" height="34" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="7" fill="#14305c" />
      <path d="M8 17l5 5 11-12" fill="none" stroke="#7fe0b0" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round" /></svg>
  )
}

function SignIn() {
  const [slug, setSlug] = useState('')
  const go = (e: FormEvent) => { e.preventDefault(); const s = slugify(slug); if (s) window.location.assign(`/${s}/`) }
  return (
    <form className="panel" onSubmit={go}>
      <h2>Sign in to your organization</h2>
      <p className="muted small" style={{ marginTop: -6 }}>Enter your organization's TestAgent address.</p>
      <div className="row" style={{ alignItems: 'center', gap: 6 }}>
        <span className="muted mono small">{window.location.host}/</span>
        <input className="i mono" style={{ flex: 1, minWidth: 120 }} value={slug} onChange={(e) => setSlug(e.target.value)} placeholder="your-organization" aria-label="Organization address" required />
        <button className="btn primary">Continue</button>
      </div>
    </form>
  )
}

function Register({ selfSignup, plan }: { selfSignup: boolean; plan: Plan | null }) {
  const [f, setF] = useState({ org_name: '', slug: '', contact_name: '', contact_email: '', contact_phone: '', country_code: '', message: '', accept_terms: false, accept_privacy: false, confirm_adult: false, username: '', password: '', password2: '', store_on_unical: true })
  const [slugEdited, setSlugEdited] = useState(false)
  const [check, setCheck] = useState<{ available: boolean; problem: string | null } | null>(null)
  const [err, setErr] = useState(''), [done, setDone] = useState(''), [busy, setBusy] = useState(false)
  const [cap, setCap] = useState<CaptchaCfg | null>(null)
  useEffect(() => { abs.get<{ captcha: CaptchaCfg }>('/api/public/registration-config').then((c) => { setCap(c.captcha); preloadCaptcha(c.captcha) }).catch(() => {}) }, [])
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value })

  useEffect(() => { if (!slugEdited) setF((x) => ({ ...x, slug: slugify(x.org_name) })) }, [f.org_name, slugEdited])
  useEffect(() => {
    if (f.slug.length < 3) { setCheck(null); return }
    const t = setTimeout(() => abs.get<{ available: boolean; problem: string | null }>(`/api/public/slug-check?slug=${encodeURIComponent(f.slug)}`).then(setCheck).catch(() => setCheck(null)), 350)
    return () => clearTimeout(t)
  }, [f.slug])

  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr(''); setBusy(true)
    if (selfSignup && f.password !== f.password2) { setErr('The passwords do not match'); setBusy(false); return }
    try {
      const captcha_token = await captchaToken(cap)
      const { password2: _p2, ...body } = f
      void _p2
      const r = await abs.post<{ message: string; status: string; redirect?: string }>('/api/public/registrations', { ...body, country: countryName(f.country_code), captcha_token })
      if (r.status === 'created' && r.redirect) { window.location.assign(r.redirect + (plan && !plan.is_trial ? `&plan=${encodeURIComponent(plan.code)}` : '')); return }
      setDone(r.message)
    }
    catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  if (done) return <div className="panel"><h2>Registration received</h2><Alert kind="ok">{done}</Alert>
    <p className="muted small">Your requested address is <code>{window.location.host}/{f.slug}/</code>. It becomes active only after approval.</p></div>

  return (
    <form className="panel" onSubmit={submit} id="register">
      <h2>{selfSignup ? 'Create your organization' : 'Register your organization'}</h2>
      {selfSignup
        ? <p className="muted small" style={{ marginTop: -6 }}>Your workspace is ready in a minute. {plan ? <>You chose <b>{plan.name}</b>; </> : null}after sign-up you start the free trial or pick a plan.</p>
        : <p className="muted small" style={{ marginTop: -6 }}>Every registration is reviewed by Unical Systems before the organization is created. The contact below receives a one-time activation link for the organization owner account.</p>}
      <Alert>{err}</Alert>
      <label className="f" htmlFor="org">Organization name</label>
      <input className="i" id="org" value={f.org_name} onChange={set('org_name')} required minLength={2} maxLength={160} />
      <label className="f" htmlFor="slug">Organization address</label>
      <div className="row" style={{ alignItems: 'center', gap: 6 }}>
        <span className="muted mono small">{window.location.host}/</span>
        <input className="i mono" id="slug" style={{ flex: 1 }} value={f.slug} onChange={(e) => { setSlugEdited(true); setF({ ...f, slug: slugify(e.target.value) }) }} required minLength={3} maxLength={40} />
      </div>
      {check && <div className="hint" style={{ color: check.available ? 'var(--ok)' : 'var(--bad)' }}>{check.available ? 'Available' : check.problem}</div>}
      <div className="grid2" style={{ gap: 12 }}>
        <div><label className="f" htmlFor="cn">Your name</label><input className="i" id="cn" value={f.contact_name} onChange={set('contact_name')} required minLength={2} /></div>
        <div><label className="f" htmlFor="ce">Work email</label><input className="i" id="ce" type="email" value={f.contact_email} onChange={set('contact_email')} required /></div>
        <div><label className="f" htmlFor="cp">Phone (optional)</label><input className="i" id="cp" value={f.contact_phone} onChange={set('contact_phone')} maxLength={40} /></div>
        <div><label className="f" htmlFor="co">Country</label>
          <select className="i" id="co" value={f.country_code} onChange={set('country_code')} required>
            <option value="" disabled>Select a country…</option>
            {COUNTRIES.map((c) => <option key={c.code} value={c.code}>{c.name}</option>)}
          </select></div>
      </div>
      {selfSignup && <div className="grid2" style={{ gap: 12 }}>
        <div><label className="f" htmlFor="un">Username (owner account)</label><input className="i" id="un" value={f.username} onChange={set('username')} required minLength={3} maxLength={80} pattern="[a-zA-Z0-9._@-]+" autoComplete="username" /></div>
        <div />
        <div><label className="f" htmlFor="pw">Password</label><input className="i" id="pw" type="password" value={f.password} onChange={set('password')} required minLength={12} autoComplete="new-password" /><div className="hint">At least 12 characters with letters and digits</div></div>
        <div><label className="f" htmlFor="pw2">Confirm password</label><input className="i" id="pw2" type="password" value={f.password2} onChange={set('password2')} required minLength={12} autoComplete="new-password" /></div>
      </div>}
      <label className="f" htmlFor="msg">What would you like to test? (optional)</label>
      <textarea className="i" id="msg" rows={3} value={f.message} onChange={set('message')} maxLength={3000} />
      <label className="check"><input type="checkbox" required checked={f.accept_terms} onChange={(e) => setF({ ...f, accept_terms: e.target.checked })} />
        <span className="small">I am authorised to register this organization and I accept the <a href="/terms" target="_blank" rel="noopener">Terms &amp; Conditions</a>. Tests may only target websites my organization is authorised to test.</span></label>
      <label className="check"><input type="checkbox" required checked={f.accept_privacy} onChange={(e) => setF({ ...f, accept_privacy: e.target.checked })} />
        <span className="small">I have read the <a href="/privacy" target="_blank" rel="noopener">Privacy Policy</a> and consent to Unical Systems processing the contact details above to review this registration and set up the organization. I can withdraw consent at any time.</span></label>
      <label className="check"><input type="checkbox" required checked={f.confirm_adult} onChange={(e) => setF({ ...f, confirm_adult: e.target.checked })} />
        <span className="small">I am 18 or older.</span></label>
      {selfSignup && <label className="check"><input type="checkbox" checked={f.store_on_unical} onChange={(e) => setF({ ...f, store_on_unical: e.target.checked })} />
        <span className="small">Store test evidence (screenshots, videos, reports) on Unical-managed servers in India. You can switch to your own SharePoint or Google Drive later in Administration.</span></label>}
      <button className="btn primary" style={{ marginTop: 16 }} disabled={busy || (check !== null && !check.available) || !f.accept_terms || !f.accept_privacy || !f.confirm_adult}>{busy ? 'Submitting…' : selfSignup ? 'Create organization' : 'Submit for approval'}</button>
      {cap?.required && <p className="muted" style={{ fontSize: 11, marginTop: 8 }}>Protected by reCAPTCHA. Google <a href="https://policies.google.com/privacy" target="_blank" rel="noopener noreferrer">Privacy</a> and <a href="https://policies.google.com/terms" target="_blank" rel="noopener noreferrer">Terms</a> apply.</p>}
    </form>
  )
}

export default function Landing() {
  const [cat, setCat] = useState<Catalog | null>(null)
  const [plan, setPlan] = useState<Plan | null>(null)
  useEffect(() => { document.title = 'TestAgent · Unical Systems'; abs.get<Catalog>('/api/public/plans').then(setCat).catch(() => setCat(null)) }, [])
  const selfSignup = !!cat?.self_signup
  const choose = (p: Plan) => { setPlan(p); document.getElementById('register')?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }
  return (
    <div style={{ maxWidth: 1120, margin: '0 auto', padding: '36px 24px 60px' }}>
      <div className="row" style={{ gap: 12, alignItems: 'center', marginBottom: 28, justifyContent: 'space-between', flexWrap: 'wrap' }}>
        <div className="row" style={{ gap: 12, alignItems: 'center' }}><Mark /><div><b style={{ fontSize: 18 }}>TestAgent</b><div className="muted small">by Unical Systems</div></div></div>
        <div className="row small" style={{ gap: 16 }}>{cat && cat.plans.length > 0 && <a href="#pricing">Pricing</a>}<a href="#register">{selfSignup ? 'Start free trial' : 'Register'}</a><a href="#signin">Sign in</a></div>
      </div>
      <h1 style={{ fontSize: 30, maxWidth: 760 }}>AI-assisted testing for web apps, APIs and mobile apps — with auditable evidence</h1>
      <p className="muted" style={{ maxWidth: 760, fontSize: 15 }}>
        Write or AI-generate end-to-end tests, run them in isolated browsers and on real phones, test and monitor your APIs, and push results to
        Azure DevOps, Jira, GitHub or GitLab. Every run keeps screenshots, video, traces, logs and fingerprinted HTML/JSON reports.
      </p>
      {selfSignup && <div className="row" style={{ gap: 10, marginTop: 14 }}><a className="btn primary" href="#register">Start your free trial</a>{cat && cat.plans.length > 0 && <a className="btn" href="#pricing">See plans &amp; pricing</a>}</div>}
      {cat && cat.plans.length > 0 && <section id="pricing" style={{ marginTop: 44 }}>
        <h2 style={{ textAlign: 'center', fontSize: 26, marginBottom: 4 }}>Plans &amp; pricing</h2>
        <p className="muted" style={{ textAlign: 'center', marginTop: 0 }}>Start with a free trial. Pay monthly, quarterly, half-yearly or annually — longer periods cost less.</p>
        <PlanGrid cat={cat} onChoose={(p) => choose(p)} ctaFor={(p) => (p.is_trial ? `Start ${p.trial_days}-day free trial` : `Choose ${p.name}`)} />
      </section>}
      <div className="grid2" style={{ marginTop: 40, alignItems: 'start' }}>
        <Register selfSignup={selfSignup} plan={plan} />
        <div id="signin">
          <SignIn />
          <div className="panel small">
            <h2>How it works</h2>
            {selfSignup
              ? <ol style={{ paddingLeft: 18, margin: 0 }}>
                <li>Create your organization and owner account.</li>
                <li>Start the free trial or choose a plan and pay online (Razorpay or PayPal).</li>
                <li>Invite your team, authorise the websites and APIs you test, and create projects.</li>
                <li>Renew before the period ends — or apply a coupon from Unical to extend your licence.</li>
              </ol>
              : <ol style={{ paddingLeft: 18, margin: 0 }}>
                <li>Register your organization and choose its address.</li>
                <li>Unical Systems reviews and approves the registration.</li>
                <li>Your owner activates the account from a one-time link, sets up branding and invites the team.</li>
                <li>Authorise the websites you test, then create projects and test cases.</li>
              </ol>}
          </div>
        </div>
      </div>
      <LegalFooter />
      <CookieNotice />
    </div>
  )
}
