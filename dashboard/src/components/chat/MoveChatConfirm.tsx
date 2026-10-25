import AlertDialog from '../ui/AlertDialog'
import { MOVE_MEMORY, placePhrase, type MoveMemory } from '../../api/remoteMachines'

interface Props {
  /** The place the chat moves TO: its stored target and its label. */
  target: string
  label: string
  /** What goes with the chat: its session files, or a summary of the
   *  conversation when the place it leaves is offline or a place's
   *  satellite cannot move the files (moveMemoryOf). */
  memory: MoveMemory
  /** The place the chat leaves, and whether it is offline (leavesOffline). */
  fromTarget: string
  fromLabel: string
  fromOffline: boolean
  onConfirm: () => void
  onCancel: () => void
}

/** Confirm dialog for a move of the chat: opened by the "Runs on" chip's
 * menu, the ChatTargetBanner's menu and the sidebar kebab's move row,
 * rendered only while the move is still possible (it goes when its
 * condition clears). It names where the chat goes, and says when the chat
 * continues from a summary: because the place it leaves is offline, or
 * because a place's satellite is too old to move the files. */
export default function MoveChatConfirm({
  target, label, memory, fromTarget, fromLabel, fromOffline, onConfirm, onCancel,
}: Props) {
  const to = placePhrase(target, label)
  let words = `The chat continues on ${to}.`
  if (memory === MOVE_MEMORY.SUMMARY) {
    words = fromOffline
      ? `${placePhrase(fromTarget, fromLabel, true)} is offline, so the chat continues on ${to} from a summary of the conversation.`
      : `The chat continues on ${to} from a summary of the conversation.`
  }
  return (
    <AlertDialog title={`Move this chat to ${to}?`} confirmLabel="Move chat" onConfirm={onConfirm} onCancel={onCancel}>
      {words}
    </AlertDialog>
  )
}
