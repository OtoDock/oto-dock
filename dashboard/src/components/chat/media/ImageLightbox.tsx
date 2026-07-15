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

// Zoom bounds for the pinch/wheel/double-tap gestures.
const ZOOM_MAX = 5
const ZOOM_DOUBLE_TAP = 2.5

interface View { z: number; x: number; y: number }

export default function ImageLightbox({ images, initialIndex = 0, onClose }: Props) {
  const [idx, setIdx] = useState(initialIndex)
  const total = images.length
  const current = images[idx]
  const [imgDim, setImgDim] = useState<{ w: number; h: number } | null>(null)
  const imgRef = useRef<HTMLImageElement | null>(null)
  const outerRef = useRef<HTMLDivElement | null>(null)
  const containRef = useRef<HTMLDivElement | null>(null)

  // Zoom/pan state. The transform is `translate(x,y) scale(z)` around the
  // image center (which coincides with the container center at rest), so x/y
  // are plain screen pixels. Gesture handlers read `viewRef` (never stale);
  // `smooth` animates programmatic jumps (double-tap) but not live gestures.
  const [view, setView] = useState<View>({ z: 1, x: 0, y: 0 })
  const viewRef = useRef(view)
  viewRef.current = view
  const [smooth, setSmooth] = useState(false)
  const [dragging, setDragging] = useState(false)
  const pointersRef = useRef(new Map<number, { x: number; y: number }>())
  const pinchRef = useRef<{ d: number; z: number; x: number; y: number; mx: number; my: number } | null>(null)
  const panRef = useRef<{ px: number; py: number; x: number; y: number } | null>(null)
  const tapRef = useRef({ t: 0, x: 0, y: 0 })

  const goPrev = useCallback(() => setIdx((i) => (i - 1 + total) % total), [total])
  const goNext = useCallback(() => setIdx((i) => (i + 1) % total), [total])

  /** Pointer position relative to the container CENTER (the transform origin). */
  const focal = useCallback((clientX: number, clientY: number) => {
    const rect = containRef.current?.getBoundingClientRect()
    if (!rect) return { x: 0, y: 0 }
    return { x: clientX - rect.left - rect.width / 2, y: clientY - rect.top - rect.height / 2 }
  }, [])

  /** Clamp zoom to bounds and pan so the image can't be pushed off-screen. */
  const applyView = useCallback((next: View, animate: boolean) => {
    const z = Math.min(ZOOM_MAX, Math.max(1, next.z))
    let x = 0
    let y = 0
    if (z > 1) {
      const img = imgRef.current
      const cont = containRef.current
      const maxX = img && cont ? Math.max(0, (img.offsetWidth * z - cont.clientWidth) / 2 + 24) : 0
      const maxY = img && cont ? Math.max(0, (img.offsetHeight * z - cont.clientHeight) / 2 + 24) : 0
      x = Math.min(maxX, Math.max(-maxX, next.x))
      y = Math.min(maxY, Math.max(-maxY, next.y))
    }
    setSmooth(animate)
    setView({ z, x, y })
  }, [])

  /** Double-tap / double-click: zoom in around the point, or reset. */
  const toggleZoomAt = useCallback((clientX: number, clientY: number) => {
    const v = viewRef.current
    if (v.z > 1) {
      applyView({ z: 1, x: 0, y: 0 }, true)
    } else {
      const f = focal(clientX, clientY)
      applyView({ z: ZOOM_DOUBLE_TAP, x: f.x * (1 - ZOOM_DOUBLE_TAP), y: f.y * (1 - ZOOM_DOUBLE_TAP) }, true)
    }
  }, [applyView, focal])

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    const cont = containRef.current
    if (!cont) return
    try { cont.setPointerCapture(e.pointerId) } catch { /* ignore */ }
    pointersRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY })
    if (pointersRef.current.size === 2) {
      const [p1, p2] = [...pointersRef.current.values()]
      const v = viewRef.current
      panRef.current = null
      pinchRef.current = {
        d: Math.hypot(p1.x - p2.x, p1.y - p2.y) || 1,
        z: v.z, x: v.x, y: v.y,
        mx: (p1.x + p2.x) / 2, my: (p1.y + p2.y) / 2,
      }
      return
    }
    if (e.pointerType === 'touch') {
      const now = Date.now()
      const lt = tapRef.current
      if (now - lt.t < 300 && Math.hypot(e.clientX - lt.x, e.clientY - lt.y) < 40) {
        tapRef.current = { t: 0, x: 0, y: 0 }
        e.preventDefault() // suppress the synthesized dblclick — it would re-toggle
        toggleZoomAt(e.clientX, e.clientY)
        return
      }
      tapRef.current = { t: now, x: e.clientX, y: e.clientY }
    }
    if (viewRef.current.z > 1) {
      panRef.current = { px: e.clientX, py: e.clientY, x: viewRef.current.x, y: viewRef.current.y }
      setDragging(true)
    }
  }, [toggleZoomAt])

  const onPointerMove = useCallback((e: React.PointerEvent) => {
    if (!pointersRef.current.has(e.pointerId)) return
    pointersRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY })
    const pinch = pinchRef.current
    if (pointersRef.current.size >= 2 && pinch) {
      const [p1, p2] = [...pointersRef.current.values()]
      const d = Math.hypot(p1.x - p2.x, p1.y - p2.y) || 1
      const z = Math.min(ZOOM_MAX, Math.max(1, pinch.z * (d / pinch.d)))
      // Keep the image point that started under the pinch midpoint pinned
      // under the CURRENT midpoint (covers two-finger drag too).
      const f0 = focal(pinch.mx, pinch.my)
      const m = focal((p1.x + p2.x) / 2, (p1.y + p2.y) / 2)
      applyView({
        z,
        x: m.x - ((f0.x - pinch.x) / pinch.z) * z,
        y: m.y - ((f0.y - pinch.y) / pinch.z) * z,
      }, false)
      return
    }
    const pan = panRef.current
    if (pan) {
      applyView({ z: viewRef.current.z, x: pan.x + (e.clientX - pan.px), y: pan.y + (e.clientY - pan.py) }, false)
    }
  }, [applyView, focal])

  const onPointerEnd = useCallback((e: React.PointerEvent) => {
    pointersRef.current.delete(e.pointerId)
    if (pointersRef.current.size < 2) pinchRef.current = null
    if (pointersRef.current.size === 1 && viewRef.current.z > 1) {
      const [p] = [...pointersRef.current.values()]
      panRef.current = { px: p.x, py: p.y, x: viewRef.current.x, y: viewRef.current.y }
    } else if (pointersRef.current.size === 0) {
      panRef.current = null
      setDragging(false)
    }
  }, [])

  // Wheel zoom needs a NON-passive listener (React's delegated onWheel is
  // passive, so preventDefault there is a no-op warning).
  useEffect(() => {
    const cont = containRef.current
    if (!cont) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const v = viewRef.current
      const z = Math.min(ZOOM_MAX, Math.max(1, v.z * (e.deltaY < 0 ? 1.25 : 0.8)))
      if (z === v.z) return
      const f = focal(e.clientX, e.clientY)
      applyView({ z, x: f.x - ((f.x - v.x) / v.z) * z, y: f.y - ((f.y - v.y) / v.z) * z }, false)
    }
    cont.addEventListener('wheel', onWheel, { passive: false })
    return () => cont.removeEventListener('wheel', onWheel)
  }, [applyView, focal])

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
  // goes to previous (matches the carousel scroll direction). Disabled
  // while zoomed — a one-finger PAN would otherwise switch images.
  useSwipeGesture(outerRef, {
    onSwipeLeft: total > 1 && view.z === 1 ? goNext : undefined,
    onSwipeRight: total > 1 && view.z === 1 ? goPrev : undefined,
    mobileOnly: true,
  })

  useEffect(() => {
    setImgDim(null)
    pointersRef.current.clear()
    pinchRef.current = null
    panRef.current = null
    setDragging(false)
    setSmooth(false)
    setView({ z: 1, x: 0, y: 0 })
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

      {/* Main image row — fills middle; image constrained by max-h-full.
          Owns the zoom gestures: pinch (touch), wheel (desktop), double-tap /
          double-click toggle, one-finger / mouse drag pan while zoomed.
          touch-none hands the browser's pinch/scroll defaults to us;
          overflow-hidden clips the scaled image at the row edges. */}
      <div
        ref={containRef}
        className="flex-1 min-h-0 flex items-center justify-center px-3 overflow-hidden touch-none"
        style={{ cursor: view.z > 1 ? (dragging ? 'grabbing' : 'grab') : 'zoom-in' }}
        onClick={(e) => e.stopPropagation()}
        onDoubleClick={(e) => toggleZoomAt(e.clientX, e.clientY)}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerEnd}
        onPointerCancel={onPointerEnd}
      >
        <img
          ref={imgRef}
          src={imageSrc(current)}
          alt={current.caption || 'Image'}
          draggable={false}
          className={`max-w-full max-h-full object-contain rounded-lg shadow-2xl select-none ${
            smooth ? 'transition-transform duration-200' : ''
          }`}
          style={{ transform: `translate3d(${view.x}px, ${view.y}px, 0) scale(${view.z})` }}
          onLoad={onImgLoad}
        />
      </div>

      {/* Footer — shrink-0, always renders fully */}
      <div
        className="shrink-0 flex flex-col items-center gap-0.5 text-center text-white px-4 pt-3"
        onClick={(e) => e.stopPropagation()}
      >
        {current.caption && (
          <p className="text-sm text-white/90 max-w-2xl max-h-24 overflow-y-auto">{current.caption}</p>
        )}
        {current.attribution && (
          <p className="text-xs text-white/60 line-clamp-1 max-w-2xl">{current.attribution}</p>
        )}
        {imgDim && (
          <p className="text-[10px] text-white/40">
            {imgDim.w} × {imgDim.h}{view.z > 1 ? ` · ${Math.round(view.z * 100)}%` : ''}
          </p>
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
