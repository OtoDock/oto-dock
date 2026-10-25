// The route boundary's crash report and the copy that puts it on the
// clipboard.

export interface CrashFacts {
  error: unknown
  componentStack: string
  at: string
  path: string
  build: string
  userAgent: string
}

/** The text "Copy crash report" puts on the clipboard: enough to find the
 * line that threw without a screenshot or a console. */
export function crashReport(f: CrashFacts): string {
  const err = f.error instanceof Error ? f.error : null
  return [
    'OtoDock dashboard crash report',
    `Time: ${f.at}`,
    `Page: ${f.path}`,
    `Build: ${f.build || 'unknown'}`,
    `Browser: ${f.userAgent}`,
    `Error: ${err ? `${err.name}: ${err.message}` : String(f.error)}`,
    '',
    'Stack:',
    (err?.stack || 'None').trim(),
    '',
    'Component stack:',
    (f.componentStack || 'None').trim(),
  ].join('\n')
}

/** The clipboard where the browser offers it (a secure origin), else a
 * selected textarea and the copy command (a plain-http install). */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (window.isSecureContext && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // Refused (no focus, no permission): the fallback below.
  }
  const ta = document.createElement('textarea')
  ta.value = text
  ta.setAttribute('readonly', '')
  ta.style.position = 'fixed'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  ta.select()
  let ok: boolean
  try {
    ok = document.execCommand('copy')
  } catch {
    ok = false
  }
  ta.remove()
  return ok
}
