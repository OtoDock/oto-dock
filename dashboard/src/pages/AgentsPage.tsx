/**
 * Agents page — the 3D company map (default), the classic grid, and the
 * Departments editor tab, over one source of truth. The /agents route keeps
 * existing (AgentGuard and two tests pin redirects to it); the map is the
 * new face, the grid stays as the fallback view (low-end devices, WebGL
 * unavailable, personal preference — the choice roams via ui-prefs).
 */
import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { useMyUiPrefs, useUpdateMyUiPrefs } from '../api/userUiPrefs'
import AgentGrid from './AgentGrid'
import AgentsMap3D from '../components/agents-map/AgentsMap3D'
import DepartmentsEditor from '../components/departments/DepartmentsEditor'
import AgentInstallModal from '../components/AgentInstallModal'
import CommunityAgentsBrowser from '../components/CommunityAgentsBrowser'

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

  const isAdmin = user?.role === 'admin'
  const canEditDepts = user?.role === 'admin' || user?.role === 'creator'

  // Roamed stickiness: agents_view ('map' | 'grid') + the admin hide toggle.
  useEffect(() => {
    if (!prefs || hydrated) return
    setHydrated(true)
    if (prefs.agents_view === 'grid') setView('grid')
    if (isAdmin && prefs.agents_map_hide_non_member === false) {
      setHideNonMember(false)
    }
  }, [prefs, hydrated, isAdmin])

  const pickView = useCallback((v: View) => {
    setView(v)
    if (v !== 'departments') {
      updatePrefs.mutate({ agents_view: v })
    }
  }, [updatePrefs])

  const toggleHide = useCallback(() => {
    setHideNonMember((h) => {
      updatePrefs.mutate({ agents_map_hide_non_member: !h })
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

  const segBtn = (active: boolean) =>
    `px-3 py-1 text-xs transition-colors ${
      active
        ? 'bg-brand-surface text-brand'
        : 'text-p-text-secondary hover:bg-p-surface-hover'
    }`

  return (
    // dvh, not vh: mobile URL-bar dynamics make 100vh taller than the
    // visual viewport, which let the page scroll down with no way back.
    <div className="h-dvh flex flex-col bg-p-bg overflow-hidden">
      {/* Top bar — standard "Back to Chat" + view controls (workspace-buttons style) */}
      <div className="flex items-center gap-2 h-12 px-4 bg-white/80 dark:bg-gray-900/80 backdrop-blur-sm border-b border-p-border-light shrink-0">
        <Link
          to="/"
          className="flex items-center justify-center gap-1.5 px-3 sm:px-8 py-1.5 rounded-lg text-sm font-medium text-white bg-brand hover:bg-brand-hover transition-colors"
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
        <div className="shrink-0 flex items-center rounded-lg border border-p-border-light overflow-hidden">
          <button
            onClick={() => pickView('map')}
            disabled={webglDead}
            title={webglDead ? '3D unavailable on this device' : 'Company map'}
            className={segBtn(view === 'map') + (webglDead ? ' opacity-40' : '')}
          >
            Map
          </button>
          <button onClick={() => pickView('grid')} className={segBtn(view === 'grid')}>
            Grid
          </button>
          {canEditDepts && (
            <button
              onClick={() => pickView('departments')}
              className={segBtn(view === 'departments')}
            >
              Departments
            </button>
          )}
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
