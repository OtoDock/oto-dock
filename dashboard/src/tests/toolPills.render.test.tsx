import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import ToolActivity from '@/components/chat/ToolActivity'
import SubagentInfo from '@/components/chat/SubagentInfo'
import BgCommandInfo from '@/components/chat/BgCommandInfo'
import DelegateTaskInfo, { DelegateResultFiles, resultFileLabel } from '@/components/chat/DelegateTaskInfo'
import { ChatFileProvider } from '@/components/chat/ChatFileContext'

// The files list opens through the DELEGATING chat's resolve-path; the
// preview surface is mocked to what it receives.
const resolveMock = vi.hoisted(() => vi.fn())
vi.mock('@/api/chats', async (orig) => ({
  ...(await orig<typeof import('@/api/chats')>()),
  resolveChatPath: (...args: unknown[]) => resolveMock(...args),
}))
vi.mock('@/components/chat/ChatFilePreview', () => ({
  default: ({ resolved }: { resolved: { agent: string; path: string } }) => (
    <div data-testid="chat-file-preview" data-agent={resolved.agent} data-path={resolved.path} />
  ),
}))

describe('ToolActivity — the expanded input', () => {
  function expand(name: string, toolInput: Record<string, unknown>) {
    const { container } = render(<ToolActivity name={name} status="done" toolInput={toolInput} />)
    fireEvent.click(container.firstElementChild!.firstElementChild!)
    return container.textContent || ''
  }

  it('shows what a notebook edit, a multi-edit and a workflow carry, not just their path or name', () => {
    expect(expand('NotebookEdit', { notebook_path: '/w/a.ipynb', cell_id: 'c1', new_source: 'print(42)' }))
      .toContain('print(42)')
    expect(expand('MultiEdit', { file_path: '/w/b.py', edits: [{ old_string: 'OLDX', new_string: 'NEWX' }] }))
      .toContain('NEWX')
    expect(expand('Workflow', { script: "name: 'sweep'\nsteps: []", args: { target: 'ARGX' } }))
      .toContain('ARGX')
  })

  it('keeps the one-line body when the path or the name is all there is', () => {
    const text = expand('Delete', { file_path: '/w/gone.txt' })
    expect(text).toContain('/w/gone.txt')
    expect(text).not.toContain('{')
    expect(expand('Skill', { name: 'pdf' })).not.toContain('{')
  })
})

describe('ToolActivity — Bash pill', () => {
  it('collapsed shows the description; expanding reveals the command', () => {
    render(
      <ToolActivity
        name="Bash"
        summary="grep -rn foo proxy/ | head"
        status="done"
        toolInput={{ command: 'grep -rn foo proxy/ | head', description: 'Search for foo in proxy' }}
        resultSummary="ok"
      />,
    )
    expect(screen.getByText('Search for foo in proxy')).toBeTruthy()
    expect(screen.queryByText('grep -rn foo proxy/ | head')).toBeNull()
    fireEvent.click(screen.getByText('Search for foo in proxy'))
    expect(screen.getByText('grep -rn foo proxy/ | head')).toBeTruthy()
  })

  it('expanding reveals the Output section when toolResult is present (codex parity)', () => {
    render(
      <ToolActivity
        name="Bash"
        status="done"
        toolInput={{ command: 'printf alpha; printf beta' }}
        toolResult={'alpha\nbeta'}
        resultSummary="2 lines"
      />,
    )
    expect(screen.getByText('2 lines')).toBeTruthy()
    expect(screen.queryByText('Output')).toBeNull()
    fireEvent.click(screen.getByText('printf alpha; printf beta', { selector: 'span' }))
    expect(screen.getByText('Output')).toBeTruthy()
    expect(screen.getByText(/alpha\s*beta/)).toBeTruthy()
  })

  it('expanding un-truncates the description title; the command fallback stays clipped', () => {
    const { rerender } = render(
      <ToolActivity
        name="Bash"
        status="done"
        toolInput={{ command: 'ls', description: 'A long description of what this does' }}
      />,
    )
    const title = () => screen.getByText('A long description of what this does')
    expect(title().className).toContain('truncate')
    fireEvent.click(title())
    expect(title().className).toContain('whitespace-normal')
    expect(title().className).not.toContain('truncate')
    // No description (e.g. Codex): the title IS the command — expanding keeps
    // it clipped, the body already shows the command verbatim.
    rerender(<ToolActivity name="Bash" status="done" toolInput={{ command: 'pwd && date' }} />)
    const cmdTitle = screen.getByText('pwd && date', { selector: 'span' })
    expect(cmdTitle.className).toContain('truncate')
    fireEvent.click(cmdTitle)
    expect(cmdTitle.className).toContain('truncate')
  })
})

