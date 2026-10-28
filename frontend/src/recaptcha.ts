// Google reCAPTCHA v3 client. Loaded only when the server says this browser must solve it
// (disabled, unprotected actions and trusted IPs never load Google's script).
declare global {
  interface Window { grecaptcha?: { ready: (cb: () => void) => void; execute: (key: string, o: { action: string }) => Promise<string> } }
}

let loading: Promise<void> | null = null
let loadedKey = ''

function load(siteKey: string): Promise<void> {
  if (loading && loadedKey === siteKey) return loading
  loadedKey = siteKey
  loading = new Promise((resolve, reject) => {
    const s = document.createElement('script')
    s.src = `https://www.google.com/recaptcha/api.js?render=${encodeURIComponent(siteKey)}`
    s.async = true
    s.onload = () => resolve()
    s.onerror = () => { loading = null; reject(new Error('Could not load the CAPTCHA. Check your connection and reload.')) }
    document.head.appendChild(s)
  })
  return loading
}

export interface CaptchaCfg { required: boolean; site_key: string; action: string }

/** Returns a fresh token for `action`, or undefined when no CAPTCHA is required. */
export async function captchaToken(cfg?: CaptchaCfg | null): Promise<string | undefined> {
  if (!cfg?.required || !cfg.site_key) return undefined
  await load(cfg.site_key)
  const g = window.grecaptcha
  if (!g) throw new Error('CAPTCHA did not initialise. Reload the page.')
  await new Promise<void>((r) => g.ready(r))
  return g.execute(cfg.site_key, { action: cfg.action })
}

/** Preload the script so the first submit is fast. */
export function preloadCaptcha(cfg?: CaptchaCfg | null) { if (cfg?.required && cfg.site_key) load(cfg.site_key).catch(() => {}) }
