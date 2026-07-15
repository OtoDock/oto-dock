import { useCallback, useEffect, useRef, useState } from 'react'
import { safeHref } from '../../../lib/safeUrl'

import { ensureMediaDownloadName } from '../../../lib/fileTypes'
import { useCoarsePointer } from '../../../hooks/useCoarsePointer'

interface Props {
  /** Final playable URL: a web URL or /v1/media/{token}. */
  src: string
  mime?: string
  poster?: string
  caption?: string
  title?: string
  /** Filename used for the download button (Android DownloadListener reads ?fn=). */
  downloadName?: string
  /** Show the download button. */
  canDownload?: boolean
}

// Shared with AudioPlayer via the same keys → one remembered volume/mute.
const VOL_KEY = 'oto.media.volume'
const MUTED_KEY = 'oto.media.muted'

function loadVolume(): { volume: number; muted: boolean } {
  try {
    const v = parseFloat(localStorage.getItem(VOL_KEY) || '')
    return {
      volume: isFinite(v) && v >= 0 && v <= 1 ? v : 1,
      muted: localStorage.getItem(MUTED_KEY) === '1',
    }
  } catch {
    return { volume: 1, muted: false }
  }
}

function saveVolume(volume: number, muted: boolean) {
  try {
    localStorage.setItem(VOL_KEY, String(volume))
    localStorage.setItem(MUTED_KEY, muted ? '1' : '0')
  } catch {
    /* private mode / storage disabled — fine */
  }
}

function fmtTime(s: number): string {
  if (!isFinite(s) || s < 0) s = 0
  const m = Math.floor(s / 60)
  const sec = Math.floor(s % 60)
  return `${m}:${sec.toString().padStart(2, '0')}`
}

function buildDownloadHref(src: string, name: string): string {
  if (!src) return ''
  if (src.includes('/v1/media/')) {
    const sep = src.includes('?') ? '&' : '?'
    return `${src}${sep}download=1&fn=${encodeURIComponent(name || 'video')}`
  }
  return src
}

/** Map a YouTube/Vimeo watch or share URL to its embed URL, or null for
 *  direct file/CDN URLs (those play in <video>). */
