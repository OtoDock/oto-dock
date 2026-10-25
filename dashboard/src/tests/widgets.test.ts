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
    // The pill says a word and keeps the raw status for the sheet.
    const pills = [...el.querySelectorAll('.odw-pill')] as HTMLElement[]
    expect(pills.map((p) => [p.textContent, p.dataset.s])).toEqual([['Working', 'generating'], ['Idle', 'idle']])
    // The sheet: the kit's font at 14 px, every pill by meaning (the run's
    // tones as the dashboard's status badge), the tables folded under 768 px
    // with a cell rule that ties the column rules and wins by source order,
    // and no text faded under AA.
    const css = document.getElementById('otodock-widgets-style')!.textContent!
    expect(css).toContain('.odw{font:14px/20px var(--p-font,')
    expect(css).toContain('[data-s="awaiting_user"],.odw .odw-pill[data-s="limit_exceeded"]{background:var(--p-status-warning-bg,')
    expect(css).toContain('[data-s="generating"]{background:var(--p-brand-surface,')
    expect(css).toContain('[data-s="pending"]{background:var(--p-status-info-bg,')
    expect(css).toContain('[data-s="completed"]{background:var(--p-status-success-bg,')
    // In a row the pill and the time keep their width; the title gives way.
    expect(css).toContain('.odw li>.odw-pill,.odw li>.odw-muted{flex:none;white-space:nowrap}')
    expect(css).toContain('.odw li>a.odw-row{display:flex;min-width:0}')
    expect(css).toContain('@media (max-width:767px){.odw table.odw-tasks thead{display:none}')
    expect(css).toContain('.odw table.odw-tasks td:nth-child(n){display:block;')
    expect(css).not.toContain('opacity:.8')
  })

  it('shows the host refusal in place when a feed is not declared', () => {
    const { feeds } = stubRuntime()
    mount(el, { widget: 'tasks' })
    feeds.get('tasks')!([], 'feed not declared by this app')
    expect(el.textContent).toContain('feed not declared')
  })

  it('renders lanes grouped by project and a task table with the last run', () => {
    const lanes = renderLanes([
      { id: 'a', title: 'Researcher', status: 'generating', project_id: 'proj-1234-5678', project_title: 'Launch plan' },
      { id: 'b', title: 'Writer', status: 'idle', project_id: 'proj-1234-5678' },
      { id: 'c', title: 'Side chat', status: 'idle' },
    ])
    expect(lanes.querySelectorAll('.odw-card')).toHaveLength(2)
    // The project's title when the feed carries one, never its id.
    expect(lanes.textContent).toContain('Launch plan')
    expect(lanes.textContent).not.toContain('proj-123')
    expect(lanes.textContent).toContain('Chats')
    expect(renderLanes([{ id: 'd', title: 'Lane', status: 'idle', project_id: 'p2' }]).textContent).toContain('A project')
    expect(renderLanes([]).textContent).toBe('No project work right now.')
    const soon = new Date(Date.now() + 3 * 3600_000).toISOString()
    const tasks = renderTasks([
      { id: 't1', name: 'Digest', schedule: '0 7 * * *', enabled: false,
        schedule_text: 'Daily at 07:00 (Europe/Athens)', next_run_time: soon,
        last_run: { id: 'r1', status: 'completed', completed_at: '2026-01-01T07:00:00Z' } },
      { id: 't2', name: 'Sync', task_type: 'trigger', enabled: true, last_run: null },
      { id: 't3', name: 'Old page', schedule: '*/5 * * * *', enabled: true, last_run: null },
      { id: 't4', name: 'Nightly', enabled: true, last_run: { id: 'r4', status: 'pending' } },
    ])
    expect(tasks.textContent).toContain('Digest')
    expect(tasks.textContent).toContain(', paused')
    expect(tasks.textContent).toContain('Done')
    expect(tasks.textContent).toContain('Queued')
    expect(tasks.textContent).toContain('Not run yet')
    expect((tasks.querySelector('.odw-pill') as HTMLElement).dataset.s).toBe('completed')
    // The schedule column prints the feed's words with the next run under
    // it; a row without them (an older proxy, a partial delta) says only
    // what kind of task it is, never the raw cron.
    const cells = [...tasks.querySelectorAll('tbody tr')].map((tr) => tr.querySelectorAll('td')[1])
    expect(cells[0].textContent).toBe(`Daily at 07:00 (Europe/Athens)Next run ${when(soon)}`)
    expect(cells[0].querySelector('.odw-next')?.textContent).toMatch(/^Next run /)
    expect(cells[1].textContent).toBe('On trigger')
    expect(cells[2].textContent).toBe('On a schedule')
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
      { id: 'n1', title: 'From another agent', chat_id: 'c9', agent_slug: 'hr-agent', severity: 'danger', read: true },
      { id: 'n2', title: 'Older proxy row', chat_id: 'c8' },
    ], null)
    for (const a of el2.querySelectorAll('a.odw-row')) (a as HTMLElement).click()
    expect(opened.slice(2)).toEqual([{ kind: 'chat', id: 'c9', agent: 'hr-agent' }, { kind: 'chat', id: 'c8' }])
    // The severity is a word; an unread row is bold, a read one plain.
    expect([...el2.querySelectorAll('.odw-pill')].map((p) => p.textContent)).toEqual(['Problem', 'Note'])
    expect([...el2.querySelectorAll('.odw-title')].map((t) => (t as HTMLElement).style.fontWeight)).toEqual(['', '600'])
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
      ['code-hygiene, mandatory', 'Writes code, text', expect.stringMatching(/^Failed Score 40% /)],
      ['review, yours', 'Every turn', 'Not run yet'],
      ['lint, broken', 'Any change', 'Not run yet'],
    ])
    const pill = el.querySelector('.odw-pill') as HTMLElement
    expect(pill.dataset.s).toBe('fail')
    ;(el.querySelector('a.odw-row') as HTMLElement).click()
    expect(opened).toEqual([{ kind: 'chat', id: 'c1' }])
    expect(el.querySelector('.odw-scroll table')?.className).toBe('odw-tasks')
    expect(renderChecks([]).textContent).toBe('No checks.')
    expect(runsWords({ events: ['commit', 'tool:mcp__x__y'], globs: ['src/**'] })).toBe('On commit, mcp__x__y, under src/**')
    expect(runsWords(undefined)).toBe('Any change')
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
    // A connected account is a line, never a dead button.
    expect([...el2.querySelectorAll('button')].map((b) => b.textContent)).toEqual(['Connect github'])
    expect([...el2.querySelectorAll('div.odw-muted')].map((d) => d.textContent)).toEqual(['microsoft connected'])
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
    expect(buttons.map((b) => b.textContent)).toEqual(['Connect a google account for this agent'])
    expect([...el.querySelectorAll('div.odw-muted')].map((d) => d.textContent)).toEqual([
      "github: the agent's account dimitris", 'microsoft connected as me@x.io',
    ])
    buttons[0].click()
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
    expect(bare.textContent).toContain('Open the page as an app, or wait a moment for it to load.')
    expect(when('')).toBe('')
    expect(when('not a date')).toBe('not a date')
  })
})
