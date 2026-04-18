import { liveBlockToMessageBlock } from '../../lib/messageBlocks'
import type { DisplayMessage, MessageBlock } from '../../components/chat/types'

/**
 * Pure reconstruction of the live-state message list from ordered live_blocks.
 * For meetings: split at meeting_turn_start to create separate messages per
 * agent. The trailing active-thinking block is appended to the current message.
 *
 * Extracted verbatim from onLiveState — no state writes; the caller owns
 * currentMsgRef / meetingSpeakerRef / setMessages and the meeting-banner merge.
 */
export function buildLiveStateMessages(
  liveBlocks: any[],
  thinking: { active?: boolean; text?: string; tokens?: number },
): DisplayMessage[] {
  const newMsgs: DisplayMessage[] = []
  let msg: DisplayMessage = {
    id: `live-${Date.now()}`,
    role: 'assistant',
    blocks: [],
    createdAt: new Date().toISOString(),
  }

  const pushBlock = (block: MessageBlock) => { msg.blocks.push(block) }

  for (const lb of liveBlocks) {
    // Meeting turn start: finalize current message, start new one with agent identity
    if (lb.type === 'system' && lb.subtype === 'meeting_turn_start') {
      if (msg.blocks.length > 0) newMsgs.push(msg)
      msg = {
        id: `live-turn-${lb.agent || Date.now()}`,
        role: 'assistant',
        blocks: [],
        createdAt: new Date().toISOString(),
        agentSlug: lb.agent || '',
        agentDisplayName: lb.agent_display_name || lb.agent || '',
        agentColor: lb.agent_color || '',
        badge: 'meeting',
      }
      continue
    }
    // Meeting events that don't need blocks: skip
    if (lb.type === 'system' && (lb.subtype === 'meeting_started' || lb.subtype === 'meeting_turn_end' || lb.subtype === 'meeting_concluded')) {
      continue
    }
    switch (lb.type) {
      case 'text':
        pushBlock({ type: 'text', content: lb.content || '' })
        break
      case 'tool':
        pushBlock({
          type: 'tool',
          name: lb.name || '',
          toolId: lb.tool_id || lb.name || '',
          summary: lb.summary || '',
          status: lb.active !== false ? 'running' : 'done',
          toolInput: lb.tool_input,
          toolResult: lb.tool_result,
          resultSummary: lb.result_summary,
        })
        break
      case 'agent':
        pushBlock({
          type: 'subagent',
          description: lb.description || 'Subagent',
          subagentType: lb.subagent_type || 'general-purpose',
          isActive: lb.active !== false,
          _toolId: lb.tool_use_id || null,  // id-keyed completion survives reconnect
          _background: lb.background || false,
          toolInput: lb.tool_input,    // expandable pill parity with live stream
          toolResult: lb.tool_result,
        } as MessageBlock)
        break
      case 'delegate':
        pushBlock({
          type: 'delegate',
          taskName: lb.task_name || '',
          agent: lb.agent || '',
          promptPreview: '',
          status: lb.status || (lb.active !== false ? 'running' : 'completed'),
          _taskId: lb.task_id || undefined,
          prompt: lb.prompt || '',     // expandable pill parity with live stream
        })
        break
      case 'command':
        pushBlock({
          type: 'bgcommand',
          command: lb.command || '',
          description: lb.description || '',
          isActive: lb.active !== false,  // active flag survives reconnect
          _toolId: lb.tool_use_id || null,
        } as MessageBlock)
        break
      default: {
        const block = liveBlockToMessageBlock(lb)
        if (block) pushBlock(block)
        break
      }
    }
  }
  // Add active thinking to the end of the current message (it's the most
  // recent content — unshift would incorrectly prepend it before earlier
  // blocks like completed thinking and tool calls).
  if (thinking.active || thinking.text) {
    msg.blocks.push({
      type: 'thinking',
      content: thinking.text || '',
      collapsed: true,
      tokens: thinking.tokens || 0,
    })
  }
  if (msg.blocks.length > 0) newMsgs.push(msg)
  return newMsgs
}
