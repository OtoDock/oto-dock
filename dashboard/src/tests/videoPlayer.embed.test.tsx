import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { Capacitor } from '@capacitor/core'

import VideoPlayer from '@/components/chat/media/VideoPlayer'
import { onAppToast, type AppToast } from '@/lib/appToasts'
import { pushEscHandler } from '@/lib/escStack'
import { pushModalLayer } from '@/lib/ui/modalLayers'

function renderEmbed(src: string) {
  const { container, unmount } = render(<VideoPlayer src={src} />)
  const iframe = container.querySelector('iframe')
  return { iframe, unmount }
}

// YouTube rejects referrer-less embeds with player error 153, and the app-wide
// Referrer-Policy is same-origin (sends no Referer cross-origin) — the iframe
// must carry its own override or every YouTube embed breaks.
describe('VideoPlayer embed iframe', () => {
  it.each([
    ['https://www.youtube.com/watch?v=a3cheXcgX5I', 'https://www.youtube.com/embed/a3cheXcgX5I'],
    ['https://youtu.be/a3cheXcgX5I', 'https://www.youtube.com/embed/a3cheXcgX5I'],
    ['https://vimeo.com/76979871', 'https://player.vimeo.com/video/76979871'],
  ])('embeds %s with a cross-origin referrer policy', (src, embedUrl) => {
    const { iframe, unmount } = renderEmbed(src)
    expect(iframe).not.toBeNull()
    expect(iframe!.getAttribute('src')).toBe(embedUrl)
    expect(iframe!.getAttribute('referrerpolicy')).toBe('strict-origin-when-cross-origin')
    unmount()
  })

  it('direct file URLs render <video>, not an iframe', () => {
    const { container, unmount } = render(
      <VideoPlayer src="https://cdn.example.com/clip.mp4" />,
    )
    expect(container.querySelector('iframe')).toBeNull()
    expect(container.querySelector('video')).not.toBeNull()
    unmount()
  })
})

// A tall clip never gets a frame taller than the screen: the player's column
// carries the pop-up's bound for the screen itself, so the frame shrinks and
// the video letterboxes in the message list and the file preview too.
describe('VideoPlayer in a host that bounds nothing', () => {
  it('bounds its column by the screen, the frame shrinking to fit', () => {
    const { container, unmount } = render(<VideoPlayer src="https://cdn.example.com/tall.mp4" />)
    const frame = container.querySelector('video')!.parentElement!
    const column = frame.parentElement!
    expect(column.className.split(/\s+/)).toEqual(
      expect.arrayContaining(['flex', 'flex-col', 'min-h-0', 'max-h-[min(70dvh,40rem)]']))
    expect(frame.className.split(/\s+/)).toContain('min-h-0')
    unmount()
  })
})

