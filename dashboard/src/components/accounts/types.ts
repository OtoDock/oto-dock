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
export interface AddAgentAccountVars {
  mcpName: string
  agentName: string
  accountLabel: string
  /** The person's own GitHub reach for the agent; absent keeps it. */
  repositories?: string[]
  readOnly?: boolean
  makeDefault?: boolean
}
/** One account of one agent's set: taken out of it, or made its default. */
export interface AgentAccountVars {
  mcpName: string
  agentName: string
  accountLabel: string
}

export interface AccountsOps {
  setIntegration: UseMutationResult<unknown, Error, SetIntegrationVars, unknown>
  deleteIntegration: UseMutationResult<unknown, Error, DeleteIntegrationVars, unknown>
  setDefault: UseMutationResult<unknown, Error, SetDefaultVars, unknown>
  /** Each agent's set of accounts is user-side only: which of the user's
   * accounts the agent may use in *user-scope* chats, and the one it starts
   * on. The agent's own accounts live in Agent Settings → MCPs
   * (``AgentAccounts``), NOT in this card. Optional so
   * service-side ops can omit them entirely. */
  addAgentAccount?: UseMutationResult<unknown, Error, AddAgentAccountVars, unknown>
  removeAgentAccount?: UseMutationResult<unknown, Error, AgentAccountVars, unknown>
  setAgentDefault?: UseMutationResult<unknown, Error, AgentAccountVars, unknown>
}
