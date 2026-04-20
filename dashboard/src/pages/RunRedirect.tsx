import { useEffect } from 'react'
import { useParams, useNavigate, Navigate } from 'react-router-dom'
import { useRun, ForbiddenError } from '../api/runs'

/**
 * /runs/:runId resolver — task runs render on the chat page now, but old
 * notification deep links and the admin History rows still carry run ids.
 * Resolve the run, then redirect to its chat with task mode toggled on
 * (chat-surface delegate runs live in a plain chat — no ?tasks there).
 */
export default function RunRedirect() {
  const { runId } = useParams<{ runId: string }>()
  const navigate = useNavigate()
  const { data: run, isLoading, error } = useRun(runId!)

  useEffect(() => {
    if (!run) return
    const chatId = run.chat_id || `task-${run.id}`
    const suffix = chatId.startsWith('task-') ? '?tasks=1' : ''
    navigate(`/chat/${run.agent}/${chatId}${suffix}`, { replace: true })
  }, [run, navigate])

  if (error instanceof ForbiddenError) {
    return <Navigate to="/" replace />
  }
  return (
    <div className="h-screen-safe flex items-center justify-center bg-p-bg">
      <div className="text-center">
        {isLoading || run ? (
          <>
            <span className="inline-block w-6 h-6 border-2 border-brand border-t-transparent rounded-full animate-spin" />
            <p className="text-sm text-p-text-secondary mt-3">Opening task run...</p>
          </>
        ) : (
          <>
            <p className="text-sm text-p-error mb-4">Run not found.</p>
            <button
              onClick={() => navigate('/')}
              className="text-sm text-brand hover:text-brand-hover"
            >
              Go home
            </button>
          </>
        )}
      </div>
    </div>
  )
}
