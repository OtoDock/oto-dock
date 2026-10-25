/**
 * Agents page — the 3D company map (default), the classic grid, and the
 * Departments editor tab, over one source of truth. The /agents route keeps
 * existing (AgentGuard and two tests pin redirects to it); the map is the
 * new face, the grid stays as the fallback view (low-end devices, WebGL
 * unavailable, personal preference — the choice roams via ui-prefs).
 */
import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { isAdmin as isPlatformAdmin, isCreatorOrAbove } from '../lib/permissions'
import { toastError } from '../lib/http/apiJson'
import { useMyUiPrefs, useUpdateMyUiPrefs } from '../api/userUiPrefs'
import AgentGrid from './AgentGrid'
import AgentsMap3D from '../components/agents-map/AgentsMap3D'
import DepartmentsEditor from '../components/departments/DepartmentsEditor'
import AgentInstallModal from '../components/AgentInstallModal'
import CommunityAgentsBrowser from '../components/CommunityAgentsBrowser'
import { BUTTON_BASE, BUTTON_SIZE, BUTTON_VARIANT, SEGMENT } from '../components/ui/classes'

type View = 'map' | 'grid' | 'departments'

export default function AgentsPage() {
  const { user } = useAuth()
  const { data: prefs } = useMyUiPrefs()
  const updatePrefs = useUpdateMyUiPrefs()
  const [view, setView] = useState<View>('map')
  // Admin default is "Mine only" — Everything is the deliberate audit/
  // overview mode, not the daily driver.
  const [hideNonMember, setHideNonMember] = useState(true)
  const [webglDead, setWebglDead] = useState(false)
  const [hydrated, setHydrated] = useState(false)
  // The map's context menu opens the SAME popups the grid header uses —
  // hosted here so they overlay whichever view is active.
  const [showCreateAgent, setShowCreateAgent] = useState(false)
  const [showCommunity, setShowCommunity] = useState(false)

  const isAdmin = isPlatformAdmin(user)
  const canEditDepts = isCreatorOrAbove(user)

  // Roamed stickiness: agents_view ('map' | 'grid') + the admin hide toggle.
  if (prefs && !hydrated) {
    setHydrated(true)
    if (prefs.agents_view === 'grid') setView('grid')
    if (isAdmin && prefs.agents_map_hide_non_member === false) {
      setHideNonMember(false)
    }
  }

  const pickView = useCallback((v: View) => {
    setView(v)
    if (v !== 'departments') {
      void updatePrefs.mutateAsync({ agents_view: v }).catch(toastError('The view was not saved'))
    }
  }, [updatePrefs])

  const toggleHide = useCallback(() => {
    setHideNonMember((h) => {
      void updatePrefs.mutateAsync({ agents_map_hide_non_member: !h }).catch(toastError('The map filter was not saved'))
      return !h
    })
  }, [updatePrefs])

  const onUnavailable = useCallback(() => {
    setWebglDead(true)
    setView('grid')
  }, [])

  const openDepartments = useCallback(() => {
    setView('departments')
  }, [])

  const segBtn = (chosen: boolean) =>
    `${BUTTON_BASE} ${BUTTON_SIZE.sm} ${chosen ? SEGMENT.on : SEGMENT.off}`

  return (
    // dvh, not vh: mobile URL-bar dynamics make 100vh taller than the
    // visual viewport, which let the page scroll down with no way back.
    <div className="h-dvh flex flex-col bg-surface-page overflow-hidden">
      {/* The 3D scene is always dark. In map view the scoped `.dark` class
          gives the bar the dark theme's tokens, so it never sits light over
          the scene; the page root stays in the person's theme because the
          install and community modals render inside it without a portal. */}
      <div
        data-testid="agents-top-bar"
        className={view === 'map' ? 'dark shrink-0 bg-surface-page' : 'contents'}
      >
      {/* Top bar — standard "Back to Chat" + view controls (workspace-buttons style) */}
      <div className="flex items-center gap-2 h-12 px-4 bg-surface-page border-b border-subtle shrink-0">
        <Link
          to="/"
          aria-label="Back to Chat"
          className={`${BUTTON_BASE} ${BUTTON_VARIANT.primary} ${BUTTON_SIZE.sm} sm:px-8`}
        >
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 19l-7-7m0 0l7-7m-7 7h18" />
          </svg>
          <span className="hidden sm:inline">Back to Chat</span>
        </Link>
        <span className="flex-1" />
        {/* The admin Mine-only/Everything toggle lives INSIDE the map
            (top-left overlay) — in the bar it overflowed mobile widths and
            pushed the Departments tab off-screen. */}
        <div role="group" aria-label="View" className="shrink-0 flex items-center gap-1">
          <button
            type="button"
            onClick={() => pickView('map')}
            disabled={webglDead}
            aria-pressed={view === 'map'}
            title={webglDead ? '3D unavailable on this device' : 'Company map'}
            className={segBtn(view === 'map')}
          >
            Map
          </button>
          <button type="button" onClick={() => pickView('grid')} aria-pressed={view === 'grid'} className={segBtn(view === 'grid')}>
            Grid
          </button>
          {canEditDepts && (
            <button
              type="button"
              onClick={() => pickView('departments')}
              aria-pressed={view === 'departments'}
              className={segBtn(view === 'departments')}
            >
              Departments
            </button>
          )}
        </div>
      </div>
      </div>

      <div className={`flex-1 min-h-0 ${view === 'map' ? 'overflow-hidden' : 'overflow-y-auto'}`}>
        {view === 'map' && !webglDead && (
          <div className="h-full">
            <AgentsMap3D
              onUnavailable={onUnavailable}
              hideNonMember={hideNonMember}
              onToggleHideNonMember={toggleHide}
              onOpenDepartments={openDepartments}
              onCreateAgent={() => setShowCreateAgent(true)}
              onBrowseCommunity={() => setShowCommunity(true)}
            />
          </div>
        )}
        {view === 'grid' && <AgentGrid embedded />}
        {view === 'departments' && (
          <main className="p-6 max-w-5xl mx-auto">
            <DepartmentsEditor />
          </main>
        )}
      </div>

      <AgentInstallModal
        open={showCreateAgent}
        mode="create"
        onClose={() => setShowCreateAgent(false)}
      />
      <CommunityAgentsBrowser
        open={showCommunity}
        onClose={() => setShowCommunity(false)}
      />
    </div>
  )
}
