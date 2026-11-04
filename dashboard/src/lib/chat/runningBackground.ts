/**
 * The background work of a chat that still runs after its turn (background
 * sub-agents and commands, by spawn id). A view rebuilt from rows reads
 * every stored spawn as finished: the history frame's `running` field says
 * which ones are not, and every rebuild marks them running again.
 */
import type { DisplayMessage, MessageBlock } from '../../components/chat/types'
import { BG_COMMAND_BLOCK } from '../kinds/block'
import type { RunningBackgroundEntry, RunningBackgroundWire } from '../../api/wireEvents'

/** The background work of the chat that still runs, by spawn id: what a view
 *  rebuilt from rows cannot know, since every stored spawn reads finished. */
export interface RunningBackground {
  agents: RunningBackgroundEntry[]
  commands: RunningBackgroundEntry[]
  /** The chat the list belongs to: another chat (a new one, the next one
   *  picked before its history arrives) reads nothing from it. */
  chatId?: string | null
}

export const NO_RUNNING_BACKGROUND: RunningBackground = { agents: [], commands: [] }

/** A background spawn's block (a background sub-agent's or a command's) as its
 *  running list, spawn id and whether it shows running, else null. A
 *  sub-agent's block is the one that carries `_background`. */
export function backgroundSpawnOf(b: MessageBlock): { list: 'agents' | 'commands'; id: string; active: boolean; description: string } | null {
  if ('_background' in b && b._background && b._toolId) {
    return { list: 'agents', id: b._toolId, active: !!b.isActive, description: b.description || '' }
  }
  if (b.type === BG_COMMAND_BLOCK && b._toolId) {
    return { list: 'commands', id: b._toolId, active: !!b.isActive, description: b.description || b.command || '' }
  }
  return null
}

/** The block of a spawn that still runs, active again. Callers pass a block
 *  `backgroundSpawnOf` names: the key test alone also holds for a foreground
 *  sub-agent's block, whose `_background` is false. */
export function runningAgain(b: MessageBlock): MessageBlock {
  return '_background' in b || b.type === BG_COMMAND_BLOCK ? { ...b, isActive: true } : b
}

/** A history frame's `running` field (absent: nothing runs). */
export function runningBackgroundOf(wire: RunningBackgroundWire | null | undefined): RunningBackground {
  const agents = Array.isArray(wire?.agents) ? wire.agents.filter((e) => e && e.id) : []
  const commands = Array.isArray(wire?.commands) ? wire.commands.filter((e) => e && e.id) : []
  return agents.length || commands.length ? { agents, commands } : NO_RUNNING_BACKGROUND
}

/** The list when it belongs to `chatId`, else nothing. */
export function runningFor(running: RunningBackground, chatId: string | null | undefined): RunningBackground {
  return chatId && running.chatId === chatId ? running : NO_RUNNING_BACKGROUND
}

/** The rebuilt view with the listed background sub-agents and commands
 *  running again (their stored spawn blocks read finished). The list is
 *  returned as it was when nothing is listed or nothing matched. */
export function markRunningBackground(messages: DisplayMessage[], running: RunningBackground): DisplayMessage[] {
  if (!running.agents.length && !running.commands.length) return messages
  const ids = { agents: new Set(running.agents.map((e) => e.id)), commands: new Set(running.commands.map((e) => e.id)) }
  const listed = (b: MessageBlock): boolean => {
    const spawn = backgroundSpawnOf(b)
    return !!spawn && !spawn.active && ids[spawn.list].has(spawn.id)
  }
  let changed = false
  const next = messages.map((msg) => {
    if (!msg.blocks.some(listed)) return msg
    changed = true
    return { ...msg, blocks: msg.blocks.map((b) => (listed(b) ? runningAgain(b) : b)) }
  })
  return changed ? next : messages
}

/** What a view shows running now, added to `running`: a rebuild from REST rows
 *  (they carry no `running` list: a refetch after new rows, a scroll up) keeps
 *  the badges the live frames lit, the status bar's too when the spawn's row
 *  is older than the page. A history frame's own list replaces it. */
export function withRunningInView(running: RunningBackground, messages: DisplayMessage[]): RunningBackground {
  const seen = { agents: new Set(running.agents.map((e) => e.id)), commands: new Set(running.commands.map((e) => e.id)) }
  const out = { agents: [...running.agents], commands: [...running.commands] }
  for (const msg of messages) {
    for (const b of msg.blocks) {
      const spawn = backgroundSpawnOf(b)
      if (spawn?.active && !seen[spawn.list].has(spawn.id)) {
        seen[spawn.list].add(spawn.id)
        out[spawn.list].push({ id: spawn.id, description: spawn.description })
      }
    }
  }
  return out.agents.length === running.agents.length && out.commands.length === running.commands.length ? running : out
}
