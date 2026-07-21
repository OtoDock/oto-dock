import { useRef, useCallback, useEffect, useState } from 'react'
import { Capacitor } from '@capacitor/core'

export type { PendingImage, PendingFile } from '../../store/types'
import type { PendingImage, PendingFile } from '../../store/types'

import { AUDIO_EXTENSIONS, VIDEO_EXTENSIONS } from '../../lib/fileTypes'
import { MicIcon } from './MicIcon'
import { VoiceControl } from './VoiceControl'
import { useCoarsePointer } from '../../hooks/useCoarsePointer'

// Any file type is accepted (no extension allowlist — see lib/fileTypes);
// caps mirror proxy/api/media/uploads.py.
const MAX_FILE_SIZE = 100 * 1024 * 1024 // 100 MB (everything but audio/video)
const MAX_MEDIA_FILE_SIZE = 250 * 1024 * 1024 // 250 MB (audio + video)

function isMediaExt(ext: string): boolean {
  return AUDIO_EXTENSIONS.has(ext) || VIDEO_EXTENSIONS.has(ext)
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
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
    ttsAvailable: boolean      // chat TTS resolvable → live voice mode possible
    live: boolean              // live voice mode on
    onSetLive: (on: boolean) => void
    speaking: boolean          // a reply is being spoken right now
    onBargeIn: () => void      // cancel the spoken reply (barge-in)
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
  workspaceOpen,
  onToggleWorkspace,
  workspaceHasNewMessage,
  appsOpen,
  onToggleApps,
  textareaRef: externalTextareaRef,
  voice,
}: Props) {
  const text = value
  const setText = onChange
  // Live mirror of the controlled value — the STT session's onFinal closure is
  // created once (when the mic starts) and would otherwise capture a stale
  // value, making each dictated phrase REPLACE the input instead of append.
  const valueRef = useRef(value)
  valueRef.current = value
  const [menuOpen, setMenuOpen] = useState(false)
  const [hasCamera, setHasCamera] = useState(false)
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
    if (anyFileUploading) return
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
  }, [text, pendingImages, pendingFiles, onSend, anyFileUploading])

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

  const readFileAsBase64 = (file: File): Promise<string> => {
    return new Promise((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(reader.result as string)
      reader.onerror = reject
      reader.readAsDataURL(file)
    })
  }

  const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files
    if (!files?.length) return
    const newImages: PendingImage[] = []
    for (const file of Array.from(files)) {
      if (!file.type.startsWith('image/')) continue
      const base64 = await readFileAsBase64(file)
      newImages.push({ id: generateId(), base64, name: file.name })
    }
    if (newImages.length) onAddImages(newImages)
    // Reset input so re-selecting same file works
    e.target.value = ''
    setMenuOpen(false)
  }

  const handleDocSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const selected = e.target.files
    if (!selected?.length) return
    const newFiles: PendingFile[] = []
    for (const file of Array.from(selected)) {
      const ext = '.' + file.name.split('.').pop()?.toLowerCase()
      const cap = isMediaExt(ext) ? MAX_MEDIA_FILE_SIZE : MAX_FILE_SIZE
      if (file.size > cap) continue
      newFiles.push({ id: generateId(), name: file.name, size: file.size, file })
    }
    if (newFiles.length) onAddFiles(newFiles)
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
    !anyFileUploading &&
    !anyFileErrored &&
    (text.trim().length > 0 || pendingImages.length > 0 || pendingFiles.length > 0)

  return (
    <div className="px-3 pb-3">
      <div className="max-w-4xl mx-auto">
        <div className="bg-white/90 dark:bg-gray-900/90 backdrop-blur-xs rounded-xl border border-p-border-light dark:border-gray-700 shadow-xs p-2">
          {/* Attachment preview row (images + files) */}
          {(pendingImages.length > 0 || pendingFiles.length > 0) && (
            <div className="flex gap-2 overflow-x-auto pt-2 pb-2 mb-2 border-b border-p-border-light/50 px-1.5 items-center">
              {pendingImages.map(img => (
                <div key={img.id} className="relative shrink-0 group">
                  <img
                    src={img.base64}
                    alt={img.name}
                    className="w-14 h-14 rounded-lg object-cover border border-p-border-light"
                  />
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
              {pendingFiles.map(f => (
                <div
                  key={f.id}
                  title={f.error || undefined}
                  className={`relative shrink-0 group flex items-center gap-1.5
                              bg-p-surface rounded-lg px-2.5 py-1.5 border
                              ${f.error ? 'border-p-accent-red' : 'border-p-border-light'}`}
                >
                  {f.uploading ? (
                    <span
                      className="w-4 h-4 border-2 border-brand border-t-transparent rounded-full animate-spin shrink-0"
                      aria-label="Uploading"
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
                  <span className="text-xs text-p-text-light">{formatFileSize(f.size)}</span>
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
              ))}
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
              onDrop={() => {
                // Native textarea drop inserts the text and SELECTS it.
                // Collapse the selection on the next frame so the user can
                // keep typing after the inserted path without accidentally
                // replacing it. We don't preventDefault — the browser's
                // native insertion handles cursor placement correctly; we
                // only fix the post-drop selection.
                requestAnimationFrame(() => {
                  const ta = textareaRef.current
                  if (!ta) return
                  const end = ta.selectionEnd
                  if (ta.selectionStart !== end) ta.setSelectionRange(end, end)
                })
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
                ttsAvailable={voice.ttsAvailable}
                live={voice.live}
                onSetLive={voice.onSetLive}
                speaking={voice.speaking}
                onBargeIn={voice.onBargeIn}
                streaming={!!streaming}
                onSendText={onSend}
                onClearInput={() => setText('')}
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
  )
}
