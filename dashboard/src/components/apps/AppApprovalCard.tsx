import { useState } from 'react'
import { useApproveApp, type AppAction, type PinnedApp } from '../../api/apps'

/**
 * Declared-actions approval card — shared between the standing apps overlay
 * and the Dock (chat/project-scoped pins), so every surface renders the SAME
 * plain-language action summary over the same sig-checked approve call.
 * Render it only when the app needs approval (`appNeedsApproval`).
 */

export function appNeedsApproval(app: PinnedApp | null | undefined): boolean {
  return !!app && app.actions.length > 0
    && (!app.actions_approved || app.approval_stale)
}

/** Compact `key: value` chip for approval-card parameter summaries. */
function ParamChip({ k, v }: { k: string; v?: string }) {
  return (
    <code className="inline-flex max-w-56 items-baseline gap-0.5 truncate rounded bg-black/8 px-1 py-px font-mono text-[10px] dark:bg-white/10">
      <span className="font-semibold">{k}</span>
      {v !== undefined && <span className="truncate">: {v}</span>}
    </code>
  )
}

function chipValue(v: unknown): string {
  const s = typeof v === 'string' ? v : JSON.stringify(v)
  return s.length > 24 ? `${s.slice(0, 24)}…` : s
}

/** One plain-language approval line — raw manifests live behind the card's
 * "show exact manifest" expander, never inline. */
function ActionLine({ ac }: { ac: AppAction }) {
  const pageFills = Object.keys(ac.args_schema?.properties ?? {})
  return (
    <li className="flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5">
      <span className="font-medium text-p-text">{ac.label}</span>
      {ac.type === 'mcp_tool' ? (
        <>
          <span>calls <code className="font-mono text-[11px]">{ac.tool}</code> on {ac.mcp}</span>
          {Object.entries(ac.fixed_args ?? {}).map(([k, v]) => (
            <ParamChip key={k} k={k} v={chipValue(v)} />
          ))}
          {pageFills.map((k) => <ParamChip key={k} k={k} v="filled by the page" />)}
          {ac.mcp_available === false && (
            <span className="text-amber-600 dark:text-amber-400">— its MCP is currently unavailable</span>
          )}
        </>
      ) : ac.type === 'fire_task' ? (
        <>
          <span>runs the task “{ac.task_name || ac.task_id}”</span>
          {pageFills.map((k) => <ParamChip key={k} k={k} v="filled by the page" />)}
        </>
      ) : ac.type === 'data_feed' ? (
        <span>
          receives the live <code className="font-mono text-[11px]">{ac.feed}</code> platform
          feed (read-only — your own view of it)
        </span>
      ) : (
        <span className="break-all">
          sends to the chat: “{(ac.prompt || '').slice(0, 100)}{(ac.prompt || '').length > 100 ? '…' : ''}”
        </span>
      )}
    </li>
  )
}

const TYPE_GROUPS: Array<{ type: AppAction['type']; heading: string }> = [
  { type: 'mcp_tool', heading: 'Tool calls' },
  { type: 'fire_task', heading: 'Task runs' },
  { type: 'send_prompt', heading: 'Chat prompts' },
  { type: 'data_feed', heading: 'Live data feeds' },
]

interface Props {
  app: PinnedApp
  /** The agent whose ['apps', agent] cache the approval refreshes — for Dock
      pins use the pin row's own agent (a project pin may be foreign). */
  agent: string
}

export default function AppApprovalCard({ app, agent }: Props) {
  const approve = useApproveApp(agent)
  const [showManifest, setShowManifest] = useState(false)
  const grouped = app.actions.length > 6

  return (
    <div className="mx-3 mt-2 rounded-xl border border-amber-500/40 bg-amber-500/5 px-3 py-2.5 text-xs">
      <p className="font-medium text-p-text">
        {app.approval_stale
          ? 'The approval for this app’s actions is stale — review and re-approve.'
          : `“${app.title || app.slug}” declares ${app.actions.length} action button${app.actions.length > 1 ? 's' : ''} — review before they work.`}
      </p>
      {grouped ? (
        TYPE_GROUPS.map(({ type, heading }) => {
          const acs = app.actions.filter((ac) => ac.type === type)
          if (!acs.length) return null
          return (
            <div key={type} className="mt-1.5">
              <p className="text-[10px] font-semibold uppercase tracking-wide text-p-text-light">
                {heading} ({acs.length})
              </p>
              <ul className="mt-0.5 space-y-0.5 text-p-text-secondary">
                {acs.map((ac) => <ActionLine key={ac.id} ac={ac} />)}
              </ul>
            </div>
          )
        })
      ) : (
        <ul className="mt-1.5 space-y-0.5 text-p-text-secondary">
          {app.actions.map((ac) => <ActionLine key={ac.id} ac={ac} />)}
        </ul>
      )}
      <button
        onClick={() => setShowManifest((v) => !v)}
        className="mt-1.5 text-p-text-light underline decoration-dotted underline-offset-2 hover:text-p-text-secondary"
      >
        {showManifest ? 'hide exact manifest' : 'show exact manifest'}
      </button>
      {showManifest && (
        <pre className="mt-1 max-h-48 overflow-auto rounded-lg bg-black/5 p-2 font-mono text-[10px] leading-snug text-p-text-secondary dark:bg-white/5">
          {JSON.stringify(app.actions, null, 2)}
        </pre>
      )}
      <div className="mt-2 flex items-center gap-2">
        {app.can_approve ? (
          <button
            onClick={() => approve.mutate({ appId: app.id, sig: app.actions_sig })}
            disabled={approve.isPending}
            className="rounded-md bg-emerald-600 px-2.5 py-1 font-medium text-white transition-colors hover:bg-emerald-700 disabled:opacity-60"
          >
            Approve actions
          </button>
        ) : (
          <span className="text-p-text-light">
            {app.scope === 'shared'
              ? 'Approval needs an editor of this agent (with run access to the tasks).'
              : 'Approval needs run access to the referenced tasks.'}
          </span>
        )}
        {approve.isError && (
          <span className="text-red-500">{(approve.error as Error).message}</span>
        )}
      </div>
    </div>
  )
}
