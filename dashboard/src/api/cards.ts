/**
 * The cards routes (the proxy's services/cards): a card's state read with the viewer's own
 * sign-in, and a button pressed by whoever views the chat, acting as themselves. The frame and the row carry the words and
 * the buttons alone, so the state is the only place a click or an outcome
 * shows.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiJson } from '../lib/http/apiJson'
import { OUTCOME, type ButtonKind, type ClickStatus, type OutcomeState } from '../lib/kinds/card'
import { noteStoreReviewError } from '../lib/device/storeReview'

export interface CardButton {
  id: string
  label: string
  kind: ButtonKind
  /** A say button's words: the message the server sends as the person's own. */
  message?: string
}

export interface CardOption {
  id: string
  label: string
  default: boolean
}

export interface CardClick {
  button: string
  status: ClickStatus
  at: string
  detail: string
  /** Who pressed: their name, read for the viewer. */
  by?: string
  /** The viewer pressed it themselves. */
  mine?: boolean
}

export interface CardOutcome {
  status: OutcomeState
  text: string
  job_id?: string
  lines?: string[]
  members?: { template: string; display_name: string; slug: string; status: string; detail: string }[]
  department_id?: string
  agent_slug?: string
}

export interface CardState {
  card_id: string
  chat_id: string
  agent: string
  title: string
  text: string
  note: string
  buttons: CardButton[]
  options: CardOption[]
  created_at: string
  resolved: boolean
  clicks: CardClick[]
  outcome: CardOutcome | null
  /** The sentence that says why the card takes no click, else null. */
  expired: string | null
}

/** A say click answers `queued` (the server sent the message through the
 *  chat's queue), a do click its `outcome`; both the fresh state. */
export interface ActResult {
  kind: ButtonKind
  queued?: boolean
  outcome?: CardOutcome
  state: CardState
}

export const cardKey = (cardId: string) => ['card', cardId] as const

/** The card's state; polled while a running outcome has a job. */
export function useCardState(cardId: string, enabled = true) {
  return useQuery<CardState>({
    queryKey: cardKey(cardId),
    queryFn: () => apiJson<CardState>(`/v1/cards/${cardId}`, {}, 'The card did not load'),
    enabled,
    retry: false,
    refetchInterval: (q) => (q.state.data?.outcome?.status === OUTCOME.RUNNING ? 2000 : false),
  })
}

export function useActOnCard(cardId: string) {
  const qc = useQueryClient()
  return useMutation<ActResult, Error, { button: string; options?: Record<string, boolean> }>({
    mutationFn: (body) => apiJson<ActResult>(`/v1/cards/${cardId}/act`, { method: 'POST', json: body }),
    onSuccess: (result) => {
      if (result.outcome?.status === OUTCOME.FAILED) noteStoreReviewError()
      qc.setQueryData(cardKey(cardId), result.state)
      void qc.invalidateQueries({ queryKey: ['agents'] })
      void qc.invalidateQueries({ queryKey: ['departments'] })
    },
    // A refusal can mean the card was answered elsewhere (another tab, a
    // second device): the card reads its state again to show that answer.
    onError: () => {
      noteStoreReviewError()
      void qc.invalidateQueries({ queryKey: cardKey(cardId) })
    },
  })
}
