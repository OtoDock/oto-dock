import { useRef, useCallback, useEffect, useState } from 'react'
import { Capacitor } from '@capacitor/core'

export type { PendingImage, PendingFile } from '../../store/types'
import type { PendingImage, PendingFile } from '../../store/types'

import { MicIcon } from './MicIcon'
import { VoiceControl } from './VoiceControl'
import { PresenceHalo } from './PresenceHalo'
import type { DuplexPhase } from '../../hooks/useDuplexVoice'

// ?presenceDebug=<phase> — the halo styling/geometry harness (see render).
const _DEBUG_PHASES: ReadonlySet<string> =
  new Set(['connecting', 'listening', 'thinking', 'speaking'])

function presenceDebugParam(): DuplexPhase | null {
  if (typeof window === 'undefined') return null
  const v = new URLSearchParams(window.location.search).get('presenceDebug')
  return v && _DEBUG_PHASES.has(v) ? (v as DuplexPhase) : null
}

// Synthetic audio for the harness: a slow swell + a faster flutter, so the
// reactive glow and the speaking sparks both exercise without a mic.
function debugLevels(): { mic: number; out: number } {
  const t = performance.now() / 1000
  const level = 0.35 + 0.3 * Math.sin(t * 1.1) + 0.25 * Math.abs(Math.sin(t * 5.3))
  return { mic: level, out: level }
}
import { useCoarsePointer } from '../../hooks/useCoarsePointer'
import { useAuth } from '../../contexts/AuthContext'
import { useTransferStore } from '../../store/transferStore'
import { Bar, fmtSize, pct } from '../common/TransferBar'
import ImageLightbox from './media/ImageLightbox'

// Any file type is accepted (no extension allowlist — see lib/fileTypes);
// the universal cap mirrors proxy config OTODOCK_MAX_FILE_MB (default 1GB) —
// one number for every file type, sync + upload.
// Shipped default; the per-install override rides user.feature_flags
// (upload_max_bytes) — see useUploadCap below.
const MAX_FILE_SIZE = 1024 * 1024 * 1024 // 1 GB (universal)

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

// Inline images ride the dashboard WS as base64 data-URLs in ONE frame with
// the message text — and uvicorn's default WS frame cap is 16MB, so big
// originals must shrink client-side or a single large photo kills the whole
// dashboard socket. Mirror the server's own vision bound (1568px / JPEG q85 —
// proxy/ws/dashboard.py downscales to exactly this before the model sees it),
// so nothing of model-visible value is lost: a 25MB phone photo becomes a few
// hundred KB. Small originals pass through byte-identical (keeps PNG alpha).
const IMAGE_PASSTHROUGH_MAX = 2 * 1024 * 1024
const IMAGE_MAX_DIM = 1568
const IMAGE_JPEG_QUALITY = 0.85
// Aggregate ENCODED budget across ALL inline images of one message — the
// per-file downscale alone doesn't protect the shared frame.
const IMAGE_MESSAGE_BUDGET = 10 * 1024 * 1024

function readFileAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result as string)
    reader.onerror = reject
    reader.readAsDataURL(file)
  })
}

function dataUrlBytes(dataUrl: string): number {
  const idx = dataUrl.indexOf(',')
  return idx < 0 ? 0 : Math.floor(((dataUrl.length - idx - 1) * 3) / 4)
}

/** Encode an image for the inline (vision) path — downscaled to the server's
 * own bound when large. Returns null when this browser can't decode it (e.g.
 * HEIC outside Safari): the caller demotes it to a regular file upload. */
async function prepareImageForChat(file: File): Promise<string | null> {
  if (file.size <= IMAGE_PASSTHROUGH_MAX) return readFileAsBase64(file)
  if (typeof createImageBitmap !== 'function') {
    // No canvas-decode path (very old engines): pass through only what can't
    // threaten the WS frame; larger falls back to a file upload.
    return file.size <= 8 * 1024 * 1024 ? readFileAsBase64(file) : null
  }
  try {
    const bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' })
    try {
      const scale = Math.min(1, IMAGE_MAX_DIM / Math.max(bitmap.width, bitmap.height))
      const w = Math.max(1, Math.round(bitmap.width * scale))
      const h = Math.max(1, Math.round(bitmap.height * scale))
      const canvas = document.createElement('canvas')
      canvas.width = w
      canvas.height = h
      const ctx = canvas.getContext('2d')
      if (!ctx) return null
      ctx.drawImage(bitmap, 0, 0, w, h)
      return canvas.toDataURL('image/jpeg', IMAGE_JPEG_QUALITY)
    } finally {
      bitmap.close()
    }
  } catch {
    return null
  }
}

