import { useEffect, useRef, useState } from 'react'
import { NavLink } from 'react-router-dom'
import type { Me } from '../api'

export function initials(me: Me): string {
  const n = (me.display_name || me.username).trim()
  const parts = n.split(/[\s._@-]+/).filter(Boolean)
  return ((parts[0]?.[0] ?? '?') + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase()
}

export function Avatar({ me, size = 32 }: { me: Me; size?: number }) {
  return <span aria-hidden="true" style={{ width: size, height: size, borderRadius: '50%', background: 'var(--accent)', color: '#fff', display: 'inline-flex',
    alignItems: 'center', justifyContent: 'center', fontWeight: 700, fontSize: size * 0.4, flex: 'none', letterSpacing: '.02em' }}>{initials(me)}</span>
}

/** Top-right account menu: who is signed in, profile, billing, privacy, sign out. */
export function UserMenu({ me, showBilling, onLinkMicrosoft, onLogout }: { me: Me; showBilling: boolean; onLinkMicrosoft?: () => void; onLogout: () => void }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const click = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false) }
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', click); document.addEventListener('keydown', key)
    return () => { document.removeEventListener('mousedown', click); document.removeEventListener('keydown', key) }
  }, [open])
  const close = () => setOpen(false)
  return (
    <div className="usermenu" ref={ref}>
      <button className="usermenu-btn" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(!open)} title="Your account">
        <Avatar me={me} />
        <span className="usermenu-name"><b>{me.display_name || me.username}</b><small>{me.role_label}{me.tenant ? ` · ${me.tenant.name}` : ''}</small></span>
        <span aria-hidden="true" style={{ fontSize: 10, opacity: .7 }}>▾</span>
      </button>
      {open && (
        <div className="usermenu-pop" role="menu">
          <div className="usermenu-head">
            <Avatar me={me} size={40} />
            <div style={{ minWidth: 0 }}><b>{me.display_name || me.username}</b>
              <div className="small muted" style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{me.email || `@${me.username}`}</div>
              <div className="small muted">{me.role_label}</div></div>
          </div>
          <NavLink role="menuitem" to="/profile" onClick={close}>My profile</NavLink>
          <NavLink role="menuitem" to="/profile?tab=password" onClick={close}>Change password</NavLink>
          {showBilling && <NavLink role="menuitem" to="/billing" onClick={close}>Plans &amp; billing</NavLink>}
          <NavLink role="menuitem" to="/my-privacy" onClick={close}>Privacy &amp; my data</NavLink>
          {onLinkMicrosoft && <a role="menuitem" href="#" onClick={(e) => { e.preventDefault(); close(); onLinkMicrosoft() }}>Link Microsoft account</a>}
          <a role="menuitem" href="#" className="danger" onClick={(e) => { e.preventDefault(); close(); onLogout() }}>Sign out</a>
        </div>
      )}
    </div>
  )
}
