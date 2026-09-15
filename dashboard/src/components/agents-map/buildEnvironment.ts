/** The living environment (terrain + wind grass): the body of the map's
 * environment effect, which AgentsMap3D still runs on `[layout, assetsTick]`
 * after its settle gate (split out on 2026-09-10). */
import * as THREE from 'three'
import { computeGrassScatter, terrainHeightAt, type MapLayout } from './layout'
import { FLOOR_Y } from './mapConstants'
import { disposeObject, WORLD_UP, type SceneBag } from './sceneBag'
import { makeGrassBladeTexture, makeRadialAlphaTexture } from './textures'

export function buildEnvironment(bag: SceneBag, layout: MapLayout) {
  const env = bag.envGroup
  for (const child of [...env.children]) {
    env.remove(child)
    disposeObject(child)
  }
  bag.wind = null
  for (const tx of bag.envTex) tx.dispose()
  bag.envTex = []

  // Meadow terrain: gentle deterministic hills (terrainHeightAt — shared
  // with the tree/tuft placement so everything sits ON the ground),
  // flattened into a clearing around every base and sunk into a basin
  // under the lake; edges dissolve via the radial alpha fade into the
  // night. The vertex-color mottling is the second scale of the
  // anti-tiling mix: a low-frequency brightness field the texture repeat
  // can't survive.
  const clearings = layout.clusters.map((c) => ({
    x: c.cx, z: c.cz, r: c.extent + 40,
  }))
  // 1200² (round 12; was 760): the edge must sit at the pano treeline
  // from every pose. Size is free — the cost is the 128² vertex grid,
  // which stays the same; the triangles just get larger, and the tuft
  // detail stays in the central ~270 units where it's visible.
  const geo = new THREE.PlaneGeometry(1200, 1200, 128, 128)
  geo.rotateX(-Math.PI / 2)
  const pos = geo.attributes.position as THREE.BufferAttribute
  const shades = new Float32Array(pos.count * 3)
  for (let i = 0; i < pos.count; i++) {
    const x = pos.getX(i)
    const z = pos.getZ(i)
    pos.setY(i, terrainHeightAt(x, z, clearings))
    const m = 0.5 + 0.5 * Math.sin(
      x * 0.021 + z * 0.017 + Math.sin(x * 0.008 - z * 0.011) * 2.2,
    )
    const shade = 0.8 + 0.25 * m
    shades[i * 3] = shade
    shades[i * 3 + 1] = shade
    shades[i * 3 + 2] = shade
  }
  geo.setAttribute('color', new THREE.BufferAttribute(shades, 3))
  geo.computeVertexNormals()
  const alphaTex = makeRadialAlphaTexture()
  if (alphaTex) bag.envTex.push(alphaTex)
  const terrain = new THREE.Mesh(
    geo,
    new THREE.MeshStandardMaterial({
      map: bag.tex.grass ?? undefined,
      // Warm amber-olive tint: converts the teal night texture into the
      // pano's yellow-green moonlit meadow under the blue-leaning
      // moonlight — the color-match half of the fog-free blend
      // (brightened + greened again in round 12, operator eyeball).
      color: bag.tex.grass ? 0xd2bc6e : 0x2e3d26,
      roughness: 0.95,
      metalness: 0,
      vertexColors: true,
      transparent: true,
      alphaMap: alphaTex ?? undefined,
    }),
  )
  terrain.position.y = FLOOR_Y - 0.1
  env.add(terrain)

  const mtx = new THREE.Matrix4()
  const rotQ = new THREE.Quaternion()
  const posV = new THREE.Vector3()
  const sclV = new THREE.Vector3()

  // Wind grass: instanced crossed-quad tufts across the WHOLE visible
  // meadow (round 12 — the ground read flat where only the clearings
  // had tufts; they ride the hills via terrainHeightAt), never under a
  // slab, bent by a vertex-shader wind against the shared uTime clock.
  // The FPS gate freezes the clock (envAnimOn); reduced-motion never
  // advances it — static tufts. Detail stays inside r=270 — further out
  // a blade is subpixel anyway, which is what keeps this cheap.
  const tufts = computeGrassScatter(
    [{ x: 0, z: 0, r: 270 }],
    layout.clusters.map((c) => ({
      x: c.cx, z: c.cz, r: (c.extent + 6) * 1.25,
    })),
    6000,
  )
  const blade = tufts.length > 0 ? makeGrassBladeTexture() : null
  if (blade) {
    blade.colorSpace = THREE.SRGBColorSpace
    bag.envTex.push(blade)
    const uTime = { value: 0 }
    const tuftMat = new THREE.MeshStandardMaterial({
      map: blade,
      alphaMap: blade,
      alphaTest: 0.38,
      // A notch brighter and warmer than the ground so the tufts read
      // as ALIVE against it (round 11 — they vanished in the dark).
      color: 0x8a9a58,
      emissive: 0x161f0c,
      emissiveIntensity: 0.6,
      roughness: 0.9,
      metalness: 0,
      side: THREE.DoubleSide,
    })
    tuftMat.customProgramCacheKey = () => 'odk-grass-wind'
    tuftMat.onBeforeCompile = (shader) => {
      shader.uniforms.uTime = uTime
      shader.vertexShader = shader.vertexShader
        .replace(
          '#include <common>',
          '#include <common>\nuniform float uTime;',
        )
        .replace('#include <begin_vertex>', [
          '#include <begin_vertex>',
          '#ifdef USE_INSTANCING',
          // Bend grows with blade height; the phase comes from the
          // tuft's world position so the meadow never sways in lockstep.
          'float odkPhase = instanceMatrix[3][0] * 0.53 + instanceMatrix[3][2] * 0.37;',
          'float odkBend = pow(clamp(position.y / 1.7, 0.0, 1.0), 1.6);',
          'transformed.x += sin(uTime * 1.7 + odkPhase) * odkBend * 0.38;',
          'transformed.z += cos(uTime * 1.28 + odkPhase * 1.6) * odkBend * 0.25;',
          '#endif',
        ].join('\n'))
    }
    bag.wind = uTime
    // Bigger blades (round 12): the tuft is the thing that makes the
    // ground read 3D, so it must be visible at overview distance.
    const quadA = new THREE.PlaneGeometry(2.2, 1.7, 1, 3)
    quadA.translate(0, 0.85, 0)
    const quadB = quadA.clone()
    quadB.rotateY(Math.PI / 2)
    for (const quad of [quadA, quadB]) {
      const inst = new THREE.InstancedMesh(quad, tuftMat, tufts.length)
      tufts.forEach((tuft, i) => {
        rotQ.setFromAxisAngle(WORLD_UP, tuft.rot)
        mtx.compose(
          posV.set(
            tuft.x,
            FLOOR_Y - 0.15 + terrainHeightAt(tuft.x, tuft.z, clearings),
            tuft.z,
          ),
          rotQ,
          sclV.setScalar(tuft.scale),
        )
        inst.setMatrixAt(i, mtx)
      })
      inst.instanceMatrix.needsUpdate = true
      env.add(inst)
    }
  }
}
