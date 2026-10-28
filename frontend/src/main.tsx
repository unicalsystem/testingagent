import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import Landing from './pages/Landing'
import LegalPage from './pages/Legal'
import { DEFAULT_TENANT, setTenant } from './api'
import './styles.css'

// URL layout:
//   /                      public landing page and organization registration
//   /privacy, /terms       public legal pages (reserved slugs, never an organization)
//   /<tenant-slug>/...     an organization's app (branded sign-in, projects, runs, administration)
//   /projects, /cases/..., /runs/..., /admin   release-1.0 links -> /<default tenant>/...
const LEGACY = new Set(['projects', 'cases', 'runs', 'admin'])
const SLUG = /^[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])$/

const first = window.location.pathname.split('/')[1] ?? ''
let root
if (LEGACY.has(first)) {
  window.location.replace(`/${DEFAULT_TENANT}${window.location.pathname}${window.location.search}${window.location.hash}`)
} else if (first === 'privacy' || first === 'terms') {
  root = <LegalPage doc={first} />
} else if (first === '' || first === 'register' || !SLUG.test(first)) {
  root = <Landing />
} else {
  setTenant(first)
  root = <BrowserRouter basename={`/${first}`}><App slug={first} /></BrowserRouter>
}

if (root) createRoot(document.getElementById('root')!).render(<StrictMode>{root}</StrictMode>)
