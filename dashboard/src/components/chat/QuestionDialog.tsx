import { useState } from 'react'
import { isRecord } from '../../lib/json/guards'
import Button from '../ui/Button'

interface QuestionOption {
  label: string
  description?: string
  // Long-form option content (Claude AskUserQuestion options[].preview — e.g.
  // full text versions to choose between). Rendered verbatim; the TUI shows it
  // in a side pane on focus, the chat card inlines it under the option.
  preview?: string
}

interface QuestionItem {
  question: string
  header?: string
  options?: QuestionOption[]
  multiSelect?: boolean
  // Codex request_user_input carries a verbatim question `id` (the answer map
  // MUST be keyed by it) plus `isOther` (custom text allowed) / `isSecret` (mask).
  id?: string
  isOther?: boolean
  isSecret?: boolean
}

interface Props {
  toolInput: unknown
  answered?: boolean
  onAnswer: (response: string) => void
  // Codex path: a request-id-correlated question whose held turn resumes only
  // when we answer the structured map. When set, the dialog builds
  // {<id>: {answers: [...]}} and calls onAnswerStructured instead of onAnswer.
  requestId?: string
  onAnswerStructured?: (requestId: string, answers: Record<string, { answers: string[] }>) => void
  // A live interactive terminal owns the dialog: the answer is typed in the
  // TUI's own picker (a card answer would only be held until the parked turn
  // ends). Render WHAT is asked, nothing to click.
  readOnly?: boolean
}

function parseQuestions(toolInput: unknown): QuestionItem[] {
  if (!toolInput) return []
  if (typeof toolInput === 'string') return [{ question: toolInput }]
  const input = isRecord(toolInput) ? toolInput : {}
  if (input.questions && Array.isArray(input.questions)) return input.questions
  if (input.question) {
    return [{
      question: String(input.question),
      header: typeof input.header === 'string' ? input.header : undefined,
      options: Array.isArray(input.options) ? input.options : undefined,
      multiSelect: input.multiSelect === true,
    }]
  }
  if (input.text) return [{ question: String(input.text) }]
  return [{ question: JSON.stringify(toolInput, null, 2) }]
}

