import { useState } from 'react'

interface QuestionOption {
  label: string
  description?: string
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
  toolInput: any
  answered?: boolean
  onAnswer: (response: string) => void
  // Codex path: a request-id-correlated question whose held turn resumes only
  // when we answer the structured map. When set, the dialog builds
  // {<id>: {answers: [...]}} and calls onAnswerStructured instead of onAnswer.
  requestId?: string
  onAnswerStructured?: (requestId: string, answers: Record<string, { answers: string[] }>) => void
}

function parseQuestions(toolInput: any): QuestionItem[] {
  if (!toolInput) return []
  if (toolInput.questions && Array.isArray(toolInput.questions)) return toolInput.questions
  if (toolInput.question) {
    return [{
      question: toolInput.question,
      header: toolInput.header,
      options: toolInput.options,
      multiSelect: toolInput.multiSelect,
    }]
  }
  if (typeof toolInput === 'string') return [{ question: toolInput }]
  if (toolInput.text) return [{ question: toolInput.text }]
  return [{ question: JSON.stringify(toolInput, null, 2) }]
}

export default function QuestionDialog({ toolInput, answered, onAnswer, requestId, onAnswerStructured }: Props) {
  const questions = parseQuestions(toolInput)
  const [activeTab, setActiveTab] = useState(0)
  const [selections, setSelections] = useState<Record<number, Set<number>>>({})
  const [customTexts, setCustomTexts] = useState<Record<number, string>>({})
  const isMulti = questions.length > 1

  if (answered) {
    return (
      <div className="my-2 p-3 rounded-lg border border-green-200 dark:border-green-800 bg-green-50 dark:bg-green-900/20 text-sm text-green-800 dark:text-green-300">
        <div className="flex items-center gap-2">
          <span>&#10003;</span>
          <span>Questions answered</span>
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
    <div className="my-2 rounded-lg border border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-900/20 overflow-hidden">
      {/* Tab bar — only for multiple questions */}
      {isMulti && (
        <div className="flex border-b border-blue-200 dark:border-blue-800">
          {questions.map((qi, idx) => (
            <button
              key={idx}
              onClick={() => setActiveTab(idx)}
              className={`flex-1 px-3 py-2 text-xs font-medium transition-colors relative ${
                idx === activeTab
                  ? 'bg-white dark:bg-p-surface text-blue-700 dark:text-blue-400 border-b-2 border-blue-600'
                  : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 hover:bg-blue-100/50 dark:hover:bg-blue-900/30'
              }`}
            >
              <span className="flex items-center justify-center gap-1.5">
                {hasAnswerFor(idx) && (
                  <span className="text-green-500 text-[10px]">&#10003;</span>
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
          <p className="text-xs font-semibold text-blue-600 dark:text-blue-400 uppercase tracking-wide mb-1">
            {q.header}
          </p>
        )}
        <p className="text-sm font-medium text-gray-900 dark:text-gray-100 mb-3">{q.question}</p>
        {q.multiSelect && (
          <p className="text-xs text-gray-400 mb-2">Select multiple</p>
        )}

        {Array.isArray(q.options) && q.options.length > 0 && (
          <div className="space-y-1.5">
            {q.options.map((opt, optIdx) => {
              const isSelected = selections[activeTab]?.has(optIdx) || false
              return (
                <button
                  key={optIdx}
                  onClick={() => toggleOption(activeTab, optIdx, q.multiSelect || false)}
                  className={`w-full text-left px-3 py-2 rounded-md border text-sm transition-colors ${
                    isSelected
                      ? 'border-blue-500 bg-blue-100 dark:bg-blue-900/30 text-blue-900 dark:text-blue-200'
                      : 'border-gray-200 dark:border-gray-700 bg-white dark:bg-p-surface text-gray-700 dark:text-gray-300 hover:border-blue-300 hover:bg-blue-50 dark:hover:bg-blue-900/20'
                  }`}
                >
                  <div className="flex items-start gap-2">
                    <span className="mt-0.5 shrink-0">
                      {q.multiSelect ? (
                        <span
                          className={`inline-flex items-center justify-center w-4 h-4 border-2 rounded-sm text-[10px] ${
                            isSelected
                              ? 'border-blue-500 bg-blue-500 text-white'
                              : 'border-gray-300 dark:border-gray-600'
                          }`}
                        >
                          {isSelected ? '\u2713' : ''}
                        </span>
                      ) : (
                        <span
                          className={`inline-flex items-center justify-center w-4 h-4 border-2 rounded-full ${
                            isSelected ? 'border-blue-500' : 'border-gray-300 dark:border-gray-600'
                          }`}
                        >
                          {isSelected && (
                            <span className="block w-2 h-2 rounded-full bg-blue-500" />
                          )}
                        </span>
                      )}
                    </span>
                    <div>
                      <span className="font-medium">{opt.label}</span>
                      {opt.description && (
                        <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{opt.description}</p>
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
          className="mt-2 w-full px-3 py-1.5 rounded-md border border-gray-200 dark:border-gray-700 text-sm
                     focus:outline-hidden focus:ring-2 focus:ring-blue-400 focus:border-transparent
                     bg-white dark:bg-p-surface text-p-text placeholder:text-p-text-light"
        />

        {/* Navigation + submit */}
        <div className="flex items-center justify-between mt-3">
          {isMulti && (
            <div className="text-xs text-gray-400">
              {activeTab + 1} of {questions.length}
            </div>
          )}
          <div className={`flex gap-2 ${isMulti ? '' : 'ml-auto'}`}>
            {isMulti && activeTab > 0 && (
              <button
                onClick={() => setActiveTab(activeTab - 1)}
                className="px-3 py-1.5 rounded-lg text-sm font-medium text-gray-600 dark:text-gray-300
                           border border-gray-300 dark:border-gray-600 hover:bg-gray-100 dark:hover:bg-p-surface-hover transition-colors"
              >
                Back
              </button>
            )}
            {isMulti && !isLast ? (
              <button
                onClick={() => setActiveTab(activeTab + 1)}
                className="px-4 py-1.5 rounded-lg text-sm font-medium text-white
                           bg-brand hover:bg-brand-hover transition-colors"
              >
                Next
              </button>
            ) : (
              <button
                onClick={handleSubmit}
                disabled={!allAnswered && isMulti}
                className="px-4 py-1.5 rounded-lg text-sm font-medium text-white
                           bg-brand hover:bg-brand-hover disabled:bg-gray-300 dark:disabled:bg-gray-700
                           disabled:cursor-not-allowed transition-colors"
              >
                Submit
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
