import { describe, it, expect } from 'vitest'
import {
  SOURCE_TYPE, TASK_CHAT_ID_PREFIX, chatKind, isExternalDriven, isTaskChatId, runIdOfChat, taskChatId,
} from '@/lib/session/kind'
import { isSharedChatOwner } from '@/lib/visibility'

// The dashboard's read of the session kinds — the mirror of the proxy's
// core/session/session_kind.py (the lock-step test on the proxy side pins
// the spellings; this pins the questions).
describe('lib/session/kind', () => {
  it('mints and reads the task chat id shape', () => {
    expect(TASK_CHAT_ID_PREFIX).toBe('task-')
    expect(taskChatId('run-1')).toBe('task-run-1')
    expect(isTaskChatId('task-run-1')).toBe(true)
    expect(isTaskChatId('task-anything')).toBe(true) // the shape, not the run
    expect(isTaskChatId('meeting-x')).toBe(false)
    expect(isTaskChatId('5a1a6e0e-0000-4000-8000-000000000000')).toBe(false)
    expect(isTaskChatId('')).toBe(false)
    expect(isTaskChatId(null)).toBe(false)
    expect(isTaskChatId(undefined)).toBe(false)
    expect(runIdOfChat('task-run-1')).toBe('run-1')
    expect(runIdOfChat('u1')).toBe('')
    expect(runIdOfChat(null)).toBe('')
  })

  it('resolves a row: the column first, the id prefix for a pre-write task row', () => {
    expect(chatKind({ id: 'u1', source_type: 'task' })).toBe(SOURCE_TYPE.TASK)
    expect(chatKind({ id: 'u1', source_type: 'phone' })).toBe(SOURCE_TYPE.PHONE)
    expect(chatKind({ id: 'u1', source_type: 'chat' })).toBe(SOURCE_TYPE.CHAT)
    expect(chatKind({ id: 'task-run-1', source_type: 'chat' })).toBe(SOURCE_TYPE.TASK)
    expect(chatKind({ id: 'task-run-1', source_type: '' })).toBe(SOURCE_TYPE.TASK)
    expect(chatKind({ id: 'task-run-1' })).toBe(SOURCE_TYPE.TASK)
    expect(chatKind({ id: 'u1', source_type: '' })).toBe(SOURCE_TYPE.CHAT)
    expect(chatKind({ id: 'u1', source_type: 'meeting' })).toBe(SOURCE_TYPE.CHAT) // no row kind spells it
    expect(chatKind({})).toBe(SOURCE_TYPE.CHAT)
    expect(chatKind(undefined)).toBe(SOURCE_TYPE.CHAT)
  })

  it('knows the externally driven kinds and the shared owner', () => {
    expect(isExternalDriven('phone')).toBe(true)
    expect(isExternalDriven('task')).toBe(false)
    expect(isExternalDriven('chat')).toBe(false)
    expect(isExternalDriven(undefined)).toBe(false)
    expect(isSharedChatOwner('agent::alpha')).toBe(true)
    expect(isSharedChatOwner('task::alpha')).toBe(false)
    expect(isSharedChatOwner('')).toBe(false)
    expect(isSharedChatOwner(undefined)).toBe(false)
  })
})
