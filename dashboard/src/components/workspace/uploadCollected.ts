// One workspace upload batch: plain files, a dropped folder or a picked
// one, into the folder on show. Each top-level folder of the batch is
// reserved on the server before its first file (`name`, else `name_1`…, an
// exclusive mkdir there), so two quick drops of one folder never mix; its
// files then land in their places inside it (the upload route creates the
// missing folders).
//
// Sequential on purpose: keeps failure attribution per file, and the
// transfer popup makes the sequencing visible. The batch has one abort
// (the popup's Cancel), stops after a run of network errors (the connection
// is gone: the rest would fail fast one by one), and ends with one toast.

import { emitAppToast } from '../../lib/appToasts'
import {
  canRead, leftOutNote, FOLDER_UPLOAD_MAX_FILES, UNREADABLE_ITEM, type CollectedItems,
} from '../../lib/droppedItems'
import { isNetworkError, reserveUploadFolder, uploadWithProgress } from '../../lib/uploadWithProgress'
import { registerBatch, useTransferStore } from '../../store/transferStore'

/** Network errors in a row that stop a batch. */
export const NETWORK_ERRORS_TO_STOP = 3

// The listing refreshes at most this often while a batch lands.
const UPLOAD_REFRESH_MS = 1000

const NOT_SENT = 'not sent: the connection dropped'

export interface UploadBatchTarget {
  agent: string
  /** The folder on show, agent-relative: where the batch lands. */
  dir: string
  /** Create an empty folder (agent-relative path). */
  createDir: (path: string) => Promise<unknown>
  /** Re-read the listing. */
  refresh: () => void
}

// NOT crypto.randomUUID(): that's secure-context-only, and plenty of
// self-hosted installs serve the dashboard over plain http on a LAN.
const newId = () => `local:${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`

const topOf = (rel: string) => rel.split('/')[0]

const isAbort = (e: unknown) => e instanceof DOMException && e.name === 'AbortError'

