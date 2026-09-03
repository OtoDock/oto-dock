import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// Data layer + auth are fully mocked — the editor is a pure view over the
// hooks, so the tests pin gating (role / can_edit), the structural-save
// contract (kept ids preserved, '' for new levels) and the inline confirm
// step for member-carrying level removals.
const h = vi.hoisted(() => ({
  role: 'member' as string,
  departments: [] as any[],
  createMutate: vi.fn(),
  updateMutate: vi.fn(),
  deleteMutate: vi.fn(),
  setLevelsMutate: vi.fn(),
}))

vi.mock('@/api/departments', () => ({
  useDepartments: () => ({ data: h.departments, isLoading: false }),
  useCreateDepartment: () => ({ mutate: h.createMutate, isPending: false, error: null }),
  useUpdateDepartment: () => ({ mutate: h.updateMutate, isPending: false, error: null }),
  useDeleteDepartment: () => ({ mutate: h.deleteMutate, isPending: false, error: null }),
  useSetDepartmentLevels: () => ({ mutate: h.setLevelsMutate, isPending: false, error: null }),
}))

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { role: h.role, sub: 'u1' } }),
}))

import DepartmentsEditor from '@/components/departments/DepartmentsEditor'

function makeDept(overrides: Record<string, any> = {}) {
  return {
    id: 'd1',
    name: 'Engineering',
    created_by_sub: 'u1',
    auto_delegation: true,
    reach: 'adjacent',
    position_hint: '',
    levels: [
      { id: 'l1', rank: 0, name: 'Head' },
      { id: 'l2', rank: 1, name: 'Senior' },
      { id: 'l3', rank: 2, name: 'Junior' },
    ],
    members: [
      { name: 'alice', display_name: 'Alice', color: '#3B82F6', description: '', level_id: 'l1', accessible: true },
      { name: 'bob', display_name: 'Bob', color: '#22C55E', description: '', level_id: 'l2', accessible: false },
    ],
    can_edit: true,
    ...overrides,
  }
}

function renderEditor() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <DepartmentsEditor />
    </QueryClientProvider>,
  )
}

