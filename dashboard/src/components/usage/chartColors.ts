import { useEffect, useState } from 'react'

// The chart library paints SVG attributes and inline styles, which take a
// colour value, not a utility class: the colours are read from the tokens'
// custom properties on the root and read again whenever its class changes.
// The theme provider's own effect runs after its children's, so a read keyed
// on the theme alone would see the old values.

export interface ChartColors {
  bar: string
  axis: string
  tooltipBg: string
  tooltipBorder: string
  tooltipText: string
  cursor: string
}

function readChartColors(): ChartColors {
  const css = getComputedStyle(document.documentElement)
  const v = (name: string) => css.getPropertyValue(name).trim()
  return {
    bar: v('--ot-status-info-solid'),
    axis: v('--ot-text-tertiary'),
    tooltipBg: v('--ot-surface-overlay'),
    tooltipBorder: v('--ot-border-subtle'),
    tooltipText: v('--ot-text-primary'),
    cursor: v('--ot-surface-hover'),
  }
}

/** The tooltip's box on the overlay surface, its text and the bar under the pointer. */
export function tooltipProps(c: ChartColors) {
  return {
    contentStyle: { fontSize: 12, backgroundColor: c.tooltipBg, border: `1px solid ${c.tooltipBorder}`, borderRadius: 8, color: c.tooltipText },
    itemStyle: { color: c.tooltipText },
    labelStyle: { color: c.tooltipText },
    cursor: { fill: c.cursor },
  }
}

export function useChartColors(): ChartColors {
  const [colors, setColors] = useState(readChartColors)
  useEffect(() => {
    const observer = new MutationObserver(() => setColors(readChartColors()))
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
    return () => observer.disconnect()
  }, [])
  return colors
}
