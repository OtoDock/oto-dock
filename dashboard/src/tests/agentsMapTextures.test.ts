/** Late-arriving texture application for the 3D map (textures.ts), headless
 *  — real three.js scene-graph objects, no WebGL context needed.
 *
 *  Live-hit 2026-08-16 (operator screenshot): on a cold cache the walnut
 *  bases stayed a flat brown slab forever. The slabs read tex.wood at BUILD
 *  time and nothing re-runs the dynamic effect when a texture lands, so a
 *  wood JPEG that arrived after the build was simply never applied — until
 *  an unrelated rebuild (paging to another department) happened to re-read
 *  it, which is why swiping "fixed" it and staying put did not. Reproduced
 *  deterministically by holding wood-walnut.jpg past the map build over CDP. */
import { describe, it, expect } from 'vitest'
import * as THREE from 'three'
import {
  applyWoodTexture,
  WOOD_FALLBACK_TINT,
} from '@/components/agents-map/textures'

/** A department slab exactly as the dynamic effect builds it while the wood
 *  texture is still in flight. */
function pendingSlab(): THREE.Mesh<THREE.BoxGeometry, THREE.MeshStandardMaterial> {
  const mesh = new THREE.Mesh(
    new THREE.BoxGeometry(1, 1, 1),
    new THREE.MeshStandardMaterial({
      color: WOOD_FALLBACK_TINT,
      transparent: true,
      opacity: 1,
    }),
  )
  mesh.userData = { type: 'dept', departmentId: 'eng', departmentName: 'Engineering' }
  return mesh
}

const woodTex = () => new THREE.Texture()

describe('applyWoodTexture', () => {
  it('gives a late texture to a slab that was built without one', () => {
    const root = new THREE.Group()
    const slab = pendingSlab()
    root.add(slab)
    expect(slab.material.map).toBeFalsy()

    const before = slab.material.version
    const wood = woodTex()
    applyWoodTexture(root, wood)

    expect(slab.material.map).toBe(wood)
    // The fallback tint must go, or it multiplies the texture into mud.
    expect(slab.material.color.getHex()).toBe(0xffffff)
    // Adding a map flips USE_MAP — without needsUpdate the program never
    // recompiles and the slab renders untextured even with material.map set.
    // (needsUpdate is a WRITE-ONLY setter in three.js; version is what it
    // bumps and what the renderer actually reads.)
    expect(slab.material.version).toBe(before + 1)
  })

  it('reaches slabs nested under the per-cluster group (dais), not just direct children', () => {
    const root = new THREE.Group()
    const dais = new THREE.Group()
    const slab = pendingSlab()
    dais.add(slab)
    root.add(dais)

    const wood = woodTex()
    applyWoodTexture(root, wood)

    expect(slab.material.map).toBe(wood)
  })

  it('patches every department at once — one held texture, all the bases', () => {
    const root = new THREE.Group()
    const slabs = ['eng', 'systems', ''].map((id) => {
      const s = pendingSlab()
      s.userData.departmentId = id
      root.add(s)
      return s
    })

    const wood = woodTex()
    applyWoodTexture(root, wood)

    for (const s of slabs) expect(s.material.map).toBe(wood)
  })

  it('leaves a slab that already has the texture alone (idempotent)', () => {
    const root = new THREE.Group()
    const slab = pendingSlab()
    const original = woodTex()
    slab.material.map = original
    slab.material.color.setHex(0xffffff)
    root.add(slab)
    const before = slab.material.version

    applyWoodTexture(root, woodTex())

    expect(slab.material.map).toBe(original)
    // No pointless shader recompile either.
    expect(slab.material.version).toBe(before)
  })

  it('ignores everything that is not a department slab', () => {
    const root = new THREE.Group()
    const chip = new THREE.Mesh(
      new THREE.BoxGeometry(1, 1, 1),
      new THREE.MeshBasicMaterial({ color: 0x112233 }),
    )
    chip.userData = { type: 'agent', slug: 'personal-assistant' }
    const bare = new THREE.Mesh(
      new THREE.BoxGeometry(1, 1, 1),
      new THREE.MeshBasicMaterial({ color: 0x445566 }),
    )
    root.add(chip, bare, new THREE.Group())

    const wood = woodTex()
    expect(() => applyWoodTexture(root, wood)).not.toThrow()

    expect((chip.material as THREE.MeshBasicMaterial).map).toBeFalsy()
    expect((chip.material as THREE.MeshBasicMaterial).color.getHex()).toBe(0x112233)
    expect((bare.material as THREE.MeshBasicMaterial).color.getHex()).toBe(0x445566)
  })

  it('does not disturb the staging dim state of a neighbouring slab', () => {
    // Wood can land while a department is staged: the others are dimmed via
    // transparent/opacity, which the patch must not touch.
    const root = new THREE.Group()
    const dimmed = pendingSlab()
    dimmed.material.opacity = 0.25
    root.add(dimmed)

    applyWoodTexture(root, woodTex())

    expect(dimmed.material.opacity).toBe(0.25)
    expect(dimmed.material.transparent).toBe(true)
  })

  it('is a no-op on an empty scene (the warm-cache path, nothing built yet)', () => {
    const root = new THREE.Group()
    expect(() => applyWoodTexture(root, woodTex())).not.toThrow()
    expect(root.children).toHaveLength(0)
  })
})

describe('the map wires the late texture in', () => {
  it('hands wood to the standing slabs instead of re-running the dynamic effect', async () => {
    // Teeth for the actual bug: the fix is only real if the loader CALLS the
    // patch. Re-adding assetsTick to the dynamic effect instead would rebuild
    // every agent chip's CSS3D DOM on a texture arrival.
    const src = await import('@/components/agents-map/AgentsMap3D?raw')
      .then((m) => m.default as string)
    const woodLoader = src.slice(src.indexOf('load(woodUrl'))
    const callSite = woodLoader.indexOf('applyWoodTexture(bag.dynamic')
    expect(callSite).toBeGreaterThan(-1)
    // ...and inside that callback, not somewhere later in the file.
    expect(callSite).toBeLessThan(woodLoader.indexOf('load(grassUrl'))
  })
})
