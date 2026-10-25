// Saving a chat image to the device: the Android app's Downloads bridge for an
// inline image, else a browser download (an external image fetched as a blob
// first, since a cross-origin <a download> is ignored).

import { saveNativeFile } from '../../../lib/nativeBridge'
import type { LightboxImage } from './ImageLightbox'

// Map mime → file extension. A naive `mime.split('/')[1]` mangles compound
// subtypes (`image/svg+xml` → `svg+xml`) and gives `jpeg` instead of the
// conventional `jpg`, so download filenames come out wrong for SVG / HEIC.
const MIME_EXT: Record<string, string> = {
  'image/jpeg': 'jpg',
  'image/png': 'png',
  'image/gif': 'gif',
  'image/webp': 'webp',
  'image/svg+xml': 'svg',
  'image/heic': 'heic',
  'image/heif': 'heif',
  'image/avif': 'avif',
  'image/bmp': 'bmp',
  'image/tiff': 'tiff',
}

function downloadFilename(img: LightboxImage, index: number): string {
  const cap = img.caption || img.attribution || `image-${index + 1}`
  const mime = (img.mimeType || 'image/jpeg').toLowerCase()
  // Fallback strips any `+suffix` (e.g. svg+xml → svg) for unmapped mimes.
  const ext = MIME_EXT[mime] || mime.split('/')[1]?.replace(/\+.*$/, '') || 'jpg'
  return cap.replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 50) + '.' + ext
}

export async function triggerDownload(img: LightboxImage, index: number) {
  const filename = downloadFilename(img, index)

  // Native Android: route base64 through the app's channel if available.
  if (img.imageData) {
    if (saveNativeFile(img.imageData, filename, img.mimeType || 'image/jpeg')) return
    const byteChars = atob(img.imageData)
    const byteArr = new Uint8Array(byteChars.length)
    for (let i = 0; i < byteChars.length; i++) byteArr[i] = byteChars.charCodeAt(i)
    const blob = new Blob([byteArr], { type: img.mimeType || 'image/jpeg' })
    const blobUrl = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = blobUrl
    link.download = filename
    link.click()
    setTimeout(() => URL.revokeObjectURL(blobUrl), 5000)
    return
  }

  // External URL: fetch → blob → download. Direct <a download> won't work
  // for cross-origin URLs without CORS, but fetch-as-blob does the trick
  // for image CDNs that allow CORS (Unsplash, Pexels, Google all do).
  const fetchUrl = img.downloadUrl || img.url
  if (!fetchUrl) return
  try {
    const resp = await fetch(fetchUrl)
    const blob = await resp.blob()
    const blobUrl = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = blobUrl
    link.download = filename
    link.click()
    setTimeout(() => URL.revokeObjectURL(blobUrl), 5000)
  } catch {
    // Fallback: open in new tab — user can right-click → save.
    window.open(fetchUrl, '_blank', 'noopener')
  }
}
