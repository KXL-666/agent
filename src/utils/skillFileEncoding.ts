export type SkillFileEncoding = 'auto' | 'utf-8' | 'gb18030' | 'utf-16le' | 'utf-16be'

function decode(bytes: Uint8Array, encoding: Exclude<SkillFileEncoding, 'auto'>) {
  const text = new TextDecoder(encoding, { fatal: true }).decode(bytes).replace(/^\uFEFF/, '')
  if (text.includes('\uFFFD')) {
    throw new Error('原文件包含无法恢复的替代字符（�），请从原始文档重新导出后导入')
  }
  if (Array.from(text).some(char => {
    const code = char.charCodeAt(0)
    return (code < 32 && code !== 9 && code !== 10 && code !== 13) || code === 127
  })) {
    throw new Error('文件包含非文本字符，请确认文件格式或手动选择正确的文件编码')
  }
  return { text, encoding }
}

/** Read original bytes before decoding: Blob.text() irreversibly replaces invalid UTF-8. */
export function decodeSkillFile(bytes: Uint8Array, encoding: SkillFileEncoding = 'auto') {
  if (encoding !== 'auto') {
    try { return decode(bytes, encoding) }
    catch { throw new Error('无法按所选编码读取文件，或原文件已含乱码。请选择正确编码或从原始文档重新导出') }
  }
  if ((bytes[0] === 0xFF && bytes[1] === 0xFE && bytes[2] === 0 && bytes[3] === 0)
    || (bytes[0] === 0 && bytes[1] === 0 && bytes[2] === 0xFE && bytes[3] === 0xFF)) {
    throw new Error('暂不支持 UTF-32 文件，请另存为 UTF-8 后导入')
  }
  // A BOM is authoritative; never reinterpret a damaged Unicode file as GBK.
  if (bytes[0] === 0xFF && bytes[1] === 0xFE) return decode(bytes, 'utf-16le')
  if (bytes[0] === 0xFE && bytes[1] === 0xFF) return decode(bytes, 'utf-16be')
  if (bytes[0] === 0xEF && bytes[1] === 0xBB && bytes[2] === 0xBF) return decode(bytes, 'utf-8')
  // Recognize common BOM-less UTF-16 Markdown by zero bytes in the ASCII lane.
  const pairs = Math.min(Math.floor(bytes.length / 2), 1024)
  let evenZeros = 0
  let oddZeros = 0
  for (let i = 0; i < pairs; i++) {
    if (bytes[i * 2] === 0) evenZeros++
    if (bytes[i * 2 + 1] === 0) oddZeros++
  }
  if (pairs >= 2 && oddZeros / pairs > .2 && evenZeros / pairs < .02) return decode(bytes, 'utf-16le')
  if (pairs >= 2 && evenZeros / pairs > .2 && oddZeros / pairs < .02) return decode(bytes, 'utf-16be')
  // Prefer valid UTF-8; Windows ANSI Chinese files usually use GBK/GB18030.
  let validUtf8 = false
  try { new TextDecoder('utf-8', { fatal: true }).decode(bytes); validUtf8 = true } catch { /* try GB18030 */ }
  if (validUtf8) return decode(bytes, 'utf-8')
  try { return decode(bytes, 'gb18030') }
  catch { throw new Error('无法识别文件编码，请手动选择编码，或将原文件另存为 UTF-8 后重新导入') }
}
