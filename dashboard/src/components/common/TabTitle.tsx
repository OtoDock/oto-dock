import { useEffect } from 'react'
import { useLocation } from 'react-router-dom'
import { useAuth } from '../../contexts/AuthContext'
import { isAdmin } from '../../lib/permissions'
import { useAgents } from '../../api/agents'
import { useApp } from '../../api/apps'
import { pageOfPath, tabTitle } from '../../lib/ui/tabTitle'

/** The one writer of `document.title`, mounted once inside the router.
 *  It reads the agent list and the app row as observers only, with the
 *  keys `AgentGuard` and `AppPage` fill: the title fetches and polls
 *  nothing, and a signed-out deep link never meets a 401 because of it. */
export default function TabTitle() {
  const { pathname } = useLocation()
  const { user } = useAuth()
  const page = pageOfPath(pathname)
  const signedIn = !!user && !user.must_change_password && !user.must_enroll_2fa
  const { data: agents } = useAgents({ all: isAdmin(user), enabled: false })
  const { data: app } = useApp(page.kind === 'appPage' ? page.id : undefined, { enabled: false })

  let name: string | null = null
  if (page.kind === 'agentPage') {
    const agent = agents?.find((a) => a.name === page.slug)
    name = agent ? (agent.display_name || agent.name) : null
  } else if (page.kind === 'appPage' && app) {
    name = app.title || app.slug
  }
  const title = tabTitle(page, { signedIn, name })

  useEffect(() => {
    document.title = title
  }, [title])
  return null
}