describe('DepartmentsEditor', () => {
  beforeEach(() => {
    h.role = 'member'
    h.departments = []
    h.createMutate.mockClear()
    h.updateMutate.mockClear()
    h.deleteMutate.mockClear()
    h.setLevelsMutate.mockClear()
  })

  it('renders a department pill that expands to levels and member chips', () => {
    h.departments = [makeDept({ can_edit: false })]
    renderEditor()

    // Collapsed pill by default: summary line only, body hidden.
    expect(screen.getByText('Engineering')).toBeInTheDocument()
    expect(screen.getByText('2 agents · 3 levels · adjacent reach')).toBeInTheDocument()
    expect(screen.queryByText('Alice')).toBeNull()
    fireEvent.click(screen.getByText('Engineering'))
    // Level names appear in the (read-only) levels list; Head/Senior also
    // appear as member group labels — hence getAllByText.
    for (const lvl of ['Head', 'Senior', 'Junior']) {
      expect(screen.getAllByText(lvl).length).toBeGreaterThan(0)
    }
    expect(screen.getByText('Alice')).toBeInTheDocument()
    // accessible=false gets the reduced-opacity treatment.
    const bob = screen.getByText('Bob')
    expect(bob.className).toContain('opacity-50')
    expect(
      screen.getByText("Assignment lives in each agent's settings (Department field)."),
    ).toBeInTheDocument()
  })

  it('hides the create card for members and shows it for creators', () => {
    h.role = 'member'
    const { unmount } = renderEditor()
    expect(screen.queryByText('New department')).toBeNull()
    unmount()

    h.role = 'creator'
    renderEditor()
    expect(screen.getByText('New department')).toBeInTheDocument()
  })

  it('submits the create form with name, levels, reach and auto-delegation', () => {
    h.role = 'creator'
    renderEditor()
    // The form hides behind the New department button (round 17).
    expect(screen.queryByLabelText('New department name')).toBeNull()
    fireEvent.click(screen.getByText('New department'))
    fireEvent.change(screen.getByLabelText('New department name'), {
      target: { value: 'Ops' },
    })
    fireEvent.click(screen.getByText('Create department'))
    expect(h.createMutate).toHaveBeenCalledWith(
      {
        name: 'Ops',
        auto_delegation: true,
        reach: 'adjacent',
        levels: ['Head', 'Senior', 'Junior'],
      },
      expect.anything(),
    )
  })

  it('can_edit=false renders read-only: no delete, disabled controls, no level editing', () => {
    h.departments = [makeDept({ can_edit: false })]
    renderEditor()

    // Collapsed by default — the body (and its switch) only exists expanded.
    expect(screen.queryByRole('switch')).toBeNull()
    fireEvent.click(screen.getByText('Engineering'))
    expect(screen.getByText('View only')).toBeInTheDocument()
    expect(screen.queryByText('Delete department')).toBeNull()
    expect(screen.getByRole('switch')).toBeDisabled()
    expect(screen.getByLabelText('Delegation reach')).toBeDisabled()
    expect(screen.queryByText('Save levels')).toBeNull()
    expect(screen.queryByText('+ Add level')).toBeNull()
    // Name is plain text, not an editable input.
    expect(screen.queryByLabelText('Department name')).toBeNull()
  })

  it('shows an inline confirm before saving a removal of a level that has members', () => {
    h.departments = [makeDept()]
    renderEditor()
    fireEvent.click(screen.getByText('Engineering'))

    // Remove "Senior" (index 1) — Bob is assigned to it.
    fireEvent.click(screen.getAllByLabelText('Remove level')[1])
    fireEvent.click(screen.getByText('Save levels'))

    // No mutation yet — the confirm step gates it.
    expect(h.setLevelsMutate).not.toHaveBeenCalled()
    const warning = screen.getByText(/This removes level/)
    expect(warning).toHaveTextContent('Senior')
    expect(warning).toHaveTextContent('1 agent')

    fireEvent.click(screen.getByText('Remove and save'))
    expect(h.setLevelsMutate).toHaveBeenCalledWith(
      {
        id: 'd1',
        levels: [
          { id: 'l1', name: 'Head' },
          { id: 'l3', name: 'Junior' },
        ],
      },
      expect.anything(),
    )
  })

  it('cancel on the confirm step saves nothing', () => {
    h.departments = [makeDept()]
    renderEditor()
    fireEvent.click(screen.getByText('Engineering'))
    fireEvent.click(screen.getAllByLabelText('Remove level')[1])
    fireEvent.click(screen.getByText('Save levels'))
    fireEvent.click(screen.getByText('Cancel'))
    expect(h.setLevelsMutate).not.toHaveBeenCalled()
    expect(screen.queryByText(/This removes level/)).toBeNull()
  })

  it('save levels preserves kept ids and sends new levels with an empty id', () => {
    h.departments = [makeDept()]
    renderEditor()
    fireEvent.click(screen.getByText('Engineering'))

    fireEvent.click(screen.getByText('+ Add level'))
    fireEvent.click(screen.getByText('Save levels'))

    expect(h.setLevelsMutate).toHaveBeenCalledWith(
      {
        id: 'd1',
        levels: [
          { id: 'l1', name: 'Head' },
          { id: 'l2', name: 'Senior' },
          { id: 'l3', name: 'Junior' },
          { id: '', name: 'New level' },
        ],
      },
      expect.anything(),
    )
  })

  it('shows the empty state, with a pointer to the create button for creators', () => {
    h.role = 'creator'
    renderEditor()
    expect(screen.getByText('No departments yet')).toBeInTheDocument()
    expect(
      screen.getByText('Create your first one with the New department button.'),
    ).toBeInTheDocument()
  })
})
