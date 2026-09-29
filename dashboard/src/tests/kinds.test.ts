import { describe, expect, it } from 'vitest'
import { WIRE } from '@/api/wireEvents'
import {
  ARTIFACT_BLOCK_KINDS, REPLAYABLE_ARTIFACT_EVENT_TYPES, evictedBy, identityKey, identityOf, isArtifactBlock,
} from '@/lib/kinds/artifact'
import { RUN_KIND, TASK_KIND, TRIGGER_KIND, formatTrigger } from '@/lib/kinds/task'
import { APP_KIND, appKind } from '@/lib/kinds/app'
import { MCP_RUNTIME, isContainerRuntime } from '@/lib/kinds/mcpRuntime'
import { getTaskTypeLabel, getTaskTypeStyle } from '@/lib/runs'

// The mirrors' predicates (core-seams phase 9). The tables themselves are
// bound to the proxy by tests/core/test_kinds.py.

describe('the artifact kinds', () => {
  it('a gallery and a failure evict the generation placeholder, a player and a transcode failure the media one', () => {
    expect(evictedBy(WIRE.IMAGES)).toBe(WIRE.IMAGE_GENERATING)
    expect(evictedBy(WIRE.IMAGE_GEN_FAILED)).toBe(WIRE.IMAGE_GENERATING)
    expect(evictedBy(WIRE.VIDEO)).toBe(WIRE.MEDIA_PROCESSING)
    expect(evictedBy(WIRE.AUDIO)).toBe(WIRE.MEDIA_PROCESSING)
    expect(evictedBy(WIRE.MEDIA_FAILED)).toBe(WIRE.MEDIA_PROCESSING)
    expect(evictedBy(WIRE.URL)).toBeNull()
    expect(evictedBy('text')).toBeNull()
  })

  it('a preview is identified by its file, a ui page by its path, only when the key is non-empty', () => {
    expect(identityOf(WIRE.DOCUMENT_PREVIEW)).toBe('fileId')
    expect(identityOf(WIRE.UI)).toBe('path')
    expect(identityOf(WIRE.IMAGES)).toBeNull()
    expect(identityKey({ type: 'document_preview', fileId: 'f1' })).toBe('f1')
    expect(identityKey({ type: 'ui', path: 'ws/a.html' })).toBe('ws/a.html')
    expect(identityKey({ type: 'ui' })).toBe('')
    expect(identityKey({ type: 'images' })).toBe('')
  })

  it('the block kinds are every kind but the two removals; the replayable ones drop the placeholders', () => {
    expect([...ARTIFACT_BLOCK_KINDS].sort()).toEqual(
      ['audio', 'document_preview', 'file', 'image_generating', 'images', 'media_processing', 'ui', 'url', 'video'],
    )
    expect(isArtifactBlock('ui')).toBe(true)
    expect(isArtifactBlock('image_gen_failed')).toBe(false)
    expect(isArtifactBlock('text')).toBe(false)
    expect([...REPLAYABLE_ARTIFACT_EVENT_TYPES].sort()).toEqual(
      ['audio', 'document_preview', 'file', 'images', 'ui', 'url', 'video'],
    )
  })
})

describe('the task kinds', () => {
  it('formatTrigger names the trigger, the manual actor, the kind, and passes an unknown word through', () => {
    expect(formatTrigger(TRIGGER_KIND.TRIGGER, 'trigger:push')).toBe('Trigger: trigger:push')
    expect(formatTrigger(TRIGGER_KIND.TRIGGER, null)).toBe('Trigger')
    expect(formatTrigger(TRIGGER_KIND.MANUAL, 'dev-admin')).toBe('dev-admin')
    expect(formatTrigger(TRIGGER_KIND.MANUAL, null)).toBe('Manual')
    expect(formatTrigger(TRIGGER_KIND.SCHEDULED, null)).toBe('Scheduled')
    expect(formatTrigger(TRIGGER_KIND.CHECK, 'check:style')).toBe('Check')
    expect(formatTrigger(TRIGGER_KIND.APP_HANDLER, 'app:1:h')).toBe('App handler')
    expect(formatTrigger(TRIGGER_KIND.APP_ACTION, 'x:y')).toBe('App action')
    expect(formatTrigger('schedule', null)).toBe('schedule')
    expect(formatTrigger('triggered', 'x')).toBe('triggered')
  })

  it('the badge words: every run kind labeled, the legacy static row kept, an unknown word verbatim', () => {
    expect(getTaskTypeLabel(RUN_KIND.TRIGGER)).toBe('Trigger')
    expect(getTaskTypeLabel(RUN_KIND.ONE_TIME)).toBe('One-time')
    expect(getTaskTypeLabel(RUN_KIND.SCHEDULED)).toBe('Recurring')
    expect(getTaskTypeLabel(RUN_KIND.APP)).toBe('App')
    expect(getTaskTypeLabel(RUN_KIND.CHECK)).toBe('Check')
    expect(getTaskTypeLabel('static')).toBe('Static')
    expect(getTaskTypeLabel('memory_run')).toBe('memory_run')
    expect(getTaskTypeLabel(null)).toBe('—')
    expect(getTaskTypeStyle(RUN_KIND.DELEGATE)).toContain('purple')
    expect(getTaskTypeStyle('nonsense')).toContain('gray')
  })

  it('the definition kinds spell the API words', () => {
    expect(TASK_KIND.ONE_TIME).toBe('one_time')
    expect(RUN_KIND.ONE_TIME).toBe('one-time')
    expect(TASK_KIND.TRIGGER).toBe(RUN_KIND.TRIGGER)
  })
})

describe('the app kind', () => {
  it('a folder app can do everything a file app cannot; an absent kind is a file', () => {
    const folder = appKind({ kind: APP_KIND.FOLDER })
    const file = appKind({ kind: APP_KIND.FILE })
    expect(Object.values(folder).every(Boolean)).toBe(true)
    expect(Object.values(file).some(Boolean)).toBe(false)
    expect(appKind({})).toBe(file)
    expect(appKind({ kind: null })).toBe(file)
  })
})

describe('the MCP runtime', () => {
  it('only docker is a container', () => {
    expect(isContainerRuntime(MCP_RUNTIME.DOCKER)).toBe(true)
    for (const rt of [MCP_RUNTIME.PYTHON, MCP_RUNTIME.NODE, MCP_RUNTIME.NONE, '', undefined, null]) {
      expect(isContainerRuntime(rt)).toBe(false)
    }
  })
})
