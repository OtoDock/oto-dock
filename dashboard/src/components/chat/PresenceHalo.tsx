// PresenceHalo — the composer IS the presence (Stage Mode Phase B).
//
// An audio-reactive glow around the chat input pill while a full-duplex
// voice session is live, driven by REAL signal: the mic analyser while the
// user speaks, the TTS output analyser while the agent replies (both via
// useDuplexVoice.getLevels — ref reads, polled from this component's own
// rAF loop, never through React state). The duplex phase is the grammar:
//   connecting → faint ember breathing
//   listening  → cool cyan breathing + mic-reactive glow + border sparks
//   thinking   → violet rotating shimmer
//   speaking   → warm waveform-reactive glow + border sparks at peaks
// No separate stage UI: off/error renders nothing and the composer is
// exactly what it is today.
//
// Geometry: a pointer-events-none canvas around the pill — up and sideways
// it extends past the pill to bloom over the message area; downward it
// extends by exactly var(--composer-pb) so it ends ON the viewport edge
// (any overshoot would resurrect the document scrollbar; sideways overflow
// is clipped by overflow-x-clip on the chat root). On mobile the viewport
// clips the bottom bloom → the Siri-style underglow, by construction.
//
// ANDROID WEBVIEW HAZARDS (operator-verified, 2026-08-11 — a white slab
// over the chat, twice): the accelerated 2D canvas corrupts alpha around
// `destination-out` erases (v1), and CSS mask-image on composited layers
// draws white-box artifacts (v2). So this component uses ONLY plain
// source-over drawing, no element masks; the top edge stays clean because
// the geometry keeps glow away from it and particles dissolve inside
// TOP_FADE. The bitmap is cleared immediately after any width/height
// reassignment (a reallocated GPU surface may come up uninitialized —
// i.e. white — and must never be composited before a clear), and
// reassignment happens only when the size actually changed.
//
// REDUCED MOTION is "calmer", NEVER "absent": several OEMs (the
// operator's Honor among them) force the system animator scale off, which
// makes the WebView report prefers-reduced-motion without user intent.
// The halo is core product UX, so reduce drops the decorative motion
// (sparks, orbiting arc, breathing oscillation) but keeps the live,
// audio-reactive glow and its state colors.

import { useEffect, useRef } from 'react'
import type { DuplexPhase } from '../../hooks/useDuplexVoice'

export interface PresenceHaloProps {
  phase: DuplexPhase
  /** Optional at the seam (older callers); zeros keep the halo breathing. */
  getLevels?: () => { mic: number; out: number }
}

// Canvas extension past the pill, CSS px. Top reaches over ChatStatusBar
// into the message area; sides stay modest — they also overflow the
// viewport on mobile (clipped by overflow-x-clip, never scrollable). The
// top must be generous AND the under-bloom's vertical extent is CLAMPED to
// the available headroom (see paint): a wide desktop pill made the bloom
// radius (0.55 × pill width ≈ 500px) overshoot a fixed extension, clipping
// the gradient mid-alpha — a visible horizontal line across the messages
// (operator photo, 2026-08-12 04:41).
const TOP_EXT = 192
const SIDE_EXT = 48
const TOP_FADE = 64 // particles dissolve within this band below the top edge

// [r, g, b] per phase — dark theme bright pastels, light theme deeper inks
// (a light background needs saturation, not brightness).
const COLORS: Record<string, { dark: number[]; light: number[] }> = {
  connecting: { dark: [249, 115, 22], light: [234, 88, 12] },
  listening: { dark: [34, 211, 238], light: [8, 145, 178] },
  thinking: { dark: [167, 139, 250], light: [124, 58, 237] },
  speaking: { dark: [251, 191, 36], light: [217, 119, 6] },
}

const MAX_PARTICLES = 48

interface Particle {
  x: number; y: number; vx: number; vy: number
  age: number; life: number; size: number
}

