import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'

// Speech capability rides react-query; not what this test is about.
vi.mock('@/hooks/useSpeechSession', () => ({
  useSpeechSession: () => ({
    available: false, status: 'idle',
    start: vi.fn(), stop: vi.fn(), toggle: vi.fn(),
  }),
}))

import ChatInput from '@/components/chat/ChatInput'

// The composer's bottom gap is a CSS utility (pb-composer-safe →
// --composer-pb) rather than an inline style: jsdom's CSSOM silently drops
// max(...env(...)) values, so the class name is the testable contract — and
// the PresenceHalo canvas sizes its bottom overhang from the same var, so
// renaming/removing the class would silently break the halo geometry too.
describe('ChatInput composer spacing', () => {
  it('wrapper carries pb-composer-safe (not a hardcoded pb-*)', () => {
    const { container } = render(
      <ChatInput
        value=""
        onChange={() => {}}
        onSend={() => {}}
        pendingImages={[]}
        onAddImages={() => {}}
        onRemoveImage={() => {}}
        pendingFiles={[]}
        onAddFiles={() => {}}
        onRemoveFile={() => {}}
      />,
    )
    const wrapper = container.firstElementChild as HTMLElement
    expect(wrapper.className).toContain('pb-composer-safe')
    expect(wrapper.className).not.toMatch(/\bpb-\d/)
  })

  // The composer keeps its size on every device: the viewport's zoom cap,
  // not a 16 px field, stops iOS zooming into it (lib/device/viewport.ts).
  it('the textarea keeps its size on a touch device', () => {
    const { container } = render(
      <ChatInput
        value=""
        onChange={() => {}}
        onSend={() => {}}
        pendingImages={[]}
        onAddImages={() => {}}
        onRemoveImage={() => {}}
        pendingFiles={[]}
        onAddFiles={() => {}}
        onRemoveFile={() => {}}
      />,
    )
    const box = container.querySelector('textarea') as HTMLTextAreaElement
    expect(box.className).not.toContain('pointer-coarse')
    expect(box.className).not.toMatch(/(^|\s)text-base(\s|$)/)
    expect(box.className).toMatch(/(^|\s)text-sm(\s|$)/)
  })
})
