import { useState, useEffect, useRef, useCallback } from 'react'
import { useAgentFileContent, useSaveAgentFile } from '../api/agents'

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
  valueRef.current = value
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
      } catch (e: any) {
        setJsonError(e.message)
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
      if ((e.metaKey || e.ctrlKey) && e.key === 's') {
        e.preventDefault()
        handleSave()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [handleSave, readOnly])

  if (isLoading) return <p className="text-sm text-gray-500 p-4">Loading...</p>
  if (error) return <p className="text-sm text-red-500 p-4">Failed to load file.</p>

  return (
    <div className="flex flex-col h-full relative">
      {/* Toolbar — hidden in `compact` mode (portal owns the filename chrome) */}
      {!compact && (
        <div className="flex items-center gap-2 px-3 py-2 border-b border-gray-200 dark:border-p-border-light bg-gray-50 dark:bg-p-surface min-w-0">
          <span className="text-xs md:text-sm font-mono text-gray-600 dark:text-gray-300 truncate min-w-0 flex-1">{path.split('/').pop()}</span>
          {readOnly && <span className="text-[10px] md:text-xs text-gray-400 dark:text-gray-500 shrink-0">read-only</span>}
          {isDirty && !readOnly && <span className="text-[10px] md:text-xs text-amber-600 shrink-0">Modified</span>}
          {savedFeedback && <span className="text-[10px] md:text-xs text-green-600 shrink-0">Saved</span>}
          {!readOnly && (
            <button
              onClick={handleSave}
              disabled={saveMutation.isPending || !isDirty}
              className="text-xs px-2.5 md:px-3 py-1 rounded-sm bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-40 shrink-0"
            >
              {saveMutation.isPending ? 'Saving...' : 'Save'}
            </button>
          )}
        </div>
      )}

      {/* Compact mode floating Save action */}
      {compact && !readOnly && isDirty && (
        <button
          onClick={handleSave}
          disabled={saveMutation.isPending}
          className="absolute top-2 right-2 z-10 text-xs px-3 py-1 rounded-lg bg-brand text-white shadow-md hover:bg-brand-hover disabled:opacity-40"
        >
          {saveMutation.isPending ? 'Saving…' : savedFeedback ? 'Saved' : 'Save'}
        </button>
      )}

      {/* Errors */}
      {jsonError && (
        <div className="px-3 py-2 bg-red-50 border-b border-red-200 text-xs text-red-700">
          Invalid JSON: {jsonError}
        </div>
      )}
      {saveMutation.error && (
        <div className="px-3 py-2 bg-red-50 border-b border-red-200 text-xs text-red-700">
          Save failed: {(saveMutation.error as Error).message}
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
        className={`flex-1 w-full p-3 font-mono text-sm text-gray-800 dark:text-gray-200 bg-white dark:bg-gray-900 resize-none focus:outline-hidden ${
          readOnly ? 'bg-gray-50 dark:bg-p-surface cursor-default' : ''
        }`}
      />
    </div>
  )
}
