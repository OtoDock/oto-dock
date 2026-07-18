/**
 * Admin → Skills page.
 *
 * Lists installed standalone skill packages (admin MCP rows with
 * ``category === 'skill'`` — filtered OUT of McpServersPage so they don't
 * double-list). Bundled skills stay visible on their MCP's admin row; this
 * page only manages the standalone packages: versions, updates (the shared
 * ``update_one`` endpoint handles the skill branch server-side), delete
 * (shared DELETE /v1/admin/mcps/{name} — accepts category "skill"), and
 * access to the community-skills browser.
 */

import { useState } from 'react'
import { useAdminMcps, useCheckMcpUpdates, useUpdateMcp, useDeleteMcp, McpServer, McpUpdateInfo } from '../../api/mcps'
import CommunitySkillsBrowser from '../../components/CommunitySkillsBrowser'

export default function SkillsPage() {
  const { data: mcps, isLoading } = useAdminMcps()
  const [showBrowse, setShowBrowse] = useState(false)
  const { data: updateData, refetch: checkUpdates, isFetching: checkingUpdates } = useCheckMcpUpdates()
  const updates = updateData?.updates || {}

  if (isLoading) return <div className="text-sm text-p-text-light">Loading skill packages...</div>

  const packages = (mcps ?? []).filter(m => m.category === 'skill')
  const updateCount = packages.filter(p => updates[p.name]).length

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 sm:gap-3 mb-4">
        <h1 className="text-lg font-bold text-p-text">Skills</h1>
        <span className="text-xs px-2 py-0.5 rounded-lg bg-p-surface dark:bg-gray-800 text-p-text-secondary font-medium">
          {packages.length} installed
        </span>
        <div className="flex-1 min-w-0" />
        {updateCount > 0 && (
          <span className="text-xs px-2 py-0.5 rounded-lg bg-brand/10 dark:bg-brand/20 text-brand font-medium">
            {updateCount} update{updateCount > 1 ? 's' : ''}
          </span>
        )}
        <button
          onClick={() => checkUpdates()}
          disabled={checkingUpdates}
          className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg border border-p-border-light text-p-text-secondary hover:bg-p-surface-hover transition-colors disabled:opacity-40"
        >
          <svg className={`w-3.5 h-3.5 ${checkingUpdates ? 'animate-spin' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
          <span className="hidden sm:inline">{checkingUpdates ? 'Checking…' : 'Check Updates'}</span>
          <span className="sm:hidden">{checkingUpdates ? '…' : 'Updates'}</span>
        </button>
        <button
          onClick={() => setShowBrowse(true)}
          className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg bg-brand text-white hover:bg-brand-hover transition-colors"
        >
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-4.35-4.35M11 19a8 8 0 100-16 8 8 0 000 16z" />
          </svg>
          <span className="hidden sm:inline">Browse Community Skills</span>
          <span className="sm:hidden">Browse</span>
        </button>
      </div>

      <p className="text-xs text-p-text-light mb-4">
        Standalone skill packages installed from the community-skills catalog.
        Skills are enabled or disabled per agent in Agent Settings → Skills.
      </p>

      {packages.length === 0 && (
        <p className="text-sm text-p-text-light py-4">
          No skill packages installed yet. Browse the community skills catalog to install one.
        </p>
      )}

      <div className="space-y-2">
        {packages.map(pkg => (
          <SkillPackageRow key={pkg.name} pkg={pkg} updateInfo={updates[pkg.name]} />
        ))}
      </div>

      <CommunitySkillsBrowser open={showBrowse} onClose={() => setShowBrowse(false)} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Row
// ---------------------------------------------------------------------------

function SkillPackageRow({ pkg, updateInfo }: { pkg: McpServer; updateInfo?: McpUpdateInfo }) {
  const updateMcp = useUpdateMcp()
  const deleteMcp = useDeleteMcp()
  const [error, setError] = useState<string | null>(null)

  const agentCount = pkg.agents?.length ?? 0
  const skillCount = pkg.skills?.length ?? 0

  const handleUpdate = () => {
    setError(null)
    updateMcp.mutate(pkg.name, {
      onError: e => setError((e as Error)?.message || 'Update failed'),
    })
  }

  const handleDelete = () => {
    if (!window.confirm(
      `Delete "${pkg.label}"? This removes the skill package and its ` +
      `per-agent enablement everywhere. This cannot be undone.`,
    )) return
    setError(null)
    deleteMcp.mutate(pkg.name, {
      onError: e => setError((e as Error)?.message || 'Delete failed'),
    })
  }

  return (
    <div className="rounded-lg border border-p-border-light bg-white dark:bg-p-surface p-3">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-sm font-medium text-p-text">{pkg.label}</span>
            <span className="text-xs font-mono text-p-text-light">{pkg.name}</span>
            {pkg.version && (
              <span className="text-[10px] text-p-text-light">v{pkg.version}</span>
            )}
            <span className="text-[10px] px-1.5 py-0.5 rounded-sm bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-400">
              {skillCount} skill{skillCount === 1 ? '' : 's'}
            </span>
            {agentCount > 0 && (
              <span className="text-[10px] px-1.5 py-0.5 rounded-sm bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400">
                enabled on {agentCount} agent{agentCount === 1 ? '' : 's'}
              </span>
            )}
          </div>
          {pkg.description && (
            <p className="text-xs text-p-text-light mt-0.5">{pkg.description}</p>
          )}
        </div>

        <div className="flex items-center gap-1.5 shrink-0">
          {updateInfo && (
            <button
              onClick={handleUpdate}
              disabled={updateMcp.isPending}
              className="text-xs px-2 py-1 rounded-sm border border-amber-300 dark:border-amber-700 text-amber-700 dark:text-amber-400 hover:bg-amber-50 dark:hover:bg-amber-900/20 disabled:opacity-40 transition-colors"
              title={updateInfo.current !== updateInfo.latest
                ? `Update ${updateInfo.current} → ${updateInfo.latest}`
                : 'Catalog content changed — reinstall to converge'}
            >
              {updateMcp.isPending ? 'Updating…' : 'Update'}
            </button>
          )}
          <button
            onClick={handleDelete}
            disabled={deleteMcp.isPending}
            className="text-xs px-2 py-1 rounded-sm border border-red-300 dark:border-red-700 text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 disabled:opacity-40 transition-colors"
          >
            {deleteMcp.isPending ? 'Deleting…' : 'Delete'}
          </button>
        </div>
      </div>

      {error && (
        <div className="mt-2 rounded-sm border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/20 px-2 py-1.5">
          <p className="text-[11px] text-red-700 dark:text-red-400 whitespace-pre-wrap">{error}</p>
        </div>
      )}
    </div>
  )
}
