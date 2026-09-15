import { lazy, Suspense, useState, useCallback, type CSSProperties } from 'react'

// The highlighter (react-syntax-highlighter with every Prism grammar) loads
// on the first fenced block a chat renders; a chat without code never loads
// it. The fallback paints the same box the highlighter does, so the swap
// changes colours, never layout.
const CodeHighlighter = lazy(() => import('./CodeHighlighter'))

// Shared by the highlighter (its customStyle) and the fallback <pre>.
export const CODE_BODY_STYLE: CSSProperties = {
  margin: 0,
  borderRadius: 0,
  padding: '1rem',
  fontSize: '0.8125rem',
  lineHeight: '1.5',
}

// vscDarkPlus's <pre>: background, text colour, mono stack, scrolling.
const FALLBACK_STYLE: CSSProperties = {
  ...CODE_BODY_STYLE,
  background: '#1e1e1e',
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
      <code className="px-1.5 py-0.5 rounded-sm bg-gray-100 dark:bg-gray-800 text-gray-800 dark:text-gray-200 text-[0.85em] font-mono border border-gray-200 dark:border-gray-700">
        {children}
      </code>
    )
  }

  return (
    <div className="code-block-wrapper group/code relative rounded-lg overflow-hidden my-3">
      {/* Header bar */}
      <div className="flex items-center justify-between px-4 py-1.5 bg-[#2d2d2d] text-gray-400 text-xs">
        <span className="font-mono">{language || 'text'}</span>
        <button
          onClick={handleCopy}
          className="flex items-center gap-1.5 px-2 py-0.5 rounded-sm text-gray-400 hover:text-white hover:bg-[#404040] transition-colors"
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