describe('SubagentInfo — expandable agent pill', () => {
  it('expands to the prompt and the foreground report', () => {
    render(
      <SubagentInfo
        description="map the satellite code"
        subagentType="Explore"
        isActive={false}
        toolInput={{ prompt: 'Explore the repo at /x and report the hook flow.' }}
        toolResult="The hook flow is: settings.json → permission_gate.py → proxy."
      />,
    )
    expect(screen.queryByText(/Explore the repo at/)).toBeNull()
    fireEvent.click(screen.getByText('map the satellite code'))
    expect(screen.getByText(/Explore the repo at/)).toBeTruthy()
    expect(screen.getByText(/The hook flow is/)).toBeTruthy()
  })

  it('stays a plain pill when there is nothing to expand (old rows)', () => {
    const { container } = render(
      <SubagentInfo description="legacy row" subagentType="general-purpose" isActive={false} />,
    )
    expect(container.querySelector('.cursor-pointer')).toBeNull()
  })
})

describe('BgCommandInfo — merged background-command pill', () => {
  it('shows the description collapsed and the paired command + output expanded', () => {
    render(
      <BgCommandInfo
        command="npm run build"
        description="Build the dashboard"
        isActive={false}
        toolInput={{ command: 'npm run build', run_in_background: true }}
        toolResult="Command running in background with ID bash_7"
      />,
    )
    expect(screen.getByText('Build the dashboard')).toBeTruthy()
    expect(screen.queryByText('npm run build')).toBeNull()
    fireEvent.click(screen.getByText('Build the dashboard'))
    expect(screen.getByText('npm run build')).toBeTruthy()
    expect(screen.getByText(/bash_7/)).toBeTruthy()
  })

  it('old-proxy rows (command == description twin) expand to the PAIRED command, never the description again', () => {
    render(
      <BgCommandInfo
        command="Build the dashboard"
        description="Build the dashboard"
        isActive={false}
        toolInput={{ command: 'npm run build', run_in_background: true }}
      />,
    )
    fireEvent.click(screen.getByText('Build the dashboard'))
    expect(screen.getByText('npm run build')).toBeTruthy()
    // exactly one occurrence — the collapsed label; not repeated in the body
    expect(screen.getAllByText('Build the dashboard')).toHaveLength(1)
  })

  it('an unpaired old-proxy row has nothing real to expand to', () => {
    const { container } = render(
      <BgCommandInfo
        command="Build the dashboard"
        description="Build the dashboard"
        isActive={false}
      />,
    )
    expect(container.querySelector('.cursor-pointer')).toBeNull()
  })

  it('the bash badge is crush-proof — flex cannot wrap it mid-word on narrow screens', () => {
    render(
      <BgCommandInfo command="sleep 10" description="A long description that squeezes the row" isActive />,
    )
    expect(screen.getByText('bash').className).toContain('shrink-0')
  })
})

