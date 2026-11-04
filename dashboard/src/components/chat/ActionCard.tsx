import { useState } from 'react'

import { useActOnCard, useCardState, type CardClick, type CardOutcome, type CardState } from '../../api/cards'
import { ApiError } from '../../lib/http/apiJson'
import { BUTTON_KIND, CARD_WORDS, CLICK_STATUS, OUTCOME, SET_MEMBER_WORDS, type SetMemberState } from '../../lib/kinds/card'
import Button from '../ui/Button'
import ChatCard from '../ui/ChatCard'

/**
 * An action card in the chat (the proxy's services/cards): the server's words, the agent's
 * note, up to three buttons. The state is read with the viewer's own
 * sign-in, and any viewer of the chat may press: the card acts for whoever
 * presses it, never as the person whose turn asked for it. A card the
 * viewer may not read shows its words and the server's sentence with no
 * button, a card already answered shows who answered and its outcome, and
 * a card whose session moved on says so. A card takes one click: a `do`
 * button posts the click and shows what the action did (a running job keeps
 * polling); a `say` button posts the click and the server sends its
 * message as the presser's own, through the chat's queue, so the dashboard
 * sends nothing after it and the button's title shows the words to come.
 * The pressed button keeps its place with a check mark, the others go quiet.
 * A refused click reads the state again: the card may have been answered
 * in another tab.
 */

interface Props {
  cardId: string
  title: string
  text: string
  note?: string
  buttons: { id: string; label: string; kind: string; message?: string }[]
  options?: { id: string; label: string; default: boolean }[]
}

export default function ActionCard({ cardId, title, text, note, buttons, options }: Props) {
  const state = useCardState(cardId)
  const act = useActOnCard(cardId)
  const [picked, setPicked] = useState<Record<string, boolean>>({})
  const [refusal, setRefusal] = useState('')

  const s = state.data
  // A refused read carries the server's sentence (a chat the viewer may not open).
  const forbidden = state.error instanceof ApiError && (state.error.status === 403 || state.error.status === 404)
    && typeof state.error.detail === 'string' && state.error.detail.endsWith('.')
  const shown = s ?? null
  const words = { title: shown?.title ?? title, text: shown?.text ?? text, note: shown?.note ?? note ?? '' }
  const rows = shown?.buttons ?? buttons
  const opts = shown?.options ?? options ?? []
  const clicked = new Set((shown?.clicks ?? []).map((c) => c.button))
  const outcome = shown?.outcome ?? null
  // One click per card: a click of either kind resolves it.
  const answered = Boolean(shown?.resolved) || clicked.size > 0
  const dead = Boolean(forbidden) || Boolean(shown?.expired) || answered
  const busy = act.isPending

  let line: string | undefined
  if (forbidden) line = (state.error as ApiError).message
  else if (state.isLoading) line = CARD_WORDS.reading
  else if (state.isError) line = CARD_WORDS.notRead
  // An answered card keeps its pressed button and its answer: the expiry
  // line is for a card nobody answered.
  else if (shown?.expired && !answered) line = shown.expired

  const press = (id: string) => {
    setRefusal('')
    const chosen: Record<string, boolean> = {}
    for (const o of opts) chosen[o.id] = picked[o.id] ?? o.default
    act.mutate({ button: id, options: chosen }, { onError: (err) => setRefusal(err.message) })
  }

  const variantOf = (b: { id: string; kind: string }) => {
    if (clicked.has(b.id)) return 'primary'
    if (answered) return 'secondary'
    return b.kind === BUTTON_KIND.DO ? 'primary' : 'secondary'
  }

  // What the press produced sits under the buttons, on the footer's next
  // row; an expired card keeps its outcome under the line.
  const result = (
    <div className="basis-full">
      {refusal && <p className="mt-1 text-xs text-status-danger-fg" role="status">{refusal}</p>}
      {outcome && shown && <Pressed state={shown} />}
      {outcome && <Outcome outcome={outcome} />}
      {!outcome && shown && <Said state={shown} />}
    </div>
  )

  return (
    <ChatCard
      title={words.title}
      line={line}
      testId="action-card"
      footer={line ? (outcome && result) : (
        <>
          {rows.map((b) => {
            // The chosen button keeps full strength (aria-disabled, no
            // press); the others go quiet (disabled).
            const pressed = clicked.has(b.id)
            const button = (
              <Button
                key={b.id}
                variant={variantOf(b)}
                size="sm"
                disabled={!pressed && (busy || dead)}
                aria-disabled={pressed ? true : undefined}
                aria-pressed={pressed ? true : undefined}
                className={pressed ? 'cursor-default' : ''}
                title={b.message}
                onClick={() => { if (!pressed) press(b.id) }}
              >
                {clicked.has(b.id) && <CheckMark />}
                {b.label}
              </Button>
            )
            if (b.kind !== BUTTON_KIND.SAY || !b.message) return button
            // The words that go out as the person's own, visible before the
            // press on every device (a phone has no hover for the title).
            return (
              <span key={b.id} className="inline-flex max-w-[16rem] flex-col items-start gap-0.5">
                {button}
                <span className="text-2xs text-tertiary" data-testid="action-card-message">{b.message}</span>
              </span>
            )
          })}
          {busy && <span className="text-xs text-tertiary">{CARD_WORDS.working}</span>}
          {result}
        </>
      )}
    >
      {words.text && <p className="mt-1 text-sm text-secondary">{words.text}</p>}
      {words.note && <p className="mt-1 text-xs italic text-tertiary" data-testid="action-card-note">{words.note}</p>}
      {!line && !dead && opts.length > 0 && (
        <div className="mt-2 flex flex-col gap-1">
          {opts.map((o) => (
            <label key={o.id} className="flex items-center gap-2 text-xs text-secondary">
              <input
                type="checkbox"
                className="accent-action"
                checked={picked[o.id] ?? o.default}
                onChange={(e) => setPicked((p) => ({ ...p, [o.id]: e.target.checked }))}
              />
              {o.label}
            </label>
          ))}
        </div>
      )}
    </ChatCard>
  )
}

