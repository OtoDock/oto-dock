// Where an agent's open_app request lands on this page (APPS.md "Live
// apps"). Pure, so the rule is testable without the page: the chat page
// handles its own agent's apps (the overlay, or the Dock for a chat pin when
// the viewer is in that chat); anything else is left to the shell's toast,
// which offers the full-screen page. A hidden tab never shows anything.

import type { OpenAppFrame } from './appLive'

export type OpenPlan = 'overlay' | 'dock' | 'ignore'

export function planOpenApp(
  f: OpenAppFrame,
  ctx: { agentName?: string; chatId?: string; visible: boolean },
): OpenPlan {
  if (!ctx.visible || !ctx.agentName || f.agent !== ctx.agentName) return 'ignore'
  if (f.scope_chat_id) return f.scope_chat_id === ctx.chatId ? 'dock' : 'ignore'
  // A project pin renders on the project view, not on a chat page.
  if (f.scope_project_id) return 'ignore'
  return 'overlay'
}
