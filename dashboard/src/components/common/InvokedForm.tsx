import { useRef, type ReactNode, type RefObject } from 'react'

import { useInvokedForm } from './useInvokedForm'

/**
 * A wrapper for a form that opens where it was invoked, when the form is not
 * a component of its own: mounted only while open, it gives the form the
 * reveal, the Escape and the focus return of `useInvokedForm`. The children
 * get `close`, the way their Cancel closes the form and gives the focus back.
 */
export default function InvokedForm({ onCancel, fallback, className, children }: {
  onCancel: () => void
  fallback?: RefObject<HTMLElement | null>
  className?: string
  children: (close: (setClosed: () => void) => void) => ReactNode
}) {
  const formRef = useRef<HTMLDivElement>(null)
  const { close, onKeyDown } = useInvokedForm(formRef, fallback)
  return (
    <div ref={formRef} onKeyDown={(e) => onKeyDown(e, onCancel)} className={className}>
      {children(close)}
    </div>
  )
}
