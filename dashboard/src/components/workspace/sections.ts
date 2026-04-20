import type { FileNode } from '../../api/agents'

export type ScopeKey =
  | 'my-workspace'
  | 'my-context'
  | 'agent-workspace'
  | 'agent-knowledge'
  | 'agent-config'

export interface WorkspaceSection {
  key: ScopeKey
  label: string
  /** Agent-relative path that is this scope's root (e.g. `users/alice/workspace`). */
  pathPrefix: string
  nodes: FileNode[]
  canWrite: boolean
  /** Sandbox-virtual prefix used when constructing drag/copy paths. */
  virtualPrefix: string
}

/**
 * 3-tier per-agent role model. Caller passes both flags:
 *
 *   - ``canEdit``  → manager OR editor OR admin. Drives writes to
 *     `agent-workspace` (the collaborative-tier scope).
 *   - ``canManage`` → manager OR admin (owner-tier). Drives writes to
 *     `agent-config` + `agent-knowledge` (the agent-behavior scopes).
 *
 * Viewer-only: both flags false; everything backend-readable but
 * server-side write APIs refuse outside the user's own dir. The UI uses
 * these flags to hide / disable write actions for read-only scopes.
 *
 * Split the API-filtered tree into the five scope sections the workspace
 * overlay shows as chips. The backend already filters out scopes the user
 * has no read access to, so this function just labels what's present.
 */
export function buildSections(
  tree: FileNode[],
  canManage: boolean,
  canEdit: boolean = canManage,
): WorkspaceSection[] {
  const sections: WorkspaceSection[] = []

  const usersNode = tree.find((n) => n.name === 'users' && n.type === 'dir')
  if (usersNode?.children?.length) {
    const userDir = usersNode.children[0]
    const userWorkspace = userDir.children?.find((c) => c.name === 'workspace')
    const userContext = userDir.children?.find((c) => c.name === 'context')

    if (userWorkspace) {
      sections.push({
        key: 'my-workspace',
        label: 'My Workspace',
        pathPrefix: userWorkspace.path,
        virtualPrefix: '/' + userWorkspace.path,
        nodes: userWorkspace.children ?? [],
        canWrite: true,
      })
    }
    if (userContext) {
      sections.push({
        key: 'my-context',
        label: 'My Context',
        pathPrefix: userContext.path,
        virtualPrefix: '/' + userContext.path,
        nodes: userContext.children ?? [],
        canWrite: true,
      })
    }
  }

  const workspaceNode = tree.find((n) => n.name === 'workspace' && n.type === 'dir')
  if (workspaceNode) {
    sections.push({
      key: 'agent-workspace',
      label: 'Shared Workspace',
      pathPrefix: workspaceNode.path,
      virtualPrefix: '/' + workspaceNode.path,
      nodes: workspaceNode.children ?? [],
      canWrite: canEdit,  // editor + manager + admin
    })
  }

  const knowledgeNode = tree.find((n) => n.name === 'knowledge' && n.type === 'dir')
  if (knowledgeNode) {
    sections.push({
      key: 'agent-knowledge',
      label: 'Knowledge',
      pathPrefix: knowledgeNode.path,
      virtualPrefix: '/' + knowledgeNode.path,
      nodes: knowledgeNode.children ?? [],
      canWrite: canManage,  // owner-only (config-tier — curated agent reference library)
    })
  }

  // /config/ is OWNER-only — only show the section to managers/admins.
  // The backend's _filter_tree strips the config node from the tree for
  // editor + viewer responses, so this branch is also a no-op on the
  // dashboard side for those roles (defense-in-depth — if a future
  // backend change ever leaks config in the tree, the UI still suppresses
  // the chip).
  if (canManage) {
    const configNode = tree.find((n) => n.name === 'config' && n.type === 'dir')
    if (configNode) {
      sections.push({
        key: 'agent-config',
        label: 'Agent Config',
        pathPrefix: configNode.path,
        virtualPrefix: '/' + configNode.path,
        nodes: configNode.children ?? [],
        canWrite: canManage,
      })
    }
  }

  return sections
}

/**
 * Walk a tree to fetch the node at `path` (agent-relative). Returns the node
 * if it's a directory; otherwise returns null. Used to materialize the
 * current folder's contents in the grid.
 */
export function findDirNode(nodes: FileNode[], path: string): FileNode | null {
  if (!path) return null
  for (const node of nodes) {
    if (node.path === path && node.type === 'dir') return node
    if (node.type === 'dir' && node.children && path.startsWith(node.path + '/')) {
      const inner = findDirNode(node.children, path)
      if (inner) return inner
    }
  }
  return null
}

/** Direct children of a folder's `path`, looked up from the section nodes. */
export function listChildren(section: WorkspaceSection, path: string): FileNode[] {
  if (!path || path === section.pathPrefix) return section.nodes
  const dir = findDirNode(section.nodes, path)
  return dir?.children ?? []
}