// The full screen button: the frame goes full screen with the player's own
// controls; a refusal is logged and the video's own full screen takes over
// where the browser has one (Safari, the only one an iPhone has). A clip that
// loaded before its element was on the page still gives the player its length.
describe('VideoPlayer full screen', () => {
  const proto = HTMLElement.prototype as { requestFullscreen?: unknown }
  const videoProto = HTMLVideoElement.prototype as {
    webkitEnterFullscreen?: unknown
    webkitSupportsFullscreen?: boolean
  }
  afterEach(() => {
    delete proto.requestFullscreen
    delete videoProto.webkitEnterFullscreen
    delete videoProto.webkitSupportsFullscreen
    vi.restoreAllMocks()
  })
  const clip = () => render(<VideoPlayer src="https://cdn.example.com/clip.mp4" />)
  const press = () => fireEvent.click(screen.getByRole('button', { name: 'Fullscreen' }))

  it('asks the frame, and nothing else when it goes', async () => {
    const asked = vi.fn(function (this: HTMLElement) { return Promise.resolve() })
    proto.requestFullscreen = asked
    const own = vi.fn()
    videoProto.webkitEnterFullscreen = own
    const { container } = clip()
    press()
    await Promise.resolve()
    expect(asked).toHaveBeenCalledTimes(1)
    expect(asked.mock.contexts[0]).toBe(container.querySelector('video')!.parentElement)
    expect(own).not.toHaveBeenCalled()
  })

  it('logs a refusal and falls back to the video\'s own full screen', async () => {
    proto.requestFullscreen = vi.fn(() => Promise.reject(new TypeError('Permissions check failed')))
    const own = vi.fn()
    videoProto.webkitEnterFullscreen = own
    videoProto.webkitSupportsFullscreen = true
    const logged = vi.spyOn(console, 'error').mockImplementation(() => {})
    clip()
    press()
    await waitFor(() => expect(own).toHaveBeenCalledTimes(1))
    expect(logged).toHaveBeenCalledWith('The video could not go full screen:', expect.any(TypeError))
  })

  it('uses the video\'s own full screen where the frame has none (an iPhone)', () => {
    const own = vi.fn()
    videoProto.webkitEnterFullscreen = own
    videoProto.webkitSupportsFullscreen = true
    clip()
    press()
    expect(own).toHaveBeenCalledTimes(1)
  })

  it('reads a clip that loaded before the player was on the page', () => {
    vi.spyOn(HTMLMediaElement.prototype, 'readyState', 'get').mockReturnValue(HTMLMediaElement.HAVE_METADATA)
    vi.spyOn(HTMLMediaElement.prototype, 'duration', 'get').mockReturnValue(125)
    clip()
    expect(screen.getByText('0:00 / 2:05')).toBeTruthy()
  })
})

