/**
 * Folders dropped on the workspace or chosen with the folder picker,
 * flattened into files that each name the folder they belong in.
 *
 * A dropped folder sits in `DataTransfer.files` as a placeholder the browser
 * cannot read (Chrome fails its upload with `net::ERR_ACCESS_DENIED`), so a
 * drop is read through its entries instead. The entries must be taken while
 * the drop event runs (`dropRoots`); walking them may happen after.
 */
import { Capacitor } from '@capacitor/core'

/** One upload, folder or picker, carries at most this many files. */
export const FOLDER_UPLOAD_MAX_FILES = 1000

/** Folders an upload leaves out, as the machine sync does (satellite and
 * proxy file_sync: SKIP_DIRS, every hidden folder but `.claude` and
 * `.codex`, a real venv), so what lands is what the machines receive. */
const SKIPPED_FOLDERS = new Set(['node_modules', '__pycache__'])
const KEPT_HIDDEN_FOLDERS = new Set(['.claude', '.codex'])
/** The file that makes a folder a Python venv (the sync's own test). */
const VENV_MARKER = 'pyvenv.cfg'

/** How many left-out folder names a message names. */
const LEFT_OUT_NAMED = 3

/** Whether a folder at this depth is left out by its name: a hidden folder
 * inside the upload, and the skipped names. A dropped or picked top folder
 * keeps its hidden name (the server reserves it without the leading dots).
 * A venv is told by its content (`VENV_MARKER`). */
function leftOut(name: string, nested: boolean): boolean {
  return SKIPPED_FOLDERS.has(name)
    || (nested && name.startsWith('.') && !KEPT_HIDDEN_FOLDERS.has(name))
}

/** A desktop browser: it has a folder picker; a phone, a tablet's touch and
 * the app shell do not (iOS ignores `webkitdirectory`). */
export function canPickFolder(): boolean {
  return !Capacitor.isNativePlatform()
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(hover: hover) and (pointer: fine)').matches
}

/** Spread on an `<input type="file">` to make it pick a folder. The
 * attribute has no React typing; desktop browsers all honour it. */
export const FOLDER_PICKER_ATTRS = { webkitdirectory: '' } as Record<string, string>

export interface CollectedFile {
  file: File
  /** Folder path below the upload target, '' for the target itself. */
  relDir: string
}

export interface CollectedItems {
  files: CollectedFile[]
  /** Folders with nothing in them, relative to the upload target. */
  emptyDirs: string[]
  /** Names the browser could not open (relative to the upload target). */
  unreadable: string[]
  /** More than `FOLDER_UPLOAD_MAX_FILES` files: nothing should be uploaded. */
  tooMany: boolean
  /** Folders left out (hidden or skipped, see `leftOut`), and the first few
   * of their names. */
  leftOut: number
  leftOutNames: string[]
}

/** What a drop holds: walkable entries, or plain files where the browser
 * offers no entries. */
export type DropRoot = FileSystemEntry | File

function joinRel(dir: string, name: string): string {
  return dir ? `${dir}/${name}` : name
}

/** The drop's top-level items. Call it inside the drop handler: the
 * browser empties `items` once the event returns. */
export function dropRoots(dt: DataTransfer | null): DropRoot[] {
  if (!dt) return []
  const items = Array.from(dt.items ?? []).filter((i) => i.kind === 'file')
  if (items.length === 0 || typeof items[0].webkitGetAsEntry !== 'function') {
    return Array.from(dt.files ?? [])
  }
  const roots: DropRoot[] = []
  for (const item of items) {
    const entry = item.webkitGetAsEntry()
    if (entry) {
      roots.push(entry)
    } else {
      const file = item.getAsFile()
      if (file) roots.push(file)
    }
  }
  return roots
}

/** A drop split for the chat composer: the files it attaches as they are,
 * and the folders it attaches one chip each. */
export function splitDropForAttach(
  dt: DataTransfer | null,
): { files: File[]; folders: FileSystemDirectoryEntry[] } {
  const files: File[] = []
  const folders: FileSystemDirectoryEntry[] = []
  if (!dt) return { files, folders }
  const items = Array.from(dt.items ?? []).filter((i) => i.kind === 'file')
  if (items.length === 0 || typeof items[0].webkitGetAsEntry !== 'function') {
    return { files: Array.from(dt.files ?? []), folders }
  }
  for (const item of items) {
    const entry = item.webkitGetAsEntry()
    if (entry?.isDirectory) {
      folders.push(entry as FileSystemDirectoryEntry)
      continue
    }
    const file = item.getAsFile()
    if (file) files.push(file)
  }
  return { files, folders }
}

function readBatch(reader: FileSystemDirectoryReader): Promise<FileSystemEntry[]> {
  return new Promise((resolve, reject) => reader.readEntries(resolve, reject))
}

/** Every child of a directory: `readEntries` answers in batches (Chrome
 * gives 100 at a time) until it returns an empty one. */
async function readAllEntries(dir: FileSystemDirectoryEntry): Promise<FileSystemEntry[]> {
  const reader = dir.createReader()
  const out: FileSystemEntry[] = []
  for (;;) {
    const batch = await readBatch(reader)
    if (batch.length === 0) return out
    out.push(...batch)
  }
}

