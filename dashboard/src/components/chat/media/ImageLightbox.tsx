import { useState, useEffect, useCallback, useRef } from 'react'
import { createPortal } from 'react-dom'
import { safeHref } from '../../../lib/safeUrl'
import { pushEscHandler } from '../../../lib/escStack'
import { useSwipeGesture } from '../../../hooks/useSwipeGesture'

export interface LightboxImage {
  /** Either url (external) or imageData+mimeType (base64) must be set. */
  url?: string
  imageData?: string
  mimeType?: string
  caption?: string
  attribution?: string
  /** Optional clickable destination — shows a "View source ↗" link in the lightbox footer. */
  linkUrl?: string
  /** Optional override for the download fetch; defaults to url. */
  downloadUrl?: string
}

interface Props {
  images: LightboxImage[]
  initialIndex?: number
  onClose: () => void
}

function imageSrc(img: LightboxImage): string {
  if (img.url) return img.url
  if (img.imageData) return `data:${img.mimeType || 'image/jpeg'};base64,${img.imageData}`
  return ''
}

// Map mime → file extension. A naive `mime.split('/')[1]` mangles compound
// subtypes (`image/svg+xml` → `svg+xml`) and gives `jpeg` instead of the
// conventional `jpg`, so download filenames come out wrong for SVG / HEIC.
const MIME_EXT: Record<string, string> = {
  'image/jpeg': 'jpg',
  'image/png': 'png',
  'image/gif': 'gif',
  'image/webp': 'webp',
  'image/svg+xml': 'svg',
  'image/heic': 'heic',
  'image/heif': 'heif',
  'image/avif': 'avif',
  'image/bmp': 'bmp',
  'image/tiff': 'tiff',
}

function downloadFilename(img: LightboxImage, index: number): string {
  const cap = img.caption || img.attribution || `image-${index + 1}`
  const mime = (img.mimeType || 'image/jpeg').toLowerCase()
  // Fallback strips any `+suffix` (e.g. svg+xml → svg) for unmapped mimes.
  const ext = MIME_EXT[mime] || mime.split('/')[1]?.replace(/\+.*$/, '') || 'jpg'
  return cap.replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 50) + '.' + ext
}

async function triggerDownload(img: LightboxImage, index: number) {
  const filename = downloadFilename(img, index)

  // Native Android: route base64 through the JS bridge if available.
  if (img.imageData) {
    const android = (window as any).Android
    if (android && typeof android.saveImageFromBase64 === 'function') {
      try {
        android.saveImageFromBase64(img.imageData, filename, img.mimeType || 'image/jpeg')
        return
      } catch {
        // Fall through to blob path
      }
    }
    const byteChars = atob(img.imageData)
    const byteArr = new Uint8Array(byteChars.length)
    for (let i = 0; i < byteChars.length; i++) byteArr[i] = byteChars.charCodeAt(i)
    const blob = new Blob([byteArr], { type: img.mimeType || 'image/jpeg' })
    const blobUrl = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = blobUrl
    link.download = filename
    link.click()
    setTimeout(() => URL.revokeObjectURL(blobUrl), 5000)
    return
  }

  // External URL: fetch → blob → download. Direct <a download> won't work
  // for cross-origin URLs without CORS, but fetch-as-blob does the trick
  // for image CDNs that allow CORS (Unsplash, Pexels, Google all do).
  const fetchUrl = img.downloadUrl || img.url
  if (!fetchUrl) return
  try {
    const resp = await fetch(fetchUrl)
    const blob = await resp.blob()
    const blobUrl = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = blobUrl
    link.download = filename
    link.click()
    setTimeout(() => URL.revokeObjectURL(blobUrl), 5000)
  } catch {
    // Fallback: open in new tab — user can right-click → save.
    window.open(fetchUrl, '_blank', 'noopener')
  }
}

