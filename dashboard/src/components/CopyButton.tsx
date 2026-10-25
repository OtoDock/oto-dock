/** Tiny copy-to-clipboard button. Swaps to a green check + "Copied" for ~2s. */

import { useState } from 'react'
import { BUTTON_BASE, BUTTON_SIZE, BUTTON_VARIANT } from './ui/classes'

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
      className={`${BUTTON_BASE} ${BUTTON_VARIANT.secondary} ${BUTTON_SIZE.xs} ${className}`}
    >
      {copied ? (
        <>
          <svg className="w-3 h-3 text-status-success-fg" fill="none" stroke="currentColor" viewBox="0 0 24 24">
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
