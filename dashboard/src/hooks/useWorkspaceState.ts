import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { computeRangeSelection } from '../lib/paths'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type WorkspaceView = 'grid' | 'tree'
export type ClipboardMode = 'cut' | 'copy'

export interface WorkspaceMemory {
  /** Last scope key the user was browsing in this agent. */
  scope: string
  /** Path of the last folder the user opened, relative to the agent root. */
  path: string
  view: WorkspaceView
  selected: string[]
  /** Anchor for Shift+click range-select. Reset whenever `path` changes. */
  lastClickedPath: string | null
}

export interface Clipboard {
  mode: ClipboardMode
  paths: string[]
  scope: string
}

export interface WorkspaceState extends WorkspaceMemory {
  open: boolean
  preview: string | null
  hasNewMessage: boolean
  /** Mobile selection-mode flag — when true, tile taps toggle selection
   * instead of opening files. Entered via long-press, exited via "Done". */
  selectionMode: boolean
}

// Persisted per agent so reopening after chat-switch / send returns to the
// same folder. Cleared on agent switch.
const memoryByAgent = new Map<string, WorkspaceMemory>()
const clipboardByAgent = new Map<string, Clipboard>()

const DEFAULT_MEMORY: WorkspaceMemory = {
  scope: '',
  path: '',
  view: 'grid',
  selected: [],
  lastClickedPath: null,
}

function readMemory(agent: string): WorkspaceMemory {
  return memoryByAgent.get(agent) ?? DEFAULT_MEMORY
}

function writeMemory(agent: string, mem: WorkspaceMemory) {
  memoryByAgent.set(agent, mem)
}

export function resetWorkspaceMemory(agent: string) {
  memoryByAgent.delete(agent)
  clipboardByAgent.delete(agent)
}

// ---------------------------------------------------------------------------
// Reducer
// ---------------------------------------------------------------------------

type Action =
  | { type: 'setScope'; scope: string; resetPath?: boolean }
  | { type: 'setPath'; path: string }
  | { type: 'setView'; view: WorkspaceView }
  | { type: 'replaceSelection'; path: string }
  | { type: 'toggleSelection'; path: string }
  | { type: 'rangeSelect'; visibleOrder: string[]; target: string }
  | { type: 'setSelectionExplicit'; paths: string[]; lastClickedPath?: string | null }
  | { type: 'clearSelection' }
  | { type: 'rehydrate'; memory: WorkspaceMemory }

