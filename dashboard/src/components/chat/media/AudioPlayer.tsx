import { useCallback, useEffect, useRef, useState } from 'react'
import { safeHref } from '../../../lib/safeUrl'

import { ensureMediaDownloadName } from '../../../lib/fileTypes'
import { useCoarsePointer } from '../../../hooks/useCoarsePointer'

// Shared with VideoPlayer via the same keys → one remembered volume/mute.
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
    /* ignore */
  }
}

interface Props {
  /** Final playable URL: a web URL or /v1/media/{token}. */
  src: string
  mime?: string
  caption?: string
  title?: string
  downloadName?: string
  canDownload?: boolean
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
    return `${src}${sep}download=1&fn=${encodeURIComponent(name || 'audio')}`
  }
  return src
}

export default function AudioPlayer({
  src, mime, caption, title, downloadName, canDownload = true,
}: Props) {
  const coarse = useCoarsePointer()
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const [playing, setPlaying] = useState(false)
  const [loading, setLoading] = useState(false)
  const [cur, setCur] = useState(0)
  const [dur, setDur] = useState(0)
  const [volume, setVolume] = useState(1)
  const [muted, setMuted] = useState(false)
  const [error, setError] = useState(false)

  useEffect(() => {
    const { volume: v, muted: m } = loadVolume()
    setVolume(v); setMuted(m)
    const a = audioRef.current
    if (a) { a.volume = v; a.muted = m }
  }, [])

  const applyVolume = useCallback((v: number, m: boolean) => {
    const a = audioRef.current
    if (a) { a.volume = v; a.muted = m }
    setVolume(v); setMuted(m)
    saveVolume(v, m)
  }, [])

  const togglePlay = () => {
    const a = audioRef.current
    if (!a) return
    if (a.paused) a.play().catch(() => {})
    else a.pause()
  }
  const onSeek = (e: React.ChangeEvent<HTMLInputElement>) => {
    const a = audioRef.current
    if (!a) return
    a.currentTime = Number(e.target.value)
    setCur(a.currentTime)
  }
  const onVolume = (e: React.ChangeEvent<HTMLInputElement>) => {
    const val = Number(e.target.value)
    applyVolume(val, val === 0)
  }
  const toggleMute = () => {
    const nextMuted = !(muted || volume === 0)
    const nextVol = !nextMuted && volume === 0 ? 0.5 : volume
    applyVolume(nextVol, nextMuted)
  }

  const dlName = ensureMediaDownloadName(downloadName || title || caption || 'audio', mime, src)
  const downloadHref = buildDownloadHref(src, dlName)

  if (error) {
    return (
      <div className="my-2 max-w-md rounded-xl border border-p-border-light bg-p-surface/50 p-3 text-sm text-p-text-secondary">
        Can't play this audio in your browser.{' '}
        {canDownload && downloadHref && (
          <a href={safeHref(downloadHref)} download={dlName} className="text-p-accent-red underline">Download instead</a>
        )}
      </div>
    )
  }

  return (
    <div className="my-2 max-w-md">
      {title && <div className="mb-1 text-sm font-medium text-p-text">{title}</div>}
      <div className="flex items-center gap-2 rounded-full border border-p-border-light bg-white px-3 py-2 dark:bg-p-surface">
        <audio
          ref={audioRef}
          src={src}
          preload="metadata"
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onWaiting={() => setLoading(true)}
          onStalled={() => setLoading(true)}
          onPlaying={() => { setLoading(false); setPlaying(true) }}
          onCanPlay={() => setLoading(false)}
          onTimeUpdate={() => { const a = audioRef.current; if (a) setCur(a.currentTime) }}
          onLoadedMetadata={() => {
            const a = audioRef.current
            if (a) { setDur(a.duration || 0); a.volume = volume; a.muted = muted }
          }}
          onError={() => setError(true)}
        >
          {mime ? <source src={src} type={mime} /> : null}
        </audio>

        <span className="shrink-0 text-[11px] tabular-nums text-p-text-secondary">{fmtTime(cur)}</span>
        <input
          type="range" min={0} max={dur || 0} step={0.1} value={cur} onChange={onSeek}
          aria-label="Seek"
          className="h-1 flex-1 min-w-0 cursor-pointer appearance-none rounded-sm bg-p-border-light accent-p-accent-red [&::-webkit-slider-thumb]:h-3 [&::-webkit-slider-thumb]:w-3 [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-p-accent-red"
        />
        <span className="shrink-0 text-[11px] tabular-nums text-p-text-light">{fmtTime(dur)}</span>

        <button onClick={toggleMute} aria-label={muted ? 'Unmute' : 'Mute'} className="shrink-0 text-p-text-secondary hover:text-p-text">
          {muted || volume === 0
            ? <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M16.5 12A4.5 4.5 0 0014 8v2.2l2.45 2.45A4.5 4.5 0 0016.5 12zM3 9v6h4l5 5V4L7 9H3z" /></svg>
            : <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3A4.5 4.5 0 0014 8v8a4.5 4.5 0 002.5-4z" /></svg>}
        </button>
        <input
          type="range" min={0} max={1} step={0.05} value={muted ? 0 : volume} onChange={onVolume}
          aria-label="Volume"
          className={`h-1 cursor-pointer appearance-none rounded-sm bg-p-border-light accent-p-accent-red [&::-webkit-slider-thumb]:h-3.5 [&::-webkit-slider-thumb]:w-3.5 [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-p-accent-red ${coarse ? 'block w-16' : 'hidden w-14 sm:block'}`}
        />

        {canDownload && downloadHref && (
          <a href={safeHref(downloadHref)} download={dlName} aria-label="Download" className="shrink-0 text-p-text-secondary hover:text-p-text">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" /></svg>
          </a>
        )}

        {/* Play/pause on the right */}
        <button
          onClick={togglePlay}
          aria-label={playing ? 'Pause' : 'Play'}
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-p-accent-red text-white hover:opacity-90"
        >
          {loading
            ? <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white" />
            : playing
              ? <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M6 5h4v14H6zM14 5h4v14h-4z" /></svg>
              : <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z" /></svg>}
        </button>
      </div>
      {caption && <div className="mt-1 text-xs text-p-text-secondary">{caption}</div>}
    </div>
  )
}
