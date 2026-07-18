import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

// ─── Preview origin-mismatch banner: a wopiUrl on a different origin can
//     never load (frame-ancestors), so the pane must explain the fix
//     (DASHBOARD_PUBLIC_URL) instead of rendering a dead iframe. ─────────────

vi.mock('@/hooks/useCollaboraLiveReload', () => ({
  useCollaboraLiveReload: () => ({ iframeRef: { current: null }, reloadAvailable: false, doReload: () => {}, modifiedRef: { current: false } }),
}))

import DocumentPreview from '@/components/chat/media/DocumentPreview'

function renderPreview(wopiUrl: string) {
  return render(
    <DocumentPreview
      wopiUrl={wopiUrl}
      filename="t2-report.docx"
      fileId="f1"
      downloadUrl="/v1/media/f1"
    />,
  )
}

describe('DocumentPreview — origin mismatch', () => {
  it('foreign-origin preview URL renders the fix-it notice, not an iframe', () => {
    renderPreview('http://localhost:8410/collabora/browser/dist/cool.html?WOPISrc=x')
    expect(screen.getByText(/can't load from this address/)).toBeInTheDocument()
    expect(screen.getByText(new RegExp(`DASHBOARD_PUBLIC_URL=${window.location.origin}`))).toBeInTheDocument()
    expect(document.querySelector('iframe')).toBeNull()
  })

  it('same-origin preview URL renders the iframe, no notice', () => {
    renderPreview(`${window.location.origin}/collabora/browser/dist/cool.html?WOPISrc=x`)
    expect(screen.queryByText(/can't load from this address/)).toBeNull()
    expect(document.querySelector('iframe')).toBeTruthy()
  })
})
