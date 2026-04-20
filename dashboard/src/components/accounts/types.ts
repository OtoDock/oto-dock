/**
 * Shared multi-account UI types + ops bundle.
 *
 * The accounts UX is rendered for per-user MCP credentials. The rendering
 * components take an ``ops`` bundle prop that provides the mutation hooks;
 * ``UserAccountsManager`` is the entry point that builds it.
 */

import type { UseMutationResult } from '@tanstack/react-query'

export interface SetIntegrationVars {
  mcpName: string
  credentials: Record<string, string>
  accountLabel?: string
}
export interface DeleteIntegrationVars {
  mcpName: string
  accountLabel: string
}
export interface SetDefaultVars {
  mcpName: string
  accountLabel: string
}
export interface SetBindingVars {
  mcpName: string
  agentName: string
  accountLabel: string
}
export interface RemoveBindingVars {
  mcpName: string
  agentName: string
}

export interface AccountsOps {
  setIntegration: UseMutationResult<unknown, Error, SetIntegrationVars, unknown>
  deleteIntegration: UseMutationResult<unknown, Error, DeleteIntegrationVars, unknown>
  setDefault: UseMutationResult<unknown, Error, SetDefaultVars, unknown>
  /** Per-agent override hooks are user-side only — they pin which of the
   * user's accounts the agent uses in *user-scope* chats. Service-side
   * (admin) bindings live in Agent Settings → MCPs via the new
   * ``ServiceAccountBindingDropdown``, NOT in this card. Optional so
   * service-side ops can omit them entirely. */
  setBinding?: UseMutationResult<unknown, Error, SetBindingVars, unknown>
  removeBinding?: UseMutationResult<unknown, Error, RemoveBindingVars, unknown>
}
