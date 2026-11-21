/**
 * The placeholder shown while the image tool works names the model family
 * that runs: GPT Image 2.6 for the `gpt-image` option (the tool sends Flare or
 * Sunburst), Nano Banana Pro otherwise. Both the chat and the artifact window
 * draw it.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import BlockRenderer from '@/components/chat/ChatBlockRenderer'
import ArtifactView from '@/components/chat/artifacts/ArtifactView'

function chat(model: string) {
  return render(
    <BlockRenderer
      block={{ type: 'image_generating', promptPreview: 'a lighthouse', model }}
      blockId="m1-b0"
      blockOrder={0}
      isUserMessage={false}
      agentName="my-agent"
      onPermissionRespond={() => {}}
    />,
  )
}

describe('image_generating label', () => {
  it('names GPT Image 2.6 for the gpt-image option in the chat', () => {
    chat('gpt-image')
    expect(screen.getByText(/GPT Image 2\.6/)).toBeTruthy()
    expect(screen.queryByText(/GPT Image 1\.5/)).toBeNull()
  })

  it('names Nano Banana Pro for the default in the chat', () => {
    chat('nano-banana')
    expect(screen.getByText(/Nano Banana Pro/)).toBeTruthy()
  })

  it('names GPT Image 2.6 in the artifact window', () => {
    render(<ArtifactView block={{ type: 'image_generating', promptPreview: 'a lighthouse', model: 'gpt-image' }} />)
    expect(screen.getByText(/GPT Image 2\.6/)).toBeTruthy()
  })
})
