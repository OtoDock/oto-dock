// XHR-based upload with byte progress. fetch() has no upload-progress events
// (short of experimental duplex streams), so the workspace/chat upload paths
// use XMLHttpRequest when they want a progress bar. Auth is the same-origin
// session cookie (mirrors apiFetch: no headers needed, 401 → back to login).

export interface UploadResponse {
  path: string
  filename: string
  size: number
  transfer_id: string
  remote_push: boolean
}

export function uploadWithProgress(
  file: File,
  agent: string,
  targetDir: string,
  onProgress: (sent: number, total: number) => void,
): Promise<UploadResponse> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', '/v1/upload')
    xhr.withCredentials = false // same-origin — cookies ride automatically
    xhr.responseType = 'json'
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(e.loaded, e.total)
    }
    xhr.onload = () => {
      if (xhr.status === 401) {
        window.location.href = '/'
        reject(new Error('Unauthorized'))
        return
      }
      if (xhr.status >= 200 && xhr.status < 300 && xhr.response) {
        resolve(xhr.response as UploadResponse)
      } else {
        const detail = (xhr.response && (xhr.response as any).detail) || `HTTP ${xhr.status}`
        reject(new Error(String(detail)))
      }
    }
    xhr.onerror = () => reject(new Error('Network error during upload'))
    xhr.onabort = () => reject(new Error('Upload aborted'))
    const fd = new FormData()
    fd.append('file', file)
    fd.append('agent', agent)
    fd.append('target_dir', targetDir)
    xhr.send(fd)
  })
}
