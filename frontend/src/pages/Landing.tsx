import { useEffect, useState, type FormEvent } from 'react'
import { abs } from '../api'
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

function Register() {
  const [f, setF] = useState({ org_name: '', slug: '', contact_name: '', contact_email: '', contact_phone: '', country_code: '', message: '', accept_terms: false, accept_privacy: false, confirm_adult: false })
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
    try { const captcha_token = await captchaToken(cap); const r = await abs.post<{ message: string }>('/api/public/registrations', { ...f, country: countryName(f.country_code), captcha_token }); setDone(r.message) }
    catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  if (done) return <div className="panel"><h2>Registration received</h2><Alert kind="ok">{done}</Alert>
    <p className="muted small">Your requested address is <code>{window.location.host}/{f.slug}/</code>. It becomes active only after approval.</p></div>

  return (
    <form className="panel" onSubmit={submit}>
      <h2>Register your organization</h2>
      <p className="muted small" style={{ marginTop: -6 }}>Every registration is reviewed by Unical Systems before the organization is created. The contact below receives a one-time activation link for the organization owner account.</p>
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
      <label className="f" htmlFor="msg">What would you like to test? (optional)</label>
      <textarea className="i" id="msg" rows={3} value={f.message} onChange={set('message')} maxLength={3000} />
      <label className="check"><input type="checkbox" required checked={f.accept_terms} onChange={(e) => setF({ ...f, accept_terms: e.target.checked })} />
        <span className="small">I am authorised to register this organization and I accept the <a href="/terms" target="_blank" rel="noopener">Terms &amp; Conditions</a>. Tests may only target websites my organization is authorised to test.</span></label>
      <label className="check"><input type="checkbox" required checked={f.accept_privacy} onChange={(e) => setF({ ...f, accept_privacy: e.target.checked })} />
        <span className="small">I have read the <a href="/privacy" target="_blank" rel="noopener">Privacy Policy</a> and consent to Unical Systems processing the contact details above to review this registration and set up the organization. I can withdraw consent at any time.</span></label>
      <label className="check"><input type="checkbox" required checked={f.confirm_adult} onChange={(e) => setF({ ...f, confirm_adult: e.target.checked })} />
        <span className="small">I am 18 or older.</span></label>
      <button className="btn primary" style={{ marginTop: 16 }} disabled={busy || (check !== null && !check.available) || !f.accept_terms || !f.accept_privacy || !f.confirm_adult}>{busy ? 'Submitting…' : 'Submit for approval'}</button>
      {cap?.required && <p className="muted" style={{ fontSize: 11, marginTop: 8 }}>Protected by reCAPTCHA. Google <a href="https://policies.google.com/privacy" target="_blank" rel="noopener noreferrer">Privacy</a> and <a href="https://policies.google.com/terms" target="_blank" rel="noopener noreferrer">Terms</a> apply.</p>}
    </form>
  )
}

export default function Landing() {
  useEffect(() => { document.title = 'TestAgent · Unical Systems' }, [])
  return (
    <div style={{ maxWidth: 1040, margin: '0 auto', padding: '36px 24px 60px' }}>
      <div className="row" style={{ gap: 12, alignItems: 'center', marginBottom: 28 }}>
        <Mark /><div><b style={{ fontSize: 18 }}>TestAgent</b><div className="muted small">by Unical Systems</div></div>
      </div>
      <h1 style={{ fontSize: 28, maxWidth: 720 }}>Automated web application testing with auditable evidence</h1>
      <p className="muted" style={{ maxWidth: 720, fontSize: 15 }}>
        Write or AI-draft end-to-end browser tests, run them in isolated browsers, and keep screenshots, video, traces,
        logs and fingerprinted HTML/JSON reports for every run. Each organization gets its own workspace, users and branded sign-in page.
      </p>
      <div className="grid2" style={{ marginTop: 24, alignItems: 'start' }}>
        <Register />
        <div>
          <SignIn />
          <div className="panel small">
            <h2>How it works</h2>
            <ol style={{ paddingLeft: 18, margin: 0 }}>
              <li>Register your organization and choose its address.</li>
              <li>Unical Systems reviews and approves the registration.</li>
              <li>Your owner activates the account from a one-time link, sets up branding and invites the team.</li>
              <li>Authorise the websites you test, then create projects and test cases.</li>
            </ol>
          </div>
        </div>
      </div>
      <LegalFooter />
      <CookieNotice />
    </div>
  )
}
