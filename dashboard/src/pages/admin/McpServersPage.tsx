import { useState } from 'react'
import { useAdminMcps, useCheckMcpUpdates, McpServer } from '../../api/mcps'
import CommunityMcpsBrowser from '../../components/CommunityMcpsBrowser'
import { McpRow } from './McpServersPage.row'
import { InstallModal } from './McpServersPage.installModal'

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
  const { data: updateData, refetch: checkUpdates, isFetching: checkingUpdates } = useCheckMcpUpdates()
  const updates = updateData?.updates || {}

  // Standalone skill packages (category "skill") live on the admin Skills
  // page — filtered out here so they don't double-list as servers.
  const mcps = allMcps?.filter(m => m.category !== 'skill')

  if (isLoading) return <div className="text-sm text-p-text-light">Loading MCP servers...</div>
  if (!mcps || mcps.length === 0) return <div className="text-sm text-p-text-light">No MCP servers found.</div>

  // Group by category (after applying the search filter)
  const q = query.trim().toLowerCase()
  const visibleMcps = q
    ? mcps.filter(m =>
        (m.label || '').toLowerCase().includes(q) ||
        (m.description || '').toLowerCase().includes(q) ||
        m.name.toLowerCase().includes(q),
      )
    : mcps
  const grouped: Record<string, McpServer[]> = {}
  for (const mcp of visibleMcps) {
    const cat = mcp.category
    if (!grouped[cat]) grouped[cat] = []
    grouped[cat].push(mcp)
  }

  const categories = Object.keys(grouped).sort((a, b) => (CATEGORY_ORDER[a] ?? 3) - (CATEGORY_ORDER[b] ?? 3))

  const enabled = mcps.filter(m => m.enabled).length
  // Skill-package updates surface on the Skills page, not here.
  const updateCount = mcps.filter(m => updates[m.name]).length

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 sm:gap-3 mb-4">
        <h1 className="text-lg font-bold text-p-text">MCP Servers</h1>
        <span className="text-xs px-2 py-0.5 rounded-lg bg-p-surface dark:bg-gray-800 text-p-text-secondary font-medium">
          {enabled}/{mcps.length} enabled
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
          onClick={() => setShowInstall(true)}
          className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg border border-p-border-light text-p-text-secondary hover:bg-p-surface-hover transition-colors"
        >
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
          </svg>
          Install
        </button>
        <button
          onClick={() => setShowBrowse(true)}
          className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg bg-brand text-white hover:bg-brand-hover transition-colors"
        >
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-4.35-4.35M11 19a8 8 0 100-16 8 8 0 000 16z" />
          </svg>
          <span className="hidden sm:inline">Browse Community</span>
          <span className="sm:hidden">Browse</span>
        </button>
      </div>

      {/* Search */}
      <div className="relative mb-4 max-w-md">
        <svg className="w-4 h-4 absolute left-2.5 top-1/2 -translate-y-1/2 text-p-text-light pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-4.35-4.35M11 19a8 8 0 100-16 8 8 0 000 16z" />
        </svg>
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search MCP servers…"
          className="w-full pl-8 pr-8 py-2 text-sm rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30"
        />
        {query && (
          <button
            onClick={() => setQuery('')}
            aria-label="Clear search"
            className="absolute right-2 top-1/2 -translate-y-1/2 w-5 h-5 flex items-center justify-center rounded-sm text-p-text-light hover:text-p-text hover:bg-p-surface-hover"
          >
            ×
          </button>
        )}
      </div>

      {categories.length === 0 && (
        <p className="text-sm text-p-text-light py-4">No MCP servers match "{query}".</p>
      )}

      {categories.map(cat => (
        <div key={cat} className="mb-6">
          <h2 className="text-xs font-semibold text-p-text-light uppercase tracking-wider mb-2">
            {CATEGORY_LABEL[cat] || cat}
          </h2>
          <div className="space-y-2">
            {grouped[cat].map(mcp => <McpRow key={mcp.name} mcp={mcp} updateInfo={updates[mcp.name]} />)}
          </div>
        </div>
      ))}

      {showInstall && <InstallModal onClose={() => setShowInstall(false)} />}
      <CommunityMcpsBrowser open={showBrowse} onClose={() => setShowBrowse(false)} />
    </div>
  )
}
