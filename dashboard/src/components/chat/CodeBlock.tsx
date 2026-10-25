import { lazy, Suspense, useState, useCallback, type CSSProperties } from 'react'
import { CODE_BODY_STYLE } from '../../lib/chat/codeStyle'

// The highlighter (react-syntax-highlighter with every Prism grammar) loads
// on the first fenced block a chat renders; a chat without code never loads
// it. The fallback paints the same box the highlighter does, so the swap
// changes colours, never layout.
const CodeHighlighter = lazy(() => import('./CodeHighlighter'))

// vscDarkPlus's <pre>: background, text colour, mono stack, scrolling.
const FALLBACK_STYLE: CSSProperties = {
  ...CODE_BODY_STYLE,
  // eslint-disable-next-line otodock/no-raw-palette -- the syntax theme's own colours
  background: '#1e1e1e',
  // eslint-disable-next-line otodock/no-raw-palette -- the syntax theme's own colours
  color: '#d4d4d4',
  fontFamily: 'Menlo, Monaco, Consolas, "Andale Mono", "Ubuntu Mono", "Courier New", monospace',
  whiteSpace: 'pre',
  overflow: 'auto',
}

interface Props {
  language?: string
  children: string
  inline?: boolean
}

export default function CodeBlock({ language, children, inline }: Props) {
  const [copied, setCopied] = useState(false)

  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(children).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }, [children])

  // Inline code
  if (inline) {
    return (
      <code className="px-1.5 py-0.5 rounded-sm bg-surface-muted text-primary text-[0.85em] font-mono">
        {children}
      </code>
    )
  }

  return (
    <div className="code-block-wrapper group/code relative rounded-card overflow-hidden my-3 border border-code-border">
      {/* Header bar */}
      <div className="flex items-center justify-between px-4 py-1.5 bg-code-bg border-b border-code-fg/10 text-code-fg/70 text-xs">
        <span className="font-mono">{language || 'text'}</span>
        <button
          onClick={handleCopy}
          className="flex items-center gap-1.5 px-2 py-0.5 rounded-sm text-code-fg/70 hover:text-code-fg hover:bg-code-fg/10 transition-colors"
        >
          {copied ? (
            <>
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
              </svg>
              Copied
            </>
          ) : (
            <>
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
              </svg>
              Copy
            </>
          )}
        </button>
      </div>
      {/* Code content: plain text at once, colours when the highlighter lands */}
      <Suspense fallback={<pre style={FALLBACK_STYLE}><code>{children}</code></pre>}>
        <CodeHighlighter language={language || 'text'}>{children}</CodeHighlighter>
      </Suspense>
    </div>
  )
}
