// Which markdown link targets are filesystem paths (MarkdownContent renders
// them as file chips, never as anchors).

// Known document/asset extensions — the last-resort path signal for hrefs
// that carry no other filesystem marker (bare `report.xlsx` relative links).
const FILE_EXT_RE = /\.(xlsx|xlsm|xls|csv|docx|doc|pptx|ppt|pdf|md|txt|rtf|odt|ods|odp|png|jpe?g|gif|webp|svg|bmp|tiff?|zip|tar|gz|7z|json|xml|ya?ml|log)$/i

// Filesystem roots on the platform's containers and common OSes. Deliberately
// NOT `/agents`: it is both an SPA route and the container mount root — the
// extension rule disambiguates those hrefs.
const POSIX_ROOT_RE = /^\/(workspace|users|knowledge|config|home|tmp|mnt|var|opt|srv|etc|root|media|private)\//i

/**
 * True when a markdown href is a filesystem path, not a navigable URL.
 * Agents sometimes emit `[open](C:\Users\...\file.xlsx)` — the browser cannot
 * open local files, so such links render as inert chips instead of anchors.
 * Order matters: the drive-letter/backslash tests must precede the generic
 * scheme test because `C:` parses as a URI scheme.
 * Accepted false positive: `./notes.md`-style relative file links become
 * chips too — they have no working SPA target anyway.
 */
export function isFileSystemPathHref(href: string): boolean {
  // Windows drive-relative + UNC. micromark's normalizeUri percent-encodes
  // '\' as %5C before urlTransform ever runs — both forms must match.
  if (/\\|%5C/i.test(href)) return true
  if (/^[a-zA-Z]:\//.test(href)) return true // C:/...
  if (/^(file|sandbox):/i.test(href)) return true
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(href)) return false // any other real scheme
  if (/^~\//.test(href)) return true
  if (/^[#?]/.test(href)) return false
  if (POSIX_ROOT_RE.test(href)) return true
  return FILE_EXT_RE.test(href.split(/[?#]/)[0])
}
