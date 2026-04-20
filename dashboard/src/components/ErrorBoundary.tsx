import React from 'react'

interface Props {
  children: React.ReactNode
  /** "page" (default) — full-screen card with a reload button, for the app
   *  root. "inline" — compact card that keeps the surrounding layout alive,
   *  for per-message / per-widget isolation. */
  variant?: 'page' | 'inline'
  /** Shown in the inline card so the user knows what failed to render. */
  label?: string
}

interface State {
  error: Error | null
}

/**
 * Render-crash containment. React unmounts the ENTIRE tree when a render
 * throws and no boundary catches it — the user sees a blank page with only
 * the background color (exactly what a corrupt chat block or a bad WS event
 * used to cause). The fallback keeps the app usable AND shows the real error
 * text, so a screenshot of the card is enough to diagnose the crash.
 */
export default class ErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('ErrorBoundary caught render crash:', error, info.componentStack)
  }

  private reset = () => this.setState({ error: null })

  render() {
    const { error } = this.state
    if (!error) return this.props.children

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
