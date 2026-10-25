// The history behaviour of a settings area on the real browser history
// (jsdom traverses entries and fires popstate): what a section tap pushes,
// what the back arrow pops, the list injected behind a cold deep link, and
// where "Back to Chat" lands.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, render, screen, fireEvent } from '@testing-library/react'
import { BrowserRouter, Link, Outlet, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { useEffect } from 'react'
import HistoryTrail from '@/components/settings/HistoryTrail'
import { useSettingsArea } from '@/components/settings/useSettingsArea'
import { readTrail } from '@/components/settings/history'

let narrow = true
let matchMediaOriginal: typeof window.matchMedia

/** What App's NavigationGuard does on a cold load of a secondary page. */
function Guard() {
  const location = useLocation()
  const navigate = useNavigate()
  useEffect(() => {
    if (location.key === 'default' && location.pathname !== '/' && !location.pathname.startsWith('/chat/')) {
      const returnTo = location.pathname + location.search
      navigate('/', { replace: true })
      navigate(returnTo)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  return null
}

function Area({ base, agent }: { base: string; agent?: string }) {
  const nav = useSettingsArea(base, agent)
  const location = useLocation()
  return (
    <div>
      <p data-testid="where">{location.pathname + location.search}</p>
      <p data-testid="mode">{nav.narrow ? (nav.onIndex ? 'list' : 'section') : 'desktop'}</p>
      <button onClick={nav.goBack}>arrow</button>
      <button onClick={nav.backToChat}>to chat</button>
      <Link to={`${base}/users`}>Users</Link>
      <Link to={`${base}/usage`}>Usage</Link>
      <Outlet />
    </div>
  )
}

/** A page outside the areas, with the links the app's pages carry. */
function Page({ name }: { name: string }) {
  const location = useLocation()
  const navigate = useNavigate()
  return (
    <div>
      <p data-testid="page">{name} at {location.pathname}</p>
      <Link to="/admin">open admin</Link>
      <Link to="/admin/users">open admin users</Link>
      <Link to="/agents">open agents</Link>
      <Link to="/agents/ops">open ops</Link>
      <button onClick={() => navigate('/admin/users', { replace: true })}>finish setup</button>
    </div>
  )
}

function App() {
  return (
    <BrowserRouter>
      <Guard />
      <HistoryTrail />
      <Routes>
        <Route path="/" element={<Page name="root" />} />
        <Route path="/chat/:name" element={<Page name="chat" />} />
        <Route path="/chat/:name/:id" element={<Page name="chat" />} />
        <Route path="/agents" element={<Page name="agents" />} />
        <Route path="/admin" element={<Area base="/admin" />}>
          <Route index element={<p>overview</p>} />
          <Route path="users" element={<p>users page</p>} />
          <Route path="usage" element={<p>usage page</p>} />
        </Route>
        <Route path="/agents/:name" element={<Area base="/agents/ops" agent="ops" />}>
          <Route index element={<p>agent overview</p>} />
          <Route path="users" element={<p>agent users</p>} />
          <Route path="usage" element={<p>agent usage</p>} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}

const idx = () => (window.history.state as { idx?: number } | null)?.idx
const where = () => screen.getByTestId('where').textContent
const page = () => screen.getByTestId('page').textContent
// jsdom runs a traversal through three zero-delay tasks before popstate.
const settle = () => act(async () => {
  for (let i = 0; i < 6; i++) await new Promise((r) => setTimeout(r, 0))
})
const click = async (label: string) => {
  await act(async () => { fireEvent.click(screen.getByText(label)) })
  await settle()
}
const back = async () => {
  await act(async () => { window.history.back() })
  await settle()
}
const forward = async () => {
  await act(async () => { window.history.forward() })
  await settle()
}

/** A cold load: the tab opens on `path` with nothing behind it. */
async function coldLoad(path: string) {
  window.history.replaceState(null, '', path)
  render(<App />)
  await settle()
}

beforeEach(() => {
  sessionStorage.clear()
  matchMediaOriginal = window.matchMedia
  window.matchMedia = ((q: string) => ({
    matches: q.includes('767') ? narrow : false,
    media: q, onchange: null, addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  })) as typeof window.matchMedia
  // A fresh entry, so an earlier test's entries never sit behind this one.
  window.history.pushState(null, '', '/blank')
})

afterEach(() => {
  window.matchMedia = matchMediaOriginal
  narrow = true
  vi.restoreAllMocks()
})

describe('a section is one history entry', () => {
  it('a row tap pushes, the arrow pops to the list', async () => {
    await coldLoad('/admin')
    expect(screen.getByTestId('mode').textContent).toBe('list')
    const at = idx()!
    await click('Users')
    expect(where()).toBe('/admin/users')
    expect(idx()).toBe(at + 1)
    expect(screen.getByTestId('mode').textContent).toBe('section')
    await click('arrow')
    expect(where()).toBe('/admin')
    expect(idx()).toBe(at)
  })

  it('browser back pops the same entry', async () => {
    await coldLoad('/admin')
    await click('Usage')
    expect(where()).toBe('/admin/usage')
    await back()
    expect(where()).toBe('/admin')
  })

  it('the arrow from a section reached from another section pops to that section', async () => {
    await coldLoad('/admin')
    await click('Users')
    await click('Usage')
    await click('arrow')
    expect(where()).toBe('/admin/users')
  })

  it('the arrow on a section with nothing of the area behind it replaces it with the list', async () => {
    narrow = false
    await coldLoad('/admin/users')
    // Folded to a phone after the desktop load: nothing was injected.
    narrow = true
    await click('arrow')
    expect(where()).toBe('/admin')
    expect(idx()).toBe(1)
  })
})

describe('the injected list entry', () => {
  it('a cold deep link on a phone gets the list behind it, the query kept', async () => {
    await coldLoad('/admin/users?x=1')
    expect(where()).toBe('/admin/users?x=1')
    expect(idx()).toBe(2)
    expect(readTrail().entries).toEqual({ 1: '/admin', 2: '/admin/users' })
    await back()
    expect(where()).toBe('/admin')
    expect(screen.getByTestId('mode').textContent).toBe('list')
    await back()
    expect(page()).toBe('root at /')
  })

  it('a section opened from a chat gets the list between the chat and it', async () => {
    await coldLoad('/chat/ops/c1')
    await click('open admin users')
    expect(where()).toBe('/admin/users')
    expect(idx()).toBe(2)
    expect(readTrail().entries).toEqual({ 0: '/chat/ops/c1', 1: '/admin', 2: '/admin/users' })
    await back()
    expect(where()).toBe('/admin')
    await back()
    expect(page()).toBe('chat at /chat/ops/c1')
  })

  it('a landing that replaced the only entry (the Setup wizard) gets the root and the list', async () => {
    await coldLoad('/')
    await click('finish setup')
    expect(where()).toBe('/admin/users')
    expect(idx()).toBe(2)
    expect(readTrail().entries).toEqual({ 0: '/', 1: '/admin', 2: '/admin/users' })
    await back()
    expect(where()).toBe('/admin')
    await back()
    expect(page()).toBe('root at /')
  })

  it('a desktop injects nothing', async () => {
    narrow = false
    await coldLoad('/admin/users')
    expect(idx()).toBe(1)
    expect(screen.getByTestId('mode').textContent).toBe('desktop')
    expect(readTrail().entries).toEqual({ 1: '/admin/users' })
  })

  it('a traversal never injects again', async () => {
    await coldLoad('/chat/ops/c1')
    await click('open admin users')
    await back()
    await back()
    expect(page()).toBe('chat at /chat/ops/c1')
    // Back on the tab's first entry keeps the trail (the entry is unkeyed,
    // but only a load from outside the app clears it).
    expect(readTrail().entries).toEqual({ 0: '/chat/ops/c1', 1: '/admin', 2: '/admin/users' })
    await forward()
    await forward()
    expect(where()).toBe('/admin/users')
    expect(idx()).toBe(2)
  })
})

describe('Back to Chat', () => {
  it('pops to the chat the area was entered from, keeping its entry number', async () => {
    narrow = false
    await coldLoad('/chat/ops/c1')
    await click('open admin')
    await click('Users')
    await click('Usage')
    expect(idx()).toBe(3)
    await click('to chat')
    expect(page()).toBe('chat at /chat/ops/c1')
    expect(idx()).toBe(0)
  })

  it('on a phone pops over the injected list too', async () => {
    await coldLoad('/chat/ops/c1')
    await click('open admin users')
    expect(idx()).toBe(2)
    await click('to chat')
    expect(page()).toBe('chat at /chat/ops/c1')
    expect(idx()).toBe(0)
  })

  it('from a cold load goes to the root entry, the default chat when the tab showed none', async () => {
    narrow = false
    await coldLoad('/admin')
    await click('Users')
    expect(idx()).toBe(2)
    await click('to chat')
    expect(page()).toBe('root at /')
    expect(idx()).toBe(0)
  })

  it("an agent area entered from the agents page pushes that agent's chat over the area", async () => {
    narrow = false
    await coldLoad('/chat/other/c2')
    await click('open agents')
    await click('open ops')
    expect(idx()).toBe(2)
    await click('to chat')
    expect(page()).toBe('chat at /chat/ops')
    expect(idx()).toBe(2)
    await back()
    expect(page()).toBe('agents at /agents')
    await back()
    expect(page()).toBe('chat at /chat/other/c2')
  })

  it('a second tap during the travel does nothing', async () => {
    narrow = false
    await coldLoad('/chat/ops/c1')
    await click('open admin')
    await click('Users')
    expect(idx()).toBe(2)
    await act(async () => {
      fireEvent.click(screen.getByText('to chat'))
      fireEvent.click(screen.getByText('to chat'))
    })
    await settle()
    expect(page()).toBe('chat at /chat/ops/c1')
    expect(idx()).toBe(0)
  })

  it('pushes the chat from the tapped entry when the browser never arrives', async () => {
    narrow = false
    await coldLoad('/chat/other/c2')
    await click('open agents')
    await click('open ops')
    expect(idx()).toBe(2)
    const go = vi.spyOn(window.history, 'go').mockImplementation(() => {})
    await click('to chat')
    expect(where()).toBe('/agents/ops')
    await act(async () => { await new Promise((r) => setTimeout(r, 1200)) })
    go.mockRestore()
    expect(page()).toBe('chat at /chat/ops')
    expect(idx()).toBe(3)
  })

  it('does nothing late once the person has moved on', async () => {
    narrow = false
    await coldLoad('/chat/other/c2')
    await click('open agents')
    await click('open ops')
    const go = vi.spyOn(window.history, 'go').mockImplementation(() => {})
    await click('to chat')
    await click('Users')
    expect(where()).toBe('/agents/ops/users')
    expect(idx()).toBe(3)
    await act(async () => { await new Promise((r) => setTimeout(r, 1200)) })
    go.mockRestore()
    expect(where()).toBe('/agents/ops/users')
    expect(idx()).toBe(3)
  })

  it('replaces in place when the area is the only entry', async () => {
    narrow = false
    await coldLoad('/')
    await click('finish setup')
    expect(idx()).toBe(0)
    await click('to chat')
    expect(page()).toBe('root at /')
    expect(idx()).toBe(0)
  })
})
