/**
 * useNativeSwitchBusy — tells the apps' install switcher that the chat holds
 * live work a switch would cut: a live meeting or a voice conversation. The
 * flag clears when both end and when the chat page unmounts; off the app the
 * bridge call is a no-op.
 */
import { useEffect } from 'react'
import { setNativeSwitchBusy } from '../../../lib/nativeBridge'

export function useNativeSwitchBusy(meetingActive: boolean, voiceActive: boolean): void {
  const busy = meetingActive || voiceActive
  useEffect(() => {
    setNativeSwitchBusy(busy)
    return () => setNativeSwitchBusy(false)
  }, [busy])
}
