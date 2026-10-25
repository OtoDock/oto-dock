import { useState, useEffect, useRef, useCallback, useLayoutEffect } from 'react'
import { useAgentFileContent, useSaveAgentFile } from '../api/agents'
import { encodePathSegments } from '../lib/paths'
import { errorText } from '../lib/http/apiJson'
import { blockedByModal } from '../lib/ui/modalLayers'
import Button from './ui/Button'

interface FileEditorProps {
  agent: string
  path: string
  readOnly?: boolean
  /** Hide the inner toolbar; useful when the editor is embedded in a portal
   * that owns the filename chrome. The Save button moves to a floating
   * action so the user keeps the ability to save. */
  compact?: boolean
}

export default function FileEditor({ agent, path, readOnly = false, compact = false }: FileEditorProps) {
  const { data: content, isLoading, error } = useAgentFileContent(agent, path)
  const saveMutation = useSaveAgentFile()

  const [value, setValue] = useState('')
  const [savedFeedback, setSavedFeedback] = useState(false)
  const [jsonError, setJsonError] = useState<string | null>(null)

  // Latest value via a ref so the sync effect can check dirtiness without
  // re-running on every keystroke.
  const valueRef = useRef(value)
  useLayoutEffect(() => { valueRef.current = value })
  // The (path, content) last loaded into the editor.
  const syncedRef = useRef<{ path: string; content: string } | null>(null)

  // Load content on first load or when the path changes. On a background
  // refetch of the SAME file (e.g. an agent edited it on disk), keep the
  // user's unsaved edits instead of silently overwriting them.
  useEffect(() => {
    if (content === undefined) return
    const prev = syncedRef.current
    const pathChanged = !prev || prev.path !== path
    const dirty = !pathChanged && valueRef.current !== prev!.content
    if (pathChanged || !dirty) setValue(content)
    syncedRef.current = { path, content }
  }, [content, path])

  const isDirty = content !== undefined && value !== content
  const isJson = path.endsWith('.json')

  const handleSave = useCallback(async () => {
    if (readOnly) return
    if (isJson) {
      try {
        JSON.parse(value)
        setJsonError(null)
      } catch (e) {
        setJsonError(errorText(e))
        return
      }
    }
    try {
      await saveMutation.mutateAsync({ agent, path, content: value })
      setSavedFeedback(true)
      setTimeout(() => setSavedFeedback(false), 2000)
    } catch {
      // mutation error is available via saveMutation.error
    }
  }, [agent, path, value, isJson, saveMutation, readOnly])

  // Ctrl/Cmd+S keyboard shortcut
  useEffect(() => {
    if (readOnly) return
    const onKeyDown = (e: KeyboardEvent) => {
      const save = (e.metaKey || e.ctrlKey) && e.key === 's'
      // With a dialog open over the editor the save waits, and the browser's
      // own "Save page" is kept away. A preview opened from inside a dialog
      // is inside it, so its target is not blocked.
      if (blockedByModal(e)) {
        if (save) e.preventDefault()
        return
      }
      if (save) {
        e.preventDefault()
        handleSave()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [handleSave, readOnly])

  if (isLoading) return <p className="text-sm text-tertiary p-4">Loading...</p>
  if (error) {
    return (
      <div className="text-sm p-4 space-y-2">
        <p className="text-status-danger-fg">{(error as Error).message || 'Failed to load file.'}</p>
        <a
          href={`/v1/agents/${encodeURIComponent(agent)}/files/${encodePathSegments(path)}?download=true&fn=${encodeURIComponent(path.split('/').pop() ?? path)}`}
          className="text-link hover:underline"
        >
          Download the file
        </a>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full relative">
      {/* Toolbar — hidden in `compact` mode (portal owns the filename chrome) */}
      {!compact && (
        <div className="flex items-center gap-2 px-3 py-2 border-b border-default bg-surface-sunken min-w-0">
          <span className="text-xs md:text-sm font-mono text-secondary truncate min-w-0 flex-1">{path.split('/').pop()}</span>
          {readOnly && <span className="text-3xs md:text-xs text-tertiary shrink-0">read-only</span>}
          {isDirty && !readOnly && <span className="text-3xs md:text-xs text-status-warning-fg shrink-0">Modified</span>}
          {savedFeedback && <span className="text-3xs md:text-xs text-status-success-fg shrink-0">Saved</span>}
          {!readOnly && (
            <Button variant="primary" size="xs"
              onClick={handleSave}
              disabled={saveMutation.isPending || !isDirty}
              className="shrink-0"
            >
              {saveMutation.isPending ? 'Saving...' : 'Save'}
            </Button>
          )}
        </div>
      )}

      {/* Compact mode floating Save action */}
      {compact && !readOnly && isDirty && (
        <Button variant="primary" size="xs"
          onClick={handleSave}
          disabled={saveMutation.isPending}
          className="absolute top-2 right-2 z-10"
        >
          {saveMutation.isPending ? 'Saving…' : savedFeedback ? 'Saved' : 'Save'}
        </Button>
      )}

      {/* Errors */}
      {jsonError && (
        <div className="px-3 py-2 bg-status-danger-bg border-b border-status-danger-border text-xs text-status-danger-fg">
          Invalid JSON: {jsonError}
        </div>
      )}
      {saveMutation.error && (
        <div className="px-3 py-2 bg-status-danger-bg border-b border-status-danger-border text-xs text-status-danger-fg">
          {(saveMutation.error as Error).message || 'The file was not saved. Try again.'}
        </div>
      )}

      {/* Editor */}
      <textarea
        value={value}
        onChange={(e) => {
          if (!readOnly) {
            setValue(e.target.value)
            if (jsonError) setJsonError(null)
          }
        }}
        readOnly={readOnly}
        spellCheck={false}
        className={`flex-1 w-full p-3 font-mono text-sm text-primary bg-surface-card resize-none focus:outline-hidden ${
          readOnly ? 'bg-surface-sunken cursor-default' : ''
        }`}
      />
    </div>
  )
}
