// The browser tab's title. A narrow tab shows only its first words, so the
// title names what the tab is about first and the product last. One owner
// writes it (`components/common/TabTitle.tsx`); this module only decides.

export const TAB_SEPARATOR = ' · '
export const PRODUCT_NAME = 'OtoDock'

// A display name can be long: the tab shows a few words of it anyway.
const NAME_MAX = 80

/** What a path shows, as the title needs it. The words are the title's own,
 *  read from the path by its shape (`App.tsx` is the route table). */
export type TabPage =
  | { kind: 'agentPage'; slug: string }
  | { kind: 'appPage'; id: string }
  | { kind: 'adminArea' }
  | { kind: 'settingsArea' }
  | { kind: 'agentsList' }
  | { kind: 'sharedSnapshot' }
  | { kind: 'other' }

const PATTERNS: Array<[RegExp, (m: RegExpMatchArray) => TabPage]> = [
  [/^\/chat\/([^/]+)(?:\/[^/]*)?\/?$/, (m) => ({ kind: 'agentPage', slug: decodeSegment(m[1]) })],
  [/^\/agents\/([^/]+)(?:\/.*)?$/, (m) => ({ kind: 'agentPage', slug: decodeSegment(m[1]) })],
  [/^\/agents\/?$/, () => ({ kind: 'agentsList' })],
  [/^\/apps\/([^/]+)\/?$/, (m) => ({ kind: 'appPage', id: decodeSegment(m[1]) })],
  [/^\/admin(?:\/.*)?$/, () => ({ kind: 'adminArea' })],
  [/^\/user-settings(?:\/.*)?$/, () => ({ kind: 'settingsArea' })],
  [/^\/shared\/[^/]+\/?$/, () => ({ kind: 'sharedSnapshot' })],
]

function decodeSegment(seg: string): string {
  try { return decodeURIComponent(seg) } catch { return seg }
}

export function pageOfPath(pathname: string): TabPage {
  for (const [re, make] of PATTERNS) {
    const m = pathname.match(re)
    if (m) return make(m)
  }
  return { kind: 'other' }
}

// The pages whose first words never change.
const FIXED: Partial<Record<TabPage['kind'], string>> = {
  adminArea: 'Admin',
  settingsArea: 'Settings',
  agentsList: 'Agents',
  sharedSnapshot: 'Shared chat',
}

/** The title for a page. `signedIn` is false before a sign-in and during a
 *  forced step (a new password, a second factor). `name` is the agent's
 *  display name or the app's title, when the page has one and it loaded. */
export function tabTitle(page: TabPage, { signedIn, name }: { signedIn: boolean; name?: string | null }): string {
  if (!signedIn) return PRODUCT_NAME
  const fixed = FIXED[page.kind]
  if (fixed) return `${fixed}${TAB_SEPARATOR}${PRODUCT_NAME}`
  const named = (name ?? '').replace(/\s+/g, ' ').trim().slice(0, NAME_MAX).trim()
  if (named && (page.kind === 'agentPage' || page.kind === 'appPage')) {
    return `${named}${TAB_SEPARATOR}${PRODUCT_NAME}`
  }
  return PRODUCT_NAME
}