interface Props {
  // Controlled textarea — parent owns the draft string so it can be
  // persisted (chatStore.draftInput) and survives chat navigation.
  value: string
  onChange: (text: string) => void
  onSend: (text: string) => void
  onAbort?: () => void
  onEditQueued?: () => void
  /** Fired once on the user's first genuine interaction with the composer
   * (first keydown or pointerdown on the textarea). Used to trigger a lazy
   * pre-warm for non-favorite agents. NOT fired on focus or on mount. */
  onEngage?: () => void
  disabled?: boolean
  /** Blocks ONLY sending (button + Enter) while keeping the composer
   * typeable — the user can keep drafting. Used while a cross-engine switch
   * awaits its confirmation (prompting is not allowed until it resolves). */
  sendDisabled?: boolean
  streaming?: boolean
  aborting?: boolean
  placeholder?: string
  queuedCount?: number
  editText?: string | null
  onClearEditText?: () => void
  pendingImages: PendingImage[]
  onAddImages: (images: PendingImage[]) => void
  onRemoveImage: (id: string) => void
  pendingFiles: PendingFile[]
  onAddFiles: (files: PendingFile[]) => void
  onRemoveFile: (id: string) => void
  /** Re-queue a failed upload from its error chip. The affordance renders
   * only for retryable failures (a file over the upload cap stays
   * remove-only — retrying can't succeed). */
  onRetryFile?: (id: string) => void
  /** Dock overlay toggle — wired by the host page only when the open chat
   * belongs to a delegation project or carries a chat-scoped pinned
   * dashboard (staged feature; stripped from the public cut with this
   * block). `dockKind` picks the tooltip: "Project dock" vs "Chat dock". */
  projectsOpen?: boolean
  onToggleProjects?: () => void
  dockKind?: 'project' | 'chat'
  /** Workspace overlay toggle button. When `onToggleWorkspace` is set, a folder
   * icon button appears as the leftmost element of the input pill. */
  workspaceOpen?: boolean
  onToggleWorkspace?: () => void
  /** Pinned mini-apps overlay toggle — permanent (right of the workspace
   * button, left of the projects toggle) whenever the host page wires it. */
  appsOpen?: boolean
  onToggleApps?: () => void
  /** Lights up a small dot on the toggle while the overlay is open and a new
   * assistant message has arrived. */
  workspaceHasNewMessage?: boolean
  /** Forwarded to the textarea so dropped agent file paths splice at the cursor. */
  textareaRef?: React.RefObject<HTMLTextAreaElement | null>
  /** Voice mode (AgentChat only). Hands-free speak → send → hear. Omitted where
   * voice mode isn't wired → no voice UI or behaviour. */
  voice?: {
    duplex?: import('./VoiceControl').DuplexControlProps  // full-duplex mode
  }
}

/** Check if camera capture is available (mobile only — desktop browsers ignore capture attr) */
async function isCameraAvailable(): Promise<boolean> {
  if (Capacitor.isNativePlatform()) return true
  // On mobile web browsers, capture attribute works. On desktop it doesn't.
  // Detect mobile via touch support + screen size heuristic.
  const isMobile = 'ontouchstart' in window && window.innerWidth < 1024
  return isMobile
}

function generateId(): string {
  return Math.random().toString(36).slice(2, 10)
}

