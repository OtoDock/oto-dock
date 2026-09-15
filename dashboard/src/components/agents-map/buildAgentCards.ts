/** The agent cards (CSS3D glass pills / stage cards) and the department
 * labels (split out of the scene effect on 2026-09-10). */
import { CSS3DSprite } from 'three/examples/jsm/renderers/CSS3DRenderer.js'
import {
  DEFAULT_TINT, LABEL_SCALE, OVERVIEW_SCALE, SPARK_MARGIN, STAGE_SCALE,
} from './mapConstants'
import { hexToRgb } from './textures'
import type { SceneBuildParams } from './buildScene'

export function buildAgentCards({
  bag, dynamic, layout, staged, arc, positions, memberCount, heat, liveSet,
  streamingSet, user, moveFromRef, linkFromRef, attemptLinkRef, openAgentRef,
  onDeptTapRef, setPopup,
}: SceneBuildParams) {
  // Agents: ONE glass language at both levels (operator round 7) — the
  // same panel anatomy, compact one-liner at overview, two-line ChartHop
  // card on stage; other clusters dim while a stage is up. No colored
  // box-shadows anywhere: activity lives in the border breathe + the
  // chip pulse (+ stage spark particles).
  for (const node of layout.nodes) {
    const pos = positions.get(node.slug)!
    const onStage = staged === node.departmentId
    const dimOther = staged !== null && !onStage
    const h = heat.get(node.slug) ?? 0
    const live = liveSet.has(node.slug)
    const streaming = streamingSet.has(node.slug)
    const [cr, cg, cb] = hexToRgb(node.color || DEFAULT_TINT)
    // Live-but-idle sessions raise the glow floor — activity is the only
    // extra language the cards speak.
    const glow = live ? Math.max(h, 0.45) : h
    const isFav = user?.default_agent === node.slug

    const root = document.createElement('div')
    root.style.pointerEvents = 'none'

    const chip = document.createElement('div')
    const chipSize = onStage ? 44 : 26
    chip.textContent = node.displayName.trim().slice(0, 2).toUpperCase()
    chip.style.cssText =
      `width:${chipSize}px;height:${chipSize}px;flex-shrink:0;` +
      `border-radius:${onStage ? 12 : 8}px;display:flex;align-items:center;` +
      'justify-content:center;font-family:Comfortaa,system-ui,sans-serif;' +
      `font-size:${onStage ? 17 : 11}px;font-weight:700;` +
      (node.grayed
        ? 'background:rgba(122,130,150,0.2);border:1px solid rgba(122,130,150,0.45);color:#8a92a8;'
        : `background:rgba(${cr},${cg},${cb},0.3);` +
          `border:1px solid rgba(${cr},${cg},${cb},0.6);` +
          `color:rgb(${Math.min(255, cr + 90)},${Math.min(255, cg + 90)},${Math.min(255, cb + 90)});`)
    if (!node.grayed) {
      chip.style.setProperty('--odk-glow-lo', `rgba(${cr},${cg},${cb},0.28)`)
      chip.style.setProperty('--odk-glow-hi', `rgba(${cr},${cg},${cb},0.6)`)
      if (streaming && !dimOther) chip.classList.add('odk-chip-live')
    }

    const card = document.createElement('div')
    card.className = 'odk-fade'
    const borderAlpha = onStage ? 0.45 + glow * 0.45 : 0.4 + glow * 0.35
    const borderColor = node.grayed
      ? 'rgba(122,130,150,0.4)'
      : `rgba(${cr},${cg},${cb},${borderAlpha.toFixed(2)})`
    card.style.cssText =
      'position:relative;display:flex;align-items:center;' +
      (onStage
        ? 'gap:11px;padding:10px 6px 10px 13px;border-radius:16px;'
        : 'gap:8px;padding:5px 4px 5px 7px;border-radius:13px;') +
      'touch-action:none;' +
      'background:rgba(15,18,34,0.82);' +
      'backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);' +
      `border:1.5px solid ${borderColor};` +
      'transition:border-color 140ms ease;' +
      `pointer-events:${dimOther ? 'none' : 'auto'};` +
      `opacity:${dimOther ? 0.3 : node.grayed ? 0.6 : 1};` +
      `box-shadow:0 ${onStage ? '6px 22px' : '4px 14px'} rgba(4,6,16,0.5);`
    if (!node.grayed) {
      card.style.setProperty('--odk-b-lo', `rgba(${cr},${cg},${cb},0.4)`)
      card.style.setProperty('--odk-b-hi', `rgba(${cr},${cg},${cb},1)`)
      if (streaming && !dimOther) card.classList.add('odk-border-breathe')
    }

    if (onStage && streaming && !node.grayed && !bag.reducedMotion) {
      const canvas = document.createElement('canvas')
      canvas.style.cssText =
        `position:absolute;left:${-SPARK_MARGIN}px;top:${-SPARK_MARGIN}px;`
        + 'pointer-events:none;'
      const ctx = canvas.getContext('2d')
      if (ctx) {
        root.appendChild(canvas)
        bag.sparks.push({
          canvas, ctx, pill: card, rgb: [cr, cg, cb],
          heat: Math.max(h, 0.3), particles: [], sized: false,
        })
      }
    }
    root.appendChild(card)
    card.appendChild(chip)

    const col = document.createElement('div')
    col.style.cssText = 'min-width:0;display:flex;flex-direction:column;'
    const name = document.createElement('button')
    name.textContent = (isFav ? '★ ' : '') + node.displayName
    name.title = node.grayed
      ? `${node.displayName} (not a member)` : `Open ${node.displayName}`
    name.style.cssText =
      `font-family:Comfortaa,system-ui,sans-serif;font-size:${onStage ? 21 : 18}px;` +
      'font-weight:600;letter-spacing:0.01em;background:none;border:0;' +
      `cursor:${node.grayed ? 'default' : 'pointer'};` +
      `color:${node.grayed ? '#8a92a8' : '#e6ebff'};` +
      `padding:0;text-align:left;white-space:nowrap;max-width:${onStage ? 170 : 150}px;` +
      'overflow:hidden;text-overflow:ellipsis;'
    const openFromCard = (e: MouseEvent) => {
      e.stopPropagation()
      if (moveFromRef.current) return
      const lf = linkFromRef.current
      if (lf) {
        attemptLinkRef.current(lf, node.slug)
        return
      }
      openAgentRef.current(node, e.clientX, e.clientY)
    }
    name.onclick = openFromCard
    col.appendChild(name)
    if (onStage && node.modeLabel) {
      // Second line = the agent's visibility mode (the Grid cards'
      // wording); omitted for grayed dept-mates whose mode is unknown.
      const lvl = document.createElement('div')
      lvl.textContent = node.modeLabel
      lvl.style.cssText =
        'font-size:12px;letter-spacing:0.14em;text-transform:uppercase;' +
        'color:#8fa0c8;margin-top:3px;white-space:nowrap;max-width:170px;' +
        'overflow:hidden;text-overflow:ellipsis;'
      col.appendChild(lvl)
    }
    card.appendChild(col)
    // The whole chip body opens the agent (grayed → info popup via
    // openAgent); only the ⋯ button diverges — its stopPropagation
    // keeps this from firing. Post-swipe clicks are already swallowed
    // by the container's capture-phase guard.
    card.onclick = openFromCard
    card.style.cursor = node.grayed ? 'default' : 'pointer'

    const more = document.createElement('button')
    more.textContent = '⋯'
    more.setAttribute('aria-label', `Options for ${node.displayName}`)
    more.style.cssText =
      'background:none;border:0;border-left:1px solid rgba(255,255,255,0.08);' +
      `cursor:pointer;color:#8b96b8;font-size:${onStage ? 24 : 18}px;line-height:1;` +
      (onStage
        ? 'padding:4px 10px 6px 12px;'
        : 'padding:2px 8px 3px 9px;') +
      'flex-shrink:0;align-self:stretch;'
    more.onclick = (e) => {
      e.stopPropagation()
      setPopup({ node, x: e.clientX, y: e.clientY })
    }
    card.appendChild(more)

    card.oncontextmenu = (e) => {
      e.preventDefault()
      e.stopPropagation()
      setPopup({ node, x: e.clientX, y: e.clientY })
    }
    if (!node.grayed && !dimOther) {
      card.onmouseenter = () => {
        card.style.borderColor = `rgba(${cr},${cg},${cb},0.95)`
      }
      card.onmouseleave = () => { card.style.borderColor = borderColor }
    }

    const obj = new CSS3DSprite(root)
    obj.scale.setScalar(onStage ? STAGE_SCALE : OVERVIEW_SCALE)
    obj.position.copy(pos)
    dynamic.add(obj)
  }

  // Department typography: spaced-caps name + member count floating over
  // each blob; the staged cluster's label rises above its amphitheater.
  for (const cluster of layout.clusters) {
    if (!cluster.name) continue // lone centered scatter needs no label
    const isStaged = staged === cluster.departmentId
    const dimOther = staged !== null && !isStaged
    const holder = document.createElement('div')
    holder.style.pointerEvents = isStaged || dimOther ? 'none' : 'auto'
    holder.style.textAlign = 'center'
    holder.style.cursor = 'pointer'
    holder.style.opacity = dimOther ? '0.25' : isStaged ? '0.9' : '1'
    holder.style.transition = 'opacity 200ms ease'
    // Smaller type + more altitude (operator round 7): long names fit a
    // phone width and the name never crowds the cards.
    const title = document.createElement('div')
    title.textContent = cluster.name.toUpperCase().split('').join(' ')
    title.style.cssText =
      'font-family:Comfortaa,system-ui,sans-serif;font-size:24px;' +
      'font-weight:600;letter-spacing:0.2em;color:rgba(240,244,255,0.95);' +
      // The halo earns its keep against the milky way band too — the
      // starfield is busy enough to eat unhaloed type.
      'text-shadow:0 2px 10px rgba(10,14,22,0.95),0 0 26px rgba(10,14,22,0.7);' +
      'white-space:nowrap;'
    holder.appendChild(title)
    const count = memberCount.get(cluster.departmentId) ?? 0
    const sub = document.createElement('div')
    sub.textContent = `${count} agent${count === 1 ? '' : 's'}`
    sub.style.cssText =
      'font-size:13px;letter-spacing:0.28em;text-transform:uppercase;' +
      'color:rgba(235,240,250,0.85);margin-top:6px;white-space:nowrap;' +
      'text-shadow:0 1px 8px rgba(10,14,22,0.9);'
    holder.appendChild(sub)
    holder.onclick = (e) =>
      onDeptTapRef.current(cluster.departmentId, e.clientX, e.clientY)
    const labelObj = new CSS3DSprite(holder)
    labelObj.scale.setScalar(LABEL_SCALE)
    const labelY = isStaged && arc
      ? 0.8 + Math.max(0, arc.rows - 1) * 4.2 + 9
      : 13
    labelObj.position.set(cluster.cx, labelY, cluster.cz)
    dynamic.add(labelObj)
  }
}
