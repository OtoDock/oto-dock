import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, renderChecks, renderLanes, renderTasks, runsWords, when } from '@/uikit/widgets'

// The widget kit (proxy APPS.md "The widget kit") rides the runtime the
// host injects: feeds arrive through `otodock.feed`, the connect button
// asks `integrations.status`, and every click moves the viewer through
// `otodock.open` by kind — the kit itself never talks to the platform.

type Cb = (rows: Record<string, unknown>[], error: string | null) => void

function stubRuntime(providers: Record<string, unknown>[] = []) {
  const feeds = new Map<string, Cb>()
  const opened: Record<string, unknown>[] = []
  window.otodock = {
    feed: (name: string, cb: Cb) => { feeds.set(name, cb) },
    platform: vi.fn(async (method: string) => method === 'integrations.status' ? { providers } : {}),
    open: (target: Record<string, unknown>) => { opened.push(target) },
  }
  return { feeds, opened }
}

describe('widgets', () => {
  let el: HTMLElement
  beforeEach(() => { el = document.createElement('div'); document.body.appendChild(el) })
  afterEach(() => { el.remove(); delete (window as { otodock?: unknown }).otodock })

  it('subscribes a session list to the feed, renders the rows and opens a chat on click', async () => {
    const { feeds, opened } = stubRuntime()
    mount(el, { widget: 'sessions', title: 'Chats' })
    expect(el.textContent).toContain('Loading')
    expect(feeds.has('sessions')).toBe(true)
    feeds.get('sessions')!([
      { id: 'c1', title: 'Plan the launch', status: 'generating', updated_at: new Date().toISOString() },
      { id: 'c2', title: '', status: 'idle' },
    ], null)
    expect(el.querySelector('h3')?.textContent).toBe('Chats')
    expect(el.textContent).toContain('Plan the launch')
    expect(el.textContent).toContain('Untitled chat')
    ;(el.querySelector('a.odw-row') as HTMLElement).click()
    expect(opened).toEqual([{ kind: 'chat', id: 'c1' }])
    expect(document.getElementById('otodock-widgets-style')).toBeTruthy()
  })

  it('shows the host refusal in place when a feed is not declared', () => {
    const { feeds } = stubRuntime()
    mount(el, { widget: 'tasks' })
    feeds.get('tasks')!([], 'feed not declared in this app\'s manifest')
    expect(el.textContent).toContain('feed not declared')
  })

  it('renders lanes grouped by project and a task table with the last run', () => {
    const lanes = renderLanes([
      { id: 'a', title: 'Researcher', status: 'generating', project_id: 'proj-1234-5678' },
      { id: 'b', title: 'Writer', status: 'idle', project_id: 'proj-1234-5678' },
      { id: 'c', title: 'Side chat', status: 'idle' },
    ])
    expect(lanes.querySelectorAll('.odw-card')).toHaveLength(2)
    expect(lanes.textContent).toContain('Project proj-123')
    expect(lanes.textContent).toContain('Chats')
    const soon = new Date(Date.now() + 3 * 3600_000).toISOString()
    const tasks = renderTasks([
      { id: 't1', name: 'Digest', schedule: '0 7 * * *', enabled: false,
        schedule_text: 'Daily at 07:00 (Europe/Athens)', next_run_time: soon,
        last_run: { id: 'r1', status: 'completed', completed_at: '2026-01-01T07:00:00Z' } },
      { id: 't2', name: 'Sync', task_type: 'trigger', enabled: true, last_run: null },
      { id: 't3', name: 'Old page', schedule: '*/5 * * * *', enabled: true, last_run: null },
    ])
    expect(tasks.textContent).toContain('Digest')
    expect(tasks.textContent).toContain('(paused)')
    expect(tasks.textContent).toContain('completed')
    expect(tasks.textContent).toContain('never')
    // The schedule column prints the feed's words with the next run under
    // it; a row without them (an older proxy, a partial delta) falls back
    // to the raw cron, then the type.
    const cells = [...tasks.querySelectorAll('tbody tr')].map((tr) => tr.querySelectorAll('td')[1])
    expect(cells[0].textContent).toBe(`Daily at 07:00 (Europe/Athens)next ${when(soon)}`)
    expect(cells[0].querySelector('.odw-next')?.textContent).toMatch(/^next /)
    expect(cells[1].textContent).toBe('trigger')
    expect(cells[2].textContent).toBe('*/5 * * * *')
    // The table wraps its name column and scrolls inside its own box, so a
    // phone never scrolls the page sideways (seen on a dashboard's Stats tab).
    expect(tasks.className).toBe('odw-scroll')
    expect(tasks.querySelector('table')?.className).toBe('odw-tasks')
  })

  it('opens targets the host knows: the agent\'s tasks for a task, the notification\'s own agent for its chat', () => {
    const { feeds, opened } = stubRuntime()
    mount(el, { widget: 'tasks' })
    feeds.get('tasks')!([{ id: 't1', name: 'Digest', schedule: '0 7 * * *', enabled: true,
      last_run: { id: 'r1', status: 'completed', completed_at: '2026-01-01T07:00:00Z' } }], null)
    const [task, run] = [...el.querySelectorAll('a.odw-row')] as HTMLElement[]
    task.click()
    run.click()
    expect(opened).toEqual([{ kind: 'agent_settings', tab: 'scheduled-tasks' }, { kind: 'run', id: 'r1' }])
    const el2 = document.createElement('div')
    mount(el2, { widget: 'notifications' })
    feeds.get('notifications')!([
      { id: 'n1', title: 'From another agent', chat_id: 'c9', agent_slug: 'hr-agent' },
      { id: 'n2', title: 'Older proxy row', chat_id: 'c8' },
    ], null)
    for (const a of el2.querySelectorAll('a.odw-row')) (a as HTMLElement).click()
    expect(opened.slice(2)).toEqual([{ kind: 'chat', id: 'c9', agent: 'hr-agent' }, { kind: 'chat', id: 'c8' }])
  })

  // The checks widget (CHECKS.md, the `checks` feed): one row per check
  // with its standing, when it runs and the last verdict as a pill that
  // opens the judged chat — the same table shape as the tasks widget.
  it('lists the checks with their last verdict and opens the judged chat', () => {
    const { feeds, opened } = stubRuntime()
    mount(el, { widget: 'checks', title: 'Checks' })
    expect(feeds.has('checks')).toBe(true)
    feeds.get('checks')!([
      { id: 'agent:code-hygiene', name: 'code-hygiene', owner: '', mandatory: true, condition: { kinds: ['code', 'text'] },
        last_verdict: { id: 'cv-1', status: 'fail', pass: false, score: 0.4, chat_id: 'c1', run_id: '', created_at: new Date().toISOString() } },
      { id: 'user:review', name: 'review', owner: 'alice', mandatory: false, condition: { always: true }, last_verdict: null },
      { id: 'agent:lint', name: 'lint', owner: '', broken: true, condition: {}, last_verdict: null },
    ], null)
    expect(el.querySelector('h3')?.textContent).toBe('Checks')
    const rows = [...el.querySelectorAll('tbody tr')].map((tr) => [...tr.querySelectorAll('td')].map((td) => td.textContent))
    expect(rows).toEqual([
      ['code-hygiene (mandatory)', 'writes code, text', expect.stringMatching(/^fail 0\.40 /)],
      ['review (yours)', 'every turn', 'never'],
      ['lint (broken)', 'any change', 'never'],
    ])
    const pill = el.querySelector('.odw-pill') as HTMLElement
    expect(pill.dataset.s).toBe('fail')
    ;(el.querySelector('a.odw-row') as HTMLElement).click()
    expect(opened).toEqual([{ kind: 'chat', id: 'c1' }])
    expect(el.querySelector('.odw-scroll table')?.className).toBe('odw-tasks')
    expect(renderChecks([]).textContent).toBe('No checks.')
    expect(runsWords({ events: ['commit', 'tool:mcp__x__y'], globs: ['src/**'] })).toBe('on commit, mcp__x__y; under src/**')
    expect(runsWords(undefined)).toBe('any change')
  })

  it('offers a connect button per provider and opens the integrations tab', async () => {
    const { opened } = stubRuntime([
      { mcp: 'github-mcp', provider: 'github', connected: false },
      { mcp: 'm365-mcp', provider: 'microsoft', connected: true },
    ])
    mount(el, { widget: 'connect', provider: 'github' })
    await new Promise((r) => setTimeout(r, 0))
    const btn = el.querySelector('button') as HTMLButtonElement
    expect(btn.textContent).toBe('Connect github')
    btn.click()
    expect(opened).toEqual([{ kind: 'user_settings', tab: 'integrations', provider: 'github' }])
    const el2 = document.createElement('div')
    mount(el2, { widget: 'connect' })
    await new Promise((r) => setTimeout(r, 0))
    const labels = [...el2.querySelectorAll('button')].map((b) => b.textContent)
    expect(labels).toEqual(['Connect github', 'microsoft connected'])
    expect((el2.querySelectorAll('button')[1] as HTMLButtonElement).disabled).toBe(true)
  })

  it("says whose account a shared app's buttons use and sends a manager to the agent's MCPs", async () => {
    // A shared app runs with the agent's service account (identity since
    // 1.7): the viewer is never told to connect their own.
    const { opened } = stubRuntime([
      { mcp: 'github-mcp', provider: 'github', connected: true, account: 'dimitris', identity: 'the agent' },
      { mcp: 'google-workspace', provider: 'google', connected: false, account: '', identity: 'the agent' },
      { mcp: 'm365-mcp', provider: 'microsoft', connected: true, account: 'me@x.io', identity: 'you' },
    ])
    mount(el, { widget: 'connect' })
    await new Promise((r) => setTimeout(r, 0))
    const buttons = [...el.querySelectorAll('button')] as HTMLButtonElement[]
    expect(buttons.map((b) => b.textContent)).toEqual([
      "github: the agent's account dimitris", 'Bind a google account to this agent', 'microsoft connected as me@x.io',
    ])
    expect(buttons.map((b) => b.disabled)).toEqual([true, false, true])
    buttons[1].click()
    expect(opened).toEqual([{ kind: 'agent_settings', tab: 'mcps' }])
  })

  it('opens a file by path and says when it changed; without the runtime it says so', () => {
    const { feeds, opened } = stubRuntime()
    mount(el, { widget: 'file', path: 'workspace/reports/q3.md', label: 'Q3 report' })
    ;(el.querySelector('button') as HTMLElement).click()
    expect(opened).toEqual([{ kind: 'file', path: 'workspace/reports/q3.md' }])
    feeds.get('file_changes')!([{ rel_path: 'workspace/reports/q3.md', at: new Date().toISOString() }], null)
    expect(el.textContent).toContain('updated')
    delete (window as { otodock?: unknown }).otodock
    const bare = document.createElement('div')
    mount(bare, { widget: 'notifications' })
    expect(bare.textContent).toContain('needs the app runtime')
    expect(when('')).toBe('')
    expect(when('not a date')).toBe('not a date')
  })
})
