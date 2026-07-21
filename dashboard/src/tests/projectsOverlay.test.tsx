import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// The overlay reads the lane graph and the board file through apiFetch; the
// mock serves both from a per-test route table.
const routes = new Map<string, unknown>()
vi.mock('@/api/auth', () => ({
  apiFetch: vi.fn(async (path: string) => {
    for (const [prefix, body] of routes) {
      if (path === prefix || path.startsWith(prefix)) {
        return { ok: true, json: async () => body }
      }
    }
    return { ok: false, json: async () => ({}) }
  }),
}))

// The pinned-dashboard panels render sandboxed AppFrames — stub them so
// these tests stay about composition, not iframe plumbing.
vi.mock('@/components/apps/AppFrame', () => ({
  default: ({ app }: { app: { slug: string } }) => (
    <div data-testid="app-frame">{app.slug}</div>
  ),
}))

import ProjectsOverlay from '@/components/projects/ProjectsOverlay'
import { useChatStore } from '@/store/chatStore'
import type { ChatPins, PinnedApp } from '@/api/apps'

const GRAPH = {
  project_id: 'p1',
  chats: [
    { id: 'orch1', title: 'Orchestrator', agent: 'dev', delegate_role: 'orchestrator', parent_chat_id: '', status: 'idle', updated_at: '' },
    { id: 'lane1', title: 'Lane One', agent: 'dev', delegate_role: 'worker', parent_chat_id: 'orch1', status: 'idle', updated_at: '' },
    { id: 'lane2', title: 'Lane Two', agent: 'dev', delegate_role: 'worker', parent_chat_id: 'orch1', status: 'idle', updated_at: '' },
  ],
}

const BOARD_TREE = {
  tree: [{
    name: 'projects', path: 'workspace/projects', type: 'dir',
    children: [{
      name: 'p1', path: 'workspace/projects/p1', type: 'dir',
      children: [{ name: 'board.md', path: 'workspace/projects/p1/board.md', type: 'file' }],
    }],
  }],
}

const BOARD_MD = '# P1\n\n## Lanes\n- [ ] Lane One — Lane One — running\n'

function pin(slug: string, over: Partial<PinnedApp> = {}): PinnedApp {
  return {
    id: `id-${slug}`, slug, title: slug, scope: 'shared', position: 0,
    rel_path: `workspace/apps/${slug}.html`, updated_at: '', actions: [],
    actions_sig: '', actions_approved: true, approval_stale: false,
    can_approve: true, can_manage: true, agent: 'dev', ...over,
  }
}

/** A complete ChatSlice — the store's persist partialize walks every field. */
function slice(chatId: string, status: 'streaming' | 'ready') {
  return {
    chatId, status, agent: 'dev', executionPath: '', executionTarget: '',
    fallbackReason: null, targetMismatch: null, warmupStartedAt: null,
    warmupError: null, lastEventAt: 0, draftInput: '', queuedMessages: [],
    pendingImages: [], pendingFiles: [],
  }
}

function renderOverlay(opts: { isProjectChat?: boolean; pins?: ChatPins; chatId?: string } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ProjectsOverlay
        agent="dev"
        chatId={opts.chatId ?? 'orch1'}
        isProjectChat={opts.isProjectChat ?? true}
        pins={opts.pins}
        onSelectChat={() => {}}
        onClose={() => {}}
      />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  routes.clear()
  vi.clearAllMocks()
  useChatStore.setState({ byChat: {} })
})

describe('ProjectsOverlay — lanes without a board file', () => {
  it('no board → plain "lane" rows, no board-drift copy or dashed border', async () => {
    routes.set('/v1/chats/orch1/project', GRAPH)
    routes.set('/v1/agents/dev/files', { tree: [] })
    renderOverlay()
    expect(await screen.findAllByText('lane · dev')).toHaveLength(2)
    expect(screen.queryByText(/not on the board yet/)).toBeNull()
    expect(document.querySelector('.border-dashed')).toBeNull()
  })

  it('board present → live cards stay on top; board rows in their own section', async () => {
    routes.set('/v1/chats/orch1/project', GRAPH)
    routes.set('/v1/agents/dev/files/', { content: BOARD_MD })
    routes.set('/v1/agents/dev/files', BOARD_TREE)
    renderOverlay()
    // The board block renders as its own section below the live cards…
    expect(await screen.findByText('Board')).toBeTruthy()
    // …and every live chat still renders as a full lane card — board rows
    // never interleave with or replace them (operator ask, 2026-07-12).
    expect(screen.getAllByText('lane · dev')).toHaveLength(2)
    expect(screen.queryByText(/not on the board yet/)).toBeNull()
    expect(document.querySelector('.border-dashed')).toBeNull()
    // The board's own lane row: static plan view with its board-file status.
    expect(screen.getAllByText('Lane One').length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText('running')).toBeTruthy()
  })
})

