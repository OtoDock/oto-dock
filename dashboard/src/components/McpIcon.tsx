/**
 * An MCP's icon: the image the proxy serves at `/v1/mcps/<name>/icon.png`
 * (the installed folder's icon.png, or the community catalog's), requested
 * only when the row says the route has one, with the first-letter tile
 * otherwise and as the fallback. Community icons are third-party marks
 * on a transparent canvas, so they sit on a white tile that keeps a black
 * mark visible on the dark theme; the platform family and the OtoDock mark
 * bring their own blue tile.
 */

import { useState } from 'react'

interface Props {
  name: string
  label: string
  category?: string
  /** Who wrote the server code (the catalog's `author`). OtoDock's own community
   *  entries carry the OtoDock mark, a full blue tile that needs no white frame. */
  author?: string
  /** The route answers an image: the row's `icon` flag (the installed folder's
   *  icon.png, or the catalog's when the proxy knows it), or a catalog entry's
   *  `icon_url` in the Browse dialog. Without it nothing is requested. */
  hasIcon?: boolean
  size?: 'sm' | 'md'
  className?: string
}

export default function McpIcon({ name, label, category, author, hasIcon, size = 'md', className = '' }: Props) {
  // The image that failed to load, by name: another name tries again.
  const [failedName, setFailedName] = useState<string | null>(null)
  const failed = failedName === name

  const community = category === 'community'
  // Third-party marks sit on a transparent canvas: a white tile keeps a black
  // mark visible on the dark theme. Our own mark and the bundled family bring
  // their own blue tile.
  const framed = community && author !== 'OtoDock'
  const box = size === 'sm' ? 'w-7 h-7 text-xs' : 'w-8 h-8 text-sm'

  if (!failed && hasIcon) {
    return (
      <span
        className={`${box} rounded-control shrink-0 overflow-hidden flex items-center justify-center ${
          // eslint-disable-next-line otodock/no-raw-palette -- a vendor's mark keeps its white tile in both themes
          framed ? 'bg-white ring-1 ring-border p-0.5' : ''
        } ${className}`}
      >
        {/* Eager on purpose: a lazy image below the fold never fires its error,
            so a row without an icon would sit on a blank tile until scrolled. */}
        <img
          src={`/v1/mcps/${encodeURIComponent(name)}/icon.png`}
          alt=""
          draggable={false}
          className="w-full h-full object-contain"
          onError={() => setFailedName(name)}
        />
      </span>
    )
  }

  const letter = (label || name).charAt(0).toUpperCase()
  return (
    <span className={`${box} rounded-control bg-surface-muted text-secondary flex items-center justify-center shrink-0 font-semibold ${className}`}>
      {letter}
    </span>
  )
}
