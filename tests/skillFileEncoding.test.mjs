import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { test } from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/utils/skillFileEncoding.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2023 } })
const { decodeSkillFile } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)

test('UTF-8 and UTF-8 BOM preserve Chinese skill instructions', () => {
  const text = '# 中文技能\n1. 输出周报，核对数据。'
  for (const prefix of ['', '\uFEFF']) assert.equal(decodeSkillFile(new TextEncoder().encode(prefix + text)).text, text)
})

test('GBK / Windows ANSI bytes import without replacement characters', () => {
  // GBK fixture for "# 中文技能\n1. 输出周报"; do not create it via a UTF-8 text reader.
  const bytes = Uint8Array.from([35,32,0xD6,0xD0,0xCE,0xC4,0xBC,0xBC,0xC4,0xDC,10,49,46,32,0xCA,0xE4,0xB3,0xF6,0xD6,0xDC,0xB1,0xA8])
  assert.equal(decodeSkillFile(bytes).text, '# 中文技能\n1. 输出周报')
  assert.equal(decodeSkillFile(bytes).encoding, 'gb18030')
  assert.equal(decodeSkillFile(bytes, 'gb18030').text, '# 中文技能\n1. 输出周报')
  assert.throws(() => decodeSkillFile(bytes, 'utf-8'), /编码/)
})

test('UTF-16 little and big endian, with and without BOM', () => {
  const text = '# 中文技能\n1. 输出周报'
  const littleEndian = Buffer.from(text, 'utf16le')
  const bigEndian = Buffer.from(littleEndian).swap16()
  for (const bytes of [littleEndian, Buffer.concat([Buffer.from([255, 254]), littleEndian]), bigEndian, Buffer.concat([Buffer.from([254, 255]), bigEndian])]) {
    assert.equal(decodeSkillFile(bytes).text, text)
  }
})

test('GB18030 supports four-byte Chinese characters', () => {
  assert.equal(decodeSkillFile(Uint8Array.from([0x95,0x32,0x82,0x36])).text, '𠀀')
})

test('damaged, binary and previously corrupted files are rejected instead of saved', () => {
  for (const bytes of [Uint8Array.from([0xFF]), Uint8Array.from([0,1,2,3]), new TextEncoder().encode('# 已经损坏�'), Uint8Array.from([0xEF,0xBB,0xBF,0xFF]), Uint8Array.from([0xFF,0xFE,0x61])]) {
    assert.throws(() => decodeSkillFile(bytes))
  }
})
