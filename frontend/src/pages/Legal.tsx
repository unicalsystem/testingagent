import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { abs, type Legal } from '../api'
import { captchaToken, preloadCaptcha, type CaptchaCfg } from '../recaptcha'
import { Alert } from '../components/ui'

// Public legal pages (/privacy, /terms). The wording is fixed here; the legal entity, contacts,
// hosting location and version come from Platform → Legal & privacy.

export function useLegal() {
  const [l, setL] = useState<Legal | null>(null)
  useEffect(() => { abs.get<Legal>('/api/public/legal').then(setL).catch(() => setL(null)) }, [])
  return l
}

const fmtVer = (v?: string) => {
  if (!v) return ''
  const d = new Date(v.slice(0, 10) + 'T00:00:00')
  return isNaN(d.getTime()) ? v : d.toLocaleDateString('en-GB', { day: 'numeric', month: 'long', year: 'numeric' })
}

function Mail({ to }: { to?: string }) { return to ? <a href={`mailto:${to}`}>{to}</a> : <i>(to be published)</i> }

export function LegalFooter({ dark = false }: { dark?: boolean }) {
  const s = dark ? { color: '#c9d6ee' } : undefined
  return (
    <div className="small" style={{ display: 'flex', gap: 14, flexWrap: 'wrap', justifyContent: 'center', marginTop: 16, ...(dark ? { color: '#9fb3d6' } : { color: 'var(--ink-3)' }) }}>
      <span>© {new Date().getFullYear()} Unical Systems</span>
      <a href="/privacy" style={s}>Privacy Policy</a>
      <a href="/terms" style={s}>Terms &amp; Conditions</a>
      <a href="/privacy#rights" style={s}>Your privacy rights</a>
    </div>
  )
}

