/** 完成提示音：Web Audio 合成「叮-咚」双音，无需音频文件 */

let ctx: AudioContext | null = null

export function playDoneSound() {
  try {
    ctx ??= new AudioContext()
    // 浏览器自动播放策略：需用户交互过页面，Sprint 由点击发起通常已满足
    if (ctx.state === 'suspended') void ctx.resume()
    const notes = [880, 1174.66] // A5 → D6 上行双音
    for (const [i, freq] of notes.entries()) {
      const osc = ctx.createOscillator()
      const gain = ctx.createGain()
      osc.type = 'sine'
      osc.frequency.value = freq
      const t = ctx.currentTime + i * 0.18
      gain.gain.setValueAtTime(0.001, t)
      gain.gain.exponentialRampToValueAtTime(0.6, t + 0.02)
      gain.gain.exponentialRampToValueAtTime(0.001, t + 0.6)
      osc.connect(gain).connect(ctx.destination)
      osc.start(t)
      osc.stop(t + 0.55)
    }
  } catch {
    /* 音频不可用时静默跳过，不影响流程 */
  }
}
