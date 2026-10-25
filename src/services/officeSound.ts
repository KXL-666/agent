/** 提示音：Web Audio 合成短音，无需音频文件 */

let ctx: AudioContext | null = null

/**
 * 浏览器自动播放策略要求页面被用户交互过才允许发声。
 * 在首次点击/按键时预热上下文，后续 Sprint 结束提示音就不会被静默拦截。
 */
function audioContext(): AudioContext | null {
  try {
    ctx ??= new AudioContext()
    if (ctx.state === 'suspended') void ctx.resume()
    return ctx
  } catch {
    /* 音频不可用时静默跳过，不影响流程 */
    return null
  }
}

export function primeAudio() {
  audioContext()
}

function playTones(
  notes: number[],
  { volume, gap, decay }: { volume: number; gap: number; decay: number },
) {
  const audio = audioContext()
  if (!audio) return
  for (const [i, freq] of notes.entries()) {
    const osc = audio.createOscillator()
    const gain = audio.createGain()
    osc.type = 'sine'
    osc.frequency.value = freq
    const t = audio.currentTime + i * gap
    gain.gain.setValueAtTime(0.001, t)
    gain.gain.exponentialRampToValueAtTime(volume, t + 0.02)
    gain.gain.exponentialRampToValueAtTime(0.001, t + decay)
    osc.connect(gain).connect(audio.destination)
    osc.start(t)
    osc.stop(t + decay)
  }
}

/** 整轮 Sprint 结束：A5 → D6 上行双音 */
export function playDoneSound() {
  playTones([880, 1174.66], { volume: 0.6, gap: 0.18, decay: 0.6 })
}

/** 单个任务流转：更轻更短的 B5 → E6 提示音，避免频繁打扰 */
export function playTaskSound() {
  playTones([987.77, 1318.51], { volume: 0.22, gap: 0.1, decay: 0.28 })
}
