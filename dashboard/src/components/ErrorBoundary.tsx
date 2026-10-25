import React from 'react'
import { pageBuildId } from '../lib/buildId'
import { crashReport, copyText } from '../lib/crash/crashReport'
import Button from './ui/Button'
import { FIELD, FIELD_REGULAR, FIELD_BORDER, CARD } from './ui/classes'

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
          <div className={`${CARD} p-5`}>
            <h2 className="text-base font-semibold text-primary">This page hit an error</h2>
            <p className="mt-1.5 text-sm text-secondary">
              The rest of the dashboard still works. Copy the crash report for whoever looks
              after this install, then try again or reload.
            </p>
            <pre className="mt-3 p-3 rounded-card bg-status-danger-bg text-xs text-status-danger-fg overflow-x-auto whitespace-pre-wrap break-all">
              {String(error?.message || error)}
            </pre>
            <div className="mt-4 flex flex-wrap items-center gap-2">
              <Button variant="primary" size="sm"
                onClick={() => void this.copy()}
              >
                Copy crash report
              </Button>
              <Button variant="secondary" size="sm"
                onClick={this.reset}
              >
                Try again
              </Button>
              <Button variant="secondary" size="sm"
                onClick={() => window.location.reload()}
              >
                Reload
              </Button>
              {this.state.copied === 'copied' && <span className="text-xs text-status-success-fg">Copied</span>}
              {this.state.copied === 'failed' && (
                <span className="text-xs text-tertiary">Copy refused here: select the report below.</span>
              )}
            </div>
            <details className="mt-3" open={this.state.copied === 'failed'}>
              <summary className="cursor-pointer text-xs text-tertiary">Crash report</summary>
              <textarea
                readOnly
                value={report}
                aria-label="Crash report"
                onFocus={(e) => e.currentTarget.select()}
                className={`${FIELD} ${FIELD_REGULAR} ${FIELD_BORDER} mt-2 h-40 font-mono`}
              />
            </details>
          </div>
        </div>
      )
    }

    if (this.props.variant === 'inline') {
      return (
        <div className="my-2 px-3 py-2 rounded-card bg-status-danger-bg border border-status-danger-border text-sm text-status-danger-fg">
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
      <div className="min-h-screen flex items-center justify-center bg-surface-page p-6">
        <div className={`${CARD} max-w-lg w-full p-6`}>
          <h1 className="font-display text-lg font-semibold text-primary">Something went wrong</h1>
          <p className="mt-2 text-sm text-secondary">
            The dashboard hit an unexpected error. Your data is safe.
            Reloading usually fixes it.
          </p>
          <pre className="mt-3 p-3 rounded-card bg-status-danger-bg text-xs text-status-danger-fg overflow-x-auto whitespace-pre-wrap break-all">
            {String(error?.message || error)}
          </pre>
          <div className="mt-4 flex gap-2">
            <Button variant="primary" size="md"
              onClick={() => window.location.reload()}
            >
              Reload
            </Button>
            <Button variant="secondary" size="md"
              onClick={this.reset}
            >
              Dismiss
            </Button>
          </div>
        </div>
      </div>
    )
  }
}
