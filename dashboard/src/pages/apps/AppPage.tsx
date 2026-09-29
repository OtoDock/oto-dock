import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { rollbackNoticeText, useApp, useHideAppForMe, usePurgeApp, useRollbackApp, useUnpinApp, type PinnedApp } from '../../api/apps'
import { useAuth } from '../../contexts/AuthContext'
import { isAdmin } from '../../lib/permissions'
import { appKind } from '../../lib/kinds/app'
import { DEPLOY_STATE } from '../../lib/status/appDeploy'
import { useDashboardWs } from '../../hooks/useDashboardWs'
import AppFrame from '../../components/apps/AppFrame'
import AppMenu from '../../components/apps/AppMenu'
import { RollbackConfirm, RollbackNotice } from '../../components/apps/AppsOverlay'
import AppApprovalCard, { appNeedsApproval } from '../../components/apps/AppApprovalCard'
import AppDeployCard from '../../components/apps/AppDeployCard'
import AppLogsPanel from '../../components/apps/AppLogsPanel'
import AppSettingsPanel from '../../components/apps/AppSettingsPanel'
import SharePopover from '../../components/sharing/SharePopover'

/**
 * /apps/:appId — one app full screen. Every app has this URL: the menu's
 * "Open full screen", navigation from other apps, notification taps. Access
 * is the serve rule (owner, agent member, admin; a Dock pin also needs its
 * chat) and a stranger gets the same absence a missing id gets. Signed out,
 * RequireAuth renders the login page in place and returns here.
 *
 * The page mounts its own dashboard socket so live reload (file_updated)
 * and the platform feeds keep working without a chat page underneath.
 * A send_prompt button follows the front-page rule: the agent's home
 * starts a new chat and delivers the framed prompt there (the pending
 * action rides router state); non-members get `unavailable`.
 */
