import { useState, useCallback } from 'react'

interface Segment {
  label: string
  path: string
}

interface Props {
  /** Sandbox-virtual prefix used for the Copy Path button (`/users/{u}/workspace/...`). */
  virtualPrefix: string
  /** Agent-relative path of the current folder. Empty means at scope root. */
  currentPath: string
  /** Agent-relative path of the scope's root (the first crumb after the back button). */
  scopeRoot: string
  /** Label shown for the scope-root crumb. */
  scopeLabel: string
  onNavigate: (path: string) => void
}

function buildSegments(scopeRoot: string, scopeLabel: string, currentPath: string): Segment[] {
  const segments: Segment[] = [{ label: scopeLabel, path: scopeRoot }]
  if (!currentPath || currentPath === scopeRoot) return segments
  const rest = currentPath.slice(scopeRoot.length + 1)
  if (!rest) return segments
  let acc = scopeRoot
  for (const part of rest.split('/')) {
    if (!part) continue
    acc = `${acc}/${part}`
    segments.push({ label: part, path: acc })
  }
  return segments
}

export default function WorkspaceBreadcrumb({
  virtualPrefix,
  currentPath,
  scopeRoot,
  scopeLabel,
  onNavigate,
}: Props) {
  const segments = buildSegments(scopeRoot, scopeLabel, currentPath)
  const atRoot = !currentPath || currentPath === scopeRoot
  const parent = atRoot
    ? null
    : segments[segments.length - 2]?.path ?? scopeRoot

  // Copy uses the sandbox-virtual form — the same string the agent will see.
  const [copied, setCopied] = useState(false)
  const copyTarget =
    currentPath && currentPath !== scopeRoot
      ? `${virtualPrefix}${currentPath.slice(scopeRoot.length)}`
      : virtualPrefix

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(copyTarget)
      setCopied(true)
      setTimeout(() => setCopied(false), 1200)
    } catch {
      // No-op; clipboard unsupported.
    }
  }, [copyTarget])

  return (
    <div className="flex items-center gap-1.5 px-3 py-1.5 text-xs">
      <button
        onClick={() => parent && onNavigate(parent)}
        disabled={atRoot}
        className={`p-1 rounded-sm transition-colors ${
          atRoot
            ? 'text-p-text-light/40 cursor-not-allowed'
            : 'text-p-text-secondary hover:bg-p-surface-hover'
        }`}
        title="Back"
      >
        <svg width={14} height={14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M15 19l-7-7 7-7" />
        </svg>
      </button>
      <div className="flex items-center gap-1 min-w-0 flex-1 overflow-hidden">
        {segments.map((seg, i) => (
          <span key={seg.path} className="flex items-center gap-1 min-w-0">
            {i > 0 && (
              <svg width={10} height={10} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} className="text-p-text-light/60 shrink-0">
                <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
              </svg>
            )}
            {i === segments.length - 1 ? (
              <span className="truncate text-p-text font-medium">{seg.label}</span>
            ) : (
              <button
                onClick={() => onNavigate(seg.path)}
                className="truncate text-p-text-secondary hover:text-brand transition-colors"
              >
                {seg.label}
              </button>
            )}
          </span>
        ))}
      </div>
      <button
        onClick={handleCopy}
        className="shrink-0 p-1 rounded-sm text-p-text-secondary hover:bg-p-surface-hover transition-colors"
        title={copied ? 'Copied!' : `Copy path: ${copyTarget}`}
      >
        {copied ? (
          <svg width={14} height={14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
          </svg>
        ) : (
          <svg width={14} height={14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
            <rect x="9" y="9" width="13" height="13" rx="2" />
            <path strokeLinecap="round" strokeLinejoin="round" d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" />
          </svg>
        )}
      </button>
    </div>
  )
}
