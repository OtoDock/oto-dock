import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiFetch } from '../../api/auth'
import MarkdownContent from '../chat/MarkdownContent'
import AppFrame from '../apps/AppFrame'
import AppApprovalCard, { appNeedsApproval } from '../apps/AppApprovalCard'
import { useChatStore } from '../../store/chatStore'
import type { ChatPins, PinnedApp } from '../../api/apps'
import PinnedFileRow from './PinnedFileRow'
import { parseBoard, type BoardLane } from './projectBoard'

// The Dock — the chat's unified panel surface, opened from the composer's
// dock toggle. Panels compose by what the anchor chat carries:
//   - delegation chat (project slug or plain lineage): the platform lane
//     section on TOP (orchestrator + live lane cards + board.md context), a
//     hairline separator, then the agent-pinned PROJECT dashboard (AppFrame)
//     below at content height — ONE page scroll;
//   - any chat with a chat-scoped pin: that dashboard, full-panel;
//   - both: lanes on top with the chat app under them, project app below.
// The lane section itself is TWO stacked blocks (operator ask — the board
// rows used to interleave between the orchestrator card and the live cards):
// the LIVE block first (orchestrator card + live lane cards, the official
// lane representation), then the BOARD block as its own section (goal, the
// board's ## Lanes rows with checkboxes, Decisions/Hand-offs, collapsible
// full document). Lane liveness joins `chatStore` (the same `chat_status`
// WS feed the sidebar rides) so the generating pulse is instant between the
// 10s polls; the poll stays for lane-graph shape + `awaiting_user`, which
// the WS doesn't carry. A renderer, not an editor — the orchestrator
// session owns the board and the pinned dashboards.

interface LaneRow {
  id: string
  title: string
  agent: string
  delegate_role: string
  parent_chat_id: string
  status: string
  updated_at: string
}

interface Props {
  agent: string
  /** The open chat (anchor) — a project member, its orchestrator, or any
      chat carrying a chat-scoped pin. */
  chatId: string
  /** Host-page signal (chat row project_id/role) — gates the lane-graph
      fetch so non-project chats with a chat dock never hit /project. */
  isProjectChat: boolean
  /** The anchor chat's Dock pins (useChatPins) — undefined while loading. */
  pins?: ChatPins
  onSelectChat: (chatId: string) => void
  onClose: () => void
  onSendPrompt?: (app: PinnedApp, action: { id: string; label: string; prompt: string }, args: unknown) => Promise<{ status: string; reason?: string }>
}

interface TreeNode {
  name: string
  path: string
  type: 'file' | 'dir'
  children?: TreeNode[]
}

/** Depth-first search for `projects/<id>/board.md` anywhere the caller can
 * see (agent workspace, or their own users/<name>/workspace — the tree is
 * already identity-filtered server-side, so no username guessing here). */
function findBoardPath(nodes: TreeNode[], suffix: string): string | null {
  for (const n of nodes) {
    if (n.type === 'file' && n.path.endsWith(suffix)) return n.path
    if (n.children) {
      const hit = findBoardPath(n.children, suffix)
      if (hit) return hit
    }
  }
  return null
}

// awaiting_user is ORANGE, not amber: the dock's identity accents (header
// badge, orchestrator card) are amber now, and the needs-attention chip must
// stay readable as its own signal beside them.
const STATUS_STYLE: Record<string, string> = {
  generating: 'bg-brand/10 text-brand border-brand/30',
  awaiting_user: 'bg-orange-100 text-orange-700 border-orange-300 dark:bg-orange-900/30 dark:text-orange-300 dark:border-orange-700/50',
  idle: 'bg-p-surface text-p-text-secondary border-p-border-light',
}

function StatusChip({ status }: { status: string }) {
  const style = STATUS_STYLE[status] ?? STATUS_STYLE.idle
  const label = status === 'awaiting_user' ? 'awaiting user' : status
  // No dot inside the generating chip: in the unified live language the CARD
  // pulses while generating and a dot means only "finished, not opened yet".
  return (
    <span className={`px-1.5 py-0.5 rounded-full text-[10px] font-medium border ${style}`}>
      {label}
    </span>
  )
}

// Unified live-card language (same as the sidebar rows / Active-now widget):
// generating = pulsing brand-surface tint, no dot; finished-unread = the tint
// held steady + a dot beside the title; otherwise the plain card surface.
function laneCardStateClass(status: string, unread: boolean): string {
  if (status === 'generating') {
    return 'oto-row-live motion-reduce:animate-none bg-brand-surface ring-1 ring-inset ring-brand/35'
  }
  if (unread) return 'bg-brand-surface ring-1 ring-inset ring-brand/30'
  return 'bg-white dark:bg-p-surface'
}

