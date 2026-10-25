import { useState, useRef, useCallback } from 'react'
import { useInstallMcp, InstallResult } from '../../api/mcps'
import Dialog from '../../components/ui/Dialog'
import Button from '../../components/ui/Button'
import { DIALOG_PANEL } from '../../components/ui/classes'
import InfoTip from '../../components/ui/InfoTip'

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
    <Dialog
      onClose={onClose}
      busy={install.isPending}
      aria-label="Install or update an MCP"
      panelClassName={`${DIALOG_PANEL} max-w-lg overflow-hidden`}
    >
        <div className="flex items-center justify-between px-5 py-4 border-b border-subtle">
          <div className="min-w-0">
            <h3 className="inline text-base font-semibold text-primary">Install or update an MCP</h3>
            <InfoTip label="Install or update an MCP">
              Node or Python dependencies are installed automatically from the <code className="text-primary">source</code> field.
            </InfoTip>
          </div>
          <button onClick={onClose} className="text-tertiary hover:text-primary text-lg">&times;</button>
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
                className={`border-2 border-dashed rounded-card p-8 text-center cursor-pointer transition-colors
                  ${dragging
                    ? 'border-action bg-action-subtle'
                    : file
                      ? 'border-status-success-border bg-status-success-bg'
                      : 'border-default hover:border-action hover:bg-surface-hover'}`}
              >
                <input ref={inputRef} type="file" accept=".zip,application/zip,application/x-zip-compressed,application/octet-stream" className="hidden" onChange={handleFileChange} />
                {file ? (
                  <div>
                    <svg className="w-8 h-8 mx-auto mb-2 text-status-success-fg" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                    <p className="text-sm font-medium text-primary">{file.name}</p>
                    <p className="text-xs text-tertiary mt-1">{(file.size / 1024).toFixed(0)} KB</p>
                  </div>
                ) : (
                  <div>
                    <svg className="w-8 h-8 mx-auto mb-2 text-tertiary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
                    </svg>
                    <p className="text-sm text-secondary">Drop a .zip file here or click to browse</p>
                    <p className="text-xs text-tertiary mt-1">Must contain a manifest.json</p>
                  </div>
                )}
              </div>

              <p className="text-2xs text-tertiary">
                The zip should contain an MCP folder with <code className="text-primary">manifest.json</code>.
                Existing MCPs with the same name will be updated.
              </p>

              {error && (
                <div className="rounded-card border border-status-danger-border bg-status-danger-bg p-3">
                  <p className="text-xs text-status-danger-fg whitespace-pre-wrap">{error}</p>
                </div>
              )}
            </>
          ) : (
            /* Success result */
            <div className="rounded-card border border-status-success-border bg-status-success-bg p-4">
              <div className="flex items-center gap-2 mb-2">
                <svg className="w-5 h-5 text-status-success-fg" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                <p className="text-sm font-medium text-status-success-fg">
                  {result.status === 'updated' ? 'Updated' : 'Installed'} successfully
                </p>
              </div>
              <div className="space-y-1 text-xs text-secondary">
                <p><span className="text-secondary">Name:</span> {result.name}</p>
                <p>
                  <span className="text-secondary">Version:</span> {result.version || 'latest'}
                  {result.old_version && <span className="text-secondary"> (was {result.old_version})</span>}
                </p>
                <p><span className="text-secondary">Runtime:</span> {result.runtime}</p>
              </div>
              {result.install_log && (
                <details className="mt-3">
                  <summary className="text-xs text-tertiary cursor-pointer hover:text-secondary">Install log</summary>
                  <pre className="mt-1 text-2xs text-tertiary bg-surface-muted rounded-control p-2 overflow-x-auto max-h-40 overflow-y-auto whitespace-pre-wrap">{result.install_log}</pre>
                </details>
              )}
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2 px-5 py-3 border-t border-subtle">
          <Button onClick={onClose}>
            {result ? 'Close' : 'Cancel'}
          </Button>
          {!result && (
            <Button variant="primary" onClick={handleInstall} disabled={!file || install.isPending}>
              {install.isPending ? 'Installing...' : 'Install'}
            </Button>
          )}
        </div>
    </Dialog>
  )
}
