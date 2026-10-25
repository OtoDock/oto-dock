import { useRef, useCallback, useEffect, useState, useLayoutEffect } from 'react'
import { Capacitor } from '@capacitor/core'

export type { PendingImage, PendingFile } from '../../store/types'
import type { PendingImage, PendingFile } from '../../store/types'

import { MicIcon } from './MicIcon'
import { VoiceControl } from './VoiceControl'
import { PresenceHalo } from './PresenceHalo'
import type { DuplexPhase } from '../../hooks/useDuplexVoice'
import { useDictationDraft } from '../../hooks/useDictationDraft'

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
import { usePrimaryPointerCoarse } from '../../hooks/usePrimaryPointerCoarse'
import { usePasteGuard } from '../../hooks/usePasteGuard'
import { useComposerHeight } from '../../hooks/useComposerHeight'
import {
  canPickFolder, collectDropped, collectPicked, insideFolder, splitDropForAttach,
  FOLDER_PICKER_ATTRS, type CollectedItems,
} from '../../lib/droppedItems'
import { FILE_ICON_PATH, FOLDER_ICON_PATH } from './attachmentIcons'
import { enterAction, insertNewline, isComposingKey, pasteAttachesFiles } from '../../lib/composerKeys'
import { useFlag } from '../../hooks/useFlag'
import { useTransferStore } from '../../store/transferStore'
import { saveDocumentPane, useDocumentPushStore } from '../../store/documentPaneStore'
import { Bar } from '../common/TransferBar'
import { POPOVER } from '../ui/classes'
import { GLASS } from '../ui/glass'
import { SEND_SAVE_TIMEOUT_MS } from './media/DocumentFrame'
import ImageLightbox from './media/ImageLightbox'
import { fmtSize, pct } from '../common/transferFormat'

// Any file type is accepted (no extension allowlist — see lib/fileTypes);
// the universal cap mirrors proxy config OTODOCK_MAX_FILE_MB (default 1GB) —
// one number for every file type, sync + upload: the per-install value rides
// user.feature_flags (upload_max_bytes), its default is in hooks/useFlag.

/** How long the composer says a send went out before the document saved. */
export const DOC_NOTICE_MS = 8000

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

/** A fresh photo shows from its data URL; one handed back from a cancelled
 * queued message (already saved in the agent's workspace) through the same
 * agent files URL the chat history uses. */
function pendingImageSrc(img: PendingImage, agentName?: string): string {
  if (img.base64) return img.base64
  if (img.path && agentName) return `/v1/agents/${agentName}/files/${encodeURI(img.path)}`
  return ''
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
  /** The chat's agent: a photo handed back from a cancelled queued message
   * shows through its agent files URL. */
  agentName?: string
  onSend: (text: string) => void
  onAbort?: () => void
  onEditQueued?: () => void
  /** Fired once on the user's first genuine interaction with the composer
   * (first keydown or pointerdown on the textarea). Used to trigger a lazy
   * pre-warm for non-favorite agents. NOT fired on focus or on mount. */
  onEngage?: () => void
  disabled?: boolean
  /** Blocks ONLY sending (the button and the send keys) while keeping the composer
   * typeable — the user can keep drafting. Used while a cross-engine switch
   * awaits its confirmation (prompting is not allowed until it resolves). */
  sendDisabled?: boolean
  streaming?: boolean
  /** A send now joins the running turn (queued or steered): Send shows
   * beside Stop. False while the session warms up, where a send waits in a
   * single slot instead. */
  queueable?: boolean
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
  /** Attach a folder as one chip (its files, their paths inside it), or a
   * promise of it while a dropped folder is read (the chip shows at once);
   * a host without it turns a dropped folder away with an error chip. */
  onAddFolder?: (name: string, items: CollectedItems | Promise<CollectedItems>) => void
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
  /** Pinned apps overlay toggle — permanent (right of the workspace
   * button, left of the projects toggle) whenever the host page wires it. */
  appsOpen?: boolean
  onToggleApps?: () => void
  /** Lights up a small dot on the toggle while the overlay is open and a new
   * assistant message has arrived. */
  workspaceHasNewMessage?: boolean
  /** Animate the shrink and the regrow on a focus change (default). The host
   * turns it off while the slot above holds something that re-lays out on
   * every frame of it: a live terminal (each row is a PTY resize), an overlay,
   * the floating document pane. */
  animateHeight?: boolean
  /** Forwarded to the textarea so dropped agent file paths splice at the cursor. */
  textareaRef?: React.RefObject<HTMLTextAreaElement | null>
  /** Voice mode (AgentChat only). Hands-free speak → send → hear. Omitted where
   * voice mode isn't wired → no voice UI or behaviour. */
  voice?: ChatInputVoice
  /** The chat the draft belongs to (the chatStore key). A change is a chat
   * switch: dictation stops, its tail is dropped, and a phrase still in
   * flight never lands under the new key. A host without chat switching
   * may omit it. */
  draftKey?: string
}

