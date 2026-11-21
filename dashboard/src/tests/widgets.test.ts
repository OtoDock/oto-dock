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

// The media widget and `mediaLink`: a file's
// short lease is kept only while it is used, a lapsed one is asked again
// on the same address, and the player's own API is what a timeline drives.
describe('widgets: media', () => {
  let el: HTMLElement
  let asked: unknown[]
  let answer: (args: Record<string, unknown>) => unknown
  const link = (n: number, lease = 120) => ({ path: `workspace/m/${n}`, url: `/v1/apps/a/client/media?t=secret-${n}`, expires_in: lease, kind: 'video', mime: 'video/mp4', size: 9 })

  beforeEach(async () => {
    vi.useFakeTimers()
    el = document.createElement('div')
    document.body.appendChild(el)
    asked = []
    answer = () => link(1)
    window.otodock = {
      platform: vi.fn(async (_method: string, args?: unknown) => { asked.push(args); return answer(args as Record<string, unknown>) }),
      open: () => {},
    }
    // jsdom plays nothing: the element's verbs are stubs the test drives.
    for (const proto of [HTMLMediaElement.prototype]) {
      proto.play = vi.fn(async function (this: HTMLMediaElement) {
        Object.defineProperty(this, 'paused', { value: false, configurable: true })
        this.dispatchEvent(new Event('play'))
      })
      proto.pause = vi.fn(function (this: HTMLMediaElement) {
        Object.defineProperty(this, 'paused', { value: true, configurable: true })
        this.dispatchEvent(new Event('pause'))
      })
      proto.load = vi.fn()
    }
  })
  afterEach(() => { vi.useRealTimers(); el.remove(); delete (window as { otodock?: unknown }).otodock })

  const tick = async (ms = 0) => {
    await vi.advanceTimersByTimeAsync(ms)
    for (let i = 0; i < 8; i++) await Promise.resolve()
  }

  it('asks in one call for every file of a tick, and gives each its link or its reason', async () => {
    const { mediaLink } = await import('@/uikit/widgets')
    answer = (args) => ({ links: (args.paths as string[]).map((p, i) => (p.endsWith('missing.png') ? { path: p, error: 'not a file' } : { ...link(i), kind: 'image' })) })
    const links = ['a.png', 'missing.png', 'c.png'].map((n) => mediaLink(`workspace/m/${n}`))
    const failed = links[1].ready.then(() => 'ok', (e: Error) => e.message)
    await tick()
    expect(asked).toEqual([{ paths: ['workspace/m/a.png', 'workspace/m/missing.png', 'workspace/m/c.png'] }])
    expect(await failed).toBe('not a file')
    expect([links[0].url, links[2].url]).toEqual(['/v1/apps/a/client/media?t=secret-0', '/v1/apps/a/client/media?t=secret-2'])
    links.forEach((l) => l.release())
    // One file alone is asked by its path.
    const one = mediaLink('workspace/m/v.mp4')
    await tick()
    expect(asked[1]).toEqual({ path: 'workspace/m/v.mp4' })
    one.release()
  })

  it('keeps the lease only while the file plays or was just touched, never in a hidden tab', async () => {
    const { mediaLink } = await import('@/uikit/widgets')
    const video = document.createElement('video')
    const l = mediaLink('workspace/m/v.mp4')
    l.attach(video)
    await tick()
    expect(video.getAttribute('src')).toBe(link(1).url)
    expect(video.crossOrigin).toBe('anonymous')
    expect(video.getAttribute('controlslist')).toBe('nodownload noremoteplayback')
    expect(video.hasAttribute('playsinline') && video.hasAttribute('disableremoteplayback')).toBe(true)
    await tick(30_000)
    video.dispatchEvent(new Event('pointerdown'))        // the person touches it
    await tick(42_000)                                   // 60 % of the lease, touched under a minute ago
    expect(asked.length).toBe(2)
    await tick(72_000)                                   // idle and paused for over a minute: it is left to lapse
    expect(asked.length).toBe(2)
    await tick(60_000)                                   // the lease ran out
    await video.play()                                   // played again: asked at once
    await tick()
    expect(asked.length).toBe(3)
    await tick(72_000)                                   // still playing: kept
    expect(asked.length).toBe(4)
    Object.defineProperty(document, 'hidden', { value: true, configurable: true })
    await tick(72_000)
    expect(asked.length).toBe(5)                         // it plays on in a hidden tab (a song): kept
    video.pause()
    video.dispatchEvent(new Event('pointerdown'))
    await tick(144_000)
    expect(asked.length).toBe(5)                         // paused in a hidden tab: nothing is kept
    Object.defineProperty(document, 'hidden', { value: false, configurable: true })
    // Touched again with time left on no lease at all: asked at once.
    video.dispatchEvent(new Event('pointerdown'))
    await tick()
    expect(asked.length).toBe(6)
    l.release()
    await tick(500_000)
    expect(asked.length).toBe(6)
  })

  it('swaps a new address at a pause and puts the time, the speed and the playing state back', async () => {
    const { mediaLink } = await import('@/uikit/widgets')
    const video = document.createElement('video')
    const changes: string[] = []
    const l = mediaLink('workspace/m/v.mp4')
    l.onchange = (url) => changes.push(url)
    l.attach(video)
    await tick()
    await video.play()
    video.currentTime = 42
    video.playbackRate = 1.5
    answer = () => link(2)                               // half an hour on: the address changed
    const again = l.refresh()
    await tick()
    await again
    expect(changes).toEqual([link(1).url, link(2).url])
    expect(video.getAttribute('src')).toBe(link(1).url)  // it plays on: nothing reloads under it
    video.pause()
    expect(video.getAttribute('src')).toBe(link(2).url)
    video.currentTime = 0
    video.dispatchEvent(new Event('loadedmetadata'))
    expect([video.currentTime, video.playbackRate]).toEqual([42, 1.5])
    // An error asks again, at most once in three seconds, and reloads on the address it gets.
    const before = asked.length
    video.dispatchEvent(new Event('error'))
    video.dispatchEvent(new Event('error'))
    await tick()
    expect(asked.length).toBe(before + 1)
    expect(HTMLMediaElement.prototype.load).toHaveBeenCalled()
    l.release()
  })

  it('mounts a player a timeline can drive, with its own bar', async () => {
    const handle = mount(el, { widget: 'media', path: 'workspace/m/v.mp4', fps: 25 }) as import('@/uikit/widgets').MediaHandle
    await tick()
    const video = el.querySelector('video') as HTMLVideoElement
    expect(video.getAttribute('src')).toBe(link(1).url)
    expect(el.querySelector('.odw-media')?.getAttribute('data-kind')).toBe('video')
    Object.defineProperty(video, 'duration', { value: 120, configurable: true })
    const times: number[] = []
    const seen: string[] = []
    handle.on('time', (t) => times.push(t))
    for (const name of ['ready', 'play', 'pause'] as const) handle.on(name, () => seen.push(name))
    video.dispatchEvent(new Event('loadedmetadata'))
    handle.seek(90)
    video.dispatchEvent(new Event('seeked'))
    expect([handle.currentTime, handle.duration, handle.paused]).toEqual([90, 120, true])
    expect(times).toEqual([90])
    expect(el.querySelector('.odw-time')?.textContent).toBe('1:30 / 2:00')
    handle.step(1)                                       // one frame at 25 a second
    expect(handle.currentTime).toBeCloseTo(90.04)
    handle.step(-2)
    expect(handle.currentTime).toBeCloseTo(89.96)
    handle.seek(500)
    expect(handle.currentTime).toBe(120)                 // never past the end
    expect(handle.rate(2)).toBe(2)
    expect(handle.rate()).toBe(2)
    handle.play()
    await tick()
    handle.pause()
    expect(seen).toEqual(['ready', 'play', 'pause'])
    // The scrubber gives way on a phone: its own width never widens the app's layout.
    expect(document.getElementById('otodock-widgets-style')!.textContent).toContain('input.odw-seek{flex:1 1 0;width:0;min-width:40px;')
    const [playBtn, , , rateBtn] = [...el.querySelectorAll('.odw-bar > *')] as HTMLElement[]
    expect(playBtn.getAttribute('aria-label')).toBe('Play')
    video.playbackRate = 1
    rateBtn.click()
    expect(video.playbackRate).toBe(1.5)
    const stop = handle.on('ended', () => seen.push('ended'))
    stop()
    video.dispatchEvent(new Event('ended'))
    expect(seen).not.toContain('ended')
    handle.destroy()
    expect(el.children.length).toBe(0)
    // No bar for an app that draws its own; audio has no picture.
    mount(el, { widget: 'media', path: 'workspace/m/v.mp4', controls: false })
    expect(el.querySelector('.odw-bar')).toBeNull()
    mount(el, { widget: 'media', path: 'workspace/m/song.mp3' })
    expect(el.querySelector('audio')).toBeTruthy()
    expect(el.querySelector('.odw-media')?.getAttribute('data-kind')).toBe('audio')
  })

  it('loops a range while it plays', async () => {
    const handle = mount(el, { widget: 'media', path: 'workspace/m/v.mp4' }) as import('@/uikit/widgets').MediaHandle
    await tick()
    const video = el.querySelector('video') as HTMLVideoElement
    Object.defineProperty(video, 'duration', { value: 120, configurable: true })
    handle.loop(10, 20)
    handle.play()
    video.currentTime = 20.5
    await tick(50)                                       // the next frame the screen draws
    expect(video.currentTime).toBe(10)
    handle.loop(null)
    video.currentTime = 30
    await tick(50)
    expect(video.currentTime).toBe(30)
    handle.destroy()
  })

  it('shows a picture, two with a split, and says when a file is not available', async () => {
    answer = (args) => ({ links: (args.paths as string[]).map((_p, i) => ({ ...link(i), kind: 'image' })) })
    const handle = mount(el, { widget: 'media', path: 'workspace/m/before.jpg', compare: 'workspace/m/after.jpg', fit: 'cover' }) as import('@/uikit/widgets').MediaHandle
    await tick()
    const [under, over] = [...el.querySelectorAll('img')] as HTMLImageElement[]
    expect([under.getAttribute('src'), over.getAttribute('src')]).toEqual([link(0).url, link(1).url])
    expect(el.querySelector('.odw-media')?.getAttribute('data-fit')).toBe('cover')
    expect(over.style.clipPath).toBe('inset(0 0 0 50%)')
    handle.split(0.25)
    expect(over.style.clipPath).toBe('inset(0 0 0 25%)')
    expect((el.querySelector('.odw-line') as HTMLElement).style.left).toBe('25%')
    const drag = el.querySelector('input.odw-split') as HTMLInputElement
    drag.value = '800'
    drag.dispatchEvent(new Event('input'))
    expect(over.style.clipPath).toBe('inset(0 0 0 80%)')
    handle.zoom(2)
    expect([under.style.transform, over.style.transform]).toEqual(['scale(2)', 'scale(2)'])
    handle.destroy()
    answer = () => { throw new Error('not available: the agent\'s files are for its members') }
    const errors: number[] = []
    const gone = mount(el, { widget: 'media', path: 'workspace/m/x.png' }) as import('@/uikit/widgets').MediaHandle
    gone.on('error', () => errors.push(1))
    await tick()
    expect(el.textContent).toContain('not available')
    expect(errors).toEqual([1])
    delete (window as { otodock?: unknown }).otodock
    mount(el, { widget: 'media', path: 'workspace/m/y.png' })
    await tick()
    expect(el.textContent).toContain('not ready')
  })

  it('names the place and the speed for a screen reader, and keeps the speed button at every width', async () => {
    const handle = mount(el, { widget: 'media', path: 'workspace/m/v.mp4' }) as import('@/uikit/widgets').MediaHandle
    await tick()
    const video = el.querySelector('video') as HTMLVideoElement
    Object.defineProperty(video, 'duration', { value: 120, configurable: true })
    handle.seek(90)
    video.dispatchEvent(new Event('seeked'))
    expect((el.querySelector('input.odw-seek') as HTMLElement).getAttribute('aria-valuetext')).toBe('1:30 of 2:00')
    const rateBtn = el.querySelector('.odw-rate') as HTMLElement
    expect(rateBtn.getAttribute('aria-label')).toBe('Speed 1x')
    rateBtn.click()
    video.dispatchEvent(new Event('ratechange'))
    expect(rateBtn.getAttribute('aria-label')).toBe('Speed 1.5x')
    expect(document.getElementById('otodock-widgets-style')!.textContent).not.toContain('odw-rate{display:none}')
  })

  it('says when a file a person was watching is no longer theirs, and stays quiet on a passing failure', async () => {
    const handle = mount(el, { widget: 'media', path: 'workspace/m/v.mp4' }) as import('@/uikit/widgets').MediaHandle
    await tick()
    const video = el.querySelector('video') as HTMLVideoElement
    const errors: number[] = []
    handle.on('error', () => errors.push(1))
    // The network drops: the element fails, so does the ask, and nothing is said.
    answer = () => { throw new Error('network error') }
    video.dispatchEvent(new Event('error'))
    await tick()
    expect(el.textContent).not.toContain('no longer available')
    // The platform is busy for longer than the kit asks again: nothing is said either.
    answer = () => { throw Object.assign(new Error('Too fast — try again in a moment'), { retry_after_ms: 300 }) }
    await tick(3100)
    video.dispatchEvent(new Event('error'))
    for (let i = 0; i < 5; i++) await tick(400)
    expect(el.textContent).not.toContain('no longer available')
    expect(el.querySelector('video')).toBeTruthy()
    expect(errors).toEqual([])
    // The person was removed from the agent: the platform refuses the ask.
    answer = () => { throw new Error("not available: the agent's files are for its members") }
    await tick(3100)
    video.dispatchEvent(new Event('error'))
    await tick()
    expect(el.textContent).toContain('This file is no longer available to you.')
    expect(el.querySelector('video')).toBeNull()
    expect(el.querySelector('.odw-media')?.hasAttribute('data-gone')).toBe(true)
    expect(errors).toEqual([1])
  })

  it('asks again when the platform says to wait, three times at most, and in one call', async () => {
    const { mediaLink } = await import('@/uikit/widgets')
    let refusals = 2
    answer = (args) => {
      if (refusals-- > 0) throw Object.assign(new Error('Too fast — try again in a moment'), { retry_after_ms: 400 })
      return { links: (args.paths as string[]).map((_p, i) => ({ ...link(i), kind: 'image' })) }
    }
    const links = ['a.png', 'b.png'].map((n) => mediaLink(`workspace/m/${n}`))
    await tick()
    expect(asked.length).toBe(1)
    await tick(460)                                      // the wait it was told, then both again in one call
    expect(asked.length).toBe(2)
    expect(asked[1]).toEqual({ paths: ['workspace/m/a.png', 'workspace/m/b.png'] })
    await tick(460)
    expect(links.map((l) => l.url)).toEqual([link(0).url, link(1).url])
    links.forEach((l) => l.release())
    // Told to wait every time: it gives up after three more asks and says why.
    refusals = 99
    const never = mediaLink('workspace/m/c.png')
    const said = never.ready.then(() => 'ok', (e: Error) => e.message)
    for (let i = 0; i < 6; i++) await tick(500)
    expect(await said).toContain('Too fast')
    expect(asked.length).toBe(3 + 4)
    // A refusal with no wait in it (not a member, not a file) is not asked again.
    answer = () => { throw new Error('not a file') }
    const gone = mediaLink('workspace/m/d.png')
    const why = gone.ready.then(() => 'ok', (e: Error) => e.message)
    await tick(2000)
    expect(await why).toBe('not a file')
    expect(asked.length).toBe(3 + 4 + 1)
  })

  it('is version 4', async () => {
    const { api } = await import('@/uikit/widgets')
    expect(api.version).toBe(4)
    expect(typeof api.mediaLink).toBe('function')
  })
})
