/**
 * The MCP assignment request's status (`mcp_assignment_requests.status`) —
 * mirrored from the proxy's authority `proxy/storage/mcp/mcp_request_store.py`
 * (`tests/storage/test_status_vocabularies.py` binds this file to it; edit
 * both). The transitions live there; the page shows and asks.
 */

export const MCP_REQUEST_STATUS = {
  PENDING: 'pending',
  APPROVED: 'approved',
  INSTALLING: 'installing',
  INSTALLED: 'installed',
  INSTALL_FAILED: 'install_failed',
  REJECTED: 'rejected',
  CANCELLED: 'cancelled',
} as const
export type McpRequestStatus = (typeof MCP_REQUEST_STATUS)[keyof typeof MCP_REQUEST_STATUS]