function fileOf(entry: FileSystemFileEntry): Promise<File> {
  return new Promise((resolve, reject) => entry.file(resolve, reject))
}

/** Nothing collected: also the shape of a plain multi-file pick. */
export function emptyCollected(files: CollectedFile[] = []): CollectedItems {
  return { files, emptyDirs: [], unreadable: [], tooMany: false, leftOut: 0, leftOutNames: [] }
}

function countLeftOut(out: CollectedItems, name: string): void {
  out.leftOut += 1
  if (out.leftOutNames.length < LEFT_OUT_NAMED && !out.leftOutNames.includes(name)) {
    out.leftOutNames.push(name)
  }
}

/** "2 folders left out (.git, node_modules)", or '' when none was. */
export function leftOutNote(items: Pick<CollectedItems, 'leftOut' | 'leftOutNames'>): string {
  if (!items.leftOut) return ''
  const n = items.leftOut
  return `${n} folder${n === 1 ? '' : 's'} left out (${items.leftOutNames.join(', ')})`
}

/** Walk a drop's roots into files with their folder paths. Left-out folders
 * are counted, never walked, so they never count toward `max`; the walk
 * stops at the first file past `max` and reports `tooMany`. */
export async function collectDropped(
  roots: DropRoot[], max: number = FOLDER_UPLOAD_MAX_FILES,
): Promise<CollectedItems> {
  const out = emptyCollected()

  const addFile = (file: File, relDir: string): boolean => {
    if (out.files.length >= max) {
      out.tooMany = true
      return false
    }
    out.files.push({ file, relDir })
    return true
  }

  const walk = async (entry: FileSystemEntry, relDir: string): Promise<void> => {
    if (out.tooMany) return
    if (entry.isFile) {
      try {
        addFile(await fileOf(entry as FileSystemFileEntry), relDir)
      } catch {
        out.unreadable.push(joinRel(relDir, entry.name))
      }
      return
    }
    const here = joinRel(relDir, entry.name)
    if (!entry.isDirectory) {
      out.unreadable.push(here)
      return
    }
    if (leftOut(entry.name, relDir !== '')) {
      countLeftOut(out, entry.name)
      return
    }
    let children: FileSystemEntry[]
    try {
      children = await readAllEntries(entry as FileSystemDirectoryEntry)
    } catch {
      out.unreadable.push(here)
      return
    }
    if (children.length === 0) {
      out.emptyDirs.push(here)
      return
    }
    if (children.some((c) => c.isFile && c.name === VENV_MARKER)) {
      countLeftOut(out, entry.name)
      return
    }
    for (const child of children) {
      await walk(child, here)
      if (out.tooMany) return
    }
  }

  for (const root of roots) {
    if (root instanceof File) {
      if (!addFile(root, '')) break
    } else {
      await walk(root, '')
    }
    if (out.tooMany) break
  }
  return out
}

/** The same shape from an `<input webkitdirectory>` pick, whose files name
 * their folder in `webkitRelativePath` (empty folders are not reported). A
 * file under a left-out folder is left out, before the cap is checked. */
export function collectPicked(
  picked: FileList | File[], max: number = FOLDER_UPLOAD_MAX_FILES,
): CollectedItems {
  const out = emptyCollected()
  const kept: CollectedFile[] = []
  const skipped = new Set<string>()
  const dirOf = (file: File) => {
    const rel = file.webkitRelativePath || file.name
    const cut = rel.lastIndexOf('/')
    return cut > 0 ? rel.slice(0, cut) : ''
  }
  const venvs = new Set(Array.from(picked).filter((f) => f.name === VENV_MARKER).map(dirOf))
  for (const file of Array.from(picked)) {
    const relDir = dirOf(file)
    const dirs = relDir ? relDir.split('/') : []
    const at = dirs.findIndex(
      (name, i) => leftOut(name, i > 0) || venvs.has(dirs.slice(0, i + 1).join('/')),
    )
    if (at < 0) {
      kept.push({ file, relDir })
      continue
    }
    const folder = dirs.slice(0, at + 1).join('/')
    if (!skipped.has(folder)) {
      skipped.add(folder)
      countLeftOut(out, dirs[at])
    }
  }
  if (kept.length > max) {
    out.tooMany = true
    return out
  }
  out.files = kept
  return out
}

/** The files of one collected folder named `top`, their paths taken
 * inside it (`top/context` → `context`). */
export function insideFolder(files: CollectedFile[], top: string): CollectedFile[] {
  return files.map((f) => ({
    ...f,
    relDir: f.relDir === top ? '' : f.relDir.startsWith(`${top}/`) ? f.relDir.slice(top.length + 1) : f.relDir,
  }))
}

/** Shown for an item the browser cannot open: a folder that reached the
 * upload as a file, or a file another program holds. */
export const UNREADABLE_ITEM = "this item can't be read (a folder, or a file another program has open)"

/** Whether the browser can read `file` at all: one byte, so a folder
 * placeholder or a locked file fails here with a reason, not mid-upload as
 * a network error. */
export function canRead(file: File): Promise<boolean> {
  return file.slice(0, 1).arrayBuffer().then(() => true, () => false)
}
