/** Tiny copy-to-clipboard button. Swaps to a green check + "Copied" for ~2s. */

import { useState } from 'react'

export function CopyButton({ text, className = '' }: { text: string; className?: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      type="button"
      onClick={() => {
        navigator.clipboard.writeText(text).then(
          () => { setCopied(true); setTimeout(() => setCopied(false), 2000) },
          () => {},
        )
      }}
      className={`inline-flex items-center gap-1 text-xs px-2 py-1 rounded-lg border border-p-border-light text-p-text-secondary hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors ${className}`}
    >
      {copied ? (
        <>
          <svg className="w-3 h-3 text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
          </svg>
          Copied
        </>
      ) : (
        'Copy'
      )}
    </button>
  )
}