function UnreadDot() {
  return (
    <span
      title="New response — not opened yet"
      className="inline-block w-1.5 h-1.5 rounded-full bg-brand shrink-0"
    />
  )
}

/** One pinned-dashboard panel: approval card (when pending) over the
 * sandboxed frame. The pin row carries its own agent (a project pin may be
 * pinned by another agent's orchestrator). `projectLanes` feeds the frame's
 * viewer-scoped `project_lanes` subscription (otodock.feed). `autoHeight`
 * sizes the frame to its content (the Dock's single-scroll layout) instead
 * of filling the parent. */
function AppPanel({ app, fallbackAgent, onSendPrompt, projectLanes, testId, autoHeight = false }: {
  app: PinnedApp
  fallbackAgent: string
  onSendPrompt?: Props['onSendPrompt']
  projectLanes?: unknown[]
  testId: string
  autoHeight?: boolean
}) {
  const appAgent = app.agent || fallbackAgent
  return (
    <div className={autoHeight ? 'flex flex-col' : 'flex flex-col min-h-0 h-full'} data-testid={testId}>
      {appNeedsApproval(app) && (
        <AppApprovalCard key={app.id} app={app} agent={appAgent} />
      )}
      {/* Docked (autoHeight) panels drop the horizontal inset: the column
          wrapper already carries the page padding, and the extra 8px made the
          dashboard sit visibly narrower than the lane cards above it
          (operator ask — align the edges, especially on mobile). */}
      <div className={autoHeight ? 'py-2' : 'flex-1 min-h-0 p-2'}>
        <AppFrame app={app} agent={appAgent} onSendPrompt={onSendPrompt} projectLanes={projectLanes} autoHeight={autoHeight} />
      </div>
    </div>
  )
}