/** The mark of the pressed button, before its label. */
function CheckMark() {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" className="size-3.5 shrink-0" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" data-testid="action-card-check">
      <path d="M3 8.5l3 3 7-7" />
    </svg>
  )
}

/** Who pressed, as the viewer reads it: "You" for their own press. */
function whoOf(click: CardClick): string {
  return click.mine === false ? (click.by || 'Someone') : 'You'
}

const labelOf = (state: CardState, click: CardClick) =>
  state.buttons.find((b) => b.id === click.button)?.label ?? click.button

function Said({ state }: { state: CardState }) {
  const said = state.clicks.filter((c) => c.status === CLICK_STATUS.SAID)
  if (said.length === 0) return null
  return (
    <p className="text-xs text-tertiary" data-testid="action-card-said">
      {whoOf(said[0])} answered {said.map((c) => labelOf(state, c)).join(', ')}.
    </p>
  )
}

/** Another viewer's press of a `do` button, named above its outcome. */
function Pressed({ state }: { state: CardState }) {
  const click = state.clicks.find((c) => c.status !== CLICK_STATUS.SAID)
  if (!click || click.mine !== false) return null
  return (
    <p className="text-xs text-tertiary" data-testid="action-card-pressed">
      {click.by || 'Someone'} pressed {labelOf(state, click)}.
    </p>
  )
}

function Outcome({ outcome }: { outcome: CardOutcome }) {
  const tone = outcome.status === OUTCOME.FAILED ? 'text-status-danger-fg' : 'text-primary'
  return (
    <div className="mt-1 text-sm" data-testid="action-card-outcome">
      <p className={tone}>{outcome.text}</p>
      {outcome.members && outcome.members.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-xs text-secondary">
          {outcome.members.map((m) => (
            <li key={m.template}>
              <span className="text-primary">{m.display_name}</span>{' '}
              {SET_MEMBER_WORDS[m.status as SetMemberState] ?? m.status}
              {m.detail ? `: ${m.detail}` : ''}
            </li>
          ))}
        </ul>
      )}
      {outcome.lines && outcome.lines.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-xs text-secondary">
          {outcome.lines.map((l, i) => <li key={i}>{l}</li>)}
        </ul>
      )}
    </div>
  )
}