export default function ImageLightbox({ images, initialIndex = 0, onClose }: Props) {
  const [idx, setIdx] = useState(initialIndex)
  const total = images.length
  const current = images[idx]
  const [imgDim, setImgDim] = useState<{ w: number; h: number } | null>(null)
  const imgRef = useRef<HTMLImageElement | null>(null)
  const outerRef = useRef<HTMLDivElement | null>(null)

  const goPrev = useCallback(() => setIdx((i) => (i - 1 + total) % total), [total])
  const goNext = useCallback(() => setIdx((i) => (i + 1) % total), [total])

  // Browser back-button → close the lightbox via the history-stack pattern.
  // Works on desktop and on the in-browser mobile site. (For the Capacitor
  // Android app, `MainActivity.OnBackPressedCallback` intercepts the back
  // gesture BEFORE it reaches popstate — see the global-flag effect below
  // for the Android-specific path.)
  useEffect(() => {
    const HISTORY_KEY = 'otoLightbox'
    window.history.pushState({ [HISTORY_KEY]: true }, '', window.location.href)
    const onPop = () => onClose()
    window.addEventListener('popstate', onPop)
    return () => {
      window.removeEventListener('popstate', onPop)
      const s = window.history.state
      if (s && typeof s === 'object' && (s as Record<string, unknown>)[HISTORY_KEY]) {
        window.history.back()
      }
    }
  }, [onClose])

  // Android back-gesture / back-button integration — same pattern as
  // WorkspaceOverlay's selection mode. MainActivity.handleBackAction's
  // injected JS checks these globals BEFORE deciding to minimize the app
  // (the chat-page default), and if it sees the lightbox active it calls
  // our close function and returns 'handled' so the app stays put.
  // Requires the matching MainActivity.java case (see
  // `MainActivity.handleBackAction` — the `__otodockLightboxActive` branch).
  useEffect(() => {
    const w = window as unknown as {
      __otodockLightboxActive?: boolean
      __otodockLightboxClose?: () => void
    }
    w.__otodockLightboxActive = true
    w.__otodockLightboxClose = () => onClose()
    return () => {
      w.__otodockLightboxActive = false
      delete w.__otodockLightboxClose
    }
  }, [onClose])

  useEffect(() => {
    const pop = pushEscHandler(onClose)
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      pop()
      document.body.style.overflow = prev
    }
  }, [onClose])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'ArrowLeft') goPrev()
      else if (e.key === 'ArrowRight') goNext()
    }
    if (total > 1) {
      window.addEventListener('keydown', onKey)
      return () => window.removeEventListener('keydown', onKey)
    }
  }, [goPrev, goNext, total])

  // Touch-swipe gesture inside the lightbox — mobile only (desktop has
  // arrow keys + the ‹ › buttons). Swipe-LEFT goes to next, swipe-RIGHT
  // goes to previous (matches the carousel scroll direction).
  useSwipeGesture(outerRef, {
    onSwipeLeft: total > 1 ? goNext : undefined,
    onSwipeRight: total > 1 ? goPrev : undefined,
    mobileOnly: true,
  })

  useEffect(() => {
    setImgDim(null)
  }, [idx])

  const onImgLoad = () => {
    const el = imgRef.current
    if (el) setImgDim({ w: el.naturalWidth, h: el.naturalHeight })
  }

  if (!current) return null

  return createPortal(
    // Three-row flex column with explicit slot sizing:
    //   header (top): shrink-0 fixed height — reserves room for the close
    //                 button + page counter without overlapping content
    //   main  (mid): flex-1 min-h-0 — image scales to fit this slot
    //   footer (bot): shrink-0 — caption + buttons always render in full
    // `h-dvh` is the modern "dynamic viewport height" (excludes the area
    // covered by OS gesture-bar / browser address-bar). On older browsers
    // `inset-0` provides the fallback dimension. `safe-area-inset-bottom`
    // adds the iOS home-indicator strip on top of our default footer pad.
    <div
      ref={outerRef}
      className="fixed inset-0 z-50 flex flex-col bg-black/80 backdrop-blur-xs overflow-hidden h-dvh"
      onClick={onClose}
      style={{
        paddingBottom: 'max(0.75rem, env(safe-area-inset-bottom))',
      }}
    >
      {/* Close button — pinned to the header row (z-10 so it sits above the image) */}
      <button
        onClick={onClose}
        className="absolute top-3 right-3 z-10 w-9 h-9 flex items-center justify-center rounded-full bg-white/10 hover:bg-white/20 text-white text-lg transition-colors"
        title="Close"
      >
        &times;
      </button>

      {/* Prev / next nav (only when gallery has >1 image) */}
      {total > 1 && (
        <>
          <button
            onClick={(e) => { e.stopPropagation(); goPrev() }}
            className="absolute left-2 sm:left-4 top-1/2 -translate-y-1/2 z-10 w-10 h-10 flex items-center justify-center rounded-full bg-white/10 hover:bg-white/20 text-white text-2xl transition-colors"
            title="Previous (←)"
          >
            ‹
          </button>
          <button
            onClick={(e) => { e.stopPropagation(); goNext() }}
            className="absolute right-2 sm:right-4 top-1/2 -translate-y-1/2 z-10 w-10 h-10 flex items-center justify-center rounded-full bg-white/10 hover:bg-white/20 text-white text-2xl transition-colors"
            title="Next (→)"
          >
            ›
          </button>
          <div className="absolute top-3 left-3 z-10 px-3 py-1 rounded-full bg-white/10 text-white text-xs">
            {idx + 1} / {total}
          </div>
        </>
      )}

      {/* Header row — fixed 56px to clear the close button + counter pill */}
      <div className="shrink-0 h-14" />

      {/* Main image row — fills middle; image constrained by max-h-full */}
      <div
        className="flex-1 min-h-0 flex items-center justify-center px-3"
        onClick={(e) => e.stopPropagation()}
      >
        <img
          ref={imgRef}
          src={imageSrc(current)}
          alt={current.caption || 'Image'}
          className="max-w-full max-h-full object-contain rounded-lg shadow-2xl"
          onLoad={onImgLoad}
        />
      </div>

      {/* Footer — shrink-0, always renders fully */}
      <div
        className="shrink-0 flex flex-col items-center gap-0.5 text-center text-white px-4 pt-3"
        onClick={(e) => e.stopPropagation()}
      >
        {current.caption && (
          <p className="text-sm text-white/90 line-clamp-2 max-w-2xl">{current.caption}</p>
        )}
        {current.attribution && (
          <p className="text-xs text-white/60 line-clamp-1 max-w-2xl">{current.attribution}</p>
        )}
        {imgDim && (
          <p className="text-[10px] text-white/40">{imgDim.w} × {imgDim.h}</p>
        )}
        <div className="mt-2 flex flex-wrap justify-center gap-2">
          <button
            onClick={(e) => { e.stopPropagation(); triggerDownload(current, idx) }}
            className="px-4 py-1.5 rounded-md bg-white/10 hover:bg-white/20 text-sm text-white transition-colors"
          >
            ⬇ Download
          </button>
          {current.linkUrl && (
            <a
              href={safeHref(current.linkUrl)}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="px-4 py-1.5 rounded-md bg-white/10 hover:bg-white/20 text-sm text-white transition-colors"
            >
              View source ↗
            </a>
          )}
        </div>
      </div>
    </div>,
    document.body,
  )
}

export { triggerDownload }
