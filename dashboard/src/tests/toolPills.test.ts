import { describe, it, expect } from 'vitest'
import { eventToBlock, pairBgCommandBlocks } from '../lib/messageBlocks'
import { getToolDetail } from '../components/chat/ToolActivity'
import type { MessageBlock } from '../components/chat/types'

describe('getToolDetail — collapsed pill title', () => {
  it('Bash prefers the description over the summary (older rows carry the command as summary)', () => {
    expect(
      getToolDetail('Bash', 'systemctl list-units --all | grep sat', {
        command: 'systemctl list-units --all | grep sat',
        description: 'Find running satellite service',
      }),
    ).toBe('Find running satellite service')
  })

  it('Bash falls back to summary, then command, when no description', () => {
    expect(getToolDetail('Bash', 'ls -la', { command: 'ls -la' })).toBe('ls -la')
    expect(getToolDetail('Bash', undefined, { command: 'ls -la' })).toBe('ls -la')
  })

  it('Bash caps a pathological description at 300 chars (CSS clips the collapsed line)', () => {
    const long = 'x'.repeat(350)
    expect(getToolDetail('Bash', undefined, { command: 'ls', description: long })).toBe(
      'x'.repeat(300) + '...',
    )
  })

  it('Workflow shows the saved name or the meta name from an inline script', () => {
    expect(getToolDetail('Workflow', undefined, { name: 'find-flaky-tests' })).toBe('find-flaky-tests')
    expect(
      getToolDetail('Workflow', undefined, {
        script: "export const meta = { name: 'review-changes', description: 'x' }",
      }),
    ).toBe('review-changes')
    expect(getToolDetail('Workflow', undefined, { script: 42 })).toBe('')
  })

  it('non-Bash tools keep summary precedence', () => {
    expect(getToolDetail('Read', 'notes.md', { file_path: '/workspace/notes.md' })).toBe('notes.md')
  })
})

describe('eventToBlock — expandable pill data from persisted events', () => {
  it('task_spawn carries the full Agent tool input and any attached report', () => {
    const b = eventToBlock({
      type: 'task_spawn',
      description: 'map the code',
      subagent_type: 'Explore',
      run_in_background: false,
      tool_use_id: 'tu1',
      tool_input: { description: 'map the code', prompt: 'Explore the repo…' },
      tool_result: 'Here is the map…',
    })
    expect(b).toMatchObject({
      type: 'subagent',
      description: 'map the code',
      toolInput: { description: 'map the code', prompt: 'Explore the repo…' },
      toolResult: 'Here is the map…',
    })
  })

  it('delegate_spawn carries the full prompt (preview stays for the collapsed line)', () => {
    const b = eventToBlock({
      type: 'delegate_spawn',
      task_name: 'triage',
      agent: 'support-bot',
      prompt_preview: 'Please triage…',
      prompt: 'Please triage the following long report…',
      task_id: 't1',
    })
    expect(b).toMatchObject({
      type: 'delegate',
      taskName: 'triage',
      prompt: 'Please triage the following long report…',
      promptPreview: 'Please triage…',
    })
  })
})

describe('pairBgCommandBlocks — one pill per background command', () => {
  const toolBlock = (toolId: string, extra: Partial<Extract<MessageBlock, { type: 'tool' }>> = {}): MessageBlock => ({
    type: 'tool', name: 'Bash', toolId, summary: '', status: 'done',
    toolInput: { command: 'sleep 99', run_in_background: true, description: 'wait' },
    toolResult: 'Command running in background with ID bash_1',
    ...extra,
  })

  it('hides the paired tool block and lends its data to the bgcommand pill', () => {
    const blocks: MessageBlock[] = [
      { type: 'text', content: 'running…' },
      toolBlock('tu9'),
      { type: 'bgcommand', command: 'sleep 99', description: 'wait', isActive: true, _toolId: 'tu9' },
    ]
    const { hiddenToolIdx, bgPairs } = pairBgCommandBlocks(blocks)
    expect(hiddenToolIdx).toEqual(new Set([1]))
    expect(bgPairs.get(2)).toEqual({
      toolInput: { command: 'sleep 99', run_in_background: true, description: 'wait' },
      toolResult: 'Command running in background with ID bash_1',
      resultSummary: undefined,
    })
  })

  it('leaves unpaired blocks alone (Codex null ids, split messages, old rows)', () => {
    const blocks: MessageBlock[] = [
      toolBlock('tu1'),
      { type: 'bgcommand', command: 'sleep 1', isActive: false, _toolId: null },
      { type: 'bgcommand', command: 'sleep 2', isActive: false, _toolId: 'absent' },
    ]
    const { hiddenToolIdx, bgPairs } = pairBgCommandBlocks(blocks)
    expect(hiddenToolIdx.size).toBe(0)
    expect(bgPairs.size).toBe(0)
  })

  it('pairs multiple concurrent background commands independently', () => {
    const blocks: MessageBlock[] = [
      toolBlock('a'),
      toolBlock('b', { toolResult: 'ID bash_2' }),
      { type: 'bgcommand', command: 'x', isActive: true, _toolId: 'b' },
      { type: 'bgcommand', command: 'y', isActive: true, _toolId: 'a' },
    ]
    const { hiddenToolIdx, bgPairs } = pairBgCommandBlocks(blocks)
    expect(hiddenToolIdx).toEqual(new Set([0, 1]))
    expect(bgPairs.get(2)?.toolResult).toBe('ID bash_2')
    expect(bgPairs.get(3)?.toolResult).toBe('Command running in background with ID bash_1')
  })
})