describe('Dock — panel composition', () => {
  it('project pin → app panel beside the lanes', async () => {
    routes.set('/v1/chats/orch1/project', GRAPH)
    routes.set('/v1/agents/dev/files', { tree: [] })
    renderOverlay({ pins: { chat: null, project: pin('proj-dash') } })
    expect(await screen.findByTestId('dock-project-app')).toBeTruthy()
    expect(screen.getByText('proj-dash')).toBeTruthy()       // the framed app
    expect(await screen.findAllByText('lane · dev')).toHaveLength(2) // lanes still there
  })

  it('chat pin on a project chat → rides below the lanes', async () => {
    routes.set('/v1/chats/orch1/project', GRAPH)
    routes.set('/v1/agents/dev/files', { tree: [] })
    renderOverlay({ pins: { chat: pin('chat-dash'), project: null } })
    expect(await screen.findByTestId('dock-chat-app')).toBeTruthy()
    expect(screen.queryByTestId('dock-project-app')).toBeNull()
  })

  it('non-project chat with a chat pin → the app IS the overlay, no lane fetch', async () => {
    renderOverlay({ isProjectChat: false, pins: { chat: pin('notes'), project: null } })
    expect(await screen.findByTestId('dock-chat-app')).toBeTruthy()
    expect(screen.queryByText('Lanes')).toBeNull()
    const { apiFetch } = await import('@/api/auth')
    expect(vi.mocked(apiFetch)).not.toHaveBeenCalled()
  })

  it('pending approval → the approval card renders over the frame', async () => {
    renderOverlay({
      isProjectChat: false,
      pins: {
        chat: pin('notes', {
          actions: [{ id: 'go', label: 'Go', type: 'send_prompt', prompt: 'hi' }],
          actions_approved: false,
        }),
        project: null,
      },
    })
    expect(await screen.findByText(/review before they work/)).toBeTruthy()
    expect(screen.getByText('Approve actions')).toBeTruthy()
  })
})

describe('Dock — round scoping (reused project slug)', () => {
  // Two delegation rounds under one project id: the anchor's round renders
  // as the live cards; the other round collapses into the disclosure.
  const MULTI = {
    project_id: 'p1',
    chats: [
      { id: 'orch1', title: 'Round One', agent: 'dev', delegate_role: 'orchestrator', parent_chat_id: '', status: 'idle', updated_at: '' },
      { id: 'old1', title: 'Old Lane', agent: 'dev', delegate_role: 'worker', parent_chat_id: 'orch0', status: 'idle', updated_at: '' },
      { id: 'orch0', title: 'Round Zero', agent: 'dev', delegate_role: 'orchestrator', parent_chat_id: '', status: 'idle', updated_at: '' },
      { id: 'lane1', title: 'Lane One', agent: 'dev', delegate_role: 'worker', parent_chat_id: 'orch1', status: 'idle', updated_at: '' },
    ],
  }

  it("live cards show only the anchor's round; other rounds collapse", async () => {
    routes.set('/v1/chats/orch1/project', MULTI)
    routes.set('/v1/agents/dev/files', { tree: [] })
    renderOverlay()
    // Current round: the anchor's own orchestrator card + its one lane.
    expect(await screen.findByText('Round One')).toBeTruthy()
    expect(screen.getByText('Lane One').closest('details')).toBeNull()
    // The foreign round is present but inside the collapsed disclosure —
    // its orchestrator labeled as such.
    expect(screen.getByText('Other project lanes (2)')).toBeTruthy()
    expect(screen.getByText('Old Lane').closest('details')).toBeTruthy()
    expect(screen.getByText('Round Zero').closest('details')).toBeTruthy()
    // Two orchestrator labels: the anchor's amber card + Round Zero's
    // collapsed row — exactly one of them inside the disclosure.
    const orchLabels = screen.getAllByText('orchestrator · dev')
    expect(orchLabels).toHaveLength(2)
    expect(orchLabels.filter((el) => el.closest('details'))).toHaveLength(1)
  })

  it('a worker anchor resolves the same round through its parent', async () => {
    routes.set('/v1/chats/lane1/project', MULTI)
    routes.set('/v1/agents/dev/files', { tree: [] })
    renderOverlay({ chatId: 'lane1' })
    expect(await screen.findByText('Round One')).toBeTruthy()
    expect(screen.getByText('Lane One').closest('details')).toBeNull()
    expect(screen.getByText('Old Lane').closest('details')).toBeTruthy()
  })

  it('a still-running lane from another round stays among the live cards', async () => {
    routes.set('/v1/chats/orch1/project', {
      project_id: 'p1',
      chats: MULTI.chats.map((c) => (c.id === 'old1' ? { ...c, status: 'generating' } : c)),
    })
    routes.set('/v1/agents/dev/files', { tree: [] })
    renderOverlay()
    // Live beats round: the generating old lane renders as a live card…
    expect(await screen.findByText('Old Lane')).toBeTruthy()
    expect(screen.getByText('Old Lane').closest('details')).toBeNull()
    // …while its idle orchestrator stays collapsed.
    expect(screen.getByText('Round Zero').closest('details')).toBeTruthy()
    expect(screen.getByText('Other project lanes (1)')).toBeTruthy()
  })
})

describe('Dock — lane liveness from chatStore', () => {
  it('a streaming slice pulses the lane instantly; ready retires a stale poll', async () => {
    routes.set('/v1/chats/orch1/project', {
      project_id: 'p1',
      chats: [
        { id: 'lane1', title: 'Lane One', agent: 'dev', delegate_role: 'worker', status: 'idle', updated_at: '' },
        { id: 'lane2', title: 'Lane Two', agent: 'dev', delegate_role: 'worker', status: 'generating', updated_at: '' },
      ],
    })
    routes.set('/v1/agents/dev/files', { tree: [] })
    useChatStore.setState({
      byChat: {
        lane1: slice('lane1', 'streaming'),
        lane2: slice('lane2', 'ready'),
      },
    })
    renderOverlay()
    // lane1: poll says idle, WS says streaming → generating pulse now.
    expect(await screen.findAllByText('generating')).toHaveLength(1)
    // lane2: poll still says generating, WS already closed the turn → idle.
    expect(screen.getAllByText('idle')).toHaveLength(1)
  })
})