describe('DelegateTaskInfo — expandable delegate pill', () => {
  it('expands to the full prompt when present', () => {
    render(
      <DelegateTaskInfo
        taskName="triage inbox"
        agent="support-bot"
        promptPreview="Please triage…"
        status="completed"
        prompt="Please triage the attached report and summarize the top issues."
      />,
    )
    expect(screen.queryByText(/summarize the top issues/)).toBeNull()
    fireEvent.click(screen.getByText('triage inbox'))
    expect(screen.getByText(/summarize the top issues/)).toBeTruthy()
  })

  it('is not expandable on preview-only legacy rows', () => {
    const { container } = render(
      <DelegateTaskInfo taskName="old row" agent="a" promptPreview="short…" status="completed" />,
    )
    expect(container.querySelector('.cursor-pointer')).toBeNull()
  })

  it('renders the agent badge on both breakpoint rows, crush-proof', () => {
    render(
      <DelegateTaskInfo taskName="lane" agent="support-bot" promptPreview="p…" status="completed" />,
    )
    const badges = screen.getAllByText('support-bot')
    expect(badges).toHaveLength(2) // sm+ inline + mobile row
    for (const b of badges) expect(b.className).toContain('shrink-0')
  })
})

describe('DelegateResultFiles — the files a worker attached', () => {
  const FILES = [
    { path: 'users/alice/workspace/inbox/content-creator/report.md', bytes: 12600 },
    { path: 'workspace/inbox/content-creator/data/a.csv', bytes: 300 },
  ]
  const SKIPPED = [{ path: 'notes/x.md', reason: 'symlink' }]

  it('labels a landed path relative to the chat workspace, as the note does', () => {
    expect(resultFileLabel('users/alice/workspace/inbox/w/report.md')).toBe('inbox/w/report.md')
    expect(resultFileLabel('workspace/inbox/w/data/a.csv')).toBe('inbox/w/data/a.csv')
    expect(resultFileLabel('users/alice/workspace/reports/q3.md')).toBe('reports/q3.md')
    expect(resultFileLabel('knowledge/x.md')).toBe('knowledge/x.md')
  })

  it('opens a file through the delegating chat, never the bubble agent', async () => {
    resolveMock.mockReset()
    resolveMock.mockResolvedValue({
      agent: 'head', path: FILES[0].path, filename: 'report.md', size: 12600, previewable: false,
    })
    render(
      <ChatFileProvider chatId="chat-head" agent="head">
        <DelegateResultFiles files={FILES} skipped={SKIPPED} />
      </ChatFileProvider>,
    )
    expect(screen.getByText('inbox/content-creator/report.md')).toBeTruthy()
    expect(screen.getByText('12.3 KB')).toBeTruthy()
    expect(screen.getByText('notes/x.md')).toBeTruthy()
    expect(screen.getByText('symlink')).toBeTruthy()
    fireEvent.click(screen.getAllByText('Open ↗')[0])
    await waitFor(() => expect(screen.getByTestId('chat-file-preview')).toBeTruthy())
    expect(resolveMock).toHaveBeenCalledWith('chat-head', FILES[0].path)
    expect(screen.getByTestId('chat-file-preview').dataset.agent).toBe('head')
  })

  it('shows a transient not-found when the chat cannot resolve the path', async () => {
    resolveMock.mockReset()
    resolveMock.mockResolvedValue(null)
    render(
      <ChatFileProvider chatId="chat-other" agent="head">
        <DelegateResultFiles files={[FILES[1]]} skipped={[]} />
      </ChatFileProvider>,
    )
    fireEvent.click(screen.getByText('Open ↗'))
    await waitFor(() => expect(screen.getByText('not found')).toBeTruthy())
    expect(screen.queryByTestId('chat-file-preview')).toBeNull()
  })

  it('is inert text without a chat context', () => {
    render(<DelegateResultFiles files={FILES} skipped={[]} />)
    expect(screen.getByText('inbox/content-creator/report.md')).toBeTruthy()
    expect(screen.queryByText('Open ↗')).toBeNull()
  })
})
