// The boot screen, before the first paint: the stored theme, the browser's
// theme colour, the field the shell paints and the hand-over. Loaded
// synchronously in the head (the shell's script policy allows no inline
// script), so nothing here waits for the page's own modules. The colours are
// the design tokens' values (surface.page, blue.800); a test holds them to
// design/tokens.json.
(function () {
  var PAGE = { light: '#faf9f9', dark: '#0d1117' }
  var FIELD = '#1d4f80'
  var SPLASH_WAIT_MS = 1500
  var ORBIT_WAIT_MS = 3000
  var LEAVE_MS = 220
  var WATCHDOG_MS = 30000
  var doc = document
  var html = doc.documentElement

  var stored = null
  try { stored = localStorage.getItem('theme') } catch (e) { /* storage refused */ }
  var dark = stored === 'dark' ||
    (stored !== 'light' && window.matchMedia('(prefers-color-scheme: dark)').matches)
  html.classList.toggle('dark', dark)

  // The Android app injects its channel at document start, before any script.
  var app = window.OtoDockNative !== undefined
  html.setAttribute('data-boot', app ? 'app' : 'web')
  // An app older than the boot screen paints the strips behind the bars
  // from the page alone: it gets the field at once, so they match the boot
  // screen from its first paint (the 1.9.0 app paints the field itself and
  // ignores the call).
  if (app) {
    try { window.OtoDockNative.postMessage(JSON.stringify({ m: 'setContainerColor', a: [FIELD] })) } catch (e) { /* no channel */ }
  }
  var meta = doc.querySelector('meta[name="theme-color"]')
  if (meta) meta.setAttribute('content', app ? FIELD : dark ? PAGE.dark : PAGE.light)

  // The orbit runs when a person can see it: in the app once the system
  // splash begins to leave (the app says so), or after a while in an app
  // older than the cue; on the web from the first paint.
  var shown = false
  function show () {
    if (shown) return
    shown = true
    html.classList.add('boot-shown')
  }
  if (app) setTimeout(show, ORBIT_WAIT_MS)
  else show()

  // The end: the page's first real screen is painted (finish). In the app
  // the overlay waits for the splash to be gone, so the two fades never
  // stack, at most SPLASH_WAIT_MS for an app that never says. Then the
  // overlay fades, tells the page (the native theme applies with it) and
  // leaves the document. A page whose script never runs is released by the
  // watchdog, so a failed load is not hidden for ever (the page's own script
  // keeps a shorter one from its start).
  var boot = { done: false, finish: finish }
  var splashGone = !app
  var asked = false
  var splashTimer = null
  function leave () {
    if (boot.done) return
    boot.done = true
    var el = doc.getElementById('boot')
    if (el) el.classList.add('is-done')
    doc.dispatchEvent(new Event('otodock:bootdone'))
    setTimeout(function () {
      if (el) el.remove()
      html.removeAttribute('data-boot')
    }, LEAVE_MS)
  }
  function finish () {
    if (asked) return
    asked = true
    if (splashGone) leave()
    else splashTimer = setTimeout(leave, SPLASH_WAIT_MS)
  }
  function splash (state) {
    if (state === 'leaving') show()
    if (state !== 'gone') return
    splashGone = true
    show()
    if (asked) {
      clearTimeout(splashTimer)
      leave()
    }
  }
  setTimeout(leave, WATCHDOG_MS)

  window.__otodockSplash = splash
  window.__otodockBoot = boot
})()
