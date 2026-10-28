import { useCallback, useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { abs, api, type Branding, type Me, type Role, type SystemStatus } from './api'
import { AppCtx } from './components/ui'
import Login, { ChangePassword, ResetPassword } from './pages/Login'
import Projects from './pages/Projects'
import ProjectPage from './pages/Project'
import CaseEditor from './pages/CaseEditor'
import RunPage from './pages/Run'
import Admin from './pages/Admin'
import Platform from './pages/Platform'
import Activate from './pages/Activate'
import MyPrivacy, { LegalGate } from './pages/MyPrivacy'
import { LegalFooter } from './pages/Legal'
import ImportWizard from './pages/Import'
import TestRunPage from './pages/TestRun'
import AutomationJobPage from './pages/AutomationJob'
import ApiSuitePage from './pages/ApiSuite'
import ApiRunPage from './pages/ApiRun'
import MonitorPage from './pages/Monitor'
import MonitoringOverview from './pages/MonitoringOverview'
import MobileCasePage from './pages/MobileCase'
import MobileRunPage from './pages/MobileRun'
import MobileSessionPage from './pages/MobileSession'
import { MonitorAlerts } from './components/MonitorAlerts'

export function Logo({ branding, size = 30 }: { branding?: Branding | null; size?: number }) {
  if (branding?.logo_data_url) return <img src={branding.logo_data_url} alt="" width={size} height={size} style={{ objectFit: 'contain', borderRadius: 6, background: '#fff' }} />
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="7" fill={branding?.primary_color || '#2a63d4'} />
      <path d="M8 17l5 5 11-12" fill="none" stroke="#7fe0b0" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

// release-1.0 coarse levels used by existing pages, mapped onto permissions
const LEVEL_PERM: Record<Role, string> = { viewer: 'project.read', tester: 'run.execute', admin: 'tenant.users' }

function alertMsg(m: string) { window.alert(m) }

const ICONS: Record<string, string> = {
  projects: 'M3 6.5A1.5 1.5 0 0 1 4.5 5h4l1.5 2h7.5A1.5 1.5 0 0 1 19 8.5v8a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 3 16.5z',
  admin: 'M11 3h2l.5 2.2 1.9.8 1.9-1.2 1.4 1.4-1.2 1.9.8 1.9L21 11v2l-2.2.5-.8 1.9 1.2 1.9-1.4 1.4-1.9-1.2-1.9.8L13 21h-2l-.5-2.2-1.9-.8-1.9 1.2-1.4-1.4 1.2-1.9-.8-1.9L3 13v-2l2.2-.5.8-1.9-1.2-1.9 1.4-1.4 1.9 1.2 1.9-.8zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z',
  platform: 'M12 3l8 4.5v9L12 21l-8-4.5v-9zM12 3v18M4 7.5l8 4.5 8-4.5',
  privacy: 'M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6zM9 12l2 2 4-4',
  monitor: 'M3 12h4l3-7 4 14 3-7h4',
}
function Ico({ name }: { name: string }) {
  return <svg className="ico" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" strokeLinecap="round" aria-hidden="true"><path d={ICONS[name]} /></svg>
}
const NAV_KEY = 'tp-nav-collapsed'
function readCollapsed() { try { return localStorage.getItem(NAV_KEY) === '1' } catch { return false } }

export default function App({ slug }: { slug: string }) {
  const [branding, setBranding] = useState<Branding | null | undefined>(undefined)
  const [me, setMe] = useState<Me | null | undefined>(undefined)
  const [status, setStatus] = useState<SystemStatus | null>(null)
  const [collapsed, setCollapsed] = useState(readCollapsed)
  const toggleNav = () => setCollapsed((c) => { try { localStorage.setItem(NAV_KEY, c ? '0' : '1') } catch { /* private mode */ } return !c })
  const nav = useNavigate()
  const loc = useLocation()
  const [menu, setMenu] = useState(false)  // mobile: slide-in navigation
  useEffect(() => { setMenu(false); window.scrollTo(0, 0) }, [loc.pathname])

  useEffect(() => {
    abs.get<Branding>(`/api/public/tenants/${encodeURIComponent(slug)}/branding`).then((b) => {
      setBranding(b)
      document.title = `${b.display_name} · TestAgent`
      document.documentElement.style.setProperty('--accent', b.primary_color)
    }).catch(() => setBranding(null))
  }, [slug])

  const refreshStatus = useCallback(() => {
    api.get<SystemStatus>('/system/status').then(setStatus).catch(() => setStatus(null))
  }, [])

  useEffect(() => {
    if (!branding) return
    api.get<Me>('/auth/me').then(setMe).catch(() => setMe(null))
    const out = () => setMe(null)
    window.addEventListener('tp:unauthorized', out)
    return () => window.removeEventListener('tp:unauthorized', out)
  }, [branding])

  useEffect(() => {
    if (!me) return
    refreshStatus()
    const t = setInterval(refreshStatus, 30000)
    return () => clearInterval(t)
  }, [me, refreshStatus])

  if (branding === undefined) return null
  if (branding === null) {
    return (
      <div className="login-wrap"><div className="login">
        <h1>Organization not found</h1>
        <p className="muted">There is no active TestAgent organization at <code>/{slug}/</code>. Check the address you were given, or <a href="/">register your organization</a>.</p>
      </div></div>
    )
  }
  if (loc.pathname === '/reset-password') return <ResetPassword branding={branding} />
  if (loc.pathname === '/activate') return <Activate branding={branding} onDone={(m) => { setMe(m); nav('/projects', { replace: true }) }} />
  if (me === undefined) return null
  if (me === null) return <Login branding={branding} onLogin={setMe} />
  if (me.must_change_password) return <ChangePassword me={me} onDone={setMe} />
  if (me.legal_pending?.length) return <LegalGate pending={me.legal_pending} onDone={() => setMe({ ...me, legal_pending: [] })} onSignOut={async () => { await api.post('/auth/logout').catch(() => {}); setMe(null) }} />

  const has = (perm: string) => me.permissions.includes(perm)
  const can = (r: Role) => has(LEVEL_PERM[r])
  const showAdmin = has('tenant.users') || has('tenant.settings') || has('tenant.audit') || has('tenant.targets') || has('tenant.groups') || has('tenant.sso')
  const notice = new URLSearchParams(loc.search).get('notice')
  const linkMicrosoft = async () => { try { const r = await api.post<{ url: string }>('/auth/microsoft/link-start'); window.location.assign(r.url) } catch (x) { alertMsg((x as Error).message) } }
  const logout = async () => { await api.post('/auth/logout').catch(() => {}); setMe(null); nav('/') }

  return (
    <AppCtx.Provider value={{ me, status, can, has, branding, refreshStatus }}>
      <div className={`shell${collapsed ? ' collapsed' : ''}`}>
        <header className="mobilebar">
          <div className="brand"><Logo branding={branding} /><div>{branding.display_name}</div></div>
          <button onClick={() => setMenu(!menu)} aria-expanded={menu} aria-controls="mainnav">{menu ? '✕ Close' : '☰ Menu'}</button>
        </header>
        <nav className={`side${menu ? ' open' : ''}`} id="mainnav" aria-label="Main">
          <button className="collapse-btn" onClick={toggleNav} aria-label={collapsed ? 'Expand menu' : 'Collapse menu'} aria-expanded={!collapsed} title={collapsed ? 'Expand menu' : 'Collapse menu'}>{collapsed ? '»' : '«'}</button>
          <div className="brand"><Logo branding={branding} /><div>{branding.display_name}<small>TestAgent · Intelligent web testing</small></div></div>
          <NavLink to="/projects" title="Projects"><Ico name="projects" /><span className="label">Projects</span></NavLink>
          <NavLink to="/monitoring" title="API monitoring"><Ico name="monitor" /><span className="label">API monitoring</span></NavLink>
          {showAdmin && <NavLink to="/admin" title="Administration"><Ico name="admin" /><span className="label">Administration</span></NavLink>}
          {me.is_platform_admin && <NavLink to="/platform" title="Platform (Unical)"><Ico name="platform" /><span className="label">Platform (Unical)</span></NavLink>}
          <NavLink to="/my-privacy" title="Privacy & my data"><Ico name="privacy" /><span className="label">Privacy &amp; my data</span></NavLink>
          <div className="spacer" />
          <div className="who">
            <b>{me.display_name || me.username}</b>{me.role_label}
            {(branding.support_email || branding.support_url) && (
              <div style={{ marginTop: 6 }}>Support: {branding.support_url ? <a href={branding.support_url} target="_blank" rel="noopener noreferrer">help</a> : null}
                {branding.support_email && <> <a href={`mailto:${branding.support_email}`}>{branding.support_email}</a></>}</div>
            )}
            {me.sign_in_methods?.microsoft && !me.microsoft_linked && <div style={{ marginTop: 8 }}><a href="#" onClick={(e) => { e.preventDefault(); linkMicrosoft() }}>Link Microsoft account</a></div>}
            <div style={{ marginTop: 8 }}><a href="#" onClick={(e) => { e.preventDefault(); logout() }}>Sign out</a></div>
          </div>
          <div className="legal-links"><a href="/privacy" target="_blank" rel="noopener">Privacy</a> · <a href="/terms" target="_blank" rel="noopener">Terms</a> · Unical Systems</div>
        </nav>
        <main className="main">
          <MonitorAlerts />
          {notice === 'microsoft_linked' && <div className="alert ok" role="status">Your Microsoft account is linked. You can now sign in with Microsoft.</div>}
          {status && !status.ai.available && (
            <div className="alert warn" role="status">
              {status.ai.configured
                ? <>Claude is temporarily unavailable ({status.ai.last_error || 'API error'}). AI generation and analysis are disabled; projects, runs and reports work normally.</>
                : <>Claude API is not configured. AI generation and analysis are disabled; everything else works normally.</>}
            </div>
          )}
          {status?.storage?.run_block_reason && (
            <div className="alert warn" role="status">{status.storage.run_block_reason}{has('tenant.storage') && <> — <NavLink to="/admin?tab=storage">open storage settings</NavLink></>}</div>
          )}
          {status?.storage?.consent_legacy && has('tenant.storage') && !status.storage.run_block_reason && (
            <div className="alert warn" role="status">Your organization's test evidence is stored on Unical-managed servers (as before release 1.3). Please confirm this or connect your own SharePoint / Google Drive — <NavLink to="/admin?tab=storage">review storage &amp; data residency</NavLink>.</div>
          )}
          {status && status.workers.length === 0 && (
            <div className="alert warn" role="status">No test worker is online. Queued runs will start when a worker connects.</div>
          )}
          <Routes>
            <Route path="/" element={<Navigate to="/projects" replace />} />
            <Route path="/projects" element={<Projects />} />
            <Route path="/projects/:pid" element={<ProjectPage />} />
            <Route path="/projects/:pid/cases/new" element={<CaseEditor />} />
            <Route path="/cases/:cid" element={<CaseEditor />} />
            <Route path="/runs/:rid" element={<RunPage />} />
            <Route path="/projects/:pid/import" element={<ImportWizard />} />
            <Route path="/test-runs/:tid" element={<TestRunPage />} />
            <Route path="/automation-jobs/:jid" element={<AutomationJobPage />} />
            <Route path="/api-suites/:sid" element={<ApiSuitePage />} />
            <Route path="/api-runs/:rid" element={<ApiRunPage />} />
            <Route path="/monitors/:mid" element={<MonitorPage />} />
            <Route path="/monitoring" element={<MonitoringOverview />} />
            <Route path="/mobile-cases/:cid" element={<MobileCasePage />} />
            <Route path="/mobile-runs/:gid" element={<MobileRunPage />} />
            <Route path="/mobile-sessions/:sid" element={<MobileSessionPage />} />
            <Route path="/my-privacy" element={<MyPrivacy />} />
            {showAdmin && <Route path="/admin" element={<Admin />} />}
            {me.is_platform_admin && <Route path="/platform" element={<Platform />} />}
            <Route path="*" element={<div className="empty">Page not found. <NavLink to="/projects">Go to projects</NavLink></div>} />
          </Routes>
          <LegalFooter />
        </main>
      </div>
    </AppCtx.Provider>
  )
}