export default function QuestionDialog({ toolInput, answered, onAnswer, requestId, onAnswerStructured, readOnly }: Props) {
  const questions = parseQuestions(toolInput)
  const [activeTab, setActiveTab] = useState(0)
  const [selections, setSelections] = useState<Record<number, Set<number>>>({})
  const [customTexts, setCustomTexts] = useState<Record<number, string>>({})
  const isMulti = questions.length > 1

  if (answered) {
    return (
      <div className="my-2 p-3 rounded-card border border-status-success-border bg-status-success-bg text-sm text-status-success-fg">
        <div className="flex items-center gap-2">
          <span>&#10003;</span>
          <span>Questions answered</span>
        </div>
      </div>
    )
  }

  if (readOnly) {
    return (
      <div
        data-testid="question-readonly"
        className="my-2 rounded-card bg-surface-card shadow-elevation-1 overflow-hidden"
      >
        <div className="p-4 space-y-3">
          {questions.map((qi, idx) => (
            <div key={idx}>
              {qi.header && (
                <p className="text-xs font-semibold text-secondary uppercase tracking-wide mb-1">
                  {qi.header}
                </p>
              )}
              <p className="text-sm font-medium text-primary">{qi.question}</p>
              {Array.isArray(qi.options) && qi.options.length > 0 && (
                <ul className="mt-1.5 space-y-1">
                  {qi.options.map((opt, oi) => (
                    <li key={oi} className="text-sm text-secondary">
                      <span className="font-medium">{opt.label}</span>
                      {opt.description && (
                        <span className="text-xs text-tertiary"> · {opt.description}</span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ))}
          <p className="text-xs text-tertiary">
            Answer in the terminal. The agent is waiting there.
          </p>
        </div>
      </div>
    )
  }

  const toggleOption = (qIdx: number, optIdx: number, multiSelect: boolean) => {
    setSelections((prev) => {
      const current = prev[qIdx] || new Set<number>()
      const next = new Set(current)
      if (multiSelect) {
        if (next.has(optIdx)) next.delete(optIdx)
        else next.add(optIdx)
      } else {
        next.clear()
        next.add(optIdx)
      }
      return { ...prev, [qIdx]: next }
    })
  }

  const hasAnswerFor = (qIdx: number) => {
    const selected = selections[qIdx]
    const custom = customTexts[qIdx]?.trim()
    return (selected && selected.size > 0) || !!custom
  }

  const handleSubmit = () => {
    // Codex: answer the held request with a structured map keyed by the VERBATIM
    // question id — {<id>: {answers: [selected labels + custom text]}}. Codex
    // accepts multiple labels and arbitrary free-text strings (probe-verified).
    if (requestId && onAnswerStructured) {
      const answers: Record<string, { answers: string[] }> = {}
      questions.forEach((q, qIdx) => {
        const selected = selections[qIdx] || new Set<number>()
        const custom = customTexts[qIdx]?.trim()
        const vals = Array.from(selected).map((i) => q.options![i]?.label || `Option ${i + 1}`)
        if (custom) vals.push(custom)
        const key = q.id ?? String(qIdx)
        answers[key] = { answers: vals }
      })
      onAnswerStructured(requestId, answers)
      return
    }
    // Claude: flatten to a single string re-injected as a fresh chat turn.
    const parts: string[] = []
    questions.forEach((q, qIdx) => {
      const selected = selections[qIdx] || new Set<number>()
      const custom = customTexts[qIdx]?.trim()
      if (isMulti && q.header) parts.push(`**${q.header}**:`)
      if (q.options && selected.size > 0) {
        const labels = Array.from(selected).map((i) => q.options![i]?.label || `Option ${i + 1}`)
        parts.push(labels.join(', '))
      }
      if (custom) parts.push(custom)
    })
    onAnswer(parts.join('\n') || 'No selection')
  }

  const allAnswered = questions.every((_, i) => hasAnswerFor(i))
  const q = questions[activeTab]
  const isLast = activeTab === questions.length - 1

  return (
    <div className="my-2 rounded-card bg-surface-card shadow-elevation-1 overflow-hidden">
      {/* Tab bar — only for multiple questions */}
      {isMulti && (
        <div className="flex border-b border-subtle">
          {questions.map((qi, idx) => (
            <button
              key={idx}
              onClick={() => setActiveTab(idx)}
              className={`flex-1 px-3 py-2 text-xs font-medium transition-colors relative ${
                idx === activeTab
                  ? 'text-link border-b-2 border-action'
                  : 'text-tertiary hover:text-secondary hover:bg-surface-hover'
              }`}
            >
              <span className="flex items-center justify-center gap-1.5">
                {hasAnswerFor(idx) && (
                  <span className="text-status-success-fg text-3xs">&#10003;</span>
                )}
                {qi.header || `Q${idx + 1}`}
              </span>
            </button>
          ))}
        </div>
      )}

      {/* Active question content */}
      <div className="p-4">
        {q.header && !isMulti && (
          <p className="text-xs font-semibold text-secondary uppercase tracking-wide mb-1">
            {q.header}
          </p>
        )}
        <p className="text-sm font-medium text-primary mb-3">{q.question}</p>
        {q.multiSelect && (
          <p className="text-xs text-tertiary mb-2">Select multiple</p>
        )}

        {Array.isArray(q.options) && q.options.length > 0 && (
          <div className="space-y-1.5">
            {q.options.map((opt, optIdx) => {
              const isSelected = selections[activeTab]?.has(optIdx) || false
              return (
                <button
                  key={optIdx}
                  onClick={() => toggleOption(activeTab, optIdx, q.multiSelect || false)}
                  className={`w-full text-left px-3 py-2 rounded-control border text-sm transition-colors ${
                    isSelected
                      ? 'border-action-subtle-hover bg-action-subtle text-primary'
                      : 'border-subtle bg-surface-card text-secondary hover:bg-surface-hover'
                  }`}
                >
                  <div className="flex items-start gap-2">
                    <span className="mt-0.5 shrink-0">
                      {q.multiSelect ? (
                        <span
                          className={`inline-flex items-center justify-center w-4 h-4 border-2 rounded-sm text-3xs ${
                            isSelected
                              ? 'border-action bg-action text-action-fg'
                              : 'border-strong'
                          }`}
                        >
                          {isSelected ? '\u2713' : ''}
                        </span>
                      ) : (
                        <span
                          className={`inline-flex items-center justify-center w-4 h-4 border-2 rounded-full ${
                            isSelected ? 'border-action' : 'border-strong'
                          }`}
                        >
                          {isSelected && (
                            <span className="block w-2 h-2 rounded-full bg-action" />
                          )}
                        </span>
                      )}
                    </span>
                    <div className="min-w-0 flex-1">
                      <span className="font-medium">{opt.label}</span>
                      {opt.description && (
                        <p className="text-xs text-tertiary mt-0.5">{opt.description}</p>
                      )}
                      {opt.preview && (
                        <pre className="mt-1.5 px-2.5 py-2 rounded-sm bg-surface-sunken text-xs text-secondary whitespace-pre-wrap break-words font-mono">
                          {opt.preview}
                        </pre>
                      )}
                    </div>
                  </div>
                </button>
              )
            })}
          </div>
        )}

        <input
          type={q.isSecret ? 'password' : 'text'}
          value={customTexts[activeTab] || ''}
          onChange={(e) =>
            setCustomTexts((prev) => ({ ...prev, [activeTab]: e.target.value }))
          }
          placeholder={q.options ? 'Or type a custom response...' : 'Type your answer...'}
          className="mt-2 w-full px-3 py-1.5 rounded-control border border-strong text-sm
                     focus:outline-hidden focus:ring-2 focus:ring-focus-ring focus:border-transparent
                     bg-surface-card text-primary placeholder:text-tertiary"
        />

        {/* Navigation + submit */}
        <div className="flex items-center justify-between mt-3">
          {isMulti && (
            <div className="text-xs text-tertiary">
              {activeTab + 1} of {questions.length}
            </div>
          )}
          <div className={`flex gap-2 ${isMulti ? '' : 'ml-auto'}`}>
            {isMulti && activeTab > 0 && (
              <Button
                size="sm"
                onClick={() => setActiveTab(activeTab - 1)}
              >
                Back
              </Button>
            )}
            {isMulti && !isLast ? (
              <Button
                size="sm" variant="primary"
                onClick={() => setActiveTab(activeTab + 1)}
              >
                Next
              </Button>
            ) : (
              <Button
                size="sm" variant="primary"
                onClick={handleSubmit}
                disabled={!allAnswered && isMulti}
              >
                Submit
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