function toEmbedUrl(src: string): string | null {
  if (!src) return null
  try {
    const u = new URL(src)
    const host = u.hostname.replace(/^www\./, '').replace(/^m\./, '')
    if (host === 'youtube.com' || host === 'youtube-nocookie.com') {
      if (u.pathname === '/watch') {
        const v = u.searchParams.get('v')
        if (v) return `https://www.youtube.com/embed/${v}`
      }
      const shorts = u.pathname.match(/^\/shorts\/([\w-]+)/)
      if (shorts) return `https://www.youtube.com/embed/${shorts[1]}`
      if (/^\/embed\//.test(u.pathname)) return src
    }
    if (host === 'youtu.be') {
      const id = u.pathname.slice(1).split('/')[0]
      if (id) return `https://www.youtube.com/embed/${id}`
    }
    if (host === 'vimeo.com') {
      const id = u.pathname.split('/').filter(Boolean)[0]
      if (id && /^\d+$/.test(id)) return `https://player.vimeo.com/video/${id}`
    }
    if (host === 'player.vimeo.com') return src
  } catch {
    return null
  }
  return null
}

const PIP_SUPPORTED =
  typeof document !== 'undefined' && !!(document as any).pictureInPictureEnabled

/**
 * Follow the PHYSICAL device orientation during web video fullscreen.
 * The initial landscape lock is a cue, not a pin: devicemotion gravity tells
 * us how the phone is actually held even while the screen orientation is
 * locked, and we re-lock to match once the hold is steady. Re-locking (vs
 * unlock()) keeps rotation working when the OS auto-rotate toggle is off,
 * where unlock() would snap back to the default orientation instead.
 * No-ops silently where motion sensors are unavailable (desktop, http).
 */
function startOrientationFollow(initial: 'landscape' | 'portrait'): () => void {
  let current = initial
  let pendingSince = 0
  const onMotion = (e: DeviceMotionEvent) => {
    const g = e.accelerationIncludingGravity
    const gx = g?.x ?? 0
    const gy = g?.y ?? 0
    if (gx * gx + gy * gy < 25) return // flat on a table — no usable signal
    // 0° = upright portrait, 90° = on its side (either way), 180° = upside down.
    const angle = Math.abs(Math.atan2(gx, gy) * 180 / Math.PI)
    const held: 'landscape' | 'portrait' | null =
      angle > 55 && angle < 125 ? 'landscape' : angle < 35 ? 'portrait' : null
    if (!held || held === current) { pendingSince = 0; return }
    const now = Date.now()
    if (!pendingSince) { pendingSince = now; return }
    if (now - pendingSince < 500) return // debounce a transient tilt
    pendingSince = 0
    current = held
    try { (screen.orientation as any).lock(held).catch(() => {}) } catch { /* unsupported */ }
  }
  window.addEventListener('devicemotion', onMotion)
  return () => window.removeEventListener('devicemotion', onMotion)
}

export default function VideoPlayer({
  src, mime, poster, caption, title, downloadName, canDownload = true,
}: Props) {
  const coarse = useCoarsePointer()
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const hideTimer = useRef<number | null>(null)
  const overControls = useRef(false)
  const lastTap = useRef(0)

  const [playing, setPlaying] = useState(false)
  const [ended, setEnded] = useState(false)
  const [loading, setLoading] = useState(false)
  const [cur, setCur] = useState(0)
  const [dur, setDur] = useState(0)
  const [buffered, setBuffered] = useState(0)
  const [volume, setVolume] = useState(1)
  const [muted, setMuted] = useState(false)
  const [fs, setFs] = useState(false)
  const [ratio, setRatio] = useState('16 / 9')
  const [error, setError] = useState(false)
  const [offline, setOffline] = useState(false)
  const [showControls, setShowControls] = useState(true)
  const [seekFlash, setSeekFlash] = useState<'fwd' | 'back' | null>(null)

  // Restore remembered volume/mute once the element exists.
  useEffect(() => {
    const { volume: v, muted: m } = loadVolume()
    setVolume(v)
    setMuted(m)
    const el = videoRef.current
    if (el) { el.volume = v; el.muted = m }
  }, [])

  const applyVolume = useCallback((v: number, m: boolean) => {
    const el = videoRef.current
    if (el) { el.volume = v; el.muted = m }
    setVolume(v)
    setMuted(m)
    saveVolume(v, m)
  }, [])

  // --- auto-hide controls (YouTube-style) ---
  const clearHide = () => {
    if (hideTimer.current) { window.clearTimeout(hideTimer.current); hideTimer.current = null }
  }
  const scheduleHide = useCallback(() => {
    clearHide()
    hideTimer.current = window.setTimeout(() => {
      const v = videoRef.current
      if (v && !v.paused && !overControls.current) setShowControls(false)
    }, 2500)
  }, [])
  const wake = useCallback(() => {
    setShowControls(true)
    scheduleHide()
  }, [scheduleHide])
  useEffect(() => () => clearHide(), [])

  const togglePlay = useCallback(() => {
    const v = videoRef.current
    if (!v) return
    if (v.paused) { setEnded(false); v.play().catch(() => {}) } else v.pause()
  }, [])

  const seekBy = useCallback((delta: number) => {
    const v = videoRef.current
    if (!v || !isFinite(v.duration)) return
    v.currentTime = Math.max(0, Math.min(v.duration, v.currentTime + delta))
    setCur(v.currentTime)
    setSeekFlash(delta >= 0 ? 'fwd' : 'back')
    window.setTimeout(() => setSeekFlash(null), 500)
  }, [])

  const onSeek = (e: React.ChangeEvent<HTMLInputElement>) => {
    const v = videoRef.current
    if (!v) return
    v.currentTime = Number(e.target.value)
    setCur(v.currentTime)
    wake()
  }

  const onVolumeInput = (e: React.ChangeEvent<HTMLInputElement>) => {
    const val = Number(e.target.value)
    applyVolume(val, val === 0)
    wake()
  }

  const toggleMute = () => {
    const nextMuted = !(muted || volume === 0)
    const nextVol = !nextMuted && volume === 0 ? 0.5 : volume
    applyVolume(nextVol, nextMuted)
    wake()
  }

  const toggleFs = () => {
    const el = wrapRef.current
    if (!el) return
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {})
    else el.requestFullscreen?.().catch(() => {})
  }

  const togglePip = async () => {
    const v = videoRef.current
    if (!v) return
    try {
      if ((document as any).pictureInPictureElement) await (document as any).exitPictureInPicture()
      else await (v as any).requestPictureInPicture()
    } catch {
      /* user gesture / not allowed — ignore */
    }
  }

  useEffect(() => {
    const onFsChange = () => setFs(!!document.fullscreenElement)
    document.addEventListener('fullscreenchange', onFsChange)
    return () => document.removeEventListener('fullscreenchange', onFsChange)
  }, [])

  // Fullscreen side-effects: native edge-to-edge + immersive (Android app),
  // Android/web Back closes fullscreen (not the app), and an initial landscape
  // rotation for wide videos on mobile — a CUE, not a pin: the physical
  // orientation is followed afterwards (natively in the app, via
  // startOrientationFollow on the web) so the user can turn the phone back to
  // portrait while staying fullscreen. All guards no-op on desktop / plain web.
  useEffect(() => {
    if (!fs) return
    const w = window as any
    const v = videoRef.current
    // videoWidth is 0 until metadata loads — never treat that as "wide" (it
    // used to rotate PORTRAIT videos to landscape when fullscreen came fast).
    const landscape = coarse && !!v && v.videoWidth > 0 && v.videoWidth >= v.videoHeight
    const close = () => { if (document.fullscreenElement) document.exitFullscreen().catch(() => {}) }
    const native = !!w.Android?.enterVideoFullscreen
    try { w.Android?.enterVideoFullscreen?.(landscape) } catch { /* no native bridge */ }
    w.__otodockVideoFullscreenActive = true       // MainActivity.handleBackAction checks this
    w.__otodockVideoFullscreenClose = close
    let stopFollow: (() => void) | null = null
    if (!native && coarse && (screen.orientation as any)?.lock) {
      if (landscape) { try { (screen.orientation as any).lock('landscape').catch?.(() => {}) } catch { /* unsupported */ } }
      stopFollow = startOrientationFollow(landscape ? 'landscape' : 'portrait')
    }
    let pushed = false
    try { history.pushState({ otoVideoFs: true }, ''); pushed = true } catch { /* ignore */ }
    const onPop = () => close()                   // web Back button closes fullscreen
    window.addEventListener('popstate', onPop)
    return () => {
      window.removeEventListener('popstate', onPop)
      stopFollow?.()
      try { w.Android?.exitVideoFullscreen?.() } catch { /* ignore */ }
      if (w.__otodockVideoFullscreenActive) {
        w.__otodockVideoFullscreenActive = false
        w.__otodockVideoFullscreenClose = undefined
      }
      try { (screen.orientation as any)?.unlock?.() } catch { /* ignore */ }
      // Remove our dummy entry only if we exited via the FS API (not popstate,
      // which already consumed it) — mirrors ImageLightbox's guard.
      if (pushed && (history.state as any)?.otoVideoFs) { try { history.back() } catch { /* ignore */ } }
    }
  }, [fs, coarse])

  // Re-arm auto-hide whenever play-state or fullscreen changes — fixes the
  // control bar staying visible after exiting fullscreen (it only re-hid on the
  // next tap/mouse-move before).
  useEffect(() => {
    if (!playing) { clearHide(); setShowControls(true); return }
    scheduleHide()
  }, [playing, fs])

  const onTime = () => {
    const v = videoRef.current
    if (!v) return
    setCur(v.currentTime)
    if (v.buffered.length) setBuffered(v.buffered.end(v.buffered.length - 1))
  }
  const onLoaded = () => {
    const v = videoRef.current
    if (!v) return
    setDur(v.duration || 0)
    if (v.videoWidth && v.videoHeight) setRatio(`${v.videoWidth} / ${v.videoHeight}`)
    v.volume = volume
    v.muted = muted
  }

  const onVideoError = () => {
    setError(true)
    // Distinguish "remote machine offline" (proxy 503) from a codec failure so
    // the message can tell the user to reconnect rather than download.
    if (src.includes('/v1/media/')) {
      fetch(src, { method: 'GET', headers: { Range: 'bytes=0-0' } })
        .then((r) => { if (r.status === 503) setOffline(true) })
        .catch(() => {})
    }
  }

  const retry = () => {
    setError(false)
    setOffline(false)
    videoRef.current?.load()
    videoRef.current?.play().catch(() => {})
  }

  const onKeyDown = (e: React.KeyboardEvent) => {
    if ((e.target as HTMLElement).tagName === 'INPUT') return
    let handled = true
    switch (e.key) {
      case ' ':
      case 'k': togglePlay(); break
      case 'ArrowLeft': seekBy(-5); break
      case 'ArrowRight': seekBy(5); break
      case 'ArrowUp': applyVolume(Math.min(1, +(volume + 0.1).toFixed(2)), false); break
      case 'ArrowDown': { const nv = Math.max(0, +(volume - 0.1).toFixed(2)); applyVolume(nv, nv === 0); break }
      case 'm': toggleMute(); break
      case 'f': toggleFs(); break
      default:
        if (/^[0-9]$/.test(e.key) && videoRef.current && isFinite(dur)) {
          videoRef.current.currentTime = dur * (Number(e.key) / 10)
          setCur(videoRef.current.currentTime)
        } else handled = false
    }
    if (handled) { e.preventDefault(); wake() }
  }

  // Tap on the video surface: desktop = play/pause; mobile = toggle controls,
  // double-tap left/right thirds = ∓10s seek, center = play/pause.
  const onSurfaceClick = (e: React.MouseEvent) => {
    if (!coarse) { togglePlay(); return }   // desktop: click anywhere = play/pause
    const now = Date.now()
    const rect = wrapRef.current?.getBoundingClientRect()
    const x = rect ? (e.clientX - rect.left) / rect.width : 0.5
    if (now - lastTap.current < 300) {       // double-tap: ∓10s on sides, toggle in center
      lastTap.current = 0
      if (x < 0.35) seekBy(-10)
      else if (x > 0.65) seekBy(10)
      else togglePlay()
      return
    }
    lastTap.current = now
    // Mobile single tap: paused → start from anywhere; playing → just toggle the
    // controls (pausing is only via the center button).
    if (videoRef.current?.paused) togglePlay()
    else setShowControls((s) => { if (!s) scheduleHide(); return !s })
  }

  const dlName = ensureMediaDownloadName(downloadName || caption || 'video', mime, src)
  const downloadHref = buildDownloadHref(src, dlName)

  // YouTube / Vimeo links can't play in <video> — embed them in an iframe
  // (their own player + controls). Direct file/CDN URLs fall through to <video>.
  const embedUrl = toEmbedUrl(src)
  if (embedUrl) {
    return (
      <div className="my-2 max-w-2xl">
        {title && <div className="mb-1 text-sm font-medium text-p-text">{title}</div>}
        <div className="relative overflow-hidden rounded-xl bg-black" style={{ aspectRatio: '16 / 9' }}>
          <iframe
            src={embedUrl}
            title={title || caption || 'video'}
            className="absolute inset-0 h-full w-full"
            allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
            allowFullScreen
          />
        </div>
        {caption && <div className="mt-1 text-xs text-p-text-secondary">{caption}</div>}
      </div>
    )
  }

  if (error) {
    return (
      <div className="my-2 max-w-md rounded-xl border border-p-border-light bg-p-surface/50 p-4 text-sm text-p-text-secondary">
        {offline
          ? 'This video is on the remote machine, which is offline. Reconnect it and try again.'
          : "Can't play this video in your browser."}{' '}
        <button onClick={retry} className="text-p-accent-red underline">Retry</button>
        {!offline && canDownload && downloadHref && (
          <>
            {' · '}
            <a href={safeHref(downloadHref)} download={dlName} className="text-p-accent-red underline">Download</a>
          </>
        )}
      </div>
    )
  }

  const controlsVisible = showControls || !playing || ended || loading
  // Center button shows whenever controls are visible (YouTube-style): play when
  // paused/ended, pause while playing. Hidden while buffering.
  const showCenter = controlsVisible && !loading

  return (
    <div className="my-2 max-w-2xl">
      {title && <div className="mb-1 text-sm font-medium text-p-text">{title}</div>}
      <div
        ref={wrapRef}
        tabIndex={0}
        onKeyDown={onKeyDown}
        onMouseMove={() => { if (!coarse) wake() }}
        className={`relative overflow-hidden rounded-xl bg-black outline-hidden ${
          fs && !controlsVisible ? 'cursor-none' : ''
        }`}
        style={{ aspectRatio: fs ? undefined : ratio }}
      >
        <video
          ref={videoRef}
          src={src}
          poster={poster || undefined}
          playsInline
          preload="metadata"
          className="h-full w-full bg-black"
          style={fs ? { objectFit: 'contain', height: '100%', width: '100%' } : undefined}
          onClick={onSurfaceClick}
          onPlay={() => { setPlaying(true); setEnded(false); wake() }}
          onPause={() => { setPlaying(false); setShowControls(true); clearHide() }}
          onEnded={() => { setEnded(true); setPlaying(false); setShowControls(true) }}
          onWaiting={() => setLoading(true)}
          onStalled={() => setLoading(true)}
          onPlaying={() => { setLoading(false); setPlaying(true) }}
          onCanPlay={() => setLoading(false)}
          onSeeking={() => setLoading(true)}
          onSeeked={() => setLoading(false)}
          onTimeUpdate={onTime}
          onProgress={onTime}
          onLoadedMetadata={onLoaded}
          onError={onVideoError}
        >
          {mime ? <source src={src} type={mime} /> : null}
        </video>

        {/* Double-tap seek flash (mobile) */}
        {seekFlash && (
          <div
            className={`pointer-events-none absolute inset-y-0 flex w-1/3 items-center justify-center text-white ${
              seekFlash === 'back' ? 'left-0' : 'right-0'
            }`}
          >
            <span className="rounded-full bg-black/50 px-3 py-1 text-sm">
              {seekFlash === 'back' ? '−10s' : '+10s'}
            </span>
          </div>
        )}

        {/* Buffering spinner */}
        {loading && (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
            <span className="h-12 w-12 animate-spin rounded-full border-4 border-white/30 border-t-white" />
          </div>
        )}

        {/* Center play / pause / replay button — only the circle is clickable
            (pointer-events-none wrapper) so taps elsewhere reach onSurfaceClick. */}
        {showCenter && (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
            <button
              onClick={(e) => { e.stopPropagation(); togglePlay() }}
              aria-label={ended ? 'Replay' : playing ? 'Pause' : 'Play'}
              className="pointer-events-auto flex h-16 w-16 items-center justify-center rounded-full bg-black/55 text-white backdrop-blur-xs transition hover:bg-black/70"
            >
              {ended
                ? <svg width="30" height="30" viewBox="0 0 24 24" fill="currentColor"><path d="M12 5V1L7 6l5 5V7a6 6 0 11-6 6H4a8 8 0 108-8z" /></svg>
                : playing
                  ? <svg width="30" height="30" viewBox="0 0 24 24" fill="currentColor"><path d="M6 5h4v14H6zM14 5h4v14h-4z" /></svg>
                  : <svg width="30" height="30" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z" /></svg>}
            </button>
          </div>
        )}

        {/* Control bar */}
        <div
          onMouseEnter={() => { overControls.current = true }}
          onMouseLeave={() => { overControls.current = false }}
          className={`absolute inset-x-0 bottom-0 flex flex-col gap-2 bg-linear-to-t from-black/75 to-transparent px-3 pb-2.5 pt-8 text-white transition-opacity duration-200 ${
            controlsVisible ? 'opacity-100' : 'pointer-events-none opacity-0'
          }`}
        >
          {/* Scrubber */}
          <div className="relative flex items-center">
            <div className="absolute left-0 right-0 h-1 rounded-sm bg-white/25" />
            <div
              className="absolute left-0 h-1 rounded-sm bg-white/40"
              style={{ width: dur ? `${(buffered / dur) * 100}%` : '0%' }}
            />
            <input
              type="range"
              min={0}
              max={dur || 0}
              step={0.1}
              value={cur}
              onChange={onSeek}
              aria-label="Seek"
              className="relative z-10 h-1 w-full cursor-pointer appearance-none bg-transparent accent-p-accent-red [&::-webkit-slider-thumb]:h-3.5 [&::-webkit-slider-thumb]:w-3.5 [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-white"
            />
          </div>
          <div className="flex items-center gap-3.5 text-sm">
            <button onClick={togglePlay} aria-label={playing ? 'Pause' : 'Play'} className="shrink-0">
              {playing
                ? <svg width="21" height="21" viewBox="0 0 24 24" fill="currentColor"><path d="M6 5h4v14H6zM14 5h4v14h-4z" /></svg>
                : <svg width="21" height="21" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z" /></svg>}
            </button>
            <span className="tabular-nums">{fmtTime(cur)} / {fmtTime(dur)}</span>
            <div className="ml-auto flex items-center gap-3.5">
              <button onClick={toggleMute} aria-label={muted ? 'Unmute' : 'Mute'} className="shrink-0">
                {muted || volume === 0
                  ? <svg width="21" height="21" viewBox="0 0 24 24" fill="currentColor"><path d="M16.5 12A4.5 4.5 0 0014 8v2.2l2.45 2.45A4.5 4.5 0 0016.5 12zM3 9v6h4l5 5V4L7 9H3z" /></svg>
                  : <svg width="21" height="21" viewBox="0 0 24 24" fill="currentColor"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3A4.5 4.5 0 0014 8v8a4.5 4.5 0 002.5-4z" /></svg>}
              </button>
              <input
                type="range" min={0} max={1} step={0.05} value={muted ? 0 : volume}
                onChange={onVolumeInput} aria-label="Volume"
                className={`h-1 cursor-pointer appearance-none rounded-sm bg-white/30 accent-white [&::-webkit-slider-thumb]:h-3.5 [&::-webkit-slider-thumb]:w-3.5 [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-white ${
                  coarse ? 'block w-12' : 'hidden w-16 sm:block'
                }`}
              />
              {PIP_SUPPORTED && (
                <button onClick={togglePip} aria-label="Picture in picture" className="shrink-0">
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="5" width="18" height="14" rx="2" /><rect x="12" y="11" width="7" height="6" rx="1" fill="currentColor" stroke="none" /></svg>
                </button>
              )}
              {canDownload && downloadHref && (
                <a href={safeHref(downloadHref)} download={dlName} onClick={(e) => e.stopPropagation()} aria-label="Download" className="shrink-0">
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" /></svg>
                </a>
              )}
              <button onClick={toggleFs} aria-label="Fullscreen" className="shrink-0">
                {fs
                  ? <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M5 16h3v3h2v-5H5v2zm3-8H5v2h5V5H8v3zm6 11h2v-3h3v-2h-5v5zm2-11V5h-2v5h5V8h-3z" /></svg>
                  : <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z" /></svg>}
              </button>
            </div>
          </div>
        </div>
      </div>
      {caption && <div className="mt-1 text-xs text-p-text-secondary">{caption}</div>}
    </div>
  )
}
