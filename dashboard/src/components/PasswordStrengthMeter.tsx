import { useMemo } from 'react'
import zxcvbn from 'zxcvbn'

const SCORE_COLORS = [
  'bg-status-danger-solid',   // 0 - very weak
  'bg-status-danger-solid',   // 1 - weak
  'bg-status-warning-solid',  // 2 - fair
  'bg-status-success-solid',  // 3 - strong
  'bg-status-success-solid',  // 4 - very strong
]

const SCORE_LABELS = [
  'Very weak',
  'Weak',
  'Fair',
  'Strong',
  'Very strong',
]

interface Props {
  password: string
  minScore?: number
  minLength?: number
}

// The scorer behind PasswordStrengthBar: zxcvbn and its dictionaries, loaded
// on demand. Rendered only through the bar's lazy boundary.
export default function PasswordStrengthMeter({ password, minScore = 3, minLength = 8 }: Props) {
  const { score, feedback } = useMemo(() => {
    if (!password) return { score: -1, feedback: '' }
    if (password.length < minLength) {
      return { score: 0, feedback: `At least ${minLength} characters required` }
    }
    // Use zxcvbn — same library as the backend for consistent scoring
    const result = zxcvbn(password)
    const score = result.score
    const passes = score >= minScore

    if (passes) return { score, feedback: '' }

    // Build feedback from zxcvbn suggestions
    const warning = result.feedback.warning || ''
    const suggestions = result.feedback.suggestions || []
    let fb = warning
    if (suggestions.length > 0) {
      fb += (fb ? ' ' : '') + suggestions.join(' ')
    }
    if (!fb) fb = `Minimum strength: ${SCORE_LABELS[minScore]}`
    return { score, feedback: fb }
  }, [password, minScore, minLength])

  if (!password) return null

  const passes = score >= minScore

  return (
    <div className="mt-1.5 space-y-1">
      <div className="flex gap-1 h-1">
        {[0, 1, 2, 3, 4].map(i => (
          <div key={i} className={`flex-1 rounded-full transition-colors ${
            i <= score ? SCORE_COLORS[score] : 'bg-surface-muted'
          }`} />
        ))}
      </div>
      <div className="flex justify-between items-center">
        <span className={`text-3xs font-medium ${passes ? 'text-status-success-fg' : 'text-secondary'}`}>
          {score >= 0 ? SCORE_LABELS[score] : ''}
        </span>
        {score >= 0 && (
          <span className={`text-3xs ${passes ? 'text-status-success-fg' : 'text-status-warning-fg'}`}>
            {passes ? 'Meets requirements' : `Minimum: ${SCORE_LABELS[minScore]}`}
          </span>
        )}
      </div>
      {feedback && <p className="text-3xs text-tertiary">{feedback}</p>}
    </div>
  )
}
