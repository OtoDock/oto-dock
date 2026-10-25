import { useCallback, useSyncExternalStore } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiFetch } from '../api/auth'
import { encodePathSegments } from '../lib/paths'
import { blobUrl, holdBlobUrl } from '../lib/media/blobUrl'

/**
 * Image thumbnail loader for workspace grid tiles.
 *
 * The cache stores raw **Blob** bytes, not object URLs. Each mounted hook
 * minted its own short-lived `URL.createObjectURL` from the cached blob
 * and revokes it on unmount. We learned the hard way that storing the
 * URL itself in React Query causes `net::ERR_FILE_NOT_FOUND`: when the
 * first tile unmounts it revokes the URL, but the cached string survives
 * and gets handed back to later remounts as a now-dead reference.
 *
 * No viewport gating — fetches eagerly. For workspaces with hundreds of
 * images, add a backend `/thumb` endpoint instead of being clever here.
 */
export function useFileThumbnail(agent: string, path: string, isImage: boolean) {
  const query = useQuery({
    queryKey: ['agent-thumb', agent, path],
    enabled: isImage && !!agent && !!path,
    staleTime: Infinity,
    gcTime: 5 * 60_000,
    queryFn: async (): Promise<Blob> => {
      const res = await apiFetch(
        `/v1/agents/${encodeURIComponent(agent)}/files/${encodePathSegments(path)}`,
      )
      if (!res.ok) throw new Error('thumbnail fetch failed')
      return res.blob()
    },
  })

  // The blob's URL is held while this tile shows it (lib/media/blobUrl):
  // made on the first hold, read back once held, revoked with the last.
  const blob = query.data ?? null
  const subscribe = useCallback((notify: () => void) => {
    if (!blob) return () => {}
    const release = holdBlobUrl(blob)
    notify()
    return release
  }, [blob])
  const getSnapshot = useCallback(() => (blob ? blobUrl(blob) : null), [blob])
  const src = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)

  return { src, loading: query.isLoading }
}
