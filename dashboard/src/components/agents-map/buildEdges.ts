/** The delegation edges — dark-glass ribbons, chevrons and glints, whole
 * map only — with their local helpers (split out of the scene effect on
 * 2026-09-10). */
import * as THREE from 'three'
import { Line2 } from 'three/examples/jsm/lines/Line2.js'
import { LineGeometry } from 'three/examples/jsm/lines/LineGeometry.js'
import { LineMaterial } from 'three/examples/jsm/lines/LineMaterial.js'
import { EDGE_BODY, EDGE_LIGHT, EDGE_LIGHT_LIFT } from './mapConstants'
import { TMP_FWD, WORLD_UP } from './sceneBag'
import type { SceneBuildParams } from './buildScene'

export function buildEdges({
  bag, dynamic, staged, positions, edges, nodeBySlug,
}: SceneBuildParams) {
  // --- delegation edges (DIRECTIONAL — each agent owns its outgoing
  // targets). Dark-glass ribbon, round-19 cut (see EDGE_BODY above for
  // why round 17's additive silver failed on the bright meadow): every
  // edge is a slim near-black navy ribbon — the agent cards' own
  // material language, legible over walnut AND bright grass — carrying
  // one faint light-catch hugging its TOP edge (the glass sheen, and
  // the visibility over the darkest night areas), glassy dart chevrons
  // along the length for direction (alternating on a mutual pair), a
  // small gap before each card, and ONE slow glint traveling the lit
  // edge. WHOLE MAP only since round 22 (the department view's
  // partners sit behind its composed camera — see the edge block),
  // drawn PER-AGENT since round 18 (the aggregated dept bridges threw
  // away exactly the who-talks-to-whom the edges exist to show).
  // Solid curves only (dashes read as broken since round 7); WebGL
  // renders under the CSS3D card layer, so never crossing a card is
  // still the whole trick.
  // Both manual AND department-compiled edges draw (round 21 — the
  // operator's marketing head "talks to" its members through dept
  // compilation, and a map that hid those links looked like missing
  // data). Dept edges only ever join same-department agents, so they
  // all land in the subtle intra tier. Mutuality counts either source.
  const drawnSources = new Set(['manual', 'department'])
  const linkDir = new Set<string>()
  for (const e of edges) {
    if (drawnSources.has(e.source)) linkDir.add(`${e.from}|${e.to}`)
  }
  /** Deterministic [0,1) from an edge key — glint phase + speed seeds. */
  const phaseOf = (s: string) => {
    let h = 0
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0
    return (h % 997) / 997
  }
  const addLine2 = (
    pts: THREE.Vector3[], lift: number, color: number, width: number,
    opacity: number, additive: boolean,
  ) => {
    const flat: number[] = []
    for (const p of pts) flat.push(p.x, p.y + lift, p.z)
    const geom = new LineGeometry()
    geom.setPositions(flat)
    const mat = new LineMaterial({
      color, linewidth: width,
      // WORLD-unit thickness (round 22): pixel widths made a phone
      // show the same line ~4x fatter relative to its small viewport
      // than a desktop — the operator's "white ropes". A world width
      // is a physical property of the glass rod instead: identical on
      // every screen, hair-thin in the distance, thicker as you zoom
      // in — which is also most of what "looks real" means here.
      worldUnits: true,
      transparent: true, opacity,
      blending: additive ? THREE.AdditiveBlending : THREE.NormalBlending,
      depthWrite: false,
    })
    mat.resolution.copy(bag.lineRes)
    bag.lineMats.push(mat)
    dynamic.add(new Line2(geom, mat))
  }
  /** The ribbon: dark body + a wide FAINT halo + the crisp light-catch,
   * both hugging the top edge. The sheen stays narrower AND dimmer than
   * the body — the glass is the line, the light only catches on it —
   * and the halo is what makes that light READ as light (a bare 1px
   * additive streak looked like a painted white line on phones, and
   * over dark grass the sheen alone was too thin to survive the
   * screen's downscale; the soft falloff fixes both). */
  const addGlassRibbon = (
    pts: THREE.Vector3[], bodyWidth: number, bodyOpacity: number,
    lightOpacity: number,
  ) => {
    addLine2(pts, 0, EDGE_BODY, bodyWidth, bodyOpacity, false)
    addLine2(
      pts, EDGE_LIGHT_LIFT, EDGE_LIGHT, bodyWidth * 1.8,
      lightOpacity * 0.22, true,
    )
    addLine2(
      pts, EDGE_LIGHT_LIFT, EDGE_LIGHT, Math.max(0.07, bodyWidth * 0.4),
      lightOpacity, true,
    )
  }
  /** Glassy dart chevrons repeated along the curve — a dark body cone
   * with a smaller lit cone nested above it, matching the ribbon.
   * One-way edges all point the same way; a mutual pair alternates. */
  const addChevrons = (
    curve: THREE.Curve<THREE.Vector3>,
    dir: 'ab' | 'ba' | 'both', opacity: number, scale: number,
  ) => {
    const len = curve.getLength()
    // Sparse darts (round 22) — a dense train read as a beaded rope
    // at phone scale, and the dots were half the "still too white".
    const count = Math.max(2, Math.min(4, Math.round(len / 16)))
    // Dark glass darts with a faint lit core — the operator's phone
    // showed round 18's bright cones as pure white blobs, so the lit
    // cone now stays well under the dark body's presence.
    const bodyGeom = new THREE.ConeGeometry(0.2 * scale, 0.8 * scale, 10)
    const bodyMat = new THREE.MeshBasicMaterial({
      color: EDGE_BODY, transparent: true,
      opacity: Math.min(0.9, opacity + 0.2),
      depthWrite: false,
    })
    const litGeom = new THREE.ConeGeometry(0.09 * scale, 0.5 * scale, 10)
    const litMat = new THREE.MeshBasicMaterial({
      color: EDGE_LIGHT, transparent: true, opacity: opacity * 0.45,
      blending: THREE.AdditiveBlending, depthWrite: false,
    })
    for (let k = 0; k < count; k++) {
      const t = 0.5
        + ((k - (count - 1) / 2) / Math.max(1, count - 1)) * 0.6
      const fwd = dir === 'ab' || (dir === 'both' && k % 2 === 0)
      const tan = curve.getTangentAt(t)
        .multiplyScalar(fwd ? 1 : -1).normalize()
      const body = new THREE.Mesh(bodyGeom, bodyMat)
      curve.getPointAt(t, body.position)
      body.quaternion.setFromUnitVectors(WORLD_UP, tan)
      dynamic.add(body)
      const lit = new THREE.Mesh(litGeom, litMat)
      lit.position.copy(body.position)
      lit.position.y += EDGE_LIGHT_LIFT * 0.6
      lit.quaternion.copy(body.quaternion)
      dynamic.add(lit)
    }
  }
  /** One glint of light per edge, riding the LIT edge. Phase = f(edge
   * key, wall clock), so the 15s scene rebuild resumes the motion. */
  const addGlint = (curve: THREE.Curve<THREE.Vector3>, seed: string) => {
    if (!bag.tex.glow) return
    // Lift a sampled copy instead of the control points — the edge mix
    // is quadratic AND cubic now, and the spline through 24 samples
    // reproduces either shape to well under a pixel.
    const lifted = new THREE.CatmullRomCurve3(
      curve.getPoints(24).map((p) => {
        p.y += EDGE_LIGHT_LIFT
        return p
      }),
    )
    const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
      map: bag.tex.glow, color: 0xffffff,
      transparent: true, depthWrite: false,
      blending: THREE.AdditiveBlending, opacity: 0.3,
    }))
    sprite.scale.setScalar(0.7)
    const speed = 0.08 + phaseOf(`${seed}~`) * 0.05
    const phase = (phaseOf(seed) + performance.now() * 0.001 * speed) % 1
    lifted.getPointAt(phase, sprite.position)
    dynamic.add(sprite)
    bag.edgeGlints.push({ sprite, curve: lifted, phase, speed })
  }
  const edgeCurve = (
    a: THREE.Vector3, b: THREE.Vector3, liftFactor: number, liftCap: number,
  ) => {
    const mid = a.clone().lerp(b, 0.5)
    mid.y += Math.min(liftCap, a.distanceTo(b) * liftFactor)
    return new THREE.QuadraticBezierCurve3(a, mid, b)
  }
  /** Cross-platform FLIGHT path: a symmetric ARCH — vertical rise
   * straight above each agent up to a shared cruise height, one bow
   * across, vertical drop onto the other agent. Two hard lessons live
   * here (rounds 19-21): (1) deck agents sit BELOW the opaque
   * grass-tuft tops (~FLOOR_Y + 2.1) and below their own platform's
   * far-lip sightline, so any sloped approach spends its last stretch
   * occluded — the lines visibly "stopped" at the wood; a vertical
   * drop happens INSIDE the platform footprint, where nothing can be
   * in front of it. (2) The cruise height must be relative to the
   * HIGHER endpoint — a stage card sits ~8 units above a remote deck,
   * and a per-endpoint climb sent the whole crossing below the stage's
   * wood horizon (the department view's "no lines at all"). */
  const flightCurve = (a: THREE.Vector3, b: THREE.Vector3) => {
    const span = a.distanceTo(b)
    const cruise = Math.max(a.y, b.y)
      + Math.min(8, Math.max(4, 3.5 + span * 0.04))
    return new THREE.CubicBezierCurve3(
      a,
      new THREE.Vector3(a.x, cruise, a.z),
      new THREE.Vector3(b.x, cruise, b.z),
      b,
    )
  }
  /** Pull an endpoint in toward the other end so the line starts/stops
   * at the panel's visual edge instead of underneath it. */
  const trimToEdge = (
    from: THREE.Vector3, toward: THREE.Vector3, inset: number,
  ) => {
    const dir = TMP_FWD.copy(toward).sub(from)
    dir.y = 0
    const len = dir.length()
    if (len < 0.001) return from.clone()
    dir.multiplyScalar(1 / len)
    return from.clone().addScaledVector(
      new THREE.Vector3(dir.x, 0, dir.z), Math.min(inset, len * 0.35),
    )
  }

  if (staged === null) {
    // Edges draw on the WHOLE MAP ONLY (round 22, operator call). In
    // the department view every cross-department partner sits BEHIND
    // the composed stage camera — the ring is at the viewer's back —
    // so an edge there can never show who talks to whom; hiding them
    // beats drawing unreadable stubs. The whole map is the delegation
    // picture, and draws PER-AGENT edges (round 18 — the aggregated
    // dept bridges lost exactly that).
    const drawnPairs = new Set<string>()
    for (const e of edges) {
      if (!drawnSources.has(e.source)) continue
      const key = [e.from, e.to].sort().join('|')
      if (drawnPairs.has(key)) continue
      drawnPairs.add(key)
      const pa = positions.get(e.from)
      const pb = positions.get(e.to)
      if (!pa || !pb) continue
      const mutual = linkDir.has(`${e.from}|${e.to}`)
        && linkDir.has(`${e.to}|${e.from}`)
      const intra = nodeBySlug.get(e.from)?.departmentId
        === nodeBySlug.get(e.to)?.departmentId
      // Edge shape depends on what it crosses. Same platform: short
      // edges HOP just above the boards, longer ones arc low over
      // their own wood, both trimmed short of the pills. DIFFERENT
      // platforms: flightCurve arches between HOVER PORTS ~pill-top
      // height (round 22) — a deck-level landing spent its last
      // couple of units below the far-lip/grass-top sightline of a
      // low camera and visibly vanished into the wood; the port keeps
      // the whole drop above that horizon and the pill bridges the
      // final bit to its agent.
      const a = pa.clone()
      const b = pb.clone()
      if (intra) {
        a.y += 0.6
        b.y += 0.6
        const span = a.distanceTo(b)
        const aEnd = trimToEdge(a, b, 3.0)
        const bEnd = trimToEdge(b, a, 3.0)
        const curve = span < 12
          ? edgeCurve(aEnd, bEnd, 0.3, 2.4)
          : edgeCurve(aEnd, bEnd, 0.11, 5)
        addGlassRibbon(curve.getPoints(40),
          0.18 + (mutual ? 0.03 : 0), 0.66, 0.26)
        addChevrons(curve, mutual ? 'both' : 'ab', 0.5, 0.85)
        addGlint(curve, key)
      } else {
        a.y += 2.6
        b.y += 2.6
        const curve = flightCurve(a, b)
        addGlassRibbon(curve.getPoints(40),
          0.22 + (mutual ? 0.03 : 0), 0.78, 0.34)
        addChevrons(curve, mutual ? 'both' : 'ab', 0.62, 1)
        addGlint(curve, key)
      }
    }
  }
}
