import { useState, useCallback } from 'react'
import { safeHref } from '../../../lib/safeUrl'
import ImageLightbox, { triggerDownload, LightboxImage } from './ImageLightbox'

export interface GalleryImage extends LightboxImage {}

interface Props {
  images: GalleryImage[]
}

function cardSrc(img: GalleryImage): string {
  if (img.url) return img.url
  if (img.imageData) return `data:${img.mimeType || 'image/jpeg'};base64,${img.imageData}`
  return ''
}

function GalleryCard({
  img, index, onOpen,
}: {
  img: GalleryImage
  index: number
  onOpen: (i: number) => void
}) {
  const src = cardSrc(img)
  const hasCaptionRow = !!(img.caption || img.attribution)

  return (
    <div className="relative rounded-xl border border-p-border-light bg-white dark:bg-p-surface overflow-hidden flex flex-col">
      <div
        className="relative cursor-pointer group bg-black/5"
        onClick={() => onOpen(index)}
      >
        <img
          src={src}
          alt={img.caption || 'Image'}
          className="w-full aspect-[4/3] object-cover"
          loading="lazy"
        />
        {/* Hover overlay for desktop zoom hint */}
        <div className="absolute inset-0 bg-black/0 group-hover:bg-black/10 transition-colors" />

        {/* Top-right: download (always visible) */}
        <button
          onClick={(e) => { e.stopPropagation(); triggerDownload(img, index) }}
          className="absolute top-1.5 right-1.5 p-1.5 rounded-md bg-black/40 hover:bg-black/60 text-white transition-colors"
          title="Download image"
          aria-label="Download image"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
            <polyline points="7 10 12 15 17 10" />
            <line x1="12" y1="15" x2="12" y2="3" />
          </svg>
        </button>

        {/* Top-left: external link (only if linkUrl set) */}
        {img.linkUrl && (
          <a
            href={safeHref(img.linkUrl)}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            className="absolute top-1.5 left-1.5 p-1.5 rounded-md bg-black/40 hover:bg-black/60 text-white transition-colors"
            title="Open source"
            aria-label="Open source"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
              <polyline points="15 3 21 3 21 9" />
              <line x1="10" y1="14" x2="21" y2="3" />
            </svg>
          </a>
        )}
      </div>

      {hasCaptionRow && (
        <div className="px-2 py-1.5 min-w-0">
          {img.caption && (
            <div className="text-xs text-p-text-secondary truncate" title={img.caption}>
              {img.caption}
            </div>
          )}
          {img.attribution && (
            <div className="text-[10px] text-p-text-light opacity-70 truncate" title={img.attribution}>
              {img.attribution}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function ImageGallery({ images }: Props) {
  const [lightboxIdx, setLightboxIdx] = useState<number | null>(null)
  const open = useCallback((i: number) => setLightboxIdx(i), [])
  const close = useCallback(() => setLightboxIdx(null), [])

  if (!images || images.length === 0) return null

  const count = images.length

  // 1 image: same look as the legacy single-image card.
  if (count === 1) {
    const img = images[0]
    return (
      <>
        <div className="my-2 max-w-md">
          <GalleryCard img={img} index={0} onOpen={open} />
        </div>
        {lightboxIdx !== null && (
          <ImageLightbox images={images} initialIndex={lightboxIdx} onClose={close} />
        )}
      </>
    )
  }

  // 2-3 images: equal-width row, no scroll.
  if (count <= 3) {
    return (
      <>
        <div className="my-2 grid gap-2" style={{ gridTemplateColumns: `repeat(${count}, minmax(0, 1fr))` }}>
          {images.map((img, i) => (
            <GalleryCard key={i} img={img} index={i} onOpen={open} />
          ))}
        </div>
        {lightboxIdx !== null && (
          <ImageLightbox images={images} initialIndex={lightboxIdx} onClose={close} />
        )}
      </>
    )
  }

  // 4+ images: horizontal scroll-snap carousel.
  return (
    <>
      <div className="my-2 flex gap-2 overflow-x-auto snap-x snap-mandatory pb-1 -mx-1 px-1">
        {images.map((img, i) => (
          <div key={i} className="shrink-0 w-[220px] sm:w-[260px] snap-start">
            <GalleryCard img={img} index={i} onOpen={open} />
          </div>
        ))}
      </div>
      {lightboxIdx !== null && (
        <ImageLightbox images={images} initialIndex={lightboxIdx} onClose={close} />
      )}
    </>
  )
}