export default function AppPage() {
  const { appId } = useParams<{ appId: string }>()
  const navigate = useNavigate()
  const { user } = useAuth()
  const { data: app, isLoading } = useApp(appId)
  // The platform's own render (APPS.md "Deploy pipeline") opens this page
  // with the copy to judge named by its hash: no dashboard socket (the
  // render principal has none) and no keep-warm.
  const [params] = useSearchParams()
  const renderSha = /^[0-9a-f]{64}$/.test(params.get('render') || '') ? (params.get('render') as string) : ''
  // The hook does not connect by itself (the chat page connects in its own
  // stream setup); without this the full-screen page had no socket, so no
  // live reload, no feeds and no live-app frames (found live on T1).
  const ws = useDashboardWs({})
  useEffect(() => {
    if (renderSha) return
    ws.connect()
    return () => ws.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  const [confirmUnpin, setConfirmUnpin] = useState(false)
  const [confirmRollback, setConfirmRollback] = useState(false)
  const [rollbackNotice, setRollbackNotice] = useState<{ text: string; error: boolean } | null>(null)
  const [sharing, setSharing] = useState(false)
  // The identity-provider confirm comes back here with `?share=1&tab=link`
  // (and `confirm_error` when it failed): the popover opens on that tab and
  // the params are dropped (SHARING.md "The confirm").
  const [searchParams, setSearchParams] = useSearchParams()
  const [shareTab, setShareTab] = useState<'people' | 'link' | undefined>(undefined)
  const [shareError, setShareError] = useState('')
  const canManage = !!app?.can_manage
  const [preview, setPreview] = useState(false)
  // `?preview=1` from preview_app's own URL: the working copy, for whoever
  // may manage the row (everyone else keeps the live release).
  useEffect(() => {
    if (searchParams.get('preview') === '1' && canManage) setPreview(true)
  }, [searchParams, canManage])
  useEffect(() => {
    if (searchParams.get('share') !== '1' || !canManage) return
    setShareTab(searchParams.get('tab') === 'link' ? 'link' : 'people')
    setShareError(searchParams.get('confirm_error') || '')
    setSharing(true)
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.delete('share')
      next.delete('tab')
      next.delete('confirm_error')
      return next
    }, { replace: true })
  }, [searchParams, setSearchParams, canManage])
  const [logsOpen, setLogsOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [deleteTyped, setDeleteTyped] = useState('')
  const unpin = useUnpinApp(app?.agent ?? '')
  const hideForMe = useHideAppForMe(app?.agent ?? '')
  const rollback = useRollbackApp(app?.agent ?? '')
  const purge = usePurgeApp(app?.agent ?? '')

  const member = !!app && !!user && (isAdmin(user) || user.agents.includes(app.agent))
  const onSendPrompt = useCallback(
    async (row: PinnedApp, action: { id: string; label: string; prompt: string }, args: unknown) => {
      if (!app) return { status: 'unavailable', reason: 'not available in this view' }
      navigate(`/chat/${app.agent}`, { state: { pendingAppAction: { app: row, action, args } } })
      return { status: 'sent' }
    },
    [app, navigate],
  )

  if (isLoading) {
    return (
      <div className="flex h-screen-safe items-center justify-center bg-p-bg text-sm text-p-text-light">
        Loading…
      </div>
    )
  }
  if (!app) {
    return (
      <div className="flex h-screen-safe flex-col items-center justify-center gap-3 bg-p-bg px-6 text-center">
        <p className="text-sm font-medium text-p-text-secondary">This app is not available.</p>
        <p className="max-w-sm text-xs text-p-text-light">
          It may have been unpinned, or it is not shared with you.
        </p>
        <button
          onClick={() => navigate('/')}
          className="rounded-md border border-p-border-light px-3 py-1.5 text-xs font-medium text-p-text-secondary transition-colors hover:bg-p-surface-hover"
        >
          Back to OtoDock
        </button>
      </div>
    )
  }

  const title = app.title || app.slug
  return (
    <div className="flex h-screen-safe flex-col overflow-clip bg-p-bg">
      <header
        className="flex shrink-0 items-center gap-2 border-b border-p-border-light/60 px-2 py-1.5"
        style={{ paddingTop: 'max(0.375rem, env(safe-area-inset-top))' }}
      >
        <button
          onClick={() => navigate(`/chat/${app.agent}`)}
          aria-label="Back to the agent"
          title="Back to the agent"
          className="flex h-7 w-7 items-center justify-center rounded-full text-p-text-secondary transition-colors hover:bg-p-surface-hover hover:text-p-text"
        >
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M15 19l-7-7 7-7" />
          </svg>
        </button>
        <h1 className="min-w-0 flex-1 truncate text-sm font-medium text-p-text">{title}</h1>
        <span className={`hidden text-[10px] uppercase tracking-wide sm:inline ${app.scope === 'shared' ? 'text-p-accent-purple' : 'text-brand'}`}>
          {app.scope === 'shared' ? 'shared' : 'personal'}
        </span>
        <AppMenu
          app={app}
          fullScreen
          onHideForMe={app.scope === 'shared' || app.granted
            ? () => { hideForMe.mutate(app.id); navigate(member ? `/chat/${app.agent}` : '/agents') }
            : undefined}
          onUnpin={app.can_manage ? () => setConfirmUnpin(true) : undefined}
          onShare={app.can_manage ? () => setSharing(true) : undefined}
          onRollback={app.can_manage ? () => { setConfirmUnpin(false); setConfirmRollback(true) } : undefined}
          onLogs={appKind(app).mayServe && app.can_manage ? () => setLogsOpen(true) : undefined}
          onSettings={appKind(app).hasSettings && app.can_manage ? () => setSettingsOpen(true) : undefined}
          onTogglePreview={appKind(app).hasPreviewBuild && app.can_manage ? () => setPreview((v) => !v) : undefined}
          previewing={preview}
          onDelete={appKind(app).deletable && app.can_manage ? () => { setDeleteTyped(''); setConfirmDelete(true) } : undefined}
        />
      </header>
      {confirmDelete && (
        <div className="mx-3 mt-2 flex flex-wrap items-center gap-2 rounded-xl border border-red-500/40 bg-red-500/5 px-3 py-2.5 text-xs" data-testid="app-delete-confirm">
          <span className="text-p-text">
            Delete “{title}” with its data? The app, its releases and its database are removed
            for good; the folder goes to the recover bin. Type <code className="font-mono">{app.slug}</code> to confirm.
          </span>
          <input
            value={deleteTyped}
            onChange={(e) => setDeleteTyped(e.target.value)}
            placeholder={app.slug}
            aria-label="Type the app's slug to confirm"
            className="w-36 rounded-md border border-p-border-light bg-p-bg px-2 py-1 font-mono text-p-text"
          />
          <div className="ml-auto flex shrink-0 items-center gap-2">
            <button
              disabled={deleteTyped.trim().toLowerCase() !== app.slug || purge.isPending}
              onClick={() => { setConfirmDelete(false); purge.mutate({ appId: app.id, confirm: deleteTyped.trim() }); navigate(`/chat/${app.agent}`) }}
              className="rounded-md bg-red-600 px-2.5 py-1 font-medium text-white transition-colors hover:bg-red-700 disabled:opacity-50"
            >
              Delete app and its data
            </button>
            <button
              onClick={() => setConfirmDelete(false)}
              className="rounded-md border border-p-border-light px-2.5 py-1 font-medium text-p-text-secondary transition-colors hover:bg-p-surface-hover"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
      {sharing && (
        <SharePopover
          app={app}
          initialTab={shareTab}
          confirmError={shareError}
          onClose={() => { setSharing(false); setShareTab(undefined); setShareError('') }}
        />
      )}
      {confirmRollback && (
        <RollbackConfirm
          app={app}
          busy={rollback.isPending}
          onCancel={() => setConfirmRollback(false)}
          onConfirm={() => {
            setConfirmRollback(false)
            const left = app.release ?? 0
            rollback.mutate(app.id, {
              onSuccess: (r) => setRollbackNotice({ text: rollbackNoticeText(app, left, r), error: false }),
              onError: (e) => setRollbackNotice({ text: (e as Error).message, error: true }),
            })
          }}
        />
      )}
      {rollbackNotice && <RollbackNotice notice={rollbackNotice} onClose={() => setRollbackNotice(null)} />}
      {confirmUnpin && (
        <div className="mx-3 mt-2 flex flex-wrap items-center gap-2 rounded-xl border border-p-border-light bg-p-surface px-3 py-2.5 text-xs">
          <span className="text-p-text">
            {app.scope === 'shared'
              ? `Unpin “${title}” for everyone? The workspace file and the approved actions are kept — ask the agent to pin it back anytime.`
              : `Unpin “${title}”? The workspace file and the approved actions are kept — ask the agent to pin it back anytime.`}
          </span>
          <div className="ml-auto flex shrink-0 items-center gap-2">
            <button
              onClick={() => { setConfirmUnpin(false); unpin.mutate(app.id); navigate(`/chat/${app.agent}`) }}
              className="rounded-md bg-red-600 px-2.5 py-1 font-medium text-white transition-colors hover:bg-red-700"
            >
              {app.scope === 'shared' ? 'Unpin for everyone' : 'Unpin'}
            </button>
            <button
              onClick={() => setConfirmUnpin(false)}
              className="rounded-md border border-p-border-light px-2.5 py-1 font-medium text-p-text-secondary transition-colors hover:bg-p-surface-hover"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
      {app.deploy_state === DEPLOY_STATE.PENDING && <AppDeployCard key={`deploy-${app.id}`} app={app} agent={app.agent} />}
      {appNeedsApproval(app) && <AppApprovalCard key={app.id} app={app} agent={app.agent} />}
      {logsOpen && <AppLogsPanel key={`logs-${app.id}`} app={app} onClose={() => setLogsOpen(false)} />}
      {settingsOpen && <AppSettingsPanel key={`settings-${app.id}`} app={app} agent={app.agent} onClose={() => setSettingsOpen(false)} />}
      <div className="relative isolate flex-1 min-h-0 p-2">
        {/* Keyed: a navigation from one app to another reuses this route
            element, and a frame instance belongs to one app. */}
        <AppFrame key={app.id} app={app} agent={app.agent} onSendPrompt={member ? onSendPrompt : undefined} preview={preview} renderSha={renderSha} />
      </div>
    </div>
  )
}
