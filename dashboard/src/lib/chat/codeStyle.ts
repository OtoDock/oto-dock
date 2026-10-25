// The code body's box, shared by the lazy highlighter and its fallback so the
// swap changes colours, never layout.

import type { CSSProperties } from 'react'

// Shared by the highlighter (its customStyle) and the fallback <pre>.
export const CODE_BODY_STYLE: CSSProperties = {
  margin: 0,
  borderRadius: 0,
  padding: '1rem',
  fontSize: '0.8125rem',
  lineHeight: '1.5',
}