function reducer(state: WorkspaceMemory, action: Action): WorkspaceMemory {
  switch (action.type) {
    case 'setScope':
      return {
        ...state,
        scope: action.scope,
        path: action.resetPath ? '' : state.path,
        selected: [],
        lastClickedPath: null,
      }
    case 'setPath':
      return { ...state, path: action.path, selected: [], lastClickedPath: null }
    case 'setView':
      return { ...state, view: action.view }
    case 'replaceSelection':
      return { ...state, selected: [action.path], lastClickedPath: action.path }
    case 'toggleSelection': {
      const next = state.selected.includes(action.path)
        ? state.selected.filter((p) => p !== action.path)
        : [...state.selected, action.path]
      return { ...state, selected: next, lastClickedPath: action.path }
    }
    case 'rangeSelect': {
      const anchor = state.lastClickedPath ?? action.target
      const range = computeRangeSelection(action.visibleOrder, anchor, action.target)
      return { ...state, selected: range, lastClickedPath: action.target }
    }
    case 'setSelectionExplicit':
      return {
        ...state,
        selected: action.paths,
        lastClickedPath: action.lastClickedPath ?? state.lastClickedPath,
      }
    case 'clearSelection':
      return { ...state, selected: [], lastClickedPath: null }
    case 'rehydrate':
      return action.memory
    default:
      return state
  }
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

/**
 * Workspace state shared by AgentChat and the overlay.
 *
 * URL params (via React Router):
 *   - `?ws=1`            → overlay is open
 *   - `?ws_preview=<p>`  → file preview portal is open over the overlay
 *
 * Non-URL state (scope / path / view / selected / clipboard / selection
 * mode) lives in this hook AND module-level per-agent maps so reopening
 * after auto-close (send, chat switch) restores the same folder.
 *
 * Agent-switch (`agentName` prop changes) resets BOTH maps — selection,
 * folder memory, and clipboard all drop.
 *
 * `lastAssistantMessageId` drives the "new message" dot: snapshot at
 * overlay-open time; dot lights while the live id diverges.
 */
export function useWorkspaceState(agentName: string, lastAssistantMessageId: string | null) {
  const [searchParams, setSearchParams] = useSearchParams()

  // Local reducer state seeded from the memory map for this agent.
  const [memory, dispatch] = useReducer(reducer, undefined, () => readMemory(agentName))

  // Mirror clipboard from the per-agent map into local state so consumers
  // re-render on changes; writes go to BOTH (component state + module map).
  const [clipboard, setClipboardState] = useState<Clipboard | null>(
    () => clipboardByAgent.get(agentName) ?? null,
  )

  // Selection mode is purely transient (resets every overlay-close); kept
  // outside the persisted memory map.
  const [selectionMode, setSelectionMode] = useState(false)

  // Keep the memory map in sync — every reducer commit persists.
  useEffect(() => {
    writeMemory(agentName, memory)
  }, [agentName, memory])

  // Reset to default + clear memory + clipboard when the user switches agents.
  const prevAgentRef = useRef(agentName)
  useEffect(() => {
    if (prevAgentRef.current === agentName) return
    resetWorkspaceMemory(prevAgentRef.current)
    prevAgentRef.current = agentName
    dispatch({ type: 'rehydrate', memory: readMemory(agentName) })
    setClipboardState(clipboardByAgent.get(agentName) ?? null)
    setSelectionMode(false)
  }, [agentName])

  const open = searchParams.get('ws') === '1'
  const previewPath = searchParams.get('ws_preview') || null

  // Exit selection mode whenever the overlay closes.
  useEffect(() => {
    if (!open) setSelectionMode(false)
  }, [open])

  // Snapshot the assistant message id at the moment the overlay opens; the
  // dot stays lit while the live id diverges. Cleared when the overlay closes.
  const seenRef = useRef<string | null>(null)
  useEffect(() => {
    if (open) {
      seenRef.current = lastAssistantMessageId
    } else {
      seenRef.current = null
    }
  // We intentionally only run on `open` toggles; updates to the message id
  // while the overlay is open must NOT reset the seen baseline.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])
  const hasNewMessage =
    open &&
    seenRef.current !== null &&
    lastAssistantMessageId !== null &&
    seenRef.current !== lastAssistantMessageId

  // ---- URL helpers (preserve all other params) ----

  const writeOpen = useCallback(
    (value: boolean) => {
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev)
        if (value) next.set('ws', '1')
        else {
          next.delete('ws')
          next.delete('ws_preview')
        }
        return next
      })
    },
    [setSearchParams],
  )

  const writePreview = useCallback(
    (path: string | null) => {
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev)
        if (path) next.set('ws_preview', path)
        else next.delete('ws_preview')
        return next
      })
    },
    [setSearchParams],
  )

  // ---- Public actions ----

  const openWorkspace = useCallback(() => writeOpen(true), [writeOpen])
  const closeWorkspace = useCallback(() => writeOpen(false), [writeOpen])
  const toggleWorkspace = useCallback(() => writeOpen(!open), [open, writeOpen])
  const openPreview = useCallback((p: string) => writePreview(p), [writePreview])
  const closePreview = useCallback(() => writePreview(null), [writePreview])

  const setScope = useCallback(
    (scope: string) => dispatch({ type: 'setScope', scope, resetPath: true }),
    [],
  )
  const setPath = useCallback((path: string) => dispatch({ type: 'setPath', path }), [])
  const setView = useCallback((view: WorkspaceView) => dispatch({ type: 'setView', view }), [])
  const select = useCallback(
    (path: string, replace: boolean = true) =>
      dispatch(replace ? { type: 'replaceSelection', path } : { type: 'toggleSelection', path }),
    [],
  )
  const rangeSelect = useCallback(
    (visibleOrder: string[], target: string) =>
      dispatch({ type: 'rangeSelect', visibleOrder, target }),
    [],
  )
  const setSelection = useCallback(
    (paths: string[], lastClickedPath?: string | null) =>
      dispatch({ type: 'setSelectionExplicit', paths, lastClickedPath }),
    [],
  )
  const clearSelection = useCallback(() => dispatch({ type: 'clearSelection' }), [])

  // Selection mode handlers.
  const enterSelectionMode = useCallback((path: string) => {
    setSelectionMode(true)
    dispatch({ type: 'replaceSelection', path })
  }, [])
  const exitSelectionMode = useCallback(() => {
    setSelectionMode(false)
    dispatch({ type: 'clearSelection' })
  }, [])

  // Clipboard.
  const setClipboard = useCallback(
    (mode: ClipboardMode, paths: string[], scope: string) => {
      if (paths.length === 0) return
      const next: Clipboard = { mode, paths, scope }
      clipboardByAgent.set(agentName, next)
      setClipboardState(next)
    },
    [agentName],
  )
  const clearClipboard = useCallback(() => {
    clipboardByAgent.delete(agentName)
    setClipboardState(null)
  }, [agentName])
  /** Drop clipboard entries whose path was just moved/deleted so a stale
   * Ctrl+V doesn't fail with 404s on the now-missing sources. Clears the
   * clipboard entirely when no paths remain. */
  const dropFromClipboard = useCallback(
    (affected: string[]) => {
      if (affected.length === 0) return
      const current = clipboardByAgent.get(agentName)
      if (!current) return
      const remaining = current.paths.filter((p) => !affected.includes(p))
      if (remaining.length === current.paths.length) return
      if (remaining.length === 0) {
        clipboardByAgent.delete(agentName)
        setClipboardState(null)
        return
      }
      const next: Clipboard = { ...current, paths: remaining }
      clipboardByAgent.set(agentName, next)
      setClipboardState(next)
    },
    [agentName],
  )

  const state = useMemo<WorkspaceState>(
    () => ({
      ...memory,
      open,
      preview: previewPath,
      hasNewMessage,
      selectionMode,
    }),
    [memory, open, previewPath, hasNewMessage, selectionMode],
  )

  return {
    state,
    clipboard,
    openWorkspace,
    closeWorkspace,
    toggleWorkspace,
    openPreview,
    closePreview,
    setScope,
    setPath,
    setView,
    select,
    rangeSelect,
    setSelection,
    clearSelection,
    enterSelectionMode,
    exitSelectionMode,
    setClipboard,
    clearClipboard,
    dropFromClipboard,
  }
}
