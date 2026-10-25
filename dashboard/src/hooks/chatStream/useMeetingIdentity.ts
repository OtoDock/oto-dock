// The meeting identity of the chat stream (split from useChatStream): the
// meeting indicator's state, the speaker identity a meeting turn gives the
// next assistant message, the meeting half of a history frame's restore,
// of a live_state snapshot and of a system frame, and the reset an abort
// runs. The speaker ref itself is core's: useChatMessages mints messages
// from it before this hook exists.
import { useState } from 'react'
import type { DisplayMessage } from '../../components/chat/types'
import { SYSTEM_SUBTYPE, type LiveStateFrame, type MeetingRestore, type SystemFrame } from '../../api/wireEvents'
import type { MeetingParticipant, StreamCore } from './core'

export function useMeetingIdentity(core: StreamCore) {
  const { meetingSpeakerRef, setMessages, currentMsgRef, ensureAssistantMsg, appendBlock } = core

  const [meetingActive, setMeetingActive] = useState(false)
  const [meetingParticipants, setMeetingParticipants] = useState<MeetingParticipant[]>([])
  const [meetingSpeaker, setMeetingSpeaker] = useState<string | null>(null)
  const [meetingRound, setMeetingRound] = useState(0)
  const [meetingMaxRounds, setMeetingMaxRounds] = useState(10)
  const [meetingLeftParticipants, setMeetingLeftParticipants] = useState<Set<string>>(new Set())

  // The meeting half of a history frame's server-computed `restore` (the
  // full history and a delta apply it alike): an active meeting sets the
  // indicator's state, anything else ends it.
  const applyMeetingRestore = (mt: MeetingRestore | null | undefined) => {
    if (mt && mt.active) {
      setMeetingActive(true)
      // Normalize: older proxies sent the DB's slug STRINGS here while the
      // live meeting_started event sends {slug, display_name, color}
      // objects — the string form crashed MeetingIndicator (undefined
      // .charAt) and blanked the app pre-ErrorBoundary.
      const parts = Array.isArray(mt.participants) ? mt.participants : []
      setMeetingParticipants(parts.map((p) =>
        typeof p === 'string'
          ? { slug: p, display_name: p, color: '' }
          : { slug: p?.slug || '', display_name: p?.display_name || p?.slug || '', color: p?.color || '' }
      ))
      setMeetingMaxRounds(mt.max_turns || 30)
    } else {
      setMeetingActive(false)
    }
  }

  /** The meeting subtypes of a system frame; true when the frame was one. */
  const onMeetingSystem = (data: SystemFrame): boolean => {
    const subtype = data.subtype
    // Meeting events: update state, selectively create blocks
    if (subtype === SYSTEM_SUBTYPE.MEETING_STARTED) {
      setMeetingActive(true)
      setMeetingParticipants(Array.isArray(data.participants) ? data.participants : [])
      setMeetingMaxRounds(data.max_turns || data.max_rounds || 30)
      setMeetingRound(0)
      setMeetingLeftParticipants(new Set())
      // Append banner to the last assistant message — but only if it
      // doesn't already have one (onLiveState may have added it).
      setMessages((prev) => {
        for (let i = prev.length - 1; i >= 0; i--) {
          if (prev[i].role === 'assistant') {
            const alreadyHas = prev[i].blocks.some(
              (b) => b.type === 'system' && b.subtype === SYSTEM_SUBTYPE.MEETING_STARTED
            )
            if (alreadyHas) return prev
            const updated = { ...prev[i], blocks: [...prev[i].blocks, { type: 'system' as const, subtype: SYSTEM_SUBTYPE.MEETING_STARTED }] }
            return [...prev.slice(0, i), updated, ...prev.slice(i + 1)]
          }
        }
        return prev
      })
      return true
    }
    if (subtype === SYSTEM_SUBTYPE.MEETING_TURN_START) {
      const agentSlug = data.agent || ''
      setMeetingSpeaker(agentSlug || null)
      setMeetingRound(data.round || 0)
      meetingSpeakerRef.current = {
        slug: agentSlug,
        displayName: data.agent_display_name || agentSlug,
        color: data.agent_color || '',
      }
      // Force new assistant message — unless the current message is
      // already for this agent (live_state reconstruction created it,
      // and this is a duplicate event from the queue).
      if (currentMsgRef.current?.agentSlug === agentSlug) {
        // Keep the existing message (it has blocks from live_state like thinking)
      } else {
        currentMsgRef.current = null
      }
      return true  // no block — agent header on the message is enough
    }
    if (subtype === SYSTEM_SUBTYPE.MEETING_TURN_END) {
      setMeetingSpeaker(null)
      meetingSpeakerRef.current = null
      return true  // no block
    }
    if (subtype === SYSTEM_SUBTYPE.MEETING_AGENT_LEFT) {
      if (data.agent) setMeetingLeftParticipants(prev => new Set([...prev, data.agent!]))
      ensureAssistantMsg()
      appendBlock({ type: 'system', subtype, agentName: data.agent_display_name || data.agent, agentColor: data.agent_color })
      return true
    }
    if (subtype === SYSTEM_SUBTYPE.MEETING_CONCLUDED) {
      setMeetingActive(false)
      setMeetingSpeaker(null)
      setMeetingParticipants([])
      setMeetingLeftParticipants(new Set())
      meetingSpeakerRef.current = null
      // Show conclusion banner
      ensureAssistantMsg()
      appendBlock({ type: 'system', subtype: SYSTEM_SUBTYPE.MEETING_CONCLUDED })
      return true
    }
    if (subtype === SYSTEM_SUBTYPE.MEETING_AGENT_FAILED) {
      ensureAssistantMsg()
      appendBlock({ type: 'system', subtype, agentName: data.agent_display_name || data.agent, agentColor: data.agent_color })
      return true
    }
    if (subtype === SYSTEM_SUBTYPE.MEETING_FAILED) {
      // The meeting never started (admission denial, spawn failure…) —
      // clear the pill state and show the reason where the "meeting is
      // set up" ack was left hanging.
      setMeetingActive(false)
      setMeetingSpeaker(null)
      setMeetingParticipants([])
      setMeetingLeftParticipants(new Set())
      meetingSpeakerRef.current = null
      ensureAssistantMsg()
      appendBlock({ type: 'system', subtype: SYSTEM_SUBTYPE.MEETING_FAILED, message: data.message })
      return true
    }
    return false
  }

  /** The speaker a live_state rebuild leaves on the last message, if any. */
  const noteLiveSpeaker = (lastMsg: DisplayMessage | undefined) => {
    if (lastMsg?.badge === 'meeting' && lastMsg.agentSlug) {
      meetingSpeakerRef.current = {
        slug: lastMsg.agentSlug,
        displayName: lastMsg.agentDisplayName || lastMsg.agentSlug,
        color: lastMsg.agentColor || '',
      }
    }
  }

  /** The meeting state a live_state snapshot carries. */
  const applyLiveStateMeeting = (data: LiveStateFrame) => {
    if (data.meeting_agent || (data.meeting_participants && data.meeting_participants.length > 0)) {
      setMeetingActive(true)
      if (data.meeting_agent) setMeetingSpeaker(data.meeting_agent)
      if (Array.isArray(data.meeting_participants)) setMeetingParticipants(data.meeting_participants)
    }
  }

  /** A stopped turn ends the meeting it was in. */
  const resetMeeting = () => {
    if (meetingActive) {
      setMeetingActive(false)
      setMeetingSpeaker(null)
      setMeetingParticipants([])
      meetingSpeakerRef.current = null
    }
  }

  return {
    meetingActive, setMeetingActive,
    meetingParticipants, setMeetingParticipants,
    meetingSpeaker, setMeetingSpeaker,
    meetingRound, setMeetingRound,
    meetingMaxRounds,
    meetingLeftParticipants,
    applyMeetingRestore, onMeetingSystem, noteLiveSpeaker, applyLiveStateMeeting, resetMeeting,
  }
}
