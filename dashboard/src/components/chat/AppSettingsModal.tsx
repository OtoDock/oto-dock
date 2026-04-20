import { useState, useEffect } from 'react'

interface Props {
  open: boolean
  onClose: () => void
}

interface Install {
  id: string
  url: string
  label: string
  favorite: boolean
  active: boolean
}

function readInstallations(): Install[] {
  try {
    const raw = (window as any).Android?.getInstallations?.()
    if (raw) return JSON.parse(raw) as Install[]
  } catch { /* not native / parse error */ }
  return []
}

function hostOf(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return url.replace(/^https?:\/\//, '').replace(/\/.*$/, '')
  }
}

export default function AppSettingsModal({ open, onClose }: Props) {
  const [installs, setInstalls] = useState<Install[]>([])
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameValue, setRenameValue] = useState('')

  const refresh = () => setInstalls(readInstallations())

  useEffect(() => {
    if (open) {
      refresh()
      setRenamingId(null)
    }
  }, [open])

  if (!open) return null

  // switchInstallation / removeInstallation recreate the activity natively, so the
  // modal is torn down with the WebView — no local state update needed for those.
  const handleSwitch = (inst: Install) => {
    if (inst.active) return
    try { (window as any).Android?.switchInstallation(inst.id) } catch { /* not native */ }
  }

  const handleFavorite = (id: string) => {
    try { (window as any).Android?.setFavorite(id) } catch { /* not native */ }
    refresh()
  }

  const handleRemove = (id: string) => {
    // Native shows the confirm dialog and recreates on confirm.
    try { (window as any).Android?.removeInstallation(id) } catch { /* not native */ }
  }

  const handleAdd = () => {
    try { (window as any).Android?.openAddInstallation() } catch { /* not native */ }
  }

  const startRename = (inst: Install) => {
    setRenamingId(inst.id)
    setRenameValue(inst.label)
  }

  const commitRename = (id: string) => {
    const v = renameValue.trim()
    if (v) {
      try { (window as any).Android?.renameInstallation(id, v) } catch { /* not native */ }
    }
    setRenamingId(null)
    refresh()
  }

  return (
    <>
      {/* Backdrop */}
      <div className="fixed inset-0 bg-black/50 z-50" onClick={onClose} />

      {/* Modal */}
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
        <div
          className="bg-white dark:bg-p-surface rounded-2xl shadow-xl border border-p-border-light w-full max-w-md p-6"
          onClick={e => e.stopPropagation()}
        >
          <h2 className="text-lg font-medium text-p-text mb-1">Installations</h2>
          <p className="text-xs text-p-text-light mb-4">
            Tap a server to switch to it. The starred server opens by default.
          </p>

          {/* Installations list */}
          <div className="space-y-2 max-h-80 overflow-y-auto -mx-1 px-1">
            {installs.length === 0 && (
              <div className="text-sm text-p-text-light py-4 text-center">No servers added.</div>
            )}

            {installs.map(inst => (
              <div
                key={inst.id}
                onClick={() => renamingId === inst.id ? undefined : handleSwitch(inst)}
                className={
                  'flex items-center gap-2 rounded-xl border px-3 py-2 transition-colors ' +
                  (inst.active
                    ? 'border-p-accent-teal/50 bg-p-accent-teal/5'
                    : 'border-p-border-light hover:bg-p-surface-hover cursor-pointer')
                }
              >
                {/* Favorite star */}
                <button
                  onClick={e => { e.stopPropagation(); handleFavorite(inst.id) }}
                  aria-label={inst.favorite ? 'Default server' : 'Set as default'}
                  className="shrink-0 p-1 -ml-1"
                >
                  <svg
                    className={'w-4 h-4 ' + (inst.favorite ? 'text-p-accent-yellow' : 'text-p-text-light')}
                    fill={inst.favorite ? 'currentColor' : 'none'}
                    stroke="currentColor"
                    strokeWidth={1.7}
                    viewBox="0 0 24 24"
                  >
                    <path strokeLinecap="round" strokeLinejoin="round" d="M11.48 3.5a.56.56 0 011.04 0l2.12 4.86 5.28.46c.5.04.7.66.32.98l-4 3.5 1.2 5.17c.11.48-.41.86-.84.6L12 17.9l-4.56 2.67c-.43.26-.95-.12-.84-.6l1.2-5.17-4-3.5a.56.56 0 01.32-.98l5.28-.46L11.48 3.5z" />
                  </svg>
                </button>

                {/* Label + host */}
                <div className="min-w-0 flex-1">
                  {renamingId === inst.id ? (
                    <input
                      autoFocus
                      value={renameValue}
                      onChange={e => setRenameValue(e.target.value)}
                      onClick={e => e.stopPropagation()}
                      onKeyDown={e => {
                        if (e.key === 'Enter') commitRename(inst.id)
                        if (e.key === 'Escape') setRenamingId(null)
                      }}
                      onBlur={() => commitRename(inst.id)}
                      className="w-full px-2 py-1 text-sm rounded-md border border-p-border bg-p-bg text-p-text outline-hidden"
                    />
                  ) : (
                    <>
                      <div className="text-sm font-medium text-p-text truncate flex items-center gap-2">
                        {inst.label}
                        {inst.active && (
                          <span className="text-[10px] font-normal text-p-accent-teal uppercase tracking-wide">
                            Active
                          </span>
                        )}
                      </div>
                      <div className="text-xs text-p-text-light truncate font-mono">{hostOf(inst.url)}</div>
                    </>
                  )}
                </div>

                {/* Rename + remove */}
                {renamingId !== inst.id && (
                  <div className="flex items-center shrink-0">
                    <button
                      onClick={e => { e.stopPropagation(); startRename(inst) }}
                      aria-label="Rename"
                      className="p-1.5 text-p-text-light hover:text-p-text"
                    >
                      <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={1.7} viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M16.5 3.5a2.12 2.12 0 013 3L7 19l-4 1 1-4 12.5-12.5z" />
                      </svg>
                    </button>
                    <button
                      onClick={e => { e.stopPropagation(); handleRemove(inst.id) }}
                      aria-label="Remove"
                      className="p-1.5 text-p-text-light hover:text-p-accent-red"
                    >
                      <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={1.7} viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M6 7h12M9 7V5a1 1 0 011-1h4a1 1 0 011 1v2m-7 0v12a1 1 0 001 1h6a1 1 0 001-1V7" />
                      </svg>
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>

          {/* Add installation */}
          <button
            onClick={handleAdd}
            className="w-full mt-4 px-4 py-2 rounded-xl border border-p-border-light text-sm font-medium text-p-text hover:bg-p-surface-hover transition-colors flex items-center justify-center gap-2"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 5v14M5 12h14" />
            </svg>
            Add installation
          </button>

          {/* Close */}
          <button
            onClick={onClose}
            className="w-full mt-2 px-4 py-2 rounded-xl text-sm text-p-text-secondary hover:bg-p-surface-hover transition-colors"
          >
            Close
          </button>
        </div>
      </div>
    </>
  )
}
