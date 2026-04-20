import { useState, useRef, useCallback } from 'react'
import { useInstallMcp, InstallResult } from '../../api/mcps'

// ---------------------------------------------------------------------------
// Install / Update Modal
// ---------------------------------------------------------------------------

export function InstallModal({ onClose }: { onClose: () => void }) {
  const install = useInstallMcp()
  const [file, setFile] = useState<File | null>(null)
  const [dragging, setDragging] = useState(false)
  const [result, setResult] = useState<InstallResult | null>(null)
  const [error, setError] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    setDragging(false)
    const dropped = e.dataTransfer.files[0]
    if (dropped?.name.endsWith('.zip')) {
      setFile(dropped)
      setError('')
    } else {
      setError('Only .zip files are accepted')
    }
  }, [])

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0]
    if (f) { setFile(f); setError('') }
  }

  const handleInstall = () => {
    if (!file) return
    setError('')
    setResult(null)
    install.mutate(file, {
      onSuccess: (r) => setResult(r),
      onError: (e: Error) => setError(e.message),
    })
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={onClose}>
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow-xl border border-p-border-light w-full max-w-lg mx-4 overflow-hidden" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-5 py-4 border-b border-p-border-light">
          <h3 className="text-base font-semibold text-p-text">Install / Update MCP</h3>
          <button onClick={onClose} className="text-p-text-light hover:text-p-text text-lg">&times;</button>
        </div>

        <div className="p-5 space-y-4">
          {!result ? (
            <>
              {/* Drop zone */}
              <div
                onDragOver={e => { e.preventDefault(); setDragging(true) }}
                onDragLeave={() => setDragging(false)}
                onDrop={handleDrop}
                onClick={() => inputRef.current?.click()}
                className={`border-2 border-dashed rounded-lg p-8 text-center cursor-pointer transition-colors
                  ${dragging
                    ? 'border-brand bg-brand/5 dark:bg-brand/10'
                    : file
                      ? 'border-green-400 dark:border-green-600 bg-green-50/50 dark:bg-green-900/10'
                      : 'border-p-border-light hover:border-brand/50 hover:bg-p-surface-hover/30'}`}
              >
                <input ref={inputRef} type="file" accept=".zip" className="hidden" onChange={handleFileChange} />
                {file ? (
                  <div>
                    <svg className="w-8 h-8 mx-auto mb-2 text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                    <p className="text-sm font-medium text-p-text">{file.name}</p>
                    <p className="text-xs text-p-text-light mt-1">{(file.size / 1024).toFixed(0)} KB</p>
                  </div>
                ) : (
                  <div>
                    <svg className="w-8 h-8 mx-auto mb-2 text-p-text-light" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
                    </svg>
                    <p className="text-sm text-p-text-secondary">Drop a .zip file here or click to browse</p>
                    <p className="text-xs text-p-text-light mt-1">Must contain a manifest.json</p>
                  </div>
                )}
              </div>

              <p className="text-[11px] text-p-text-light">
                The zip should contain an MCP folder with <code className="text-brand">manifest.json</code>.
                Node/Python dependencies are installed automatically from the <code className="text-brand">source</code> field.
                Existing MCPs with the same name will be updated.
              </p>

              {error && (
                <div className="rounded-lg border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/20 p-3">
                  <p className="text-xs text-red-700 dark:text-red-400 whitespace-pre-wrap">{error}</p>
                </div>
              )}
            </>
          ) : (
            /* Success result */
            <div className="rounded-lg border border-green-200 dark:border-green-800 bg-green-50 dark:bg-green-900/20 p-4">
              <div className="flex items-center gap-2 mb-2">
                <svg className="w-5 h-5 text-green-600 dark:text-green-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                <p className="text-sm font-medium text-green-700 dark:text-green-400">
                  {result.status === 'updated' ? 'Updated' : 'Installed'} successfully
                </p>
              </div>
              <div className="space-y-1 text-xs text-p-text-secondary">
                <p><span className="text-p-text-light">Name:</span> {result.name}</p>
                <p>
                  <span className="text-p-text-light">Version:</span> {result.version || 'latest'}
                  {result.old_version && <span className="text-p-text-light"> (was {result.old_version})</span>}
                </p>
                <p><span className="text-p-text-light">Runtime:</span> {result.runtime}</p>
              </div>
              {result.install_log && (
                <details className="mt-3">
                  <summary className="text-xs text-p-text-light cursor-pointer hover:text-p-text-secondary">Install log</summary>
                  <pre className="mt-1 text-[11px] text-p-text-light bg-gray-100 dark:bg-gray-900 rounded-sm p-2 overflow-x-auto max-h-40 overflow-y-auto whitespace-pre-wrap">{result.install_log}</pre>
                </details>
              )}
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2 px-5 py-3 border-t border-p-border-light bg-gray-50/50 dark:bg-gray-900/30">
          <button
            onClick={onClose}
            className="px-3 py-1.5 text-sm rounded-lg border border-p-border-light text-p-text-secondary hover:bg-p-surface-hover transition-colors"
          >
            {result ? 'Close' : 'Cancel'}
          </button>
          {!result && (
            <button
              onClick={handleInstall}
              disabled={!file || install.isPending}
              className="px-4 py-1.5 text-sm rounded-lg bg-brand text-white hover:bg-brand-hover disabled:opacity-40 transition-colors"
            >
              {install.isPending ? 'Installing...' : 'Install'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