const why = (e: unknown) => (e instanceof Error && e.message) || 'Upload failed'

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`

const joinRel = (dir: string, name: string) => (dir ? `${dir}/${name}` : name)

export async function uploadCollected(
  items: CollectedItems, { agent, dir, createDir, refresh }: UploadBatchTarget,
): Promise<void> {
  const note = leftOutNote(items)
  if (items.tooMany) {
    emitAppToast({
      title: 'Not uploaded',
      body: `That is more than ${FOLDER_UPLOAD_MAX_FILES} files. Upload it in parts, or as a zip file.`,
      severity: 'warning',
    })
    return
  }
  const total = items.files.length + items.unreadable.length
  if (total === 0 && items.emptyDirs.length === 0) {
    if (note) emitAppToast({ title: 'Nothing uploaded', body: `${note}.`, severity: 'info' })
    return
  }

  const batchId = newId()
  const abort = new AbortController()
  const unregister = registerBatch(batchId, abort)
  const ts = () => useTransferStore.getState()
  const begin = (filename: string, bytesTotal: number) => {
    const clientId = newId()
    ts().beginLocalUpload({ clientId, agent, targetDir: dir, filename, bytesTotal, batchId })
    return clientId
  }
  const failRow = (filename: string, error: string) => ts().failLocalUpload(begin(filename, 0), error)

  let uploaded = 0
  let failed = 0
  let emptyFailed = 0
  let stopped: 'cancelled' | 'network' | null = null
  let networkRun = 0
  // A failure joins (or breaks) the run of network errors; true once the
  // run is long enough to stop the batch.
  const runOut = (e: unknown) => {
    networkRun = isNetworkError(e) ? networkRun + 1 : 0
    return networkRun >= NETWORK_ERRORS_TO_STOP
  }

  // Each top-level folder, reserved where it lands, or why it is not.
  const reserved = new Map<string, { path: string; name: string } | Error>()
  /** Where a folder of the batch is on the server (`target`) and how a
   * row names it (`shown`, the reserved name in place of the dropped one). */
  const place = (relDir: string): { target: string; shown: string } | Error => {
    if (!relDir) return { target: dir, shown: '' }
    const head = topOf(relDir)
    const got = reserved.get(head) ?? new Error(NOT_SENT)
    if (got instanceof Error) return got
    const rest = relDir.slice(head.length)
    return { target: got.path + rest, shown: got.name + rest }
  }
  const shownPath = (relDir: string, name: string) => {
    const at = place(relDir)
    return joinRel(at instanceof Error ? relDir : at.shown, name)
  }

  // A top-level folder is reserved when the batch first needs it (its first
  // file, else its empty folders): a Cancel or a dropped connection leaves
  // the later ones uncreated. Throws an AbortError when cancelled meanwhile.
  const placeOf = async (relDir: string): Promise<{ target: string; shown: string } | Error> => {
    const head = relDir ? topOf(relDir) : ''
    if (head && !reserved.has(head)) {
      try {
        reserved.set(head, await reserveUploadFolder(agent, head, abort.signal, dir))
        networkRun = 0
      } catch (e) {
        if (isAbort(e)) throw e
        reserved.set(head, new Error(`the folder could not be created: ${why(e)}`))
        if (runOut(e)) stopped = 'network'
      }
    }
    return place(relDir)
  }

  let next = 0
  try {
    let refreshedAt = 0
    for (; next < items.files.length && !stopped; next++) {
      const { file, relDir } = items.files[next]
      let at: { target: string; shown: string } | Error
      try {
        at = await placeOf(relDir)
      } catch {
        stopped = 'cancelled'
        break
      }
      if (stopped) break // the reservation found the connection gone
      const clientId = begin(shownPath(relDir, file.name), file.size)
      if (at instanceof Error) {
        failed += 1
        ts().failLocalUpload(clientId, at.message)
        continue
      }
      try {
        if (!(await canRead(file))) {
          failed += 1
          ts().failLocalUpload(clientId, UNREADABLE_ITEM)
          continue
        }
        const res = await uploadWithProgress(
          file, agent, at.target,
          (sent) => ts().updateLocalUpload(clientId, sent),
          abort.signal,
        )
        ts().linkUpload(clientId, {
          transferId: res.transfer_id, relPath: res.path, remotePush: res.remote_push,
        })
        uploaded += 1
        networkRun = 0
      } catch (e) {
        if (isAbort(e) || abort.signal.aborted) {
          ts().failLocalUpload(clientId, 'cancelled')
          stopped = 'cancelled'
          break
        }
        failed += 1
        ts().failLocalUpload(clientId, why(e))
        if (runOut(e)) stopped = 'network'
      }
      // Big batches appear as they land, not only at the end.
      if (Date.now() - refreshedAt >= UPLOAD_REFRESH_MS) {
        refreshedAt = Date.now()
        refresh()
      }
    }
    if (stopped === 'network') {
      // The rest would each fail the same way: shown as not sent.
      for (const { file, relDir } of items.files.slice(next)) {
        failed += 1
        failRow(shownPath(relDir, file.name), NOT_SENT)
      }
    }
    if (!stopped) {
      for (const rel of items.emptyDirs) {
        let at: { target: string; shown: string } | Error
        try {
          at = await placeOf(rel)
        } catch {
          stopped = 'cancelled'
          break
        }
        if (at instanceof Error) {
          emptyFailed += 1
          failRow(rel, at.message)
        } else if (rel.includes('/')) {
          // A top-level empty folder is its reservation; a nested one is made.
          try {
            await createDir(at.target)
          } catch (e) {
            emptyFailed += 1
            failRow(at.shown, why(e))
          }
        }
      }
    }
    // Named after the reservations made (a top folder holding only
    // unreadable items is never reserved).
    for (const rel of items.unreadable) {
      failed += 1
      const cut = rel.lastIndexOf('/')
      failRow(cut > 0 ? shownPath(rel.slice(0, cut), rel.slice(cut + 1)) : rel, UNREADABLE_ITEM)
    }
  } finally {
    unregister()
    refresh()
  }

  const tail = [
    emptyFailed ? `${plural(emptyFailed, 'empty folder')} not created.` : '',
    note ? `${note}.` : '',
  ].filter(Boolean).join(' ')
  const body = (head: string) => (tail ? `${head} ${tail}` : head)
  if (stopped === 'cancelled') {
    emitAppToast({
      title: 'Upload cancelled',
      body: body(`Uploaded ${uploaded} of ${plural(total, 'file')}; the rest were not sent.`),
      severity: 'info',
    })
  } else if (failed || emptyFailed) {
    emitAppToast({
      title: 'Upload incomplete',
      body: body(failed
        ? `${failed} of ${plural(total, 'file')} failed${stopped === 'network' ? ': the connection dropped' : ''}.`
        : `Uploaded ${plural(uploaded, 'file')}.`),
      severity: 'warning',
    })
  } else {
    emitAppToast({
      title: 'Uploaded',
      body: body(total ? `Uploaded ${plural(uploaded, 'file')}.` : 'The empty folders were created.'),
      severity: 'success',
    })
  }
}
