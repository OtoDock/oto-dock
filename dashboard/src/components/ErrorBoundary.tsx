import React from 'react'
import { pageBuildId } from '../lib/buildId'

interface Props {
  children: React.ReactNode
  /** "page" (default) — full-screen card with a reload button, for the app
   *  root. "inline" — compact card that keeps the surrounding layout alive,
   *  for per-message / per-widget isolation. "route" — one page's card in
   *  place of the page, with a crash report to copy; the layout around it
   *  (the admin sidebar, the header) stays. */
  variant?: 'page' | 'inline' | 'route'
  /** Shown in the inline card so the user knows what failed to render. */
  label?: string
  /** A caught error clears when this changes (a route boundary passes the
   *  path, so leaving the broken page shows the next one). Never a `key`:
   *  that would remount healthy children on every change. */
  resetKey?: string
}

interface State {
  error: Error | null
  at: string
  componentStack: string
  copied: '' | 'copied' | 'failed'
  /** The resetKey the state belongs to: a new one clears a caught error. */
  shownFor: string | undefined
}

interface CrashFacts {
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
    (err?.stack || '(none)').trim(),
    '',
    'Component stack:',
    (f.componentStack || '(none)').trim(),
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
  let ok = false
  try {
    ok = document.execCommand('copy')
  } catch {
    ok = false
  }
  ta.remove()
  return ok
}

/**
 * Render-crash containment. React unmounts the ENTIRE tree when a render
 * throws and no boundary catches it — the user sees a blank page with only
 * the background color (exactly what a corrupt chat block or a bad WS event
 * used to cause). The fallback keeps the app usable AND shows the real error
 * text, so a screenshot of the card is enough to diagnose the crash.
 */
export default class ErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null, at: '', componentStack: '', copied: '', shownFor: this.props.resetKey }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error, at: new Date().toISOString(), copied: '' }
  }

  // Cleared before the render, so a page left after a crash never paints
  // the stale card for a frame.
  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    if (props.resetKey === state.shownFor) return null
    return { shownFor: props.resetKey, error: null, componentStack: '', copied: '' }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('ErrorBoundary caught render crash:', error, info.componentStack)
    this.setState({ componentStack: info.componentStack || '' })
  }

  private reset = () => this.setState({ error: null, componentStack: '', copied: '' })

  private report = () => crashReport({
    error: this.state.error,
    componentStack: this.state.componentStack,
    at: this.state.at,
    path: window.location.pathname + window.location.search,
    build: pageBuildId(),
    userAgent: navigator.userAgent,
  })

  private copy = async () => {
    const ok = await copyText(this.report())
    this.setState({ copied: ok ? 'copied' : 'failed' })
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children

    if (this.props.variant === 'route') {
      const report = this.report()
      return (
        <div role="alert" className="mx-auto my-8 max-w-xl px-4" data-testid="route-crash">
          <div className="rounded-xl border border-p-border-light bg-white dark:bg-p-surface p-5 shadow-sm">
            <h2 className="text-base font-semibold text-p-text">This page hit an error</h2>
            <p className="mt-1.5 text-sm text-p-text-secondary">
              The rest of the dashboard still works. Copy the crash report for whoever looks
              after this install, then try again or reload.
            </p>
            <pre className="mt-3 p-3 rounded-lg bg-p-bg text-xs text-red-600 dark:text-red-400 overflow-x-auto whitespace-pre-wrap break-all">
              {String(error?.message || error)}
            </pre>
            <div className="mt-4 flex flex-wrap items-center gap-2">
              <button
                onClick={() => void this.copy()}
                className="px-3 py-1.5 rounded-lg bg-brand text-white text-sm font-medium hover:bg-brand-hover"
              >
                Copy crash report
              </button>
              <button
                onClick={this.reset}
                className="px-3 py-1.5 rounded-lg border border-p-border-light text-sm text-p-text-secondary hover:bg-p-surface-hover"
              >
                Try again
              </button>
              <button
                onClick={() => window.location.reload()}
                className="px-3 py-1.5 rounded-lg border border-p-border-light text-sm text-p-text-secondary hover:bg-p-surface-hover"
              >
                Reload
              </button>
              {this.state.copied === 'copied' && <span className="text-xs text-p-success">Copied</span>}
              {this.state.copied === 'failed' && (
                <span className="text-xs text-p-text-light">Copy refused here: select the report below.</span>
              )}
            </div>
            <details className="mt-3" open={this.state.copied === 'failed'}>
              <summary className="cursor-pointer text-xs text-p-text-light">Crash report</summary>
              <textarea
                readOnly
                value={report}
                aria-label="Crash report"
                onFocus={(e) => e.currentTarget.select()}
                className="mt-2 h-40 w-full rounded-lg border border-p-border-light bg-p-bg p-2 font-mono text-[11px] text-p-text-secondary"
              />
            </details>
          </div>
        </div>
      )
    }

    if (this.props.variant === 'inline') {
      return (
        <div className="my-2 px-3 py-2 rounded-lg bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-900/40 text-sm text-red-800 dark:text-red-300">
          <div className="font-medium">
            {this.props.label || 'This content'} failed to render
          </div>
          <div className="mt-1 text-xs opacity-80 font-mono break-all">
            {String(error?.message || error)}
          </div>
          <button
            onClick={this.reset}
            className="mt-2 text-xs underline opacity-90 hover:opacity-100"
          >
            Try again
          </button>
        </div>
      )
    }

    return (
      <div className="min-h-screen flex items-center justify-center bg-p-bg p-6">
        <div className="max-w-lg w-full rounded-xl border border-p-border-light bg-white dark:bg-p-surface p-6 shadow-sm">
          <h1 className="text-lg font-semibold text-p-text">Something went wrong</h1>
          <p className="mt-2 text-sm text-p-text-secondary">
            The dashboard hit an unexpected error while rendering. Your data is
            safe — reloading usually fixes it.
          </p>
          <pre className="mt-3 p-3 rounded-lg bg-p-bg text-xs text-red-600 dark:text-red-400 overflow-x-auto whitespace-pre-wrap break-all">
            {String(error?.message || error)}
          </pre>
          <div className="mt-4 flex gap-2">
            <button
              onClick={() => window.location.reload()}
              className="px-4 py-2 rounded-lg bg-brand text-white text-sm font-medium hover:opacity-90"
            >
              Reload
            </button>
            <button
              onClick={this.reset}
              className="px-4 py-2 rounded-lg border border-p-border-light text-sm text-p-text-secondary hover:bg-p-bg"
            >
              Dismiss
            </button>
          </div>
        </div>
      </div>
    )
  }
}
