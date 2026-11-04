// Types for the settings-map generator (design/settings-map.mjs), for the
// tests that import it.
import type { CatalogEntry } from '../src/components/settings/catalog.ts'

export const DASHBOARD: string
export const REPO: string
export const CATALOG_JSON: string
export const SETTINGS_MAP: string
export const AREA_LABELS: Record<string, string>
export const SECTION_LABELS: Record<string, Record<string, string>>
export function whereOf(entry: CatalogEntry): string
export function whoOf(entry: CatalogEntry): string
export function jsonRow(entry: CatalogEntry): Record<string, unknown>
export function catalogJson(): string
export function settingsMap(): string
export function buildSettingsOutputs(): Map<string, string>
