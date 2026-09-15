/** The agent (⋯ / right-click) and map (long-press / right-click) menu
 * actions — plain builders returning `MapMenuAction[]` for the MapMenu
 * overlays (split out of AgentsMap3D.tsx on 2026-09-10). */
import type { Dispatch, SetStateAction } from 'react'
import type { NavigateFunction } from 'react-router-dom'
import type { QueryClient } from '@tanstack/react-query'
import type { useSetDefaultAgent, useUpdateAgent } from '../../api/agents'
import type { User } from '../../api/auth'
import type { Department, useAdminAddUserAgent } from '../../api/departments'
import { canManageAgent } from '../../lib/permissions'
import type { MapNode } from './layout'
import type { MenuState, PopupState } from './mapConstants'
import { MenuIcon, type MapMenuAction } from './MapOverlays'

export function buildAgentActions({
  popup, user, navigate, setDefault, refreshUser, popupPartners, nodeBySlug,
  attemptUnlink, canCreateDepartments, setLinkFrom, setMoveFrom, updateAgent,
  setNotice, popupDept, qc, addMe,
}: {
  popup: PopupState | null
  user: User | null
  navigate: NavigateFunction
  setDefault: ReturnType<typeof useSetDefaultAgent>
  refreshUser: () => Promise<void>
  popupPartners: { partner: string; glyph: string }[]
  nodeBySlug: Map<string, MapNode>
  attemptUnlink: (a: string, b: string) => Promise<void>
  canCreateDepartments: boolean
  setLinkFrom: Dispatch<SetStateAction<string | null>>
  setMoveFrom: Dispatch<SetStateAction<string | null>>
  updateAgent: ReturnType<typeof useUpdateAgent>
  setNotice: Dispatch<SetStateAction<string | null>>
  popupDept: Department | undefined
  qc: QueryClient
  addMe: ReturnType<typeof useAdminAddUserAgent>
}): MapMenuAction[] {
  const agentActions: MapMenuAction[] = []
  if (popup) {
    const n = popup.node
    if (!n.grayed) {
      agentActions.push({
        key: 'open',
        label: 'Open chat',
        icon: <MenuIcon d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />,
        onClick: () => navigate(`/chat/${n.slug}`),
      })
      agentActions.push({
        key: 'favorite',
        label: user?.default_agent === n.slug ? 'Favorite ★' : 'Set as favorite',
        icon: <MenuIcon d="M11.049 2.927c.3-.921 1.603-.921 1.902 0l1.519 4.674a1 1 0 00.95.69h4.915c.969 0 1.371 1.24.588 1.81l-3.976 2.888a1 1 0 00-.363 1.118l1.518 4.674c.3.922-.755 1.688-1.538 1.118l-3.976-2.888a1 1 0 00-1.176 0l-3.976 2.888c-.783.57-1.838-.196-1.538-1.118l1.518-4.674a1 1 0 00-.363-1.118l-3.976-2.888c-.783-.57-.38-1.81.588-1.81h4.914a1 1 0 00.951-.69l1.519-4.674z" />,
        onClick: () => {
          setDefault.mutate(n.slug, { onSuccess: () => void refreshUser() })
        },
      })
      if (user && canManageAgent(user, n.slug)) {
        agentActions.push({
          key: 'link',
          label: 'Link delegation…',
          icon: <MenuIcon d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />,
          onClick: () => { setLinkFrom(n.slug); setMoveFrom(null) },
        })
        for (const { partner, glyph } of popupPartners) {
          agentActions.push({
            key: `unlink-${partner}`,
            label: `Unlink ${glyph} “${nodeBySlug.get(partner)?.displayName ?? partner}”`,
            icon: <MenuIcon d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1M4 4l16 16" />,
            onClick: () => void attemptUnlink(n.slug, partner),
          })
        }
      }
      // Department assignment is the wider grant (wires N edges at once):
      // manager alone is not enough — the backend field gate needs the
      // platform admin/creator role on top.
      if (user && canManageAgent(user, n.slug) && canCreateDepartments) {
        agentActions.push({
          key: 'move-dept',
          label: 'Move to department…',
          icon: <MenuIcon d="M17 8l4 4m0 0l-4 4m4-4H3" />,
          onClick: () => { setMoveFrom(n.slug); setLinkFrom(null) },
        })
        if (n.departmentId) {
          agentActions.push({
            key: 'remove-dept',
            label: 'Remove from department',
            icon: <MenuIcon d="M15 12H9m12 0a9 9 0 11-18 0 9 9 0 0118 0z" />,
            onClick: () => {
              updateAgent.mutate(
                { name: n.slug, department_id: '', department_level_id: '' },
                {
                  onSuccess: () => {
                    setNotice(`Removed ${n.displayName} from ${popupDept?.name ?? 'its department'}`)
                    qc.invalidateQueries({ queryKey: ['departments'] })
                    qc.invalidateQueries({ queryKey: ['delegation-edges-all'] })
                  },
                  onError: (e) => setNotice(e.message),
                },
              )
            },
          })
        }
      }
    } else if (user?.role === 'admin') {
      agentActions.push({
        key: 'add-me',
        label: 'Add me to this agent',
        icon: <MenuIcon d="M18 9v3m0 0v3m0-3h3m-3 0h-3m-2-5a4 4 0 11-8 0 4 4 0 018 0zM3 20a6 6 0 0112 0v1H3v-1z" />,
        onClick: () => {
          addMe.mutate(
            { sub: user.sub, agent: n.slug },
            {
              onSuccess: () => {
                setNotice(`Added you to ${n.displayName}`)
                void refreshUser()
              },
              onError: (e) => setNotice(e.message),
            },
          )
        },
      })
    }
  }
  return agentActions
}

