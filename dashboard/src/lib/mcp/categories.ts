// The MCP lists' category filter values and the match the lists apply.

export type McpCategoryFilterValue = 'all' | 'core' | 'custom' | 'community'

export function matchesMcpCategory(category: string, filter: McpCategoryFilterValue): boolean {
  return filter === 'all' || category === filter
}
