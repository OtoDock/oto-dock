import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useAdminMcps, useCheckMcpUpdates, useMcpUpdateState, isUpdateOffer, McpServer } from '../../api/mcps'
import CommunityMcpsBrowser from '../../components/CommunityMcpsBrowser'
import McpCategoryFilter from '../../components/McpCategoryFilter'
import { formatRelativeTime } from '../../lib/format'
import { McpRow } from './McpServersPage.row'
import { InstallModal } from './McpServersPage.installModal'
import { matchesMcpCategory, type McpCategoryFilterValue } from '../../lib/mcp/categories'
import Button from '../../components/ui/Button'
import { Input } from '../../components/ui/Field'
import { BADGE_BASE, BADGE_TONE } from '../../components/ui/classes'
import { PAGE_TITLE } from '../../components/ui/classes'

const CATEGORY_ORDER: Record<string, number> = { core: 0, custom: 1, community: 2 }
const CATEGORY_LABEL: Record<string, string> = { core: 'Core', custom: 'Custom', community: 'Community' }

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function McpServersPage() {
  const { data: allMcps, isLoading } = useAdminMcps()
  const [showInstall, setShowInstall] = useState(false)
  const [showBrowse, setShowBrowse] = useState(false)
  const [query, setQuery] = useState('')
  const [categoryFilter, setCategoryFilter] = useState<McpCategoryFilterValue>('all')
  // The last check as the proxy persisted it is what the page shows; a check
  // run from this page (or the Skills page, which shares the check query)
  // answers in the same shape and replaces it.
  const qc = useQueryClient()
  const { data: updateState } = useMcpUpdateState()
  const { data: checkData, refetch: checkUpdates, isFetching: checkingUpdates, dataUpdatedAt } = useCheckMcpUpdates()
  useEffect(() => {
    if (checkData) qc.setQueryData(['mcp-update-state'], checkData)
  }, [checkData, dataUpdatedAt, qc])
  const updates = updateState?.updates || {}
  const checkedAt = updateState?.checked_at || ''

  // Standalone skill packages (category "skill") live on the admin Skills
  // page — filtered out here so they don't double-list as servers.
  const mcps = allMcps?.filter(m => m.category !== 'skill')

  if (isLoading) return <div className="text-sm text-tertiary">Loading MCP servers...</div>
  if (!mcps || mcps.length === 0) return <div className="text-sm text-tertiary">No MCP servers found.</div>

  // Group by category (after applying the category and search filters)
  const q = query.trim().toLowerCase()
  const categoryCounts: Record<string, number> = {}
  for (const m of mcps) categoryCounts[m.category] = (categoryCounts[m.category] ?? 0) + 1
  const visibleMcps = mcps.filter(m =>
    matchesMcpCategory(m.category, categoryFilter) && (
      !q ||
      (m.label || '').toLowerCase().includes(q) ||
      (m.description || '').toLowerCase().includes(q) ||
      m.name.toLowerCase().includes(q)
    ),
  )
  const grouped: Record<string, McpServer[]> = {}
  for (const mcp of visibleMcps) {
    const cat = mcp.category
    if (!grouped[cat]) grouped[cat] = []
    grouped[cat].push(mcp)
  }

  const categories = Object.keys(grouped).sort((a, b) => (CATEGORY_ORDER[a] ?? 3) - (CATEGORY_ORDER[b] ?? 3))

  const enabled = mcps.filter(m => m.enabled).length
  // Skill-package updates surface on the Skills page, not here. A finished
  // switch and an install ahead of the catalog are not offers.
  const updateCount = mcps.filter(m => isUpdateOffer(updates[m.name])).length

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 sm:gap-3 mb-4">
        <h1 className={PAGE_TITLE}>MCP Servers</h1>
        <span className={`${BADGE_BASE} ${BADGE_TONE.neutral}`}>
          {enabled}/{mcps.length} enabled
        </span>
        <div className="flex-1 min-w-0" />
        {updateCount > 0 && (
          <span className={`${BADGE_BASE} ${BADGE_TONE.info}`}>
            {updateCount} update{updateCount > 1 ? 's' : ''}
          </span>
        )}
        {checkedAt && !checkingUpdates && (
          <span className="hidden sm:inline text-2xs text-tertiary" title={checkedAt}>
            checked {formatRelativeTime(checkedAt)}
          </span>
        )}
        <Button variant="secondary" size="xs"
          onClick={() => checkUpdates()}
          disabled={checkingUpdates}
        >
          <svg className={`w-3.5 h-3.5 ${checkingUpdates ? 'animate-spin' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
          <span className="hidden sm:inline">{checkingUpdates ? 'Checking…' : 'Check Updates'}</span>
          <span className="sm:hidden">{checkingUpdates ? '…' : 'Updates'}</span>
        </Button>
        <Button variant="secondary" size="xs"
          onClick={() => setShowInstall(true)}
        >
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
          </svg>
          Install
        </Button>
        <Button variant="primary" size="xs"
          onClick={() => setShowBrowse(true)}
        >
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-4.35-4.35M11 19a8 8 0 100-16 8 8 0 000 16z" />
          </svg>
          <span className="hidden sm:inline">Browse Community</span>
          <span className="sm:hidden">Browse</span>
        </Button>
      </div>

      {/* Search + category filter */}
      <div className="mb-4 max-w-md flex items-center gap-2">
        <div className="relative flex-1 min-w-0">
          <svg className="w-4 h-4 absolute left-2.5 top-1/2 -translate-y-1/2 text-tertiary pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-4.35-4.35M11 19a8 8 0 100-16 8 8 0 000 16z" />
          </svg>
          <Input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search MCP servers…"
            className="pl-8 pr-8"
          />
          {query && (
            <button
              onClick={() => setQuery('')}
              aria-label="Clear search"
              className="absolute right-2 top-1/2 -translate-y-1/2 w-5 h-5 flex items-center justify-center rounded-control text-tertiary hover:text-primary hover:bg-surface-hover"
            >
              ×
            </button>
          )}
        </div>
        <McpCategoryFilter value={categoryFilter} onChange={setCategoryFilter} counts={categoryCounts} />
      </div>

      {categories.length === 0 && (
        <p className="text-sm text-tertiary py-4">
          {q ? `No MCP servers match "${query}".` : 'No MCP servers in this category.'}
        </p>
      )}

      {categories.map(cat => (
        <div key={cat} className="mb-6">
          <h2 className="text-xs font-semibold text-tertiary uppercase tracking-wider mb-2">
            {CATEGORY_LABEL[cat] || cat}
          </h2>
          <div className="space-y-2">
            {grouped[cat].map(mcp => (
              <McpRow key={mcp.name} mcp={mcp} updateInfo={updates[mcp.name]}
                autoUpdateEnabled={updateState?.auto_update_enabled ?? true} />
            ))}
          </div>
        </div>
      ))}

      {showInstall && <InstallModal onClose={() => setShowInstall(false)} />}
      <CommunityMcpsBrowser open={showBrowse} onClose={() => setShowBrowse(false)} />
    </div>
  )
}