export default function ProjectsOverlay({
  agent, chatId, isProjectChat, pins, onSelectChat, onClose, onSendPrompt,
}: Props) {
  const { data: graph } = useQuery({
    queryKey: ['chat-project', chatId],
    queryFn: async () => {
      const res = await apiFetch(`/v1/chats/${chatId}/project`)
      if (!res.ok) throw new Error('Failed to fetch project')
      return res.json() as Promise<{ project_id: string; chats: LaneRow[] }>
    },
    enabled: isProjectChat,
    refetchInterval: 10_000,
  })

  const projectId = graph?.project_id
  const { data: boardMd } = useQuery({
    queryKey: ['project-board', agent, projectId],
    queryFn: async () => {
      if (!projectId) return null
      // Board location follows the orchestrator's workspace: shared agents
      // write workspace/, personal-scope work lands under the writer's own
      // users/<name>/workspace. Locate it through the (identity-filtered)
      // file tree instead of guessing path prefixes.
      const treeRes = await apiFetch(`/v1/agents/${encodeURIComponent(agent)}/files`)
      if (!treeRes.ok) return null
      const { tree } = await treeRes.json() as { tree: TreeNode[] }
      const boardPath = findBoardPath(tree ?? [], `projects/${projectId}/board.md`)
      if (!boardPath) return null
      const res = await apiFetch(
        `/v1/agents/${encodeURIComponent(agent)}/files/${boardPath.split('/').map(encodeURIComponent).join('/')}`,
      )
      if (!res.ok) return null
      const data = await res.json()
      return typeof data?.content === 'string' ? data.content : null
    },
    enabled: !!projectId,
    refetchInterval: 10_000,
  })

  // Live turn state per lane: chatStore's WS truth beats the 10s poll in
  // BOTH directions — a streaming slice pulses instantly, a ready/failed
  // slice retires a stale "generating" instead of pulsing 10s too long.
  // `awaiting_user` only the poll knows, so anything else passes through.
  const byChat = useChatStore((s) => s.byChat)
  const liveStatus = (id: string, polled: string): string => {
    const st = byChat[id]?.status
    if (st === 'streaming') return 'generating'
    if ((st === 'ready' || st === 'failed') && polled === 'generating') return 'idle'
    return polled
  }
  // Finished-unread per lane chat — the same store flag the sidebar dot
  // rides (chat_status flips it on a background finish, chat_read clears).
  const laneUnread = (id: string): boolean => !!byChat[id]?.unread

  const board = useMemo(() => (boardMd ? parseBoard(boardMd) : null), [boardMd])
  const chats = graph?.chats ?? []

  // The live cards show the ANCHOR's delegation round: the root the open chat
  // belongs to (its parent, or itself when it delegated) plus that root's own
  // workers. A long-lived project slug accretes rounds — lanes from OTHER
  // rounds collapse into the "Other project lanes" disclosure below, so the
  // dock never buries the current round under finished history.
  const anchor = chats.find((c) => c.id === chatId)
  const rootId = anchor?.parent_chat_id || chatId
  const roundChats = anchor
    ? chats.filter((c) => c.id === rootId || c.parent_chat_id === rootId)
    : chats
  const orchestrator = roundChats.find((c) => c.id === rootId)
    ?? roundChats.find((c) => c.delegate_role === 'orchestrator')

  // The `project_lanes` feed rows a docked dashboard may subscribe to — the
  // viewer's own /project slice with the WS-live status merged in.
  const laneFeedRows = useMemo(
    () => (graph ? chats.map((c) => ({ ...c, status: (() => {
      const st = byChat[c.id]?.status
      if (st === 'streaming') return 'generating'
      if ((st === 'ready' || st === 'failed') && c.status === 'generating') return 'idle'
      return c.status
    })() })) : undefined),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [graph, byChat],
  )

  // The LIVE block's lanes = the current round's worker chats; the board's
  // "## Lanes" rows render separately in the board block below (never
  // interleaved). Live beats round: a still-running lane from an earlier
  // round is project live state and stays among the cards (an adopting
  // orchestrator keeps full live visibility) — only finished history
  // collapses into the disclosure.
  const isLive = (c: LaneRow) => {
    const s = liveStatus(c.id, c.status)
    return s === 'generating' || s === 'awaiting_user'
  }
  const outsideRound = chats.filter((c) => !roundChats.includes(c))
  const laneChats = [
    ...roundChats.filter((c) => c.id !== (orchestrator?.id ?? '')),
    ...outsideRound.filter(isLive),
  ]
  const otherLanes = outsideRound.filter((c) => !isLive(c))

  const projectApp = pins?.project ?? null
  const chatApp = pins?.chat ?? null
  const filePins = pins?.files ?? []

  // Pinned files — read-only living documents (plan files, specs, notes),
  // each a collapsed row that expands to a live render. Rendered as its own
  // section in BOTH dock flavors.
  const filePinsSection = filePins.length > 0 ? (
    <div className="mb-4">
      <div className="border-t border-p-border-light/60 mb-3" />
      <p className="text-[10px] font-semibold text-p-text-light uppercase tracking-wider mb-1.5">Pinned files</p>
      <div className="space-y-1.5">
        {filePins.map((f) => <PinnedFileRow key={f.id} pin={f} />)}
      </div>
    </div>
  ) : null

  // ── Chat dock (non-project chat): the pinned app IS the overlay ──────────
  if (!isProjectChat) {
    return (
      <div className="flex-1 min-h-0 flex flex-col bg-p-bg pt-14" data-testid="projects-overlay">
        <div className="flex items-center justify-between px-4 py-1">
          <h2 className="text-sm font-semibold text-p-text truncate">
            {chatApp?.title || chatApp?.slug || 'Chat dock'}
          </h2>
          <button
            onClick={onClose}
            className="w-7 h-7 rounded-lg flex items-center justify-center text-p-text-light hover:text-p-text hover:bg-p-surface-hover transition-colors"
            title="Close chat dock"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
        {filePinsSection ? (
          // With file pins the chat dock becomes a page scroll: the app (if
          // any) at content height on top, the pinned files below.
          <div className="flex-1 min-h-0 overflow-y-auto">
            <div className="max-w-5xl mx-auto px-2 sm:px-4 pb-8">
              {chatApp && (
                <div className="rounded-xl border border-p-border-light overflow-hidden bg-white dark:bg-p-surface mb-4">
                  <AppPanel app={chatApp} fallbackAgent={agent} onSendPrompt={onSendPrompt} testId="dock-chat-app" autoHeight />
                </div>
              )}
              {filePinsSection}
            </div>
          </div>
        ) : chatApp ? (
          <AppPanel app={chatApp} fallbackAgent={agent} onSendPrompt={onSendPrompt} testId="dock-chat-app" />
        ) : (
          <div className="flex-1 flex items-center justify-center text-sm text-p-text-light">
            Nothing docked to this chat.
          </div>
        )}
      </div>
    )
  }

  // One card shape for round lanes AND the collapsed other-round rows (which
  // may include other rounds' orchestrators — the role label says so).
  const laneCard = (c: LaneRow) => {
    const status = liveStatus(c.id, c.status)
    const unread = laneUnread(c.id)
    return (
      <button
        key={c.id}
        onClick={() => onSelectChat(c.id)}
        className={`w-full text-left px-3 py-2 rounded-xl border border-p-border-light hover:bg-p-surface-hover transition-colors flex items-center justify-between gap-2 ${
          laneCardStateClass(status, unread)
        }`}
      >
        <span className="min-w-0 flex items-center gap-1.5">
          {unread && status !== 'generating' && <UnreadDot />}
          <span className="min-w-0">
            <span className="block text-xs font-medium text-p-text truncate">{c.title || c.id.slice(0, 8)}</span>
            <span className="block text-[10px] text-p-text-light">{c.delegate_role === 'orchestrator' ? 'orchestrator' : 'lane'} · {c.agent}</span>
          </span>
        </span>
        <StatusChip status={status} />
      </button>
    )
  }

  // ── Project dock: pinned project app (primary) beside the lane cards ─────
  const lanesPanel = (
    // px-2 on mobile (operator ask): the lane cards and the docked dashboard
    // below reach closer to the screen edges; sm+ keeps the roomier inset.
    <div className="max-w-5xl mx-auto px-2 sm:px-4 pt-16 pb-3">
      {/* Header */}
      <div className="flex items-center justify-between mb-1">
        <h2 className="text-lg font-semibold text-p-text truncate">
          {board?.title || projectId || 'Project'}
        </h2>
        <div className="flex items-center gap-2">
          {board?.status && (
            <span className="px-2 py-0.5 rounded-full text-[10px] font-semibold uppercase tracking-wide bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300 border border-amber-200 dark:border-amber-700/50">
              {board.status}
            </span>
          )}
          <button
            onClick={onClose}
            className="w-7 h-7 rounded-lg flex items-center justify-center text-p-text-light hover:text-p-text hover:bg-p-surface-hover transition-colors"
            title="Close project dock"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      </div>
      {/* Orchestrator card — amber, the delegating session's accent */}
      {orchestrator && (
        <button
          onClick={() => onSelectChat(orchestrator.id)}
          className={`w-full text-left mb-3 px-3 py-2 rounded-xl border-l-2 border-l-amber-500 dark:border-l-amber-400 border border-p-border-light hover:bg-p-surface-hover transition-colors flex items-center justify-between gap-2 ${
            laneCardStateClass(liveStatus(orchestrator.id, orchestrator.status), laneUnread(orchestrator.id))
          }`}
        >
          <span className="min-w-0 flex items-center gap-1.5">
            {laneUnread(orchestrator.id) && liveStatus(orchestrator.id, orchestrator.status) !== 'generating' && <UnreadDot />}
            <span className="min-w-0">
              <span className="block text-xs font-semibold text-p-text truncate">
                {orchestrator.title || 'Orchestrator'}
              </span>
              <span className="block text-[10px] text-p-text-light">orchestrator · {orchestrator.agent}</span>
            </span>
          </span>
          <StatusChip status={liveStatus(orchestrator.id, orchestrator.status)} />
        </button>
      )}

      {/* Lanes — the LIVE worker chats (the pulse cards). The board's own
          lane rows live in the board block below, never between these. */}
      <p className="text-[10px] font-semibold text-p-text-light uppercase tracking-wider mb-1.5">Lanes</p>
      {laneChats.length === 0 && (
        <p className="text-xs text-p-text-light mb-3">No lanes yet.</p>
      )}
      <div className="space-y-1.5 mb-4">
        {laneChats.map(laneCard)}
      </div>

      {/* Lanes from OTHER delegation rounds of this project (a reused slug
          accretes them over time) — present but out of the way. */}
      {otherLanes.length > 0 && (
        <details className="mb-4">
          <summary className="text-[10px] font-semibold text-p-text-light uppercase tracking-wider cursor-pointer select-none">
            Other project lanes ({otherLanes.length})
          </summary>
          <div className="space-y-1.5 mt-1.5">
            {otherLanes.map(laneCard)}
          </div>
        </details>
      )}

      {/* ── Board block — the board.md content as its own section, BELOW the
          live block: goal, the board's lane rows (plan view, non-interactive),
          Decisions/Hand-offs, and the collapsible full document. ── */}
      {(board || boardMd) && (
        <div className="mb-4">
          <div className="border-t border-p-border-light/60 mb-3" />
          <p className="text-[10px] font-semibold text-p-text-light uppercase tracking-wider mb-1.5">Board</p>
          {board?.goal && (
            <p className="text-xs text-p-text-secondary mb-2">{board.goal}</p>
          )}
          {board && board.lanes.length > 0 && (
            <div className="space-y-1 mb-3">
              {board.lanes.map((lane: BoardLane, i: number) => (
                <div
                  key={`${lane.name}-${i}`}
                  className="px-3 py-1.5 rounded-lg border border-p-border-light/70 bg-white dark:bg-p-surface flex items-center justify-between gap-2"
                >
                  <span className="min-w-0 flex items-center gap-2">
                    <span className={`inline-block w-3.5 h-3.5 rounded border text-[9px] leading-[13px] text-center shrink-0 ${
                      lane.done
                        ? 'bg-emerald-500 border-emerald-500 text-white'
                        : 'border-p-border bg-transparent text-transparent'
                    }`}>✓</span>
                    <span className="block text-xs text-p-text-secondary truncate">{lane.name}</span>
                  </span>
                  <StatusChip status={lane.done ? 'idle' : lane.status || 'idle'} />
                </div>
              ))}
            </div>
          )}

          {/* Decisions & hand-offs from the board document */}
          {board && (board.decisions.length > 0 || board.handoffs.length > 0) && (
            <div className="grid sm:grid-cols-2 gap-3 mb-3">
              {board.decisions.length > 0 && (
                <div className="px-3 py-2 rounded-xl border border-p-border-light bg-white dark:bg-p-surface">
                  <p className="text-[10px] font-semibold text-p-text-light uppercase tracking-wider mb-1">Decisions</p>
                  <ul className="text-xs text-p-text-secondary space-y-1 list-disc list-inside">
                    {board.decisions.map((d, i) => <li key={i}>{d}</li>)}
                  </ul>
                </div>
              )}
              {board.handoffs.length > 0 && (
                <div className="px-3 py-2 rounded-xl border border-p-border-light bg-white dark:bg-p-surface">
                  <p className="text-[10px] font-semibold text-p-text-light uppercase tracking-wider mb-1">Hand-offs</p>
                  <ul className="text-xs text-p-text-secondary space-y-1 list-disc list-inside">
                    {board.handoffs.map((d, i) => <li key={i}>{d}</li>)}
                  </ul>
                </div>
              )}
            </div>
          )}

          {/* Full board document for everything the structured view skips. */}
          {boardMd && (
            <details className="rounded-xl border border-p-border-light bg-white dark:bg-p-surface px-3 py-2">
              <summary className="text-[10px] font-semibold text-p-text-light uppercase tracking-wider cursor-pointer select-none">
                Board file
              </summary>
              <div className="mt-2 text-sm">
                <MarkdownContent>{boardMd}</MarkdownContent>
              </div>
            </details>
          )}
        </div>
      )}

      {/* Pinned files — living documents beside the board (plan file,
          specs), collapsed rows below the board block. */}
      {filePinsSection}

      {/* The anchor chat's own docked dashboard rides below the lanes —
          content-height, the page scroll owns it. */}
      {chatApp && (
        <div className="mt-4 rounded-xl border border-p-border-light overflow-hidden bg-white dark:bg-p-surface">
          <AppPanel app={chatApp} fallbackAgent={agent} onSendPrompt={onSendPrompt} projectLanes={laneFeedRows} testId="dock-chat-app" autoHeight />
        </div>
      )}
    </div>
  )

  if (!projectApp) {
    // Lanes-only — one scroll container.
    return (
      <div className="flex-1 min-h-0 overflow-y-auto bg-p-bg" data-testid="projects-overlay">
        {lanesPanel}
      </div>
    )
  }

  // App + lanes: ONE page scroll on every breakpoint — the platform lane
  // section on top (orchestrator + lanes ARE the delegate session's live
  // state), a hairline separator, then the pinned project dashboard at its
  // natural content height. No inner scroll containers: the frame adopts the
  // app's reported height, so the user scrolls the page, never a panel.
  return (
    <div className="flex-1 min-h-0 bg-p-bg overflow-y-auto" data-testid="projects-overlay">
      {lanesPanel}
      {/* Same 5xl column AND the same horizontal inset as the lanes — the
          platform section and the dashboard read as one page, edges aligned
          on every breakpoint (operator ask). */}
      <div className="max-w-5xl mx-auto px-2 sm:px-4 pb-8">
        <div className="border-t border-p-border-light/60 mb-3" />
        <AppPanel app={projectApp} fallbackAgent={agent} onSendPrompt={onSendPrompt} projectLanes={laneFeedRows} testId="dock-project-app" autoHeight />
      </div>
    </div>
  )
}
