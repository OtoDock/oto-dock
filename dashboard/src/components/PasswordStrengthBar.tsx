import { lazy, Suspense } from 'react'

// The scorer (zxcvbn, the largest single dependency in the app) loads on the
// first keystroke into a new-password field; nothing else needs it. The
// parents never read the score: the backend scores with the same library,
// so a meter that is still loading cannot block a submit.
const PasswordStrengthMeter = lazy(() => import('./PasswordStrengthMeter'))

interface Props {
  password: string
  minScore?: number
  minLength?: number
}

export default function PasswordStrengthBar(props: Props) {
  if (!props.password) return null
  return (
    <Suspense fallback={null}>
      <PasswordStrengthMeter {...props} />
    </Suspense>
  )
}
