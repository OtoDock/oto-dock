import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiFetch } from '../api/auth'
import { encodePathSegments } from '../lib/paths'

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

  const [src, setSrc] = useState<string | null>(null)
  useEffect(() => {
    if (!query.data) {
      setSrc(null)
      return
    }
    const url = URL.createObjectURL(query.data)
    setSrc(url)
    return () => URL.revokeObjectURL(url)
  }, [query.data])

  return { src, loading: query.isLoading }
}
