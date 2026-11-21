// The one rule for "does this frame belong to the chat on screen".
import { describe, it, expect } from 'vitest'
import { WIRE } from '../../api/wireEvents'
import { WORDS_FRAMES, frameIsForShownChat, isForShownChat } from './shownChat'

describe('isForShownChat', () => {
  it('takes a frame of the chat on screen and drops one of another chat', () => {
    expect(isForShownChat('chat-a', 'chat-a')).toBe(true)
    expect(isForShownChat('chat-b', 'chat-a')).toBe(false)
  })

  it('gives the new-chat view no chat but its own', () => {
    expect(isForShownChat('chat-a', null)).toBe(false)
    expect(isForShownChat(undefined, null)).toBe(true)
  })

  it('lets a frame that names no chat through only when it carries no words', () => {
    expect(isForShownChat(undefined, 'chat-a')).toBe(true)
    expect(isForShownChat('', 'chat-a')).toBe(true)
    expect(isForShownChat(undefined, 'chat-a', true)).toBe(false)
    expect(isForShownChat(undefined, null, true)).toBe(false)
  })

  it('takes every frame for a consumer with no chat of its own', () => {
    expect(isForShownChat('chat-a', undefined)).toBe(true)
    expect(isForShownChat(undefined, undefined, true)).toBe(true)
  })
})

describe('frameIsForShownChat', () => {
  it('holds a chat result, its text and its live state to their chat', () => {
    for (const type of [WIRE.DELEGATE_RESULT, WIRE.TEXT, WIRE.LIVE_STATE]) {
      expect(WORDS_FRAMES.has(type), type).toBe(true)
      expect(frameIsForShownChat(type, undefined, 'chat-a'), type).toBe(false)
      expect(frameIsForShownChat(type, 'chat-b', 'chat-a'), type).toBe(false)
      expect(frameIsForShownChat(type, 'chat-a', 'chat-a'), type).toBe(true)
    }
  })

  it('lets a refusal, a limit and a pick that name no chat through', () => {
    for (const type of [WIRE.ERROR, WIRE.LIMIT_WARNING, WIRE.MODE_CHANGED, WIRE.MODEL_CHANGED, WIRE.DONE]) {
      expect(WORDS_FRAMES.has(type), type).toBe(false)
      expect(frameIsForShownChat(type, undefined, 'chat-a'), type).toBe(true)
    }
  })

  it('never drops a frame that belongs to no chat', () => {
    expect(frameIsForShownChat(WIRE.CHAT_STATUS, 'chat-b', 'chat-a')).toBe(true)
    expect(frameIsForShownChat(WIRE.NOTIFICATION, undefined, 'chat-a')).toBe(true)
    expect(frameIsForShownChat('not-a-frame', 'chat-b', 'chat-a')).toBe(true)
  })
})
