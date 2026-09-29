import { describe, expect, it } from 'vitest'
import { CHAT_PHASE, LANE_STATUS, isLiveChatPhase, laneStatusOf } from '@/lib/status/chat'
import { RUN_STATUS, isLiveRunStatus } from '@/lib/status/run'
import { MACHINE_STATE, isReachableMachine } from '@/lib/status/machine'

// The mirrors' predicates (core-seams phase 8). The sets themselves are
// bound to the proxy by tests/storage/test_status_vocabularies.py.

describe('laneStatusOf', () => {
  const phases = [undefined, ...Object.values(CHAT_PHASE)] as const
  const polled = Object.values(LANE_STATUS)

  it('a streaming slice generates whatever the poll said', () => {
    for (const p of polled) expect(laneStatusOf(CHAT_PHASE.STREAMING, p)).toBe(LANE_STATUS.GENERATING)
  })

  it('a ready or failed slice retires a stale generating and passes the rest through', () => {
    for (const phase of [CHAT_PHASE.READY, CHAT_PHASE.FAILED]) {
      expect(laneStatusOf(phase, LANE_STATUS.GENERATING)).toBe(LANE_STATUS.IDLE)
      expect(laneStatusOf(phase, LANE_STATUS.AWAITING_USER)).toBe(LANE_STATUS.AWAITING_USER)
      expect(laneStatusOf(phase, LANE_STATUS.IDLE)).toBe(LANE_STATUS.IDLE)
    }
  })

  it('every other phase passes the poll through', () => {
    for (const phase of phases) {
      if (phase === CHAT_PHASE.STREAMING || phase === CHAT_PHASE.READY || phase === CHAT_PHASE.FAILED) continue
      for (const p of polled) expect(laneStatusOf(phase, p)).toBe(p)
    }
  })
})

describe('the liveness predicates', () => {
  it('a chat is live while warming or streaming', () => {
    expect(isLiveChatPhase(CHAT_PHASE.WARMING)).toBe(true)
    expect(isLiveChatPhase(CHAT_PHASE.STREAMING)).toBe(true)
    for (const p of [CHAT_PHASE.IDLE, CHAT_PHASE.READY, CHAT_PHASE.FINISHED, CHAT_PHASE.FAILED, undefined, null]) {
      expect(isLiveChatPhase(p)).toBe(false)
    }
  })

  it('a run is live while pending or running', () => {
    expect(isLiveRunStatus(RUN_STATUS.PENDING)).toBe(true)
    expect(isLiveRunStatus(RUN_STATUS.RUNNING)).toBe(true)
    for (const s of [RUN_STATUS.COMPLETED, RUN_STATUS.FAILED, RUN_STATUS.CANCELLED, RUN_STATUS.LIMIT_EXCEEDED, 'timeout', undefined, null]) {
      expect(isLiveRunStatus(s)).toBe(false)
    }
  })

  it('a machine is reachable while online or stale', () => {
    expect(isReachableMachine(MACHINE_STATE.ONLINE)).toBe(true)
    expect(isReachableMachine(MACHINE_STATE.STALE)).toBe(true)
    for (const s of [MACHINE_STATE.PAUSED, MACHINE_STATE.DISCONNECTED, MACHINE_STATE.NEVER_CONNECTED, 'offline', null]) {
      expect(isReachableMachine(s)).toBe(false)
    }
  })
})