export function buildMapActions({
  menu, onOpenDepartments, canCreateDepartments, setNewDeptName,
  onCreateAgent, onBrowseCommunity,
}: {
  menu: MenuState | null
  onOpenDepartments: (departmentId?: string) => void
  canCreateDepartments: boolean
  setNewDeptName: Dispatch<SetStateAction<string | null>>
  onCreateAgent: () => void
  onBrowseCommunity: () => void
}): MapMenuAction[] {
  const mapActions: MapMenuAction[] = []
  if (menu) {
    if (menu.departmentId) {
      mapActions.push({
        key: 'edit-dept',
        label: `Edit “${menu.departmentName}”…`,
        icon: <MenuIcon d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />,
        onClick: () => onOpenDepartments(menu.departmentId!),
      })
    }
    if (canCreateDepartments) {
      mapActions.push({
        key: 'new-dept',
        label: 'New department…',
        icon: <MenuIcon d="M12 4v16m8-8H4" />,
        onClick: () => setNewDeptName(''),
      })
      // Same actions (and the same page-hosted popups) as the grid view's
      // header buttons — the map must not make agent creation harder to
      // reach than the fallback view (operator round 17).
      mapActions.push({
        key: 'create-agent',
        label: 'Create agent…',
        icon: <MenuIcon d="M18 9v3m0 0v3m0-3h3m-3 0h-3m-2-5a4 4 0 11-8 0 4 4 0 018 0zM3 20a6 6 0 0112 0v1H3v-1z" />,
        onClick: onCreateAgent,
      })
      mapActions.push({
        key: 'browse-community',
        label: 'Browse community…',
        icon: <MenuIcon d="M21 21l-4.35-4.35m1.85-5.65a7.5 7.5 0 11-15 0 7.5 7.5 0 0115 0z" />,
        onClick: onBrowseCommunity,
      })
    }
    mapActions.push({
      key: 'departments',
      label: 'Departments editor',
      icon: <MenuIcon d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4" />,
      onClick: () => onOpenDepartments(),
    })
  }
  return mapActions
}
