/** Browser Origin validation only; not an authentication mechanism. */
export function isLoopbackOrigin(origin) {
  if (typeof origin !== 'string') return false
  try {
    const parsed = new URL(origin)
    return ['http:', 'https:'].includes(parsed.protocol)
      && !parsed.username && !parsed.password && !parsed.search && !parsed.hash
      && parsed.origin === origin
      && ['localhost', '127.0.0.1', '[::1]'].includes(parsed.hostname)
  } catch { return false }
}

export function isSameOriginRequest(req) {
  const origin = req.headers.origin
  return origin === undefined || (typeof origin === 'string' && origin === `http://${req.headers.host}`)
}
