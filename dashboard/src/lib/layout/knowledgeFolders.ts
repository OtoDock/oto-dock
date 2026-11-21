/**
 * The folders of an agent's knowledge tree as the folder pickers list them:
 * the share form's (the one folder a library shares) and the External
 * identities card's (the folders external identities may read). Directories
 * only, in tree order, depth for the indent. `memory` at any depth never
 * (the server refuses it), nor a dot folder, and the libraries' mirror root
 * `shared/` only under a root the caller names (the server's
 * `knowledge_choices` names each attached library's).
 */

import type { FileNode } from '../../api/agents'
import { HEAD } from './tree'

export interface FolderRow {
  /** Relative to the knowledge folder, `''` for the folder itself. */
  path: string
  depth: number
}

/** The knowledge folder's mirror root of the attached libraries. */
export const SHARED_ROOT = 'shared'
const MEMORY = 'memory'

const offered = (n: FileNode) => n.type === 'dir' && n.name !== MEMORY && !n.name.startsWith('.')

function knowledgeChildren(tree: FileNode[] | undefined): FileNode[] {
  return (tree ?? []).find((n) => n.type === 'dir' && n.name === HEAD.KNOWLEDGE)?.children ?? []
}

function walk(nodes: FileNode[], prefix: string, depth: number, out: FolderRow[]) {
  for (const n of nodes) {
    if (!offered(n)) continue
    const path = prefix ? `${prefix}/${n.name}` : n.name
    out.push({ path, depth })
    if (n.children) walk(n.children, path, depth + 1, out)
  }
}

/** Every folder of the knowledge tree but the libraries' mirrors: the
 *  share form's list (a library shares this agent's own folders). */
export function knowledgeFolders(tree: FileNode[] | undefined): FolderRow[] {
  const out: FolderRow[] = []
  walk(knowledgeChildren(tree).filter((n) => n.name !== SHARED_ROOT), '', 0, out)
  return out
}

/** Each of `roots` and the folders inside it, a root at depth 0. A root the
 *  tree lacks (a library attached a moment ago, not mirrored yet) is still
 *  a row. */
export function foldersUnder(tree: FileNode[] | undefined, roots: string[]): FolderRow[] {
  const out: FolderRow[] = []
  for (const root of roots) {
    let nodes = knowledgeChildren(tree)
    let node: FileNode | undefined
    for (const segment of root.split('/')) {
      node = nodes.find((n) => n.type === 'dir' && n.name === segment)
      nodes = node?.children ?? []
    }
    out.push({ path: root, depth: 0 })
    if (node?.children) walk(node.children, root, 1, out)
  }
  return out
}

/** Whether folder `path` lies inside folder `parent`, segment by segment. */
export function isInside(path: string, parent: string): boolean {
  return path.startsWith(`${parent}/`)
}
