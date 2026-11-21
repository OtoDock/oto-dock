// A link that names an agent's page and no chat (`/chat/<agent>?…`) is about
// the agent itself: a file-conflict notification's Recover bin. Followed from
// a chat of that agent, it opens over that chat, so the chat and a live
// terminal stay on screen. Any other link is followed as it is.
import { withWorkspaceParams } from '../workspaceParams'

const AGENT_PAGE = /^\/chat\/([^/?#]+)\/?(\?[^#]*)?$/
const CHAT_PAGE = /^\/chat\/([^/]+)\/[^/]+\/?$/

/** Where `target` opens from `here` (the router's location, or the window's). */
export function agentLinkHere(
  target: string,
  here: { pathname: string; search: string },
): string {
  const link = AGENT_PAGE.exec(target)
  const chat = CHAT_PAGE.exec(here.pathname)
  if (!link || !chat || chat[1] !== link[1]) return target
  // The chat's own workspace params stay (an open overlay, a preview); the
  // link's params ride on top.
  const params = new URLSearchParams(withWorkspaceParams('', here.search).slice(1))
  for (const [key, value] of new URLSearchParams(link[2] ?? '')) params.set(key, value)
  const query = params.toString()
  return query ? `${here.pathname}?${query}` : here.pathname
}
