// Recursive per-section workspace search (Phase G, 1.4.0). Pure tree logic —
// the overlay renders the results per view mode:
//  - GRID view: the flat match list (each row shows its subfolder path);
//  - TREE view: a pruned tree (matches + their ancestor folders, ancestors
//    auto-expanded — VS Code filter behavior).
// Purely client-side over the already-loaded section subtree; no backend.

import type { FileNode } from '../api/agents'

export const SEARCH_RESULT_CAP = 200

export interface SearchMatch {
  node: FileNode
  /** Subfolder path relative to the section root ('' for direct children). */
  parentRel: string
}

export interface SearchResult {
  matches: SearchMatch[]
  /** True when more matches existed beyond SEARCH_RESULT_CAP. */
  truncated: boolean
}

/** Case-insensitive substring filter over the whole section subtree. */
export function searchSection(
  roots: FileNode[], sectionPrefix: string, query: string,
): SearchResult {
  const q = query.trim().toLowerCase()
  const matches: SearchMatch[] = []
  let truncated = false
  if (!q) return { matches, truncated }

  const prefix = sectionPrefix.replace(/\/+$/, '')
  const walk = (nodes: FileNode[]) => {
    for (const n of nodes) {
      if (truncated) return
      if (n.name.toLowerCase().includes(q)) {
        if (matches.length >= SEARCH_RESULT_CAP) {
          truncated = true
          return
        }
        // Parent path relative to the section root.
        const parent = n.path.slice(0, Math.max(0, n.path.length - n.name.length - 1))
        const rel = parent === prefix
          ? ''
          : parent.startsWith(prefix + '/')
            ? parent.slice(prefix.length + 1)
            : parent
        matches.push({ node: n, parentRel: rel })
      }
      if (n.type === 'dir' && n.children?.length) walk(n.children)
    }
  }
  walk(roots)
  return { matches, truncated }
}

/**
 * Pruned tree for the TREE view: keep matched nodes plus every ancestor
 * folder of a match; non-matching branches disappear. Returns new node
 * objects (children replaced) — never mutates the source tree. The caller
 * auto-expands all returned folders.
 */
export function pruneTree(roots: FileNode[], query: string): FileNode[] {
  const q = query.trim().toLowerCase()
  if (!q) return roots
  const prune = (nodes: FileNode[]): FileNode[] => {
    const out: FileNode[] = []
    for (const n of nodes) {
      const selfMatch = n.name.toLowerCase().includes(q)
      if (n.type === 'dir') {
        const kept = prune(n.children ?? [])
        if (selfMatch || kept.length) {
          // A matched folder keeps its full subtree visible; a mere ancestor
          // keeps only the pruned children that lead to matches.
          out.push({ ...n, children: selfMatch ? n.children ?? [] : kept })
        }
      } else if (selfMatch) {
        out.push(n)
      }
    }
    return out
  }
  return prune(roots)
}

/** Every folder path in a pruned tree — the auto-expand set for TREE view. */
export function expandedDirsOf(roots: FileNode[]): Set<string> {
  const out = new Set<string>()
  const walk = (nodes: FileNode[]) => {
    for (const n of nodes) {
      if (n.type === 'dir') {
        out.add(n.path)
        walk(n.children ?? [])
      }
    }
  }
  walk(roots)
  return out
}