/** The shape of the `voice` prop; the host page builds it
 * (pages/agent/chat/useChatDuplexVoice.ts). */
export type ChatInputVoice = {
  duplex?: import('./VoiceControl').DuplexControlProps  // full-duplex mode
}

/** Check if camera capture is available (mobile only — desktop browsers ignore capture attr) */
async function isCameraAvailable(): Promise<boolean> {
  if (Capacitor.isNativePlatform()) return true
  // On mobile web browsers, capture attribute works. On desktop it doesn't.
  // Detect mobile via touch support + screen size heuristic.
  const isMobile = 'ontouchstart' in window && window.innerWidth < 1024
  return isMobile
}

/** A collected folder's items with their paths taken inside it. */
function inside(got: CollectedItems, top: string): CollectedItems {
  return { ...got, files: insideFolder(got.files, top) }
}

function generateId(): string {
  return Math.random().toString(36).slice(2, 10)
}

export default function ChatInput({
  value,
  onChange,
  agentName,
  onSend,
  onAbort,
  onEditQueued,
  onEngage,
  disabled,
  sendDisabled,
  streaming,
  queueable,
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
  onAddFolder,
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
  animateHeight = true,
  voice,
  draftKey,
}: Props) {
  const text = value
  const setText = onChange
  const presenceDebugPhase = presenceDebugParam()
  // Per-install universal upload cap (OTODOCK_MAX_FILE_MB) with the shipped
  // default as fallback.
  const uploadCap = useFlag('upload_max_bytes')
  // Live mirror of the controlled value — the STT session's onFinal closure is
  // created once (when the mic starts) and would otherwise capture a stale
  // value, making each dictated phrase REPLACE the input instead of append.
  const valueRef = useRef(value)
  useLayoutEffect(() => { valueRef.current = value })
  // The dictation draft hook is built further down (it needs the height
  // controls); the send and input handlers above it reach it through a ref.
  const dictRef = useRef<ReturnType<typeof useDictationDraft> | null>(null)
  // Same live mirror for the draft key; the hook keeps the key the mic
  // started under and compares the two: they disagree from the commit that
  // switched chats until the discard below lands, and a phrase arriving in
  // that window belongs to the old chat, never the new one — the check is
  // a plain read on purpose.
  const draftKeyRef = useRef(draftKey)
  useLayoutEffect(() => { draftKeyRef.current = draftKey })
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
  const [dictationSlot, setDictationSlot] = useState<HTMLDivElement | null>(null)  // the mic's sentence row
  const [micDiscardSignal, setMicDiscardSignal] = useState(0)  // bump → close the mic, drop the tail (send)
  const internalRef = useRef<HTMLTextAreaElement>(null)
  const textareaRef = (externalTextareaRef ?? internalRef) as React.RefObject<HTMLTextAreaElement>
  const fileInputRef = useRef<HTMLInputElement>(null)
  const cameraInputRef = useRef<HTMLInputElement>(null)
  const docInputRef = useRef<HTMLInputElement>(null)
  const folderInputRef = useRef<HTMLInputElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const plusBtnRef = useRef<HTMLButtonElement>(null)
  const rootRef = useRef<HTMLDivElement>(null)

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
  }, [editText, onClearEditText, setText, textareaRef])

  const anyFileUploading = pendingFiles.some(f => f.uploading)
  const anyFileErrored = pendingFiles.some(f => f.error)
  // Touch devices have no hover, so the remove (✕) buttons must stay visible;
  // on desktop (fine pointer) they reveal on tile hover.
  const coarse = useCoarsePointer()
  const removeBtnVisibility = coarse ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'
  // The send keys exist only where the primary pointer is fine (a laptop or a
  // desktop); a phone or a tablet sends with the button.
  const touchPrimary = usePrimaryPointerCoarse()
  const pasteGuard = usePasteGuard()
  // The pending-photo lightbox is portaled out of the composer: while it is
  // open, using it never shrinks the composer.
  const height = useComposerHeight(textareaRef, rootRef, { animate: animateHeight, holdOpen: lightbox !== null })

  // The chat's document pane holds unsaved edits: the agent reads the
  // stored file, so a send saves them first (FEATURES.md "Document pane").
  const docDirty = useDocumentPushStore((s) => (draftKey ? s.dirty[draftKey] ?? null : null))
  const docDirtyName = useDocumentPushStore((s) => (draftKey ? s.dirtyNames[draftKey] ?? '' : ''))
  const [docSaving, setDocSaving] = useState(false)
  const [docNotice, setDocNotice] = useState('')
  useEffect(() => {
    if (!docNotice) return
    const t = setTimeout(() => setDocNotice(''), DOC_NOTICE_MS)
    return () => clearTimeout(t)
  }, [docNotice])
  const attachmentsRef = useRef(0)
  // The composer's own guards as they are when the save answers.
  const blockedRef = useRef(false)
  // The page's send as it is at the moment it runs: a send that waited for
  // the save goes out with the page's state then (a turn may have started
  // or ended, attachments changed), never the state of the tap.
  const onSendRef = useRef(onSend)
  useLayoutEffect(() => {
    attachmentsRef.current = pendingImages.length + pendingFiles.length
    blockedRef.current = anyFileUploading || anyFileErrored || !!sendDisabled
    onSendRef.current = onSend
  })
  // The save a send waits for, with the chat it was started in: a chat
  // switch lets it go (its send is then dropped), so the next chat's
  // composer is never held by it.
  const savingRef = useRef<{ key: string; token: object } | null>(null)
  const [docKey, setDocKey] = useState(draftKey)
  if (docKey !== draftKey) {
    setDocKey(draftKey)
    setDocSaving(false)
    setDocNotice('')
  }

  const handleSend = useCallback(async () => {
    if (anyFileUploading || sendDisabled || savingRef.current?.key === draftKey) return
    let trimmed = text.trim()
    if (!trimmed && pendingImages.length === 0 && pendingFiles.length === 0) return
    if (docDirty && draftKey) {
      // The button stays disabled while the editor saves (up to 1.5 s). What
      // goes out is the composer as it is then, in the chat it was sent in.
      const key = draftKey
      const mine = {}
      savingRef.current = { key, token: mine }
      setDocSaving(true)
      let saved: boolean
      try {
        saved = await saveDocumentPane(key, SEND_SAVE_TIMEOUT_MS)
      } finally {
        if (savingRef.current?.token === mine) {
          savingRef.current = null
          setDocSaving(false)
        }
      }
      if (draftKeyRef.current !== key || blockedRef.current) return
      trimmed = valueRef.current.trim()
      if (!trimmed && attachmentsRef.current === 0) return
      if (!saved) setDocNotice("The document's last edits are not saved yet.")
    }
    onSendRef.current(trimmed)
    setText('')
    // Close an open dictation mic AND drop its tail: the text was just sent,
    // so a late stop-flush final must not re-fill the cleared input. Reset
    // the dictation accumulator for the same reason.
    dictRef.current?.reset()
    setMicDiscardSignal(n => n + 1)
  }, [text, pendingImages, pendingFiles, anyFileUploading, sendDisabled, docDirty, draftKey, setText])

  // First genuine interaction with the composer → fire onEngage once. Guarded
  // by a ref so it never re-fires; bound to keydown/pointerdown (NOT focus or
  // mount) so a programmatic .focus() can't trigger a lazy pre-warm.
  const engagedRef = useRef(false)
  const handleEngage = () => {
    if (engagedRef.current) return
    engagedRef.current = true
    onEngage?.()
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    handleEngage()
    height.settle()
    pasteGuard.onKeyDown(e)
    if (isComposingKey(e.nativeEvent)) return
    const action = enterAction(e.nativeEvent, { coarse: touchPrimary, pasteHeld: pasteGuard.held() })
    if (action !== 'pass') e.preventDefault()
    if (action === 'newline') insertNewline(e.currentTarget, setText)
    if (action === 'send' && !anyFileUploading && !anyFileErrored) void handleSend()
    if (e.key === 'ArrowUp' && !text && queuedCount > 0 && onEditQueued) {
      e.preventDefault()
      onEditQueued()
    }
  }

  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setText(e.target.value)
    dictRef.current?.rebaseline(e.target.value)
    height.settle()
    height.fit()
  }

  const nudgeResize = () => height.fit()
  // Autosize on EVERY value change: programmatic sets bypass handleInput —
  // a draft restored from localStorage on mount, edit/template insertion,
  // and clears all rendered at rows=1 (or stayed stretched) until the next
  // keystroke. Idempotent with the onChange-path resize.
  useEffect(() => {
    nudgeResize()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value])
  // Mic dictation: interims REPLACE (a preview), finals ACCUMULATE, a language
  // switch REWRITES the words the session placed (hooks/useDictationDraft.ts).
  // Bypasses handleInput, so the hook nudges the textarea auto-resize.
  const dict = useDictationDraft({
    valueRef, draftKeyRef, setText,
    // Dictated text must land in view; phone mode's caption bridge calls this
    // on every utterance and leaves the height alone.
    onStart: () => { if (!voice?.duplex?.active) height.grow() },
    afterChange: nudgeResize,
  })
  useEffect(() => { dictRef.current = dict })
  const { onMicActive, showInterim, appendTranscript, replaceTranscript } = dict
  // A chat switch ends dictation: close the mic and drop its tail. Bumped
  // even when the mic is already off — a stopped attempt keeps delivering
  // its tail on purpose (the mic-button stop wants the last phrase), and
  // that tail must not follow the user into the next chat. Compared against
  // the previous key, so the mount run and StrictMode's second run are
  // no-ops. The minted id adopted after a send is a key change too and ends
  // a dictation started in that window (the send already closed the mic).
  const prevDraftKeyRef = useRef(draftKey)
  useEffect(() => {
    if (prevDraftKeyRef.current === draftKey) return
    prevDraftKeyRef.current = draftKey
    dictRef.current?.reset(true)
    setMicDiscardSignal(n => n + 1)
  }, [draftKey])

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
      let budget = pendingImages.reduce((a, img) => a + (img.base64 ? dataUrlBytes(img.base64) : 0), 0)
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
            error: 'Too many photos for one message. Remove some, or send this one as a file.',
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

  const handleUploadFolder = () => {
    setMenuOpen(false)
    folderInputRef.current?.click()
  }

  const handleFolderSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const picked = e.target.files ? Array.from(e.target.files) : []
    e.target.value = ''
    if (!picked.length || !onAddFolder) return
    const top = (picked[0].webkitRelativePath || picked[0].name).split('/')[0]
    onAddFolder(top, inside(collectPicked(picked), top))
  }

  const canSend =
    !disabled &&
    !sendDisabled &&
    !docSaving &&
    !anyFileUploading &&
    !anyFileErrored &&
    (text.trim().length > 0 || pendingImages.length > 0 || pendingFiles.length > 0)

  // While a turn runs Stop takes Send's place; with something to send, Send
  // comes back beside a square Stop so a phone can still queue a message.
  // Never in phone mode: the live caption fills the box, and the engine sends
  // the spoken turn itself.
  const queueSend = !!queueable && canSend && !aborting && !voice?.duplex?.active
  const sendButton = (
    <button
      onClick={() => void handleSend()}
      disabled={!canSend}
      aria-label="Send"
      title={touchPrimary ? undefined : 'Send: Shift+Enter'}
      aria-keyshortcuts={touchPrimary ? undefined : 'Shift+Enter Control+Enter Meta+Enter'}
      className="w-9 h-9 rounded-control text-sm font-medium text-action-fg flex items-center justify-center
                 bg-action hover:bg-action-hover disabled:bg-surface-muted disabled:text-tertiary disabled:cursor-not-allowed
                 transition-colors shrink-0"
    >
      <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 19V5m0 0l-7 7m7-7l7 7" />
      </svg>
    </button>
  )

  return (
    <div ref={rootRef} className="px-3 pb-composer-safe">
      <div className="max-w-4xl mx-auto">
        {(docNotice || docDirty) && (
          <p role="status" className="mb-1 truncate px-1 text-xs text-status-warning-fg">
            {docNotice || (docSaving
              ? `Saving ${docDirtyName || 'the document'} before sending.`
              : `Unsaved edits in ${docDirtyName || 'the document'}.`)}
          </p>
        )}
        {/* The dictation's sentence (a slow link, a stop, the time limit):
            the mic renders it here, above the pill, never over its text. */}
        <div ref={setDictationSlot} />
        {/* relative wrapper: the PresenceHalo canvas positions against the
            pill; the pill's explicit z-index keeps content above the glow
            (before, that layering only held by accident of backdrop-blur's
            stacking context) and, at z-40, lifts the popups that open
            upward from the pill (the language menu, the model dropdown,
            the mic hints) above every layer of the chat column (the
            floating document pane at z-15, the terminal's permission bar
            at z-20, the artifact windows at z-30): a popup's top rows over
            a live terminal used to be caught by those. Modals and toasts
            stay fixed at z-50. */}
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
          className={`relative z-40 ${GLASS} rounded-composer p-2`}
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
            // A dropped folder reaches `files` as a placeholder no upload can
            // read: it is walked through its entry and attached as one chip.
            const { files, folders } = splitDropForAttach(e.dataTransfer)
            setDragDepth(0)
            if (!files.length && !folders.length) return // internal path drag → textarea handles it
            e.preventDefault()
            for (const folder of folders) {
              if (!onAddFolder) {
                onAddFiles([{
                  id: generateId(), name: folder.name, size: 0, folder: true,
                  error: 'Folders upload in the workspace here',
                }])
                continue
              }
              onAddFolder(folder.name, collectDropped([folder]).then((got) => inside(got, folder.name)))
            }
            if (files.length) void ingestFiles(files)
          }}
        >
          {dragDepth > 0 && (
            <div className="absolute inset-0 z-20 rounded-composer border-2 border-dashed border-action bg-action-subtle
                            flex items-center justify-center pointer-events-none">
              <span className="text-sm font-medium text-link">Drop to attach</span>
            </div>
          )}
          {/* Attachment preview row (images + files) */}
          {(pendingImages.length > 0 || pendingFiles.length > 0) && (
            <div className="flex gap-2 overflow-x-auto pt-2 pb-2 mb-2 border-b border-subtle px-1.5 items-center">
              {pendingImages.map((img, i) => (
                <div key={img.id} className="relative shrink-0 group">
                  {/* Tile opens the standard lightbox (zoom/swipe) so the
                      photo can be checked while composing; ✕ stays removal. */}
                  <button
                    type="button"
                    onClick={() => setLightbox({ images: [...pendingImages], idx: i })}
                    aria-label={`Preview ${img.name}`}
                    title="Click to preview"
                    className="block rounded-control focus:outline-hidden focus-visible:ring-2 focus-visible:ring-focus-ring"
                  >
                    <img
                      src={pendingImageSrc(img, agentName)}
                      alt={img.name}
                      className="w-14 h-14 rounded-card object-cover border border-subtle cursor-pointer"
                    />
                  </button>
                  <button
                    onClick={() => onRemoveImage(img.id)}
                    aria-label="Remove image"
                    className={`absolute -top-1.5 -right-1.5 z-10 w-5 h-5 rounded-full bg-status-danger-fill text-on-solid
                               flex items-center justify-center text-xs ring-2 ring-surface-card
                               shadow-elevation-1 transition-opacity ${removeBtnVisibility}`}
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
                    ? (f.folder && f.fileCount == null ? 'Reading the folder…' : 'Queued')
                    : uploadingNow
                      ? `${uploadPct}% · ${fmtSize(f.uploadSent ?? 0)} of ${fmtSize(f.uploadTotal ?? f.size)}`
                      : syncing
                        ? 'Syncing to machines…'
                        : f.note || null
                return (
                  <div
                    key={f.id}
                    title={f.error || undefined}
                    className={`relative shrink-0 group flex flex-col gap-1
                                bg-surface-muted rounded-card px-2.5 py-1.5 border
                                ${(uploadingNow || syncing) ? 'min-w-[160px]' : ''}
                                max-w-[220px]
                                ${f.error ? 'border-status-danger-solid' : 'border-subtle'}`}
                  >
                    <div className="flex items-center gap-1.5">
                      {f.uploading ? (
                        <span
                          className="w-4 h-4 border-2 border-action border-t-transparent rounded-full animate-spin shrink-0"
                          aria-label={f.queued ? 'Queued' : 'Uploading'}
                        />
                      ) : f.error ? (
                        <svg className="w-4 h-4 text-status-danger-fg shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                                d="M12 9v2m0 4h.01M12 5l7 12H5l7-12z" />
                        </svg>
                      ) : (
                        <svg className="w-4 h-4 text-secondary shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                                d={f.folder ? FOLDER_ICON_PATH : FILE_ICON_PATH} />
                        </svg>
                      )}
                      <span className="text-xs text-primary truncate max-w-[100px]">{f.name}</span>
                      <span className="text-xs text-tertiary shrink-0">
                        {f.folder
                          ? f.fileCount == null ? 'Folder' : `${f.fileCount} file${f.fileCount === 1 ? '' : 's'}`
                          : formatFileSize(f.size)}
                      </span>
                      {f.error && onRetryFile && ((f.file && f.size <= uploadCap) || f.folderFiles) && (
                        <button
                          onClick={() => onRetryFile(f.id)}
                          aria-label="Retry upload"
                          title="Retry upload"
                          className="w-4 h-4 rounded-full text-secondary hover:text-link
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
                        className={`w-4 h-4 rounded-full bg-status-danger-fill text-on-solid flex items-center justify-center
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
                        className={`text-3xs leading-tight truncate
                                    ${f.error ? 'text-status-danger-fg' : 'text-tertiary'}`}
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
                title={workspaceOpen ? 'Close workspace: Esc' : 'Open workspace: Ctrl+E'}
                className={`relative w-9 h-9 -mr-0.5 rounded-control flex items-center justify-center transition-colors shrink-0
                  ${workspaceOpen
                    ? 'bg-action text-action-fg hover:bg-action-hover'
                    : 'text-secondary hover:text-primary hover:bg-surface-hover'}
                `}
              >
                <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
                  <path d="M2 6a2 2 0 012-2h5l2 2h5a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V6z" />
                </svg>
                {workspaceOpen && workspaceHasNewMessage && (
                  <span className="absolute top-1 right-1 w-2 h-2 rounded-full bg-action-fg ring-2 ring-action" />
                )}
              </button>
            )}
            {/* Pinned apps toggle — permanent, right of workspace. */}
            {onToggleApps && (
              <button
                onClick={onToggleApps}
                title={appsOpen ? 'Close apps' : 'Open apps'}
                className={`w-9 h-9 -mr-0.5 rounded-control flex items-center justify-center transition-colors shrink-0
                  ${appsOpen
                    ? 'bg-action text-action-fg hover:bg-action-hover'
                    : 'text-secondary hover:text-primary hover:bg-surface-hover'}
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
                className={`w-9 h-9 -mr-0.5 rounded-control flex items-center justify-center transition-colors shrink-0
                  ${projectsOpen
                    ? 'bg-action text-action-fg hover:bg-action-hover'
                    : 'text-secondary hover:text-primary hover:bg-surface-hover'}
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
                className="w-9 h-9 rounded-control flex items-center justify-center
                           text-secondary hover:text-primary hover:bg-surface-hover
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
                  className={`absolute bottom-full left-0 mb-2 ${POPOVER} py-1 min-w-[160px] z-50`}
                >
                  {hasCamera && (
                    <button
                      onClick={handleCamera}
                      className="flex items-center gap-2.5 w-full px-3 py-2 text-sm text-primary
                                 hover:bg-surface-hover transition-colors"
                    >
                      <svg className="w-4 h-4 text-secondary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
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
                    className="flex items-center gap-2.5 w-full px-3 py-2 text-sm text-primary
                               hover:bg-surface-hover transition-colors"
                  >
                    <svg className="w-4 h-4 text-secondary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                            d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
                    </svg>
                    Upload Photo
                  </button>
                  <button
                    onClick={handleUploadFile}
                    className="flex items-center gap-2.5 w-full px-3 py-2 text-sm text-primary
                               hover:bg-surface-hover transition-colors"
                  >
                    <svg className="w-4 h-4 text-secondary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                            d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                    </svg>
                    Upload File
                  </button>
                  {onAddFolder && canPickFolder() && (
                    <button
                      onClick={handleUploadFolder}
                      className="flex items-center gap-2.5 w-full px-3 py-2 text-sm text-primary
                                 hover:bg-surface-hover transition-colors"
                    >
                      <svg className="w-4 h-4 text-secondary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d={FOLDER_ICON_PATH} />
                      </svg>
                      Upload Folder
                    </button>
                  )}
                </div>
              )}
            </div>

            <textarea
              ref={textareaRef}
              value={text}
              onChange={handleInput}
              onKeyDown={handleKeyDown}
              onKeyUp={pasteGuard.onKeyUp}
              onPointerDown={handleEngage}  // first genuine interaction → lazy pre-warm (non-favorite agents)
              onFocus={() => {
                setMicStopSignal(n => n + 1)  // clicking the input stops/pauses the mic
                height.onFocus()
              }}
              onBlur={(e) => {
                pasteGuard.onBlur()
                height.onBlur(e)
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
                pasteGuard.onPaste()
                // Ctrl/Cmd+V with files on the clipboard (screenshots, copied
                // files) attaches them exactly like the + menu; a paste from
                // an office suite (its text beside a picture of it) inserts
                // the text instead, and plain text pastes are untouched.
                const files = e.clipboardData?.files
                const clip = e.clipboardData
                if (files && files.length > 0 && !disabled
                    && pasteAttachesFiles(clip.getData?.('text/plain') ?? '', files,
                                          (clip.getData?.('text/html') ?? '')
                                          + (clip.getData?.('text/rtf') ?? ''))) {
                  e.preventDefault()
                  void ingestFiles(files)
                }
              }}
              placeholder={placeholder || 'Type a message...'}
              disabled={disabled}
              rows={1}
              className="flex-1 resize-none rounded-control px-3 py-2 text-sm bg-transparent
                         focus:outline-hidden order-first basis-full
                         disabled:text-tertiary placeholder:text-tertiary"
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
                onDictateReplace={replaceTranscript}
                onDictateActive={onMicActive}
                interruptSignal={micStopSignal}
                discardSignal={micDiscardSignal}
                disabled={disabled}
                sentenceSlot={dictationSlot}
              />
            ) : (
              <MicIcon
                onTranscript={appendTranscript}
                onInterim={showInterim}
                onReplace={replaceTranscript}
                onActive={onMicActive}
                discardSignal={micDiscardSignal}
                disabled={disabled}
                sentenceSlot={dictationSlot}
              />
            )}
            {streaming && onAbort ? (
              <>
                {queueSend ? (
                  <button
                    onClick={onAbort}
                    aria-label="Stop"
                    title="Stop"
                    className="w-9 h-9 rounded-control flex items-center justify-center text-on-solid shrink-0
                               bg-status-danger-fill hover:bg-status-danger-fill-hover transition-colors"
                  >
                    <svg className="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24">
                      <rect x="5" y="5" width="14" height="14" rx="2" />
                    </svg>
                  </button>
                ) : (
                  <button
                    onClick={onAbort}
                    disabled={aborting}
                    className={`px-4 py-2 rounded-control text-sm font-medium transition-colors shrink-0 ${
                      aborting
                        ? 'bg-surface-muted text-tertiary cursor-not-allowed'
                        : 'bg-status-danger-fill text-on-solid hover:bg-status-danger-fill-hover'
                    }`}
                  >
                    {aborting ? 'Stopping...' : 'Stop'}
                  </button>
                )}
                {queueSend && sendButton}
              </>
            ) : sendButton}
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
          {/* Hidden folder picker — desktop browsers only */}
          <input
            ref={folderInputRef}
            type="file"
            onChange={handleFolderSelect}
            className="hidden"
            {...FOLDER_PICKER_ATTRS}
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
            const m = img.base64?.match(/^data:([^;,]+);base64,(.*)$/)
            return m
              ? { imageData: m[2], mimeType: m[1], caption: img.name }
              : { url: pendingImageSrc(img, agentName), caption: img.name }
          })}
          initialIndex={Math.min(lightbox.idx, lightbox.images.length - 1)}
          onClose={() => setLightbox(null)}
        />
      )}
    </div>
  )
}
