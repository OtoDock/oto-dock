/** The walnut department slabs — frame, brass, bollards, spotlight — one
 * dais per cluster (split out of the scene effect on 2026-09-10). */
import * as THREE from 'three'
import { RoundedBoxGeometry } from 'three/examples/jsm/geometries/RoundedBoxGeometry.js'
import { RING_RADIUS } from './layout'
import { FLOOR_Y } from './mapConstants'
import { WOOD_FALLBACK_TINT } from './textures'
import type { SceneBuildParams } from './buildScene'

export function buildDais({
  bag, dynamic, layout, staged, arc,
}: SceneBuildParams) {
  // Walnut bases: every cluster stands on its OWN square slab sunk into
  // the meadow (the staged slab grows under the amphitheater and its
  // frame brightens: the base literally becomes the stage floor). The
  // slab bodies are the raycast targets for department taps.
  // Overview slabs must never overlap (round 12 — big departments did):
  // the square's circumscribed circle is capped by the ring-slot
  // spacing. The staged slab still grows freely under its amphitheater;
  // its neighbors are dimmed then, so the cap only binds at overview.
  const slotChord = layout.clusters.length > 1
    ? 2 * RING_RADIUS * Math.sin(Math.PI / layout.clusters.length)
    : Infinity
  const daisCap = (slotChord / 2 - 2) / Math.SQRT2
  const daisRadius = new Map<string, number>()
  for (const cluster of layout.clusters) {
    const isStaged = staged === cluster.departmentId
    let reach = cluster.extent
    if (isStaged && arc) {
      reach = 8
      for (const s of arc.slots) {
        reach = Math.max(
          reach, Math.hypot(s.x - cluster.cx, s.z - cluster.cz),
        )
      }
    }
    daisRadius.set(
      cluster.departmentId,
      isStaged ? reach + 6 : Math.min(reach + 6, daisCap),
    )
  }
  for (const cluster of layout.clusters) {
    const isStaged = staged === cluster.departmentId
    const dimOther = staged !== null && !isStaged
    const isIndependent = cluster.departmentId === ''
    const r = daisRadius.get(cluster.departmentId)!
    const accent = new THREE.Color(
      isIndependent ? '#aab6cc' : cluster.accent,
    )
    const size = r * 2
    const dais = new THREE.Group()
    // Premium SQUARE walnut slab (operator round 9): the wood shows in
    // its natural color — the department identity lives in the glowing
    // frame inset on the top face. Rounded corners, real thickness.
    const slab = new THREE.Mesh(
      new RoundedBoxGeometry(size, 1.3, size, 4, 0.16),
      new THREE.MeshStandardMaterial({
        // Still loading ⇒ the flat fallback tint; applyWoodTexture hands
        // this material the map the moment the JPEG lands. The key is
        // omitted rather than set to undefined: three warns per material
        // on an undefined parameter, once per department per rebuild.
        ...(bag.tex.wood ? { map: bag.tex.wood } : {}),
        color: bag.tex.wood ? 0xffffff : WOOD_FALLBACK_TINT,
        metalness: 0.06,
        roughness: 0.52,
        envMapIntensity: 0.8,
        transparent: true,
        opacity: dimOther ? 0.25 : 1,
      }),
    )
    // Sunk slightly into the meadow — the base belongs to the ground.
    slab.position.y = FLOOR_Y + 0.55
    slab.userData = {
      type: 'dept',
      departmentId: cluster.departmentId,
      departmentName: cluster.name || 'Independent',
    }
    dais.add(slab)
    bag.hitTargets.push(slab)
    // The glowing accent frame: four thin light bars inset on the top.
    // At night the frame IS the department's light — it burns brighter
    // than the daylight rounds and spills onto the grass below.
    const frameHalf = size / 2 - 1
    const frameMat = new THREE.MeshBasicMaterial({
      color: accent,
      transparent: true,
      depthWrite: false,
      opacity: dimOther ? 0.12 : isStaged ? 1 : 0.9,
    })
    const frameLen = frameHalf * 2 + 0.18
    for (const [bx, bz, horizontal] of [
      [0, -frameHalf, true], [0, frameHalf, true],
      [-frameHalf, 0, false], [frameHalf, 0, false],
    ] as const) {
      const bar = new THREE.Mesh(
        new THREE.BoxGeometry(
          horizontal ? frameLen : 0.18, 0.07,
          horizontal ? 0.18 : frameLen,
        ),
        frameMat,
      )
      bar.position.set(bx, FLOOR_Y + 1.23, bz)
      dais.add(bar)
    }
    // Brass jewellery (round 10): a thin metallic inlay line just inside
    // the light frame, corner caps where the frame bars meet, and a
    // lighter beveled highlight strip along the outer top rim — the
    // moonlight and the env map do the rest.
    const brassMat = new THREE.MeshStandardMaterial({
      color: 0xd9b36a,
      metalness: 0.9,
      roughness: 0.3,
      envMapIntensity: 1.2,
      transparent: true,
      opacity: dimOther ? 0.2 : 1,
    })
    const inlayHalf = frameHalf - 0.55
    const inlayLen = inlayHalf * 2 + 0.12
    for (const [bx, bz, horizontal] of [
      [0, -inlayHalf, true], [0, inlayHalf, true],
      [-inlayHalf, 0, false], [inlayHalf, 0, false],
    ] as const) {
      const line = new THREE.Mesh(
        new THREE.BoxGeometry(
          horizontal ? inlayLen : 0.12, 0.05,
          horizontal ? 0.12 : inlayLen,
        ),
        brassMat,
      )
      line.position.set(bx, FLOOR_Y + 1.22, bz)
      dais.add(line)
    }
    for (const [cxr, czr] of [
      [-frameHalf, -frameHalf], [frameHalf, -frameHalf],
      [-frameHalf, frameHalf], [frameHalf, frameHalf],
    ] as const) {
      const cap = new THREE.Mesh(
        new THREE.BoxGeometry(0.6, 0.09, 0.6), brassMat,
      )
      cap.position.set(cxr, FLOOR_Y + 1.24, czr)
      dais.add(cap)
    }
    const bevelMat = new THREE.MeshStandardMaterial({
      color: 0x9a7a52,
      metalness: 0.25,
      roughness: 0.5,
      envMapIntensity: 0.9,
      transparent: true,
      opacity: dimOther ? 0.18 : 0.9,
    })
    const bevelHalf = size / 2 - 0.24
    const bevelLen = bevelHalf * 2 + 0.3
    for (const [bx, bz, horizontal] of [
      [0, -bevelHalf, true], [0, bevelHalf, true],
      [-bevelHalf, 0, false], [bevelHalf, 0, false],
    ] as const) {
      const strip = new THREE.Mesh(
        new THREE.BoxGeometry(
          horizontal ? bevelLen : 0.3, 0.06,
          horizontal ? 0.3 : bevelLen,
        ),
        bevelMat,
      )
      strip.position.set(bx, FLOOR_Y + 1.21, bz)
      dais.add(strip)
    }
    if (bag.tex.glow) {
      // Soft contact shadow on the grass — the base sits IN the world,
      // it doesn't float over it.
      const shadow = new THREE.Mesh(
        new THREE.CircleGeometry(size * 0.78, 48),
        new THREE.MeshBasicMaterial({
          map: bag.tex.glow,
          color: 0x0a0f0a,
          transparent: true,
          depthWrite: false,
          opacity: dimOther ? 0.12 : 0.3,
        }),
      )
      shadow.rotation.x = -Math.PI / 2
      shadow.position.set(0, FLOOR_Y + 0.04, 0)
      dais.add(shadow)
      // The base's light ON THE GROUND at every zoom level: one soft
      // warm additive pool under each slab, neutral warm like the
      // bollards, never the dept color. TIGHT (round 13) — the round-12
      // size read as an unfocused glow wash at overview; the focused
      // light is the spot's job.
      const pool = new THREE.Mesh(
        new THREE.PlaneGeometry(size * 1.3, size * 1.3),
        new THREE.MeshBasicMaterial({
          map: bag.tex.glow,
          color: 0xffd9a0,
          transparent: true,
          depthWrite: false,
          blending: THREE.AdditiveBlending,
          opacity: dimOther ? 0.04 : isStaged ? 0.15 : 0.09,
        }),
      )
      pool.rotation.x = -Math.PI / 2
      pool.position.set(0, FLOOR_Y + 0.06, 0)
      dais.add(pool)
    }
    // Corner bollard lamps (round 11 — the operator's "furniture"): a
    // small warm lantern on each corner of the slab, always burning at
    // night. Warm and NEUTRAL — the dept-color spill pools of round 10
    // read as stains and were cut.
    const postMat = new THREE.MeshStandardMaterial({
      color: 0x2a2622, metalness: 0.6, roughness: 0.5,
      transparent: true, opacity: dimOther ? 0.2 : 1,
    })
    const bulbMat = new THREE.MeshBasicMaterial({
      color: 0xffe3b0, transparent: true,
      opacity: dimOther ? 0.15 : 1,
    })
    for (const [bx, bz] of [
      [-frameHalf, -frameHalf], [frameHalf, -frameHalf],
      [-frameHalf, frameHalf], [frameHalf, frameHalf],
    ] as const) {
      const post = new THREE.Mesh(
        new THREE.CylinderGeometry(0.07, 0.1, 1.5, 8), postMat,
      )
      post.position.set(bx, FLOOR_Y + 1.24 + 0.75, bz)
      dais.add(post)
      const bulb = new THREE.Mesh(
        new THREE.SphereGeometry(0.24, 12, 10), bulbMat,
      )
      bulb.position.set(bx, FLOOR_Y + 1.24 + 1.62, bz)
      dais.add(bulb)
      if (bag.tex.glow) {
        const halo = new THREE.Sprite(new THREE.SpriteMaterial({
          map: bag.tex.glow, color: 0xffd9a0,
          transparent: true, depthWrite: false,
          blending: THREE.AdditiveBlending,
          opacity: dimOther ? 0.06 : 0.5,
        }))
        halo.scale.setScalar(2.6)
        halo.position.set(bx, FLOOR_Y + 1.24 + 1.62, bz)
        dais.add(halo)
      }
    }
    // EVERY department's light burns all night (round 14 — the operator
    // kept the look and wanted it everywhere): one warm SpotLight per
    // dais, always on. The light COUNT only changes with the department
    // list, so shader recompiles stay rare; the staged one brightens
    // and the others soften while a stage is up. Local coordinates —
    // the group's rotation carries the inward tilt.
    const spot = new THREE.SpotLight(
      0xfff1d6, dimOther ? 25 : isStaged ? 130 : 100, 170,
      Math.min(1.05, Math.atan((r * 1.5) / 52)), 0.7, 1,
    )
    spot.position.set(0, FLOOR_Y + 52, -10)
    spot.target.position.set(0, FLOOR_Y, r * 0.2)
    dais.add(spot)
    dais.add(spot.target)
    dais.position.set(cluster.cx, 0, cluster.cz)
    // Face the slab's edges toward the composed stage camera.
    dais.rotation.y = Math.atan2(cluster.outX, cluster.outZ)
    dynamic.add(dais)
  }
}
