import type { ReactNode } from 'react'

import { CARD } from './classes'

/**
 * The one frame of every card in the chat (the settings card, the action
 * card, the list card): the shell, the title row with a small line under
 * it, the body, a state line in the controls' place and the footer row.
 * It holds no state and reads nothing: each card fills the slots.
 */
interface Props {
  title: ReactNode
  /** A small tertiary line under the title: the settings card's page line,
   *  a list card's count. */
  sub?: ReactNode
  /** One sentence that stands where the controls would. */
  line?: string
  /** The body: the words, a note, the controls, the rows. */
  children?: ReactNode
  /** The row of buttons or links. */
  footer?: ReactNode
  testId: string
}

export default function ChatCard({ title, sub, line, children, footer, testId }: Props) {
  return (
    <div className={`my-2 w-full max-w-md p-3 ${CARD}`} data-testid={testId}>
      <div className="min-w-0">
        <p className="text-sm font-medium text-primary">{title}</p>
        {sub && <p className="text-xs text-tertiary" data-testid={`${testId}-sub`}>{sub}</p>}
      </div>
      {children}
      {line && <p className="mt-1 text-sm text-secondary" data-testid={`${testId}-line`}>{line}</p>}
      {footer && <div className="mt-3 flex flex-wrap items-center gap-2">{footer}</div>}
    </div>
  )
}