function roundedRectPath(
  ctx: CanvasRenderingContext2D,
  x: number, y: number, w: number, h: number, r: number,
) {
  // Manual path — ctx.roundRect is too new to lean on (and absent in the
  // jsdom test double).
  ctx.beginPath()
  ctx.moveTo(x + r, y)
  ctx.arcTo(x + w, y, x + w, y + h, r)
  ctx.arcTo(x + w, y + h, x, y + h, r)
  ctx.arcTo(x, y + h, x, y, r)
  ctx.arcTo(x, y, x + w, y, r)
  ctx.closePath()
}

export function PresenceHalo({ phase, getLevels }: PresenceHaloProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const phaseRef = useRef(phase)
  const levelsRef = useRef(getLevels)
  phaseRef.current = phase
  levelsRef.current = getLevels

  const active = phase !== 'off' && phase !== 'error'

  useEffect(() => {
    if (!active) return
    const canvas = canvasRef.current
    const ctx = canvas?.getContext('2d')
    if (!canvas || !ctx) return // jsdom / ancient browsers: no visual, no crash

    const reduced = !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    // Breadcrumbs are deliberate: this surface has device-specific failure
    // modes we can only debug from a remote console.
    console.info(
      `[presence] active phase=${phaseRef.current} reduced=${reduced} `
      + `dpr=${window.devicePixelRatio || 1}`)

    // Pill rect in canvas coordinates — re-measured on resize (the bottom
    // extension is var(--composer-pb), so it can't be a constant here).
    const geom = { x: SIDE_EXT, y: TOP_EXT, w: 0, h: 0 }
    const measure = () => {
      const parent = canvas.parentElement
      if (!parent) return
      const dpr = window.devicePixelRatio || 1
      // 4096 cap: stay under every GPU's texture ceiling (an oversized
      // canvas silently composites as a white/blank quad on mobile).
      const w = Math.min(4096, Math.max(1, Math.round(canvas.clientWidth * dpr)))
      const h = Math.min(4096, Math.max(1, Math.round(canvas.clientHeight * dpr)))
      if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w
        canvas.height = h
        // A fresh GPU surface may be uninitialized (white) on WebView —
        // clear it before it can ever composite.
        ctx.setTransform(1, 0, 0, 1, 0, 0)
        ctx.clearRect(0, 0, w, h)
        console.info(`[presence] canvas ${w}x${h}`)
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      const cr = canvas.getBoundingClientRect()
      const pr = parent.getBoundingClientRect()
      geom.x = pr.left - cr.left
      geom.y = pr.top - cr.top
      geom.w = pr.width
      geom.h = pr.height
    }
    measure()
    let ro: ResizeObserver | null = null
    if (typeof ResizeObserver !== 'undefined' && canvas.parentElement) {
      ro = new ResizeObserver(() => measure())
      ro.observe(canvas.parentElement)
    }
    const onWinResize = () => measure()
    window.addEventListener('resize', onWinResize)

    // Animation state — envelope + color live across frames; phase changes
    // crossfade instead of snapping (the ref flips, the lerp follows).
    let env = 0
    const rgb = [...(COLORS[phaseRef.current]?.dark ?? COLORS.listening.dark)]
    const particles: Particle[] = []
    let raf = 0
    let running = false
    let painted = false

    const paint = (now: number) => {
      const t = now / 1000
      const dark = document.documentElement.classList.contains('dark')
      const ph = phaseRef.current
      // A level-tap glitch (closed AudioContext etc.) must dim, not kill.
      let levels = { mic: 0, out: 0 }
      try { levels = levelsRef.current?.() ?? levels } catch { /* keep zeros */ }

      // Target intensity: phase base + breathing + audio gain. Reduced
      // motion stills the breath (oscillation) but keeps audio reactivity.
      // The MIC term rides EVERY live phase (operator ask 2026-08-25): the
      // halo must visibly react the moment the user's voice registers —
      // even mid-'speaking' or through a long 'thinking' tool phase, and
      // even for speech that never dispatches a prompt. Phase changes the
      // color; the voice always drives the light.
      const breath = reduced ? 0.5
        : 0.5 + 0.5 * Math.sin(t * (ph === 'connecting' ? 2.2 : 1.5))
      let target: number
      switch (ph) {
        case 'connecting': target = 0.22 + 0.10 * breath; break
        case 'listening': target = 0.30 + 0.12 * breath + 0.55 * levels.mic; break
        case 'thinking': target = 0.34 + 0.08 * breath + 0.40 * levels.mic; break
        case 'speaking':
          target = 0.40 + 0.55 * levels.out + 0.35 * levels.mic; break
        default: target = 0
      }
      target = Math.min(1, target)
      env += (target - env) * (target > env ? 0.35 : 0.08)

      // Color crossfade toward the phase color.
      const goal = (COLORS[ph] ?? COLORS.listening)[dark ? 'dark' : 'light']
      for (let i = 0; i < 3; i++) rgb[i] += (goal[i] - rgb[i]) * 0.08
      const col = (a: number) =>
        `rgba(${rgb[0] | 0},${rgb[1] | 0},${rgb[2] | 0},${Math.max(0, Math.min(1, a))})`
      // Light backgrounds drown additive glow — drop overall alpha.
      const A = (dark ? 1 : 0.72) * env

      const W = canvas.clientWidth
      const H = canvas.clientHeight
      ctx.clearRect(0, 0, W, H)
      if (geom.w <= 0 || geom.h <= 0) return

      // 1. Under-bloom: ELLIPTICAL gradient anchored below the pill's
      // bottom center — wide sideways (most falls past the viewport edge on
      // mobile: the underglow), with its vertical semi-axis clamped so the
      // fade reaches EXACTLY zero inside the canvas on every viewport. An
      // unclamped circle overshot the top on wide desktop pills and the
      // clip drew a visible line (the gradient's last stop is col(0) at
      // r=radius — containment IS the guarantee of a clean edge).
      const cx = geom.x + geom.w / 2
      const by = geom.y + geom.h
      const radius = Math.max(1, geom.w * 0.55)
      const vScale = Math.min(0.6, Math.max(0.05, (by - 4) / radius))
      ctx.save()
      ctx.translate(cx, by)
      ctx.scale(1, vScale)
      const under = ctx.createRadialGradient(0, 0, 0, 0, 0, radius)
      under.addColorStop(0, col(0.28 * A))
      under.addColorStop(0.5, col(0.10 * A))
      under.addColorStop(1, col(0))
      ctx.fillStyle = under
      ctx.fillRect(-radius, -radius, radius * 2, radius * 2)
      ctx.restore()

      // 2. The ring: layered rounded-rect strokes just outside the pill
      // (rounded-xl = 12px radius; +3px so the pill border stays crisp).
      const rx = geom.x - 3
      const ry = geom.y - 3
      const rw = geom.w + 6
      const rh = geom.h + 6
      const layers: Array<[number, number]> = [[12, 0.10], [6, 0.22], [1.5, 0.60]]
      for (const [lw, la] of layers) {
        roundedRectPath(ctx, rx, ry, rw, rh, 15)
        ctx.lineWidth = lw
        ctx.strokeStyle = col(la * A)
        ctx.stroke()
      }

      // 3. Thinking: a bright arc orbiting the ring (animated dash).
      if (ph === 'thinking' && !reduced) {
        const perim = 2 * (rw + rh)
        roundedRectPath(ctx, rx, ry, rw, rh, 15)
        ctx.setLineDash([perim * 0.18, perim * 0.82])
        ctx.lineDashOffset = -((t * 0.22) % 1) * perim
        ctx.lineWidth = 2.5
        ctx.strokeStyle = col(0.85 * A)
        ctx.stroke()
        ctx.setLineDash([])
      }

      // 4. Sparks along the border, spawn rate ∝ the LIVE level: the TTS
      // output while the agent speaks, the mic whenever the user does —
      // in ANY live phase (same pool, phase color follows the crossfade —
      // amber vs cyan vs violet for free). The user's voice must throw
      // sparks even over the agent's speech or a silent tool phase.
      const sparkLevel = ph === 'speaking' ? Math.max(levels.out, levels.mic)
        : ph === 'listening' || ph === 'thinking' ? levels.mic : 0
      if (!reduced && sparkLevel > 0.18) {
        const want = Math.min(3, Math.ceil(sparkLevel * 3))
        for (let i = 0; i < want && particles.length < MAX_PARTICLES; i++) {
          // A point on one of the four edges + its outward normal.
          const side = (Math.random() * 4) | 0
          const u = Math.random()
          let px = 0, py = 0, nx = 0, ny = 0
          if (side === 0) { px = rx + u * rw; py = ry; ny = -1 }
          else if (side === 1) { px = rx + u * rw; py = ry + rh; ny = 1 }
          else if (side === 2) { px = rx; py = ry + u * rh; nx = -1 }
          else { px = rx + rw; py = ry + u * rh; nx = 1 }
          const speed = 24 + Math.random() * 40
          particles.push({
            x: px, y: py,
            vx: nx * speed + (Math.random() - 0.5) * 14,
            vy: ny * speed + (Math.random() - 0.5) * 14,
            age: 0, life: 0.55 + Math.random() * 0.4,
            size: 1 + Math.random() * 1.6,
          })
        }
      }
      for (let i = particles.length - 1; i >= 0; i--) {
        const p = particles[i]
        p.age += 1 / 60
        if (p.age >= p.life
            || (ph !== 'speaking' && ph !== 'listening' && ph !== 'thinking')) {
          particles.splice(i, 1); continue
        }
        p.x += p.vx / 60
        p.y += p.vy / 60
        // Age fade + dissolve inside the top band (no element mask — see
        // the WebView hazards note in the header).
        const fade = (1 - p.age / p.life) * Math.max(0, Math.min(1, p.y / TOP_FADE))
        ctx.beginPath()
        ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2)
        ctx.fillStyle = col(0.7 * fade * A)
        ctx.fill()
      }
    }

    // A throw mid-paint (exotic WebView 2D quirks) must not kill the rAF
    // loop silently — degrade to "no halo this session" with a breadcrumb.
    let broken = false
    const safePaint = (now: number): boolean => {
      if (broken) return false
      try {
        paint(now)
        if (!painted) { painted = true; console.info('[presence] first frame painted') }
        return true
      } catch (e) {
        broken = true
        console.warn('[presence] paint failed — halo disabled for this session', e)
        return false
      }
    }

    const loop = (now: number) => {
      if (!safePaint(now)) { running = false; return }
      raf = requestAnimationFrame(loop)
    }
    const startLoop = () => {
      if (running || broken) return
      running = true
      raf = requestAnimationFrame(loop)
    }
    const stopLoop = () => {
      running = false
      cancelAnimationFrame(raf)
    }

    startLoop()
    const onVisibility = () => {
      if (document.hidden) stopLoop()
      else startLoop()
    }
    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      stopLoop()
      document.removeEventListener('visibilitychange', onVisibility)
      window.removeEventListener('resize', onWinResize)
      ro?.disconnect()
    }
    // Deliberately NOT keyed on `phase`: remounting the loop per phase flip
    // would drop the color crossfade — the loop reads phaseRef each frame.
  }, [active])

  if (!active) return null
  // EXPLICIT width/height, never left+right / top+bottom pairs: a canvas is
  // a REPLACED element, and absolutely-positioned replaced elements keep
  // their INTRINSIC size instead of stretching between opposing insets.
  // With inset pairs, the element stayed 300x150 at the pill's top-left
  // (the desktop "misplaced rectangle"), and on dpr>1 phones measure()'s
  // bitmap writes fed back into the intrinsic size — exponential growth on
  // every resize until the element scrolled the page and the bitmap blew
  // past the GPU texture limit and composited WHITE (the operator's white
  // slab, all three rounds).
  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      data-phase={phase}
      className="absolute pointer-events-none"
      style={{
        top: -TOP_EXT,
        left: -SIDE_EXT,
        width: `calc(100% + ${2 * SIDE_EXT}px)`,
        height: `calc(100% + ${TOP_EXT}px + var(--composer-pb, 22px))`,
      }}
    />
  )
}
