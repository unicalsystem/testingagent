import { useCallback, useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useNavigate } from 'react-router-dom'
import { api, type Me, type Role, type SystemStatus } from './api'
import { AppCtx } from './components/ui'
import Login, { ChangePassword } from './pages/Login'
import Projects from './pages/Projects'
import ProjectPage from './pages/Project'
import CaseEditor from './pages/CaseEditor'
import RunPage from './pages/Run'
import Admin from './pages/Admin'

const RANK: Record<Role, number> = { viewer: 0, tester: 1, admin: 2 }

function Logo() {
  return (
    <svg width="30" height="30" viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="7" fill="#2a63d4" />
      <path d="M8 17l5 5 11-12" fill="none" stroke="#7fe0b0" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

export default function App() {
  const [me, setMe] = useState<Me | null | undefined>(undefined)
  const [status, setStatus] = useState<SystemStatus | null>(null)
  const nav = useNavigate()

  const refreshStatus = useCallback(() => {
    api.get<SystemStatus>('/system/status').then(setStatus).catch(() => setStatus(null))
  }, [])

  useEffect(() => {
    api.get<Me>('/auth/me').then(setMe).catch(() => setMe(null))
    const out = () => setMe(null)
    window.addEventListener('tp:unauthorized', out)
    return () => window.removeEventListener('tp:unauthorized', out)
  }, [])

  useEffect(() => {
    if (!me) return
    refreshStatus()
    const t = setInterval(refreshStatus, 30000)
    return () => clearInterval(t)
  }, [me, refreshStatus])

  if (me === undefined) return null
  if (me === null) return <Login onLogin={setMe} />
  if (me.must_change_password) return <ChangePassword me={me} onDone={setMe} />

  const can = (r: Role) => RANK[me.role] >= RANK[r]
  const logout = async () => { await api.post('/auth/logout').catch(() => {}); setMe(null); nav('/') }

  return (
    <AppCtx.Provider value={{ me, status, can, refreshStatus }}>
      <div className="shell">
        <nav className="side" aria-label="Main">
          <div className="brand"><Logo /><div>TestPilot AI<small>Intelligent web testing</small></div></div>
          <NavLink to="/projects">Projects</NavLink>
          {can('admin') && <NavLink to="/admin">Administration</NavLink>}
          <div className="spacer" />
          <div className="who">
            <b>{me.display_name || me.username}</b>{me.role}
            <div style={{ marginTop: 8 }}><a href="#" onClick={(e) => { e.preventDefault(); logout() }}>Sign out</a></div>
          </div>
        </nav>
        <main className="main">
          {status && !status.ai.available && (
            <div className="alert warn" role="status">
              {status.ai.configured
                ? <>Claude is temporarily unavailable ({status.ai.last_error || 'API error'}). AI generation and analysis are disabled; projects, runs and reports work normally.</>
                : <>Claude API is not configured. AI generation and analysis are disabled; everything else works normally.</>}
            </div>
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
            {can('admin') && <Route path="/admin" element={<Admin />} />}
            <Route path="*" element={<div className="empty">Page not found. <a href="/projects">Go to projects</a></div>} />
          </Routes>
        </main>
      </div>
    </AppCtx.Provider>
  )
}