// What a person sees when the view around the page gives no full screen. An
// Android app whose build lost what its web view looks for answers
// `document.fullscreenEnabled === false` for the page and every frame: there the player's frame fills the page from
// the top layer and the app hides its bars, through the player's one full
// screen state. Everywhere else a full screen that cannot be had says so,
// with "Update the OtoDock app." only in the Android app, where an update is
// what fixes it.
describe('VideoPlayer where the view gives no full screen', () => {
  type Native = { postMessage: ReturnType<typeof vi.fn>; addEventListener: ReturnType<typeof vi.fn> }
  const w = window as unknown as {
    OtoDockNative?: Native
    __otodockVideoFullscreenActive?: boolean
    __otodockVideoFullscreenClose?: () => void
    __otodockDialogClose?: () => void
  }
  const proto = HTMLElement.prototype as {
    requestFullscreen?: unknown
    showPopover?: unknown
    hidePopover?: unknown
  }
  const videoProto = HTMLVideoElement.prototype as {
    webkitEnterFullscreen?: unknown
    webkitSupportsFullscreen?: boolean
  }
  let toasts: AppToast[] = []
  let offToasts = () => {}
  let fullscreenElement: Element | null = null

  // Both apps carry the channel. Which app it is, the page reads from
  // Capacitor's bridge.
  const inApp = (platform: 'android' | 'ios' = 'android'): Native => {
    const native: Native = { postMessage: vi.fn(), addEventListener: vi.fn() }
    w.OtoDockNative = native
    vi.spyOn(Capacitor, 'getPlatform').mockReturnValue(platform)
    return native
  }
  const viewCan = (can: boolean | undefined) =>
    Object.defineProperty(document, 'fullscreenEnabled', { configurable: true, get: () => can })
  const called = (native: Native, method: string) =>
    native.postMessage.mock.calls.filter(([m]) => typeof m === 'string' && m.includes(`"m":"${method}"`)).length
  const setFullscreen = (el: Element | null) => {
    fullscreenElement = el
    act(() => { document.dispatchEvent(new Event('fullscreenchange')) })
  }
  const clip = () => render(<VideoPlayer src="https://cdn.example.com/clip.mp4" />)
  const frameOf = (container: HTMLElement) => container.querySelector('video')!.parentElement!
  // jsdom has no top layer: it reads an element with the popover attribute as
  // not displayed, so the open frame's button is found with `hidden`.
  const press = (i = 0) => fireEvent.click(screen.getAllByRole('button', { name: 'Fullscreen', hidden: true })[i])
  const NO_FULLSCREEN = 'This video could not go full screen.'

  beforeEach(() => {
    toasts = []
    offToasts = onAppToast((t) => { toasts.push(t) })
    fullscreenElement = null
    Object.defineProperty(document, 'fullscreenElement', { configurable: true, get: () => fullscreenElement })
    Object.defineProperty(document, 'exitFullscreen', {
      configurable: true, value: vi.fn(() => { setFullscreen(null); return Promise.resolve() }),
    })
  })
  afterEach(() => {
    offToasts()
    delete w.OtoDockNative
    delete proto.requestFullscreen
    delete proto.showPopover
    delete proto.hidePopover
    delete videoProto.webkitEnterFullscreen
    delete videoProto.webkitSupportsFullscreen
    Reflect.deleteProperty(document, 'fullscreenEnabled')
    Reflect.deleteProperty(document, 'fullscreenElement')
    Reflect.deleteProperty(document, 'exitFullscreen')
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('fills the page in an app whose view cannot, and the button leaves it', () => {
    const native = inApp()
    viewCan(false)
    const asked = vi.fn(() => Promise.reject(new TypeError('Fullscreen is not supported')))
    proto.requestFullscreen = asked
    const shown = vi.fn()
    proto.showPopover = shown
    proto.hidePopover = vi.fn()
    const { container } = clip()
    const frame = frameOf(container)
    expect(frame.hasAttribute('popover')).toBe(false)

    press()
    expect(asked).not.toHaveBeenCalled()
    expect(frame.getAttribute('popover')).toBe('manual')
    expect(shown).toHaveBeenCalledTimes(1)
    expect(shown.mock.contexts[0]).toBe(frame)
    expect(frame.style.position).toBe('fixed')
    expect(frame.style.width).toBe('100%')
    expect(frame.style.height).toBe('100%')
    expect(frame.style.aspectRatio).toBe('')
    expect(called(native, 'enterVideoFullscreen')).toBe(1)
    expect(w.__otodockVideoFullscreenActive).toBe(true)
    expect(toasts).toEqual([])

    press()
    expect(frame.hasAttribute('popover')).toBe(false)
    expect(frame.style.position).toBe('')
    expect(called(native, 'exitVideoFullscreen')).toBe(1)
    expect(w.__otodockVideoFullscreenActive).toBe(false)
  })

  it('leaves the page-level full screen on the app\'s Back and on Escape', () => {
    inApp()
    viewCan(false)
    proto.showPopover = vi.fn()
    proto.hidePopover = vi.fn()
    const { container } = clip()
    const frame = frameOf(container)

    press()
    expect(frame.getAttribute('popover')).toBe('manual')
    act(() => { w.__otodockVideoFullscreenClose?.() })
    expect(frame.hasAttribute('popover')).toBe(false)

    press()
    expect(frame.getAttribute('popover')).toBe('manual')
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(frame.hasAttribute('popover')).toBe(false)
  })

  // An app without its own full screen view asks an open dialog first on Back, and the dialog
  // closes what is open inside it: the full screen goes before the layer
  // that holds the player (a file preview opened from the dialog).
  it('leaves before the layer that holds it when Back closes a dialog\'s top layer', () => {
    inApp()
    viewCan(false)
    proto.showPopover = vi.fn()
    proto.hidePopover = vi.fn()
    const dialog = document.createElement('div')
    document.body.appendChild(dialog)
    const closeDialog = vi.fn()
    const popModal = pushModalLayer({ id: 'video-test', el: dialog, portalRoot: dialog, onEscape: closeDialog })
    const closePreview = vi.fn()
    const popPreview = pushEscHandler(closePreview, dialog)
    const { container, unmount } = render(<VideoPlayer src="https://cdn.example.com/clip.mp4" />, { container: dialog })
    const frame = frameOf(container)

    press()
    expect(frame.getAttribute('popover')).toBe('manual')
    act(() => { w.__otodockDialogClose?.() })
    expect(frame.hasAttribute('popover')).toBe(false)
    expect(closePreview).not.toHaveBeenCalled()
    expect(closeDialog).not.toHaveBeenCalled()

    act(() => { w.__otodockDialogClose?.() })
    expect(closePreview).toHaveBeenCalledTimes(1)
    unmount()
    popPreview()
    popModal()
    dialog.remove()
  })

  it('takes the page-level path only in an app whose view cannot', async () => {
    const shown = vi.fn()
    proto.showPopover = shown
    proto.hidePopover = vi.fn()

    // A browser whose page cannot: no app to hide the bars, so the sentence.
    viewCan(false)
    const first = clip()
    press()
    expect(frameOf(first.container).hasAttribute('popover')).toBe(false)
    expect(toasts).toEqual([{ title: NO_FULLSCREEN, body: '', severity: 'warning' }])
    first.unmount()

    // An app whose view can: the frame's own full screen, as in a browser.
    inApp()
    viewCan(true)
    const asked = vi.fn(() => Promise.resolve())
    proto.requestFullscreen = asked
    const second = clip()
    press()
    await Promise.resolve()
    expect(asked).toHaveBeenCalledTimes(1)
    expect(frameOf(second.container).hasAttribute('popover')).toBe(false)
    expect(shown).not.toHaveBeenCalled()
    second.unmount()

    // The same app once its view cannot: now the page-level one.
    viewCan(false)
    const third = clip()
    press()
    expect(frameOf(third.container).getAttribute('popover')).toBe('manual')
    expect(shown).toHaveBeenCalledTimes(1)
  })

  it('says so in an app whose view cannot when the page cannot fill itself either', () => {
    inApp()
    viewCan(false)
    const { container } = clip()
    press()
    expect(frameOf(container).hasAttribute('popover')).toBe(false)
    expect(toasts).toEqual([{ title: NO_FULLSCREEN, body: 'Update the OtoDock app.', severity: 'warning' }])
  })

  // An iPhone's WebKit without full screen for an element leaves the
  // property out, and its app carries the channel too: the page must never
  // stand in there. The app has its video's own full screen instead.
  it('never fills the page where the view does not say it cannot (the iOS app)', () => {
    inApp('ios')
    viewCan(undefined)
    const shown = vi.fn()
    proto.showPopover = shown
    proto.hidePopover = vi.fn()
    const own = vi.fn()
    videoProto.webkitEnterFullscreen = own
    videoProto.webkitSupportsFullscreen = false
    const first = clip()
    press()
    expect(frameOf(first.container).hasAttribute('popover')).toBe(false)
    expect(shown).not.toHaveBeenCalled()
    expect(own).not.toHaveBeenCalled()
    expect(toasts).toEqual([{ title: NO_FULLSCREEN, body: '', severity: 'warning' }])
    first.unmount()

    // The same press once the clip is ready: the video's own full screen.
    toasts = []
    videoProto.webkitSupportsFullscreen = true
    const second = clip()
    press()
    expect(own).toHaveBeenCalledTimes(1)
    expect(frameOf(second.container).hasAttribute('popover')).toBe(false)
    expect(toasts).toEqual([])
    second.unmount()

    // The stubs are live: the same page with a view that says no is filled.
    videoProto.webkitSupportsFullscreen = false
    viewCan(false)
    const third = clip()
    press()
    expect(frameOf(third.container).getAttribute('popover')).toBe('manual')
    expect(shown).toHaveBeenCalledTimes(1)
  })

  // The second line names the one fault an update fixes: the Android app
  // whose view has no full screen. A refusal for any other reason, and
  // every refusal in the iOS app, says the first line alone.
  it('names the app\'s update only in the Android app whose view has no full screen', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const refused = () => Promise.reject(new TypeError('Permissions check failed'))

    inApp('ios')
    viewCan(true)
    proto.requestFullscreen = vi.fn(refused)
    const first = clip()
    press()
    await waitFor(() => expect(toasts).toEqual([{ title: NO_FULLSCREEN, body: '', severity: 'warning' }]))
    first.unmount()

    toasts = []
    inApp('android')
    const second = clip()
    press()
    await waitFor(() => expect(toasts).toEqual([{ title: NO_FULLSCREEN, body: '', severity: 'warning' }]))
    second.unmount()

    toasts = []
    viewCan(false)
    clip()
    press()
    expect(toasts).toEqual([{ title: NO_FULLSCREEN, body: 'Update the OtoDock app.', severity: 'warning' }])
  })

  it('says so when a refusal has nothing to fall back to, and not when the video\'s own took over', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    proto.requestFullscreen = vi.fn(() => Promise.reject(new TypeError('Permissions check failed')))
    const first = clip()
    press()
    await waitFor(() => expect(toasts).toEqual([{ title: NO_FULLSCREEN, body: '', severity: 'warning' }]))
    first.unmount()

    toasts = []
    const own = vi.fn()
    videoProto.webkitEnterFullscreen = own
    videoProto.webkitSupportsFullscreen = true
    clip()
    press()
    await waitFor(() => expect(own).toHaveBeenCalledTimes(1))
    expect(toasts).toEqual([])
  })

  it('says so where nothing can go full screen, and when the video\'s own throws', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const first = clip()
    press()
    expect(toasts).toEqual([{ title: NO_FULLSCREEN, body: '', severity: 'warning' }])
    first.unmount()

    // An iPhone before the clip's shape is known: its own full screen is not ready.
    toasts = []
    const own = vi.fn()
    videoProto.webkitEnterFullscreen = own
    videoProto.webkitSupportsFullscreen = false
    const second = clip()
    press()
    expect(own).not.toHaveBeenCalled()
    expect(toasts).toHaveLength(1)
    second.unmount()

    toasts = []
    videoProto.webkitSupportsFullscreen = true
    videoProto.webkitEnterFullscreen = vi.fn(() => { throw new DOMException('not ready', 'InvalidStateError') })
    clip()
    press()
    expect(toasts).toHaveLength(1)
  })

  it('says so in an app when its own full screen is ended at once, and only the player that asked', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    inApp()
    viewCan(true)
    proto.requestFullscreen = vi.fn(() => Promise.resolve())
    const { container } = render(
      <>
        <VideoPlayer src="https://cdn.example.com/one.mp4" />
        <VideoPlayer src="https://cdn.example.com/two.mp4" />
      </>,
    )
    const frame = container.querySelectorAll('video')[0].parentElement!

    // Ended by the app around the page 200 ms after it began.
    press(0)
    await Promise.resolve()
    setFullscreen(frame)
    vi.advanceTimersByTime(200)
    setFullscreen(null)
    expect(toasts).toEqual([{ title: NO_FULLSCREEN, body: 'Update the OtoDock app.', severity: 'warning' }])

    // Left by the person after a while: nothing to say.
    toasts = []
    press(0)
    await Promise.resolve()
    setFullscreen(frame)
    vi.advanceTimersByTime(5000)
    setFullscreen(null)
    expect(toasts).toEqual([])

    // Left through the player's own button at once: the person did it.
    press(0)
    await Promise.resolve()
    setFullscreen(frame)
    vi.advanceTimersByTime(200)
    press(0)
    expect(document.fullscreenElement).toBeNull()
    expect(toasts).toEqual([])
  })

  it('never says it in a browser or the iOS app, where only the person ends a full screen', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    viewCan(true)
    proto.requestFullscreen = vi.fn(() => Promise.resolve())
    const { container } = clip()
    const endedAtOnce = async () => {
      press()
      await Promise.resolve()
      setFullscreen(frameOf(container))
      vi.advanceTimersByTime(200)
      setFullscreen(null)
    }
    await endedAtOnce()
    expect(toasts).toEqual([])

    inApp('ios')
    await endedAtOnce()
    expect(toasts).toEqual([])

    // The same end in the Android app is the one that speaks.
    inApp('android')
    await endedAtOnce()
    expect(toasts).toEqual([{ title: NO_FULLSCREEN, body: 'Update the OtoDock app.', severity: 'warning' }])
  })
})