const COOKIE_KEY = 'tp-cookie-notice'
export function CookieNotice() {
  const [show, setShow] = useState(() => { try { return localStorage.getItem(COOKIE_KEY) !== '1' } catch { return true } })
  if (!show) return null
  const ok = () => { try { localStorage.setItem(COOKIE_KEY, '1') } catch { /* private mode */ } setShow(false) }
  return (
    <div role="region" aria-label="Cookie notice" style={{ position: 'fixed', left: 16, right: 16, bottom: 16, zIndex: 60, maxWidth: 760, margin: '0 auto',
      background: '#fff', border: '1px solid var(--line)', borderRadius: 10, boxShadow: '0 10px 30px rgba(0,0,0,.15)', padding: '12px 16px',
      display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
      <span className="small" style={{ flex: 1, minWidth: 240 }}>
        We use only essential cookies (to keep you signed in and to secure sign-in). We do not use advertising or analytics cookies,
        and we do not sell or share personal data. If bot protection is switched on, Google reCAPTCHA runs on sign-in and registration pages.{' '}
        <a href="/privacy#cookies">Learn more</a></span>
      <button className="btn sm primary" onClick={ok}>OK</button>
    </div>
  )
}

function Shell({ title, version, children }: { title: string; version?: string; children: ReactNode }) {
  useEffect(() => { document.title = `${title} · TestAgent · Unical Systems` }, [title])
  return (
    <div style={{ background: 'var(--bg)', minHeight: '100vh' }}>
      <div style={{ maxWidth: 860, margin: '0 auto', padding: '32px 24px 60px' }}>
        <a href="/" className="small">← TestAgent · Unical Systems</a>
        <div className="panel legal" style={{ marginTop: 14, padding: '26px 32px' }}>
          <h1 style={{ fontSize: 24 }}>{title}</h1>
          {version && <p className="muted small">Version {version} · effective {fmtVer(version)}</p>}
          {children}
        </div>
        <LegalFooter />
      </div>
    </div>
  )
}

export function PrivacyPolicy({ l }: { l: Legal }) {
  const e = l.entity_name || 'Unical Systems'
  const go = l.grievance_officer_name || 'Grievance Officer'
  return (
    <>
      <p><b>{e}</b> ("we", "us") operates TestAgent, a web-application testing service for organizations. This policy explains what personal
        data we process, why, how long we keep it and the rights you have. It is written to meet India's Digital Personal Data Protection Act, 2023 and
        the DPDP Rules, 2025 (DPDP Act), the UK General Data Protection Regulation and Data Protection Act 2018 (UK GDPR), and US state privacy laws
        including the California Consumer Privacy Act as amended by the CPRA (CCPA).</p>

      <h2>1. Who is responsible</h2>
      <p>For account, registration, sign-in and support data, {e} is the <b>Data Fiduciary</b> (DPDP Act), the <b>controller</b> (UK GDPR) and the
        <b> business</b> (CCPA). For the content an organization tests with TestAgent (websites, test cases, screenshots, recordings, logs and
        reports) the <b>organization is the Data Fiduciary / controller</b> and we act as its <b>Data Processor / processor / service provider</b>,
        following its instructions under our <a href="/terms#dpa">data processing terms</a>.</p>
      <p>{e}, {l.address || 'Hyderabad, India'}.{l.contact_email && <> Contact: <Mail to={l.contact_email} />.</>}</p>

      <h2>2. Personal data we process</h2>
      <table className="t"><thead><tr><th>Category</th><th>Examples</th><th>Purpose</th><th>Legal basis (UK GDPR)</th></tr></thead><tbody>
        <tr><td>Registration</td><td>organization name, contact name, work e-mail, phone, country, message, IP address</td><td>review and approve an organization, contact you about it</td><td>steps before a contract; legitimate interests</td></tr>
        <tr><td>Account</td><td>username, display name, e-mail, role, group and project access, linked Microsoft account identifier</td><td>provide the service and control access</td><td>contract</td></tr>
        <tr><td>Security &amp; audit</td><td>sign-in times, IP address, browser user-agent, actions performed, failed sign-ins, CAPTCHA scores</td><td>keep the service secure, investigate misuse, meet legal obligations</td><td>legitimate interests; legal obligation</td></tr>
        <tr><td>Consent &amp; requests</td><td>which version of these documents you accepted and when; privacy requests and our responses</td><td>prove consent and handle your rights</td><td>legal obligation</td></tr>
        <tr><td>Customer test content</td><td>whatever appears on the websites an organization tests (this can include personal data in screenshots and recordings)</td><td>run tests for the organization</td><td>the organization's basis; we process it only on its instructions</td></tr>
      </tbody></table>
      <p>Under the DPDP Act we process your personal data with your <b>consent</b>, given when you register or accept these terms, or for a
        <b> legitimate use</b> permitted by section 7 (for example, where you voluntarily provide data for a specified purpose, or to comply with law).
        We do not process sensitive personal information to infer characteristics, we do not profile you, and we make no automated decisions that
        produce legal or similarly significant effects.</p>

      <h2>3. Where data is stored</h2>
      <p>The service is hosted on servers located in <b>{l.hosting_location || 'India'}</b>. Each organization chooses where its test artifacts are
        stored: on Unical-managed storage, or only in its own Microsoft SharePoint or Google Drive. With customer-owned storage the files are uploaded to
        the organization's storage and removed from our servers once the upload is verified; if that storage is unavailable they wait on our server for a
        limited time and are then deleted. Tests still run on our servers, so pages are processed there while a test runs.</p>

      <h2>4. Who we share data with</h2>
      <p>We do <b>not sell</b> personal data and we do <b>not share</b> it for cross-context behavioural advertising. We use these service providers,
        bound by contract to process data only for us:</p>
      <ul>
        <li><b>Hosting provider</b> (virtual private server) — runs the service and stores data.</li>
        <li><b>Anthropic (Claude API)</b> — only when someone uses an AI feature: draft test cases, or analyse a failed run (step descriptions, error
          messages and the failure screenshot are sent). Organizations can switch AI features off.</li>
        <li><b>Google (reCAPTCHA)</b> — when bot protection is switched on for sign-in, registration or password reset.</li>
        <li><b>Microsoft</b> — when you sign in with Microsoft 365, or your organization stores artifacts in SharePoint.</li>
        <li><b>Google Drive</b> — when your organization stores artifacts in its Google Drive.</li>
        <li><b>E-mail delivery provider</b> — to send invitations, password resets and notices.</li>
      </ul>
      <p>We may disclose data where required by law, to protect rights and safety, or in a business transfer subject to this policy.</p>

      <h2>5. International transfers</h2>
      <p>Some providers process data outside {l.hosting_location || 'India'}. Transfers from India follow the DPDP Act (including any country
        restrictions notified by the Government of India). Transfers of UK personal data to countries without UK adequacy regulations use the UK
        International Data Transfer Agreement or the UK Addendum to the EU Standard Contractual Clauses, or another lawful mechanism.</p>

      <h2>6. How long we keep data</h2>
      <ul>
        <li>Accounts: while the account exists; erased on request or when the organization deletes it.</li>
        <li>Audit and security logs: {l.retention?.audit_retention_days || 730} days.</li>
        <li>Rejected registrations: {l.retention?.registration_retention_days || 365} days. Expired sign-in sessions: {l.retention?.session_retention_days || 30} days.</li>
        <li>Test artifacts: as set by each organization (it can choose automatic deletion after a period, and can delete everything held on Unical servers at any time).</li>
        <li>Backups are overwritten on a rolling basis; deleted data disappears from backups when they expire.</li>
      </ul>

      <h2 id="rights">7. Your rights</h2>
      <p>Depending on where you live you may: <b>access</b> your data and receive a summary of how it is processed; receive a <b>copy</b> in a portable
        format; <b>correct</b>, complete or update it; have it <b>erased</b>; <b>withdraw consent</b> (as easily as you gave it — this does not affect
        processing before withdrawal); <b>restrict</b> or <b>object</b> to processing; <b>opt out</b> of sale, sharing or targeted advertising (we do
        none of these); <b>nominate</b> another person to exercise your rights if you die or become incapable (DPDP Act section 14); and get
        <b> grievance redressal</b>. We will not discriminate against you for exercising your rights. We honour Global Privacy Control signals.</p>
      <p><b>How:</b> signed-in users can download their data and raise requests under <i>Privacy &amp; my data</i> in the app. Anyone can use the form
        below or e-mail us. We verify your identity before acting and respond within <b>{l.response_days || 30} days</b>. An authorised agent may act
        for you with your signed permission. If your request concerns test content held for an organization, we will pass it to that organization.</p>

      <h2>8. Grievance Officer, DPO and representatives</h2>
      <p><b>{go}</b>: <Mail to={l.grievance_officer_email || l.contact_email} />{l.grievance_officer_phone && <> · {l.grievance_officer_phone}</>}.
        {l.dpo_email && <> Data protection contact: <Mail to={l.dpo_email} />.</>}
        {l.uk_representative && <> UK representative (Article 27 UK GDPR): {l.uk_representative}.</>}
        {l.us_contact && <> United States contact: {l.us_contact}.</>}</p>
      <p>If you are not satisfied: in India you may complain to the <b>Data Protection Board of India</b> after using our grievance process; in the UK to
        the <b>Information Commissioner's Office</b> (ico.org.uk); in the US to your state Attorney General or, in California, the California Privacy
        Protection Agency.</p>

      <h2>9. Security and breaches</h2>
      <p>We use encryption in transit (HTTPS), encryption of stored secrets and provider credentials, tenant isolation, role-based access with
        time-limited project assignments, audit logging, CAPTCHA and lockout against password attacks, and least-privilege connections to customer
        storage. If a personal data breach occurs we will notify the Data Protection Board of India and affected people as the DPDP Rules require, the ICO
        within 72 hours where required, affected organizations without undue delay, and others as required by applicable US state laws.</p>

      <h2 id="cookies">10. Cookies and similar technologies</h2>
      <p>We use only <b>strictly necessary</b> cookies: a session cookie that keeps you signed in, and a short-lived cookie that protects the
        Microsoft sign-in flow. Your browser's local storage remembers small interface preferences (for example, a collapsed menu or that you saw this
        notice). When bot protection is on, Google reCAPTCHA sets its own cookies on sign-in, registration and password-reset pages to tell people from
        bots. We use no advertising, analytics or tracking cookies.</p>

      <h2>11. Children</h2>
      <p>TestAgent is a business service for people aged 18 or over acting for an organization. We do not knowingly process children's personal
        data, and we do not track, monitor the behaviour of, or target advertising at children. If you believe a child has given us data, contact us
        and we will delete it.</p>

      <h2>12. Changes</h2>
      <p>We will tell you about material changes before they take effect. Signed-in users are asked to accept the new version.</p>

      <PublicRequestForm l={l} />
    </>
  )
}

export function Terms({ l }: { l: Legal }) {
  const e = l.entity_name || 'Unical Systems'
  return (
    <>
      <p>These Terms &amp; Conditions govern use of TestAgent, provided by <b>{e}</b> ({l.address || 'Hyderabad, India'}). By registering an
        organization, accepting an invitation or using the service you agree to them on behalf of yourself and the organization you represent. A signed
        order form or master agreement with {e} takes precedence where it differs.</p>

      <h2>1. Eligibility and accounts</h2>
      <p>You must be 18 or over and authorised to bind your organization. Keep credentials confidential, use your own account, and tell us promptly about
        any unauthorised use. Organization administrators control their users, roles, project access and settings, and are responsible for them.</p>

      <h2>2. Acceptable use</h2>
      <ul>
        <li>Test only websites and applications that your organization owns or is <b>authorised in writing</b> to test. You confirm this each time you
          authorise a test target.</li>
        <li>Do not use the service for attacks, vulnerability exploitation, denial of service, load testing beyond what the site owner allowed,
          scraping in breach of a site's terms, collecting personal data you have no right to collect, or any unlawful purpose.</li>
        <li>Do not attempt to access other organizations' data, bypass security controls, or reverse engineer the service.</li>
        <li>Do not put secrets other than test credentials in test steps; store credentials as project secrets.</li>
      </ul>
      <p>We may suspend access to prevent harm or breach of these terms, and will tell you why unless the law prevents it.</p>

      <h2>3. Your content and storage</h2>
      <p>Your organization owns its test cases, results and evidence. Your organization chooses whether artifacts are kept on Unical-managed storage
        or only in its own SharePoint or Google Drive, and is responsible for the configuration and security of its own storage. We do not change that
        choice without your administrator's action.</p>

      <h2>4. AI features</h2>
      <p>AI-drafted test cases and failure analyses are suggestions produced by Anthropic's Claude models. Review them before relying on them. Using an
        AI feature sends the relevant step text, errors and, for analysis, the failure screenshot to Anthropic. Administrators can switch AI features off.</p>

      <h2 id="dpa">5. Data processing terms</h2>
      <p>For personal data in your organization's content, your organization is the Data Fiduciary / controller and {e} is its Data Processor /
        processor / service provider. We will:</p>
      <ul>
        <li>process it only to provide the service and on your documented instructions, and not sell, share, retain or use it for any other purpose
          (including outside the direct business relationship), or combine it with other data, except as the law allows;</li>
        <li>ensure our people and service providers are bound by confidentiality and comparable data-protection terms, and tell you about new
          service providers so you can object;</li>
        <li>apply reasonable security safeguards, notify you of a personal data breach without undue delay, and help you meet your breach-notification,
          rights-request and impact-assessment obligations;</li>
        <li>delete or return the data at the end of the service (and on your instruction during it), except where the law requires us to keep it;</li>
        <li>make available information needed to demonstrate compliance and allow reasonable audits.</li>
      </ul>
      <p>You are responsible for having a lawful basis and giving any notices required for the personal data in the sites you test.</p>

      <h2>6. Fees</h2>
      <p>Fees, if any, are as agreed in your order form. Taxes are additional where applicable.</p>

      <h2>7. Availability and changes</h2>
      <p>We aim for high availability but, unless a service level is agreed in writing, the service is provided without guaranteed uptime. We may
        improve and change the service; we will give notice of changes that materially reduce it.</p>

      <h2>8. Intellectual property and confidentiality</h2>
      <p>{e} owns the service and its software. You receive a non-exclusive, non-transferable right to use it during your subscription. Each party
        keeps the other's confidential information confidential and uses it only for this relationship.</p>

      <h2>9. Warranties and liability</h2>
      <p>Except as expressly stated, the service is provided "as is". Test results indicate behaviour observed during a run and are not a guarantee that
        an application is free of defects. To the extent the law allows, neither party is liable for indirect or consequential loss, and each party's
        total liability is limited to the fees paid for the service in the twelve months before the claim. Nothing limits liability that cannot be
        limited by law, including under applicable consumer-protection and data-protection laws.</p>

      <h2>10. Suspension and termination</h2>
      <p>Either party may end the service as agreed in the order form, or immediately for material breach not fixed within 30 days of notice. On
        termination your access ends; you may export your data before then, after which we delete it as described in the Privacy Policy.</p>

      <h2>11. Law and disputes</h2>
      <p>These terms are governed by the laws of India. Courts in Hyderabad, Telangana have exclusive jurisdiction, without depriving consumers of
        protections mandatory where they live. Mandatory data-protection rights under the UK GDPR and US state laws apply in addition.</p>

      <h2>12. Changes and contact</h2>
      <p>We may update these terms; signed-in users are asked to accept new versions. Questions: <Mail to={l.contact_email || l.grievance_officer_email} />.</p>
    </>
  )
}

function PublicRequestForm({ l }: { l: Legal }) {
  const [f, setF] = useState({ name: '', email: '', organization: '', kind: 'access', details: '' })
  const [msg, setMsg] = useState(''), [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const [cap, setCap] = useState<CaptchaCfg | null>(null)
  useEffect(() => { abs.get<{ captcha: CaptchaCfg }>('/api/public/registration-config').then((c) => { setCap(c.captcha); preloadCaptcha(c.captcha) }).catch(() => {}) }, [])
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setErr(''); setBusy(true)
    try { const captcha_token = await captchaToken(cap); const r = await abs.post<{ message: string }>('/api/public/privacy-requests', { ...f, captcha_token }); setMsg(r.message) }
    catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  if (msg) return <div id="request"><h2>13. Make a request</h2><Alert kind="ok">{msg}</Alert></div>
  return (
    <form id="request" onSubmit={submit}>
      <h2>13. Make a request</h2>
      <p className="small muted">Signed in? Use <i>Privacy &amp; my data</i> in the app instead — it can download your data immediately.</p>
      <Alert>{err}</Alert>
      <div className="grid2" style={{ gap: 12 }}>
        <div><label className="f" htmlFor="pn">Your name</label><input className="i" id="pn" required minLength={2} value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></div>
        <div><label className="f" htmlFor="pe">E-mail</label><input className="i" id="pe" type="email" required value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} /></div>
        <div><label className="f" htmlFor="po">Organization (optional)</label><input className="i" id="po" value={f.organization} onChange={(e) => setF({ ...f, organization: e.target.value })} /></div>
        <div><label className="f" htmlFor="pk">Request</label>
          <select className="i" id="pk" value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })}>
            {l.request_kinds.map((k) => <option key={k.kind} value={k.kind}>{k.label}</option>)}</select></div>
      </div>
      <label className="f" htmlFor="pd">Details</label>
      <textarea className="i" id="pd" rows={3} maxLength={4000} value={f.details} onChange={(e) => setF({ ...f, details: e.target.value })}
        placeholder={f.kind === 'nominate' ? 'Name and contact details of the person you nominate' : ''} />
      <button className="btn primary" style={{ marginTop: 12 }} disabled={busy}>{busy ? 'Sending…' : 'Send request'}</button>
    </form>
  )
}

export default function LegalPage({ doc }: { doc: 'privacy' | 'terms' }) {
  const l = useLegal()
  useEffect(() => { if (l && window.location.hash) document.getElementById(window.location.hash.slice(1))?.scrollIntoView() }, [l])
  const title = doc === 'privacy' ? 'Privacy Policy' : 'Terms & Conditions'
  if (!l) return <Shell title={title}><p className="muted">Loading…</p></Shell>
  return (
    <Shell title={title} version={doc === 'privacy' ? l.privacy_version : l.terms_version}>
      {doc === 'privacy' ? <PrivacyPolicy l={l} /> : <Terms l={l} />}
    </Shell>
  )
}
