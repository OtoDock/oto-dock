import { useCallback, useState } from 'react'

const NONE: ReadonlySet<string> = new Set()

/**
 * The open delegate cards of the chat on screen (`DelegateCard`): page
 * state, never stored, so a reload or another chat starts with every card
 * collapsed. The set tracks the chat it belongs to:
 * the render that sees another chat starts an empty one (the render-adjust
 * rule, no effect). Held by the page, above `ChatMessages`, so the rich
 * view laid over a live terminal (which remounts the list) keeps the cards
 * as they were.
 */
export function useDelegateCards(chatId: string | null | undefined) {
  const owner = chatId || ''
  const [state, setState] = useState<{ chatId: string; open: ReadonlySet<string> }>(
    () => ({ chatId: owner, open: NONE }),
  )
  if (state.chatId !== owner) setState({ chatId: owner, open: NONE })
  const openCards = state.chatId === owner ? state.open : NONE
  const toggleCard = useCallback((key: string) => {
    setState((s) => {
      const next = new Set(s.chatId === owner ? s.open : NONE)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return { chatId: owner, open: next }
    })
  }, [owner])
  return { openCards, toggleCard }
}
