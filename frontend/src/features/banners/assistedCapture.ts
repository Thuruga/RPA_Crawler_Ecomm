export type AssistedCaptureRequest = {
  targets: Array<{ brand_key: string; page_url: string }>
  receiver_path: string
  token: string
  expires_at: number
}

const APP_SOURCE = 'scrapper-banners-app'
const EXTENSION_SOURCE = 'scrapper-banners-extension'

export function startAssistedBannerCapture(request: AssistedCaptureRequest, timeoutMs = 1800): Promise<void> {
  const receiverUrl = new URL(request.receiver_path, window.location.origin).href
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => {
      window.removeEventListener('message', onMessage)
      reject(new Error('Extensão de banners não detectada'))
    }, timeoutMs)

    function onMessage(event: MessageEvent) {
      if (event.source !== window || event.origin !== window.location.origin) return
      if (event.data?.source !== EXTENSION_SOURCE || event.data?.type !== 'CAPTURE_ACCEPTED') return
      window.clearTimeout(timeout)
      window.removeEventListener('message', onMessage)
      if (event.data.ok) resolve()
      else reject(new Error(event.data.error || 'A extensão recusou a captura'))
    }

    window.addEventListener('message', onMessage)
    window.postMessage({
      source: APP_SOURCE,
      type: 'START_ASSISTED_CAPTURE',
      request: {
        targets: request.targets,
        receiverUrl,
        token: request.token,
      },
    }, window.location.origin)
  })
}