export default function ChatInput({
  value,
  onChange,
  onSend,
  onAbort,
  onEditQueued,
  onEngage,
  disabled,
  sendDisabled,
  streaming,
  aborting,
  placeholder,
  queuedCount = 0,
  editText,
  onClearEditText,
  pendingImages,
  onAddImages,
  onRemoveImage,
  pendingFiles,
  onAddFiles,
  onRemoveFile,
  onRetryFile,
  workspaceOpen,
  onToggleWorkspace,
  workspaceHasNewMessage,
  appsOpen,
  onToggleApps,
  projectsOpen,
  onToggleProjects,
  dockKind,
  textareaRef: externalTextareaRef,
  voice,
}: Props) {
  const text = value
  const setText = onChange
  const presenceDebugPhase = presenceDebugParam()
  // Per-install universal upload cap (OTODOCK_MAX_FILE_MB) with the shipped
  // default as fallback.
  const { user } = useAuth()
  const uploadCap = user?.feature_flags?.upload_max_bytes ?? MAX_FILE_SIZE
  // Live mirror of the controlled value — the STT session's onFinal closure is
  // created once (when the mic starts) and would otherwise capture a stale
  // value, making each dictated phrase REPLACE the input instead of append.
  const valueRef = useRef(value)
  valueRef.current = value
  const [menuOpen, setMenuOpen] = useState(false)
  const [hasCamera, setHasCamera] = useState(false)
  // External-file drag over the composer (counter: child enter/leave pairs
  // would flicker a boolean — the FileGrid drop-zone pattern).
  const [dragDepth, setDragDepth] = useState(0)
  // Tapping an image tile opens the standard lightbox over a SNAPSHOT of the
  // pending photos. Snapshot, not the live array: background store activity
  // (the draft→chat slice re-key at send-time warmup, a transfer, another
  // tab clearing the draft) can make `pendingImages` transiently empty, and
  // a live-array render condition would yank the viewer mid-look — the
  // 2026-08-13 "picture closes itself after ~10s" report. Same
  // defer-while-viewed philosophy as the doc-preview chip (76cdb5ca): what
  // the user is actively viewing only closes by their own hand (Esc/✕/back).
  const [lightbox, setLightbox] =
    useState<{ images: PendingImage[]; idx: number } | null>(null)
  // Phase-2 join: uploaded chips show the remote-machine sync driven by the
  // transfer_* WS events (same store the workspace popup reads).
  const transfersById = useTransferStore(s => s.byId)
  const [micStopSignal, setMicStopSignal] = useState(0)      // bump → close the mic, keep the tail (input focus)
  const [micDiscardSignal, setMicDiscardSignal] = useState(0)  // bump → close the mic, drop the tail (send)
  const internalRef = useRef<HTMLTextAreaElement>(null)
  const textareaRef = (externalTextareaRef ?? internalRef) as React.RefObject<HTMLTextAreaElement>
  const fileInputRef = useRef<HTMLInputElement>(null)
  const cameraInputRef = useRef<HTMLInputElement>(null)
  const docInputRef = useRef<HTMLInputElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const plusBtnRef = useRef<HTMLButtonElement>(null)

  // Detect camera availability on mount
  useEffect(() => {
    isCameraAvailable().then(setHasCamera)
  }, [])

  // Close menu on outside click
  useEffect(() => {
    if (!menuOpen) return
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node) &&
          plusBtnRef.current && !plusBtnRef.current.contains(e.target as Node)) {
        setMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [menuOpen])

  useEffect(() => {
    if (editText != null) {
      setText(editText)
      onClearEditText?.()
      if (textareaRef.current) {
        textareaRef.current.focus()
        const len = editText.length
        textareaRef.current.setSelectionRange(len, len)
      }
    }
  }, [editText, onClearEditText])

  const anyFileUploading = pendingFiles.some(f => f.uploading)
  const anyFileErrored = pendingFiles.some(f => f.error)
  // Touch devices have no hover, so the remove (✕) buttons must stay visible;
  // on desktop (fine pointer) they reveal on tile hover.
  const coarse = useCoarsePointer()
  const removeBtnVisibility = coarse ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'

  const handleSend = useCallback(() => {
    if (anyFileUploading || sendDisabled) return
    const trimmed = text.trim()
    if (!trimmed && pendingImages.length === 0 && pendingFiles.length === 0) return
    onSend(trimmed)
    setText('')
    // Close an open dictation mic AND drop its tail: the text was just sent,
    // so a late stop-flush final must not re-fill the cleared input. Reset
    // the dictation accumulator for the same reason.
    dictBaseRef.current = ''
    setMicDiscardSignal(n => n + 1)
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
  }, [text, pendingImages, pendingFiles, onSend, anyFileUploading, sendDisabled])

  // First genuine interaction with the composer → fire onEngage once. Guarded
  // by a ref so it never re-fires; bound to keydown/pointerdown (NOT focus or
  // mount) so a programmatic .focus() can't trigger a lazy pre-warm.
  const engagedRef = useRef(false)
  const handleEngage = () => {
    if (engagedRef.current) return
    engagedRef.current = true
    onEngage?.()
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    handleEngage()
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      if (!anyFileUploading && !anyFileErrored) handleSend()
    }
    if (e.key === 'ArrowUp' && !text && queuedCount > 0 && onEditQueued) {
      e.preventDefault()
      onEditQueued()
    }
  }

  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setText(e.target.value)
    // Manual edit mid-dictation → re-baseline so the next partial/final builds on
    // the edited text (otherwise a deleted phrase reappears on the next utterance).
    if (dictatingRef.current) dictBaseRef.current = e.target.value
    const ta = e.target
    ta.style.height = 'auto'
    ta.style.height = Math.min(ta.scrollHeight, 200) + 'px'
  }

  // Mic dictation shows live interim text as you speak and commits each final at
  // a pause (the native-dictation feel). `dictBaseRef` is the committed text the
  // live interim is appended onto: interims REPLACE (preview), finals ACCUMULATE.
  // Bypasses handleInput, so nudge the textarea auto-resize manually.
  const dictBaseRef = useRef('')
  const dictatingRef = useRef(false)   // mic actively dictating → manual edits re-baseline
  const joinText = (a: string, b: string) =>
    a ? `${a}${a.endsWith(' ') || a.endsWith('\n') ? '' : ' '}${b}` : b
  const nudgeResize = () => {
    const ta = textareaRef.current
    if (ta) { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 200) + 'px' }
  }
  // Autosize on EVERY value change: programmatic sets bypass handleInput —
  // a draft restored from localStorage on mount, edit/template insertion,
  // and clears all rendered at rows=1 (or stayed stretched) until the next
  // keystroke. Idempotent with the onChange-path resize.
  useEffect(() => {
    nudgeResize()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value])
  // Dictation started → snapshot whatever's already in the input as the base
  // (live partials append onto it). Live-mode barge-in is handled in VoiceControl.
  const onMicActive = (active: boolean) => {
    dictatingRef.current = active
    if (active) dictBaseRef.current = valueRef.current
  }
  // Live partial → show base+interim without committing (the next partial/final replaces it).
  const showInterim = (t: string) => { setText(joinText(dictBaseRef.current, t)); nudgeResize() }
  // Finalized phrase → commit it onto the base so the next utterance builds after it.
  const appendTranscript = (t: string) => {
    dictBaseRef.current = joinText(dictBaseRef.current, t)
    setText(dictBaseRef.current)
    nudgeResize()
  }

  // ONE routing helper behind every entrance — pickers, camera, drag-and-drop
  // and clipboard paste — so caps, downscaling, demotion and error chips
  // behave identically no matter how a file arrives. Images go to the inline
  // vision path (downscaled, budgeted); everything else (plus images this
  // browser can't decode) becomes a PendingFile upload. Oversize files are
  // added as VISIBLE error chips — the old silent skip made a too-big pick
  // look like the app ate the file.
  const ingestFiles = useCallback(async (incoming: FileList | File[]) => {
    // Materialize before any await: live FileLists empty when the input's
    // value is reset by the caller right after this call.
    const files = Array.from(incoming)
    if (files.length === 0) return
    const docFiles = files.filter(f => !f.type.startsWith('image/'))
    const imageFiles = files.filter(f => f.type.startsWith('image/'))

    if (imageFiles.length) {
      const newImages: PendingImage[] = []
      let budget = pendingImages.reduce((a, img) => a + dataUrlBytes(img.base64), 0)
      for (const file of imageFiles) {
        const prepared = await prepareImageForChat(file)
        if (prepared === null) {
          docFiles.push(file) // undecodable here — attach as a file instead
          continue
        }
        const bytes = dataUrlBytes(prepared)
        if (budget + bytes > IMAGE_MESSAGE_BUDGET) {
          // The shared WS frame can't take more inline images this message —
          // surface it as an error chip instead of silently dropping (retry
          // uploads it as a file the agent can still open).
          onAddFiles([{
            id: generateId(), name: file.name, size: file.size, file,
            error: 'Inline photo limit for one message reached — remove some photos or send it as a file',
          }])
          continue
        }
        budget += bytes
        newImages.push({ id: generateId(), base64: prepared, name: file.name })
      }
      if (newImages.length) onAddImages(newImages)
    }

    if (docFiles.length) {
      onAddFiles(docFiles.map(file => ({
        id: generateId(), name: file.name, size: file.size, file,
        ...(file.size > uploadCap
          ? { error: `Over the ${formatFileSize(uploadCap)} upload limit` }
          : {}),
      })))
    }
  }, [pendingImages, onAddImages, onAddFiles, uploadCap])

  const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files
    if (files?.length) await ingestFiles(files)
    // Reset input so re-selecting same file works
    e.target.value = ''
    setMenuOpen(false)
  }

  const handleDocSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files
    if (files?.length) await ingestFiles(files)
    e.target.value = ''
    setMenuOpen(false)
  }

  const handleCamera = () => {
    setMenuOpen(false)
    // Use a dedicated file input with capture="environment" — this triggers
    // the native camera on both Android WebView and mobile browsers.
    // More reliable than Capacitor Camera plugin in remote URL mode.
    cameraInputRef.current?.click()
  }

  const handleUploadPhoto = () => {
    setMenuOpen(false)
    fileInputRef.current?.removeAttribute('capture')
    fileInputRef.current?.click()
  }

  const handleUploadFile = () => {
    setMenuOpen(false)
    docInputRef.current?.click()
  }

  const canSend =
    !disabled &&
    !sendDisabled &&
    !anyFileUploading &&
    !anyFileErrored &&
    (text.trim().length > 0 || pendingImages.length > 0 || pendingFiles.length > 0)

  return (
    <div className="px-3 pb-composer-safe">
      <div className="max-w-4xl mx-auto">
        {/* relative wrapper: the PresenceHalo canvas positions against the
            pill; the pill's explicit z-[1] keeps content above the glow
            (before, that layering only held by accident of backdrop-blur's
            stacking context). */}
        <div className="relative">
        {presenceDebugPhase ? (
          // ?presenceDebug=<phase> forces the halo with synthetic levels —
          // the styling/geometry harness (real phone-mode sessions need a
          // live engine, which headless verification and device testing
          // don't have). Visual-only; harmless in production.
          <PresenceHalo phase={presenceDebugPhase} getLevels={debugLevels} />
        ) : voice?.duplex && (
          <PresenceHalo phase={voice.duplex.phase} getLevels={voice.duplex.getLevels} />
        )}
        <div
          className="relative z-[1] bg-white/90 dark:bg-gray-900/90 backdrop-blur-xs rounded-xl border border-p-border-light dark:border-gray-700 shadow-xs p-2"
          // Drop zone for EXTERNAL files only (dataTransfer carries 'Files').
          // Internal agent-path drags never do, so their native text insert
          // into the textarea keeps working untouched. preventDefault on the
          // external path is load-bearing: an unhandled file drop navigates
          // the page away.
          onDragEnter={(e) => {
            if (disabled || !Array.from(e.dataTransfer?.types ?? []).includes('Files')) return
            e.preventDefault()
            setDragDepth(d => d + 1)
          }}
          onDragOver={(e) => {
            if (disabled || !Array.from(e.dataTransfer?.types ?? []).includes('Files')) return
            e.preventDefault()
            e.dataTransfer.dropEffect = 'copy'
          }}
          onDragLeave={(e) => {
            if (disabled || !Array.from(e.dataTransfer?.types ?? []).includes('Files')) return
            setDragDepth(d => Math.max(0, d - 1))
          }}
          onDrop={(e) => {
            if (disabled) return
            const files = e.dataTransfer?.files
            setDragDepth(0)
            if (!files?.length) return // internal path drag → textarea handles it
            e.preventDefault()
            void ingestFiles(files)
          }}
        >
          {dragDepth > 0 && (
            <div className="absolute inset-0 z-20 rounded-xl border-2 border-dashed border-brand bg-brand/10
                            flex items-center justify-center pointer-events-none">
              <span className="text-sm font-medium text-brand">Drop to attach</span>
            </div>
          )}
          {/* Attachment preview row (images + files) */}
          {(pendingImages.length > 0 || pendingFiles.length > 0) && (
            <div className="flex gap-2 overflow-x-auto pt-2 pb-2 mb-2 border-b border-p-border-light/50 px-1.5 items-center">
              {pendingImages.map((img, i) => (
                <div key={img.id} className="relative shrink-0 group">
                  {/* Tile opens the standard lightbox (zoom/swipe) so the
                      photo can be checked while composing; ✕ stays removal. */}
                  <button
                    type="button"
                    onClick={() => setLightbox({ images: [...pendingImages], idx: i })}
                    aria-label={`Preview ${img.name}`}
                    title="Click to preview"
                    className="block rounded-lg focus:outline-hidden focus-visible:ring-2 focus-visible:ring-brand"
                  >
                    <img
                      src={img.base64}
                      alt={img.name}
                      className="w-14 h-14 rounded-lg object-cover border border-p-border-light cursor-pointer"
                    />
                  </button>
                  <button
                    onClick={() => onRemoveImage(img.id)}
                    aria-label="Remove image"
                    className={`absolute -top-1.5 -right-1.5 z-10 w-5 h-5 rounded-full bg-p-accent-red text-white
                               flex items-center justify-center text-xs ring-2 ring-white dark:ring-gray-900
                               shadow-sm transition-opacity ${removeBtnVisibility}`}
                    style={{ fontSize: '10px', lineHeight: 1 }}
                  >
                    ✕
                  </button>
                </div>
              ))}
              {pendingFiles.map(f => {
                const transfer = f.transferId ? transfersById[f.transferId] : undefined
                const syncing = Boolean(
                  f.uploadedPath && f.remotePush && transfer && transfer.doneAt === null,
                )
                const uploadingNow = Boolean(f.uploading && !f.queued)
                const uploadPct = pct(f.uploadSent ?? 0, f.uploadTotal ?? f.size)
                let syncPct = 0
                if (syncing && transfer) {
                  const rows = Object.values(transfer.machines)
                  const total = rows.reduce((a, r) => a + r.bytesTotal, 0)
                  const sent = rows.reduce(
                    (a, r) => a + (r.state === 'done' ? r.bytesTotal : r.bytesSent), 0,
                  )
                  syncPct = pct(sent, total)
                }
                const subText = f.error
                  ? f.error
                  : f.queued
                    ? 'Queued'
                    : uploadingNow
                      ? `${uploadPct}% · ${fmtSize(f.uploadSent ?? 0)} of ${fmtSize(f.uploadTotal ?? f.size)}`
                      : syncing
                        ? 'Syncing to machines…'
                        : null
                return (
                  <div
                    key={f.id}
                    title={f.error || undefined}
                    className={`relative shrink-0 group flex flex-col gap-1
                                bg-p-surface rounded-lg px-2.5 py-1.5 border
                                ${(uploadingNow || syncing) ? 'min-w-[160px]' : ''}
                                max-w-[220px]
                                ${f.error ? 'border-p-accent-red' : 'border-p-border-light'}`}
                  >
                    <div className="flex items-center gap-1.5">
                      {f.uploading ? (
                        <span
                          className="w-4 h-4 border-2 border-brand border-t-transparent rounded-full animate-spin shrink-0"
                          aria-label={f.queued ? 'Queued' : 'Uploading'}
                        />
                      ) : f.error ? (
                        <svg className="w-4 h-4 text-p-accent-red shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                                d="M12 9v2m0 4h.01M12 5l7 12H5l7-12z" />
                        </svg>
                      ) : (
                        <svg className="w-4 h-4 text-p-text-secondary shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                                d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                        </svg>
                      )}
                      <span className="text-xs text-p-text truncate max-w-[100px]">{f.name}</span>
                      <span className="text-xs text-p-text-light shrink-0">{formatFileSize(f.size)}</span>
                      {f.error && onRetryFile && f.size <= uploadCap && (
                        <button
                          onClick={() => onRetryFile(f.id)}
                          aria-label="Retry upload"
                          title="Retry upload"
                          className="w-4 h-4 rounded-full text-p-text-secondary hover:text-brand
                                     flex items-center justify-center shrink-0"
                        >
                          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round"
                                  d="M4 4v5h5M20 20v-5h-5M5.07 9A8 8 0 0119.4 6.6M18.93 15A8 8 0 014.6 17.4" />
                          </svg>
                        </button>
                      )}
                      <button
                        onClick={() => onRemoveFile(f.id)}
                        aria-label="Remove file"
                        className={`w-4 h-4 rounded-full bg-p-accent-red text-white flex items-center justify-center
                                   text-xs shrink-0 transition-opacity ${removeBtnVisibility}`}
                        style={{ fontSize: '9px', lineHeight: 1 }}
                      >
                        ✕
                      </button>
                    </div>
                    {(uploadingNow || syncing) && (
                      <Bar value={uploadingNow ? uploadPct : syncPct} />
                    )}
                    {subText && (
                      <span
                        className={`text-[10px] leading-tight truncate
                                    ${f.error ? 'text-p-accent-red' : 'text-p-text-light'}`}
                        title={subText}
                      >
                        {subText}
                      </span>
                    )}
                  </div>
                )
              })}
            </div>
          )}

          {/* Input row — unified two-row layout on every breakpoint: the
              textarea takes a full line on top (full width for text, and the
              sliding live-voice toggle can't shift it), the controls sit on
              the line below. */}
          <div className="flex flex-wrap items-end gap-1">
            {/* Workspace toggle (leftmost; only when wired by the host page) */}
            {onToggleWorkspace && (
              <button
                onClick={onToggleWorkspace}
                title={workspaceOpen ? 'Close workspace (Esc)' : 'Open workspace (Ctrl+E)'}
                className={`relative w-9 h-9 -mr-0.5 rounded-lg flex items-center justify-center transition-colors shrink-0
                  ${workspaceOpen
                    ? 'bg-brand text-white hover:bg-brand-hover'
                    : 'text-p-text-secondary hover:text-brand hover:bg-brand/5'}
                `}
              >
                <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
                  <path d="M2 6a2 2 0 012-2h5l2 2h5a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V6z" />
                </svg>
                {workspaceOpen && workspaceHasNewMessage && (
                  <span className="absolute top-1 right-1 w-2 h-2 rounded-full bg-white ring-2 ring-brand" />
                )}
              </button>
            )}
            {/* Pinned mini-apps toggle — permanent, right of workspace. */}
            {onToggleApps && (
              <button
                onClick={onToggleApps}
                title={appsOpen ? 'Close mini-apps' : 'Open mini-apps'}
                className={`w-9 h-9 -mr-0.5 rounded-lg flex items-center justify-center transition-colors shrink-0
                  ${appsOpen
                    ? 'bg-emerald-600 text-white hover:bg-emerald-700'
                    : 'text-p-text-secondary hover:text-emerald-600 hover:bg-emerald-500/5'}
                `}
              >
                <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
                  <rect x="3.5" y="3.5" width="7.2" height="7.2" rx="1.5" />
                  <rect x="13.3" y="3.5" width="7.2" height="7.2" rx="1.5" />
                  <rect x="3.5" y="13.3" width="7.2" height="7.2" rx="1.5" />
                  <rect x="13.3" y="13.3" width="7.2" height="7.2" rx="1.5" />
                </svg>
              </button>
            )}
            {/* Dock toggle — present only when the open chat has a dock:
                a delegation project or a chat-scoped pinned dashboard (host
                page decides). Layout-panels glyph, distinct from the apps
                four-squares. */}
            {onToggleProjects && (
              <button
                onClick={onToggleProjects}
                title={`${projectsOpen ? 'Close' : 'Open'} ${dockKind === 'chat' ? 'chat dock' : 'project dock'}`}
                className={`w-9 h-9 -mr-0.5 rounded-lg flex items-center justify-center transition-colors shrink-0
                  ${projectsOpen
                    ? 'bg-violet-600 text-white hover:bg-violet-700'
                    : 'text-p-text-secondary hover:text-violet-600 hover:bg-violet-500/5'}
                `}
              >
                <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
                  <rect x="3.5" y="4" width="9" height="16" rx="1.5" />
                  <rect x="15" y="4" width="5.5" height="7.2" rx="1.5" />
                  <rect x="15" y="13.8" width="5.5" height="6.2" rx="1.5" />
                </svg>
              </button>
            )}
            {/* + button */}
            <div className="relative">
              <button
                ref={plusBtnRef}
                onClick={() => setMenuOpen(!menuOpen)}
                disabled={disabled}
                className="w-9 h-9 rounded-lg flex items-center justify-center
                           text-p-text-secondary hover:text-brand hover:bg-brand/5
                           disabled:opacity-40 disabled:cursor-not-allowed
                           transition-colors shrink-0"
              >
                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
                </svg>
              </button>

              {/* Popup menu */}
              {menuOpen && (
                <div
                  ref={menuRef}
                  className="absolute bottom-full left-0 mb-2 bg-white dark:bg-p-surface rounded-lg border border-p-border-light
                             shadow-lg py-1 min-w-[160px] z-50"
                >
                  {hasCamera && (
                    <button
                      onClick={handleCamera}
                      className="flex items-center gap-2.5 w-full px-3 py-2 text-sm text-p-text
                                 hover:bg-p-surface-hover transition-colors"
                    >
                      <svg className="w-4 h-4 text-p-text-secondary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                              d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z" />
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                              d="M15 13a3 3 0 11-6 0 3 3 0 016 0z" />
                      </svg>
                      Take Photo
                    </button>
                  )}
                  <button
                    onClick={handleUploadPhoto}
                    className="flex items-center gap-2.5 w-full px-3 py-2 text-sm text-p-text
                               hover:bg-p-surface-hover transition-colors"
                  >
                    <svg className="w-4 h-4 text-p-text-secondary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                            d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
                    </svg>
                    Upload Photo
                  </button>
                  <button
                    onClick={handleUploadFile}
                    className="flex items-center gap-2.5 w-full px-3 py-2 text-sm text-p-text
                               hover:bg-p-surface-hover transition-colors"
                  >
                    <svg className="w-4 h-4 text-p-text-secondary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                            d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                    </svg>
                    Upload File
                  </button>
                </div>
              )}
            </div>

            <textarea
              ref={textareaRef}
              value={text}
              onChange={handleInput}
              onKeyDown={handleKeyDown}
              onPointerDown={handleEngage}  // first genuine interaction → lazy pre-warm (non-favorite agents)
              onFocus={() => setMicStopSignal(n => n + 1)}  // clicking the input stops/pauses the mic
              onBlur={() => {
                // Click-to-edit abandoned: leaving an EMPTY composer while a
                // live conversation is held resumes listening (a non-empty
                // draft keeps the hold — Send is the only way it dispatches).
                if (voice?.duplex?.active && !text.trim()) voice.duplex.onRelease?.()
              }}
              onDrop={() => {
                // Native textarea drop inserts the text and SELECTS it.
                // Collapse the selection on the next frame so the user can
                // keep typing after the inserted path without accidentally
                // replacing it. We don't preventDefault — the browser's
                // native insertion handles cursor placement correctly; we
                // only fix the post-drop selection. (External FILE drops are
                // consumed by the composer container's onDrop instead.)
                requestAnimationFrame(() => {
                  const ta = textareaRef.current
                  if (!ta) return
                  const end = ta.selectionEnd
                  if (ta.selectionStart !== end) ta.setSelectionRange(end, end)
                })
              }}
              onPaste={(e) => {
                // Ctrl/Cmd+V with files on the clipboard (screenshots, copied
                // files) attaches them exactly like the + menu; plain text
                // pastes are untouched.
                const files = e.clipboardData?.files
                if (files && files.length > 0 && !disabled) {
                  e.preventDefault()
                  void ingestFiles(files)
                }
              }}
              placeholder={placeholder || 'Type a message...'}
              disabled={disabled}
              rows={1}
              className="flex-1 resize-none rounded-lg px-3 py-2 text-sm bg-transparent
                         focus:outline-hidden order-first basis-full
                         disabled:text-p-text-light placeholder:text-p-text-light"
              style={{ maxHeight: '200px' }}
            />

            {/* Right controls (mic/live + send) — pushed to the right end of
                the controls line via ml-auto. */}
            <div className="flex items-end gap-1 ml-auto">
            {/* Mic / live-voice control. Voice-enabled pages get VoiceControl;
                others (e.g. a host without the voice prop) get plain dictation. */}
            {voice ? (
              <VoiceControl
                duplex={voice.duplex ? {
                  ...voice.duplex,
                  // The unmute tap hands a held draft back to the engine
                  // (sticky-mute release) — the draft lives HERE, so this
                  // component supplies the read + the clear.
                  getHeldDraft: () => text,
                  onDraftConsumed: () => setText(''),
                } : undefined}
                onDictateInterim={showInterim}
                onDictateFinal={appendTranscript}
                onDictateActive={onMicActive}
                interruptSignal={micStopSignal}
                discardSignal={micDiscardSignal}
                disabled={disabled}
              />
            ) : (
              <MicIcon
                onTranscript={appendTranscript}
                onInterim={showInterim}
                onActive={onMicActive}
                disabled={disabled}
              />
            )}
            {streaming && onAbort ? (
              <button
                onClick={onAbort}
                disabled={aborting}
                className={`px-4 py-2 rounded-lg text-sm font-medium text-white transition-colors shrink-0 ${
                  aborting
                    ? 'bg-p-text-light cursor-not-allowed'
                    : 'bg-p-accent-red hover:bg-red-700'
                }`}
              >
                {aborting ? 'Stopping...' : 'Stop'}
              </button>
            ) : (
              <button
                onClick={handleSend}
                disabled={!canSend}
                className="w-9 h-9 rounded-lg text-sm font-medium text-white flex items-center justify-center
                           bg-brand hover:bg-brand-hover disabled:bg-p-surface disabled:text-p-text-light disabled:cursor-not-allowed
                           transition-colors shrink-0"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 19V5m0 0l-7 7m7-7l7 7" />
                </svg>
              </button>
            )}
            </div>
          </div>

          {/* Hidden file input for gallery/upload */}
          <input
            ref={fileInputRef}
            type="file"
            accept="image/*"
            multiple
            onChange={handleFileSelect}
            className="hidden"
          />
          {/* Hidden file input for camera capture (separate so capture attr is permanent) */}
          <input
            ref={cameraInputRef}
            type="file"
            accept="image/*"
            capture="environment"
            onChange={handleFileSelect}
            className="hidden"
          />
          {/* Hidden file input for file upload — any type */}
          <input
            ref={docInputRef}
            type="file"
            multiple
            onChange={handleDocSelect}
            className="hidden"
          />
        </div>
        </div>
      </div>

      {/* Standard image lightbox over the pending photos (tap a tile). Same
          component chat messages use — zoom, swipe, download all work.
          Renders from the open-time snapshot (see the state comment). */}
      {lightbox !== null && lightbox.images.length > 0 && (
        <ImageLightbox
          images={lightbox.images.map(img => {
            const m = img.base64.match(/^data:([^;,]+);base64,(.*)$/)
            return m
              ? { imageData: m[2], mimeType: m[1], caption: img.name }
              : { url: img.base64, caption: img.name }
          })}
          initialIndex={Math.min(lightbox.idx, lightbox.images.length - 1)}
          onClose={() => setLightbox(null)}
        />
      )}
    </div>
  )
}
