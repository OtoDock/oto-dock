// A download started from the page, of a signed address the server minted (the
// workspace's zip, a person's export).

/** Start the download of `href` under `filename`. */
export function clickDownload(href: string, filename: string): void {
  const a = document.createElement('a')
  a.href = href
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
}
