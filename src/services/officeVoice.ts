/** 浏览器语音识别（Web Speech API）：不支持或失败时如实上报，不造假结果 */

type SpeechRecognitionResultLike = ArrayLike<ArrayLike<{ transcript: string }>>

type SpeechRecognitionLike = {
  lang: string
  interimResults: boolean
  maxAlternatives: number
  start(): void
  onresult: ((event: { results: SpeechRecognitionResultLike }) => void) | null
  onerror: ((event: { error?: string }) => void) | null
  onend: (() => void) | null
}

type SpeechWindow = Window & {
  SpeechRecognition?: new () => SpeechRecognitionLike
  webkitSpeechRecognition?: new () => SpeechRecognitionLike
}

export function isVoiceSupported(): boolean {
  const w = window as SpeechWindow
  return Boolean(w.SpeechRecognition ?? w.webkitSpeechRecognition)
}

export function startVoiceRecognition(): Promise<string> {
  return new Promise((resolve, reject) => {
    const w = window as SpeechWindow
    const Recognition = w.SpeechRecognition ?? w.webkitSpeechRecognition
    if (!Recognition) {
      reject(new Error('当前浏览器不支持语音识别'))
      return
    }

    let settled = false
    const finish = (fn: () => void) => {
      if (settled) return
      settled = true
      fn()
    }

    const recognition = new Recognition()
    recognition.lang = 'zh-CN'
    recognition.interimResults = false
    recognition.maxAlternatives = 1

    recognition.onresult = (event) => {
      const transcript = event.results?.[0]?.[0]?.transcript?.trim() ?? ''
      if (transcript) {
        finish(() => resolve(transcript))
      } else {
        finish(() => reject(new Error('没有识别到内容')))
      }
    }
    recognition.onerror = (event) => {
      const code = event?.error ?? 'unknown'
      const message =
        code === 'not-allowed' || code === 'service-not-allowed'
          ? '麦克风权限被拒绝，请在浏览器设置中允许'
          : code === 'network'
            ? '语音识别服务网络不可用'
            : code === 'no-speech'
              ? '没有听到说话内容'
              : `语音识别失败（${code}）`
      finish(() => reject(new Error(message)))
    }
    recognition.onend = () => {
      finish(() => reject(new Error('没有识别到内容')))
    }

    try {
      recognition.start()
    } catch {
      finish(() => reject(new Error('无法启动语音识别')))
    }
  })
}
