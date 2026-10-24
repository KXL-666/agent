import { useEffect, useRef, useState, type FormEvent } from 'react'
import { OFFICE_API_BASE } from '@/config/officeMode'
import { AGENT_ROSTER } from '@/scene/layout/officeLayout'
import { decodeSkillFile, type SkillFileEncoding } from '@/utils/skillFileEncoding'

type Skill = { id: string; name: string; description: string; instructions: string; rosters: number[]; enabled: boolean;
  source?: string; install_status?: string; install_issues?: string[]; tools?: { path: string; runtime: string }[] }
type Draft = Omit<Skill, 'id'> & { id?: string }
const emptyDraft = (): Draft => ({ name: '', description: '', instructions: '', rosters: [], enabled: true })
const templates = [
  { name: '专业周报', description: '撰写周报、工作总结和进度汇报时使用。', instructions: '# 周报写作\n1. 先整理本周完成事项、量化结果、风险与下周计划。\n2. 缺失的数据明确标记为“待补充”，不得编造事实或数字。\n3. 正文按“本周成果 / 问题与风险 / 下周计划”组织，结论在前。\n4. 提交前核对日期、负责人、任务状态，保持表述简洁。' },
  { name: '代码质量审查', description: '开发、测试、评审代码时使用。', instructions: '# 代码质量审查\n1. 检查需求覆盖、边界条件、异常处理和输入校验。\n2. 检查密钥泄露、注入风险、权限和文件路径处理。\n3. 按严重程度列出发现，写明触发条件、影响和修复建议。\n4. 区分已验证的问题与推测；未执行的测试不得宣称通过。\n5. 保持当前任务要求的输出格式。' },
  { name: '数据分析规范', description: '处理表格、统计指标和撰写数据结论时使用。', instructions: '# 数据分析\n1. 先确认指标定义、时间范围、单位和数据来源。\n2. 检查空值、重复值和异常值，说明处理方式。\n3. 计算时展示公式和口径，区分相关关系与因果关系。\n4. 缺少数据时提出需要补充的字段，不编造分析结果。\n5. 输出核心发现、证据、局限和可执行建议。' },
]

async function request<T>(path = '', method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch(`${OFFICE_API_BASE}/api/skills${path}`, {
    method, headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await response.json()
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败，请检查字段长度和后端状态')
  return data as T
}

export function SkillsManager({ names }: { names: string[] }) {
  const [skills, setSkills] = useState<Skill[]>([])
  const [draft, setDraft] = useState<Draft | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [query, setQuery] = useState('')
  const [member, setMember] = useState(1)
  const [preview, setPreview] = useState('')
  const [trial, setTrial] = useState('')
  const [result, setResult] = useState('')
  const [deleteId, setDeleteId] = useState<string | null>(null)
  const [fileEncoding, setFileEncoding] = useState<SkillFileEncoding>('auto')
  const [packageUrl, setPackageUrl] = useState('')
  const [packageFiles, setPackageFiles] = useState<Record<string, { path: string; size: number }[]>>({})
  const importRef = useRef<HTMLInputElement>(null)
  const zipRef = useRef<HTMLInputElement>(null)
  const editorRef = useRef<HTMLElement>(null)
  const editorOpen = draft !== null
  const editorId = draft?.id

  async function load() {
    setLoading(true)
    try { setSkills((await request<{ skills: Skill[] }>()).skills); setError('') }
    catch (err) { setError(String(err instanceof Error ? err.message : err)) }
    finally { setLoading(false) }
  }
  useEffect(() => { void load() }, [])
  useEffect(() => { if (editorOpen) editorRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }) }, [editorOpen, editorId])

  async function mutate(action: () => Promise<unknown>, message: string) {
    setBusy(true); setError(''); setNotice('')
    try {
      await action()
      setSkills((await request<{ skills: Skill[] }>()).skills)
      setNotice(message); setPreview(''); setResult(''); setDeleteId(null)
    } catch (err) { setError(err instanceof Error ? err.message : String(err)) }
    finally { setBusy(false) }
  }
  function edit(value: Draft) { setDraft({ ...value, rosters: [...value.rosters] }); setNotice(''); setDeleteId(null) }
  async function save(event: FormEvent) {
    event.preventDefault()
    if (!draft) return
    await mutate(async () => {
      await request(draft.id ? `/${draft.id}` : '', draft.id ? 'PUT' : 'POST', draft)
      setDraft(null)
    }, '技能已保存。从成员的下一次模型调用开始生效。')
  }
  async function importFile(file?: File) {
    if (!file) return
    if (file.size > 64000) { setError('文件过大，请导入 64KB 以内的 Markdown 文件'); return }
    await mutate(async () => {
      const decoded = decodeSkillFile(new Uint8Array(await file.arrayBuffer()), fileEncoding)
      const imported = await request<Skill>('/import', 'POST', { markdown: decoded.text })
      setDraft(imported)
    }, '已导入为停用草稿，请核对指令、分配成员并启用。')
  }
  async function installUrl() {
    if (!packageUrl.trim()) return
    await mutate(async () => {
      await request('/install-url', 'POST', { url: packageUrl.trim(), roster: member })
      setPackageUrl('')
    }, '已下载完整技能包；请查看每项技能的依赖与运行状态。')
  }
  async function installZip(file?: File) {
    if (!file) return
    await mutate(async () => {
      const form = new FormData()
      form.append('roster', String(member))
      form.append('file', file)
      const response = await fetch(`${OFFICE_API_BASE}/api/skills/install-zip`, { method: 'POST', body: form })
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || '技能包安装失败')
    }, '已导入完整 ZIP 技能包；请查看每项技能的依赖与运行状态。')
  }
  async function showPackageFiles(skill: Skill) {
    try {
      const roster = skill.rosters[0] ?? member
      const data = await request<{ files: { path: string; size: number }[] }>(`/${skill.id}/files?roster=${roster}`)
      setPackageFiles(previous => ({ ...previous, [skill.id]: data.files }))
    } catch (err) { setError(err instanceof Error ? err.message : String(err)) }
  }
  async function inspect(runTrial = false) {
    setBusy(true); setError(''); setResult(''); setPreview('')
    try {
      const data = await request<{ prompt: string }>(`/member/${member}`)
      setPreview(data.prompt || '该成员没有启用的技能。')
      if (runTrial) {
        const response = await request<{ reply: string; model: string; skills: { name: string }[] }>('/trial', 'POST', { roster: member, message: trial })
        setResult(`模型：${response.model}\n已加载技能：${response.skills.map(s => s.name).join('、') || '无'}\n\n${response.reply}`)
      }
    } catch (err) { setError(err instanceof Error ? err.message : String(err)) }
    finally { setBusy(false) }
  }
  function download(skill: Skill) {
    if (skill.source) {
      const anchor = document.createElement('a')
      anchor.href = `${OFFICE_API_BASE}/api/skills/${skill.id}/export?roster=${skill.rosters[0] ?? member}`
      anchor.download = `${skill.name}.zip`
      anchor.click()
      return
    }
    const text = `---\nname: ${JSON.stringify(skill.name)}\ndescription: ${JSON.stringify(skill.description)}\n---\n\n${skill.instructions}\n`
    const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }))
    const anchor = document.createElement('a'); anchor.href = url; anchor.download = 'SKILL.md'; anchor.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
  const visible = skills.filter(s => `${s.name} ${s.description}`.toLowerCase().includes(query.toLowerCase()))
  return (
    <div className="skills-manager">
      <details className="view-card skill-library">
        <summary className="skill-library-summary"><h2>技能库</h2><span className="skill-library-chevron" aria-hidden="true" /></summary>
        <div className="skill-section-head">
          <p>把工作方法教给成员，保存后在聊天、Sprint 与产物生成时加载。</p>
          <div className="skill-actions">
            <label>文件编码 <select aria-label="导入文件编码" value={fileEncoding} disabled={busy} onChange={e => setFileEncoding(e.target.value as SkillFileEncoding)}>
              <option value="auto">自动识别</option>
              <option value="utf-8">UTF-8</option>
              <option value="gb18030">GBK / GB18030（中文 ANSI）</option>
              <option value="utf-16le">UTF-16 LE</option>
              <option value="utf-16be">UTF-16 BE</option>
            </select></label>
            <button type="button" disabled={busy || loading} onClick={() => importRef.current?.click()}>导入 SKILL.md</button>
            <button type="button" className="view-primary-btn" disabled={busy || loading} onClick={() => edit(emptyDraft())}>＋ 新建技能</button>
            <input ref={importRef} hidden type="file" accept=".md,.markdown,.txt" onChange={e => { void importFile(e.target.files?.[0]); e.target.value = '' }} />
            <input ref={zipRef} hidden type="file" accept=".zip,application/zip" onChange={e => { void installZip(e.target.files?.[0]); e.target.value = '' }} />
          </div>
        </div>
        <div className="skill-explanation">完整技能包会保留脚本与附件，识别 Python / Node 依赖并注册可运行脚本。依赖未就绪的技能会标明原因且不会启用；单独导入 SKILL.md 仍只有文字指令。演示模式不会执行技能。</div>
        <div className="skill-actions">
          <select aria-label="安装给成员" value={member} disabled={busy} onChange={e => setMember(Number(e.target.value))}>{names.map((name, i) => <option key={i} value={i + 1}>{name}</option>)}</select>
          <input aria-label="GitHub 或 ZIP 链接" placeholder="GitHub 技能目录或 HTTPS ZIP 链接" value={packageUrl} onChange={e => setPackageUrl(e.target.value)} />
          <button type="button" disabled={busy || !packageUrl.trim()} onClick={() => void installUrl()}>下载安装完整技能包</button>
          <button type="button" disabled={busy} onClick={() => zipRef.current?.click()}>导入 ZIP 技能包</button>
        </div>
        {error && <div className="skill-error" role="alert">{error} <button type="button" disabled={busy} onClick={() => void load()}>重新加载</button></div>}
        {notice && <p className="skill-notice" role="status">{notice}</p>}
        <div className="skill-templates"><span>从模板开始</span>{templates.map(t => <button type="button" key={t.name} disabled={busy || loading} onClick={() => edit({ ...emptyDraft(), ...t })}>{t.name} ＋</button>)}</div>
        <input className="skill-search" aria-label="搜索技能" placeholder="搜索技能名称或适用场景…" value={query} onChange={e => setQuery(e.target.value)} />
        {loading ? <p role="status">正在加载技能…</p> : skills.length === 0 ? <div className="skill-empty">还没有配置技能。新建一个技能，或导入已有 SKILL.md，再分配给需要它的成员。</div> : visible.length === 0 ? <p>没有匹配的技能。</p> : <div className="skill-library-grid">{visible.map(skill => (
          <details className="skill-item" key={skill.id}>
            <summary className="skill-item-summary"><h3>{skill.name}</h3><span className="skill-item-chevron" aria-hidden="true" /></summary>
            <div className="skill-item-content">
            <label className="skill-toggle"><input type="checkbox" checked={skill.enabled} disabled={busy} onChange={e => void mutate(() => request(`/${skill.id}`, 'PUT', { ...skill, enabled: e.target.checked }), '技能状态已更新。')} />{skill.enabled ? '已启用' : '已停用'}</label>
            <p>{skill.description || '未填写适用场景'}</p>
            <div className="agent-skill-row">{skill.rosters.length ? skill.rosters.map(no => <span className="view-chip small" key={no}>{names[no - 1]}</span>) : <span className="view-chip small">待分配成员</span>}</div>
            <small>{skill.instructions.length} 字指令 · {skill.install_status === 'ready' ? `可运行脚本 ${skill.tools?.length ?? 0} 个` : skill.install_status === 'needs_attention' ? '依赖未就绪' : skill.enabled && skill.rosters.length ? '下次调用加载' : '尚未生效'}</small>
            {skill.source ? <small>来源：{skill.source}</small> : null}
            {skill.tools?.length ? <small>脚本：{skill.tools.map(tool => tool.path).join('、')}</small> : null}
            {skill.install_issues?.length ? <p className="skill-error">{skill.install_issues.join('；')}</p> : null}
            {skill.source ? <button type="button" onClick={() => void showPackageFiles(skill)}>查看脚本与附件</button> : null}
            {skill.install_status === 'needs_attention' ? <button type="button" disabled={busy} onClick={() => void mutate(() => request(`/${skill.id}/repair`, 'POST'), '已重新检查技能依赖。')}>重试依赖安装</button> : null}
            {packageFiles[skill.id]?.length ? <ul>{packageFiles[skill.id].map(file => <li key={file.path}><a href={`${OFFICE_API_BASE}/api/skills/${skill.id}/files/${file.path.split('/').map(encodeURIComponent).join('/')}?roster=${skill.rosters[0] ?? member}`} download>{file.path}</a> <small>{file.size} B</small></li>)}</ul> : null}
            <div className="skill-actions"><button type="button" disabled={busy} onClick={() => edit(skill)}>编辑与分配</button><button type="button" onClick={() => download(skill)}>{skill.source ? '导出完整技能包' : '导出'}</button><button type="button" disabled={busy} onClick={() => setDeleteId(skill.id)}>删除</button></div>
            {deleteId === skill.id && <div className="skill-delete" role="alert">删除后所有成员将停止加载此技能。<button type="button" disabled={busy} onClick={() => void mutate(async () => { await request(`/${skill.id}`, 'DELETE'); if (draft?.id === skill.id) setDraft(null) }, '技能已删除。')}>确认删除</button><button type="button" onClick={() => setDeleteId(null)}>取消</button></div>}
            </div>
          </details>
        ))}</div>}
      </details>

      {draft && <section className="view-card skill-editor" ref={editorRef} aria-label="技能编辑器">
        <div className="skill-section-head"><h2>{draft.id ? '编辑技能' : '新建技能'}</h2><button type="button" disabled={busy} onClick={() => setDraft(null)}>关闭编辑</button></div>
        <form onSubmit={e => void save(e)}>
          <label>技能名称<input required maxLength={80} value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })} placeholder="例如：毕业论文写作规范" /></label>
          <label>适用场景<textarea rows={2} maxLength={1000} value={draft.description} onChange={e => setDraft({ ...draft, description: e.target.value })} placeholder="说明什么时候使用这个技能" /></label>
          <label>技能指令（Markdown）<textarea required rows={11} maxLength={12000} value={draft.instructions} onChange={e => setDraft({ ...draft, instructions: e.target.value })} placeholder="写清步骤、标准、示例以及交付前的检查项…" /><small>{draft.instructions.length} / 12000 字</small></label>
          <fieldset><legend>分配给成员</legend><div className="skill-assignees">{AGENT_ROSTER.map((entry, i) => <label key={entry.id}><input type="checkbox" checked={draft.rosters.includes(i + 1)} onChange={e => setDraft({ ...draft, rosters: e.target.checked ? [...draft.rosters, i + 1] : draft.rosters.filter(no => no !== i + 1) })} /><span>{names[i]}<small>{entry.role}</small></span></label>)}</div></fieldset>
          <label className="skill-toggle"><input type="checkbox" checked={draft.enabled} onChange={e => setDraft({ ...draft, enabled: e.target.checked })} />启用此技能{draft.rosters.length === 0 && '（尚未分配成员，保存后仍不会加载）'}</label>
          <div className="skill-actions"><button className="view-primary-btn" type="submit" disabled={busy}>{busy ? '保存中…' : '保存技能'}</button><button type="button" disabled={busy} onClick={() => setDraft(null)}>取消</button></div>
        </form>
      </section>}

      <section className="view-card skill-trial">
        <h2>成员技能检查与试用</h2><p>预览实际加载的技能指令，或用一条任务检查效果。试用会调用该成员的 API 并产生模型用量。</p>
        <div className="skill-actions"><select aria-label="选择试用成员" value={member} disabled={busy} onChange={e => { setMember(Number(e.target.value)); setPreview(''); setResult('') }}>{names.map((name, i) => <option key={i} value={i + 1}>{name} · {AGENT_ROSTER[i].role}</option>)}</select><button type="button" disabled={busy} onClick={() => void inspect()}>预览已加载指令</button></div>
        <textarea aria-label="技能试用任务" rows={3} maxLength={2000} value={trial} onChange={e => setTrial(e.target.value)} placeholder="输入一条与技能相关的任务，例如：根据以下事实撰写本周周报…" />
        <button type="button" className="view-primary-btn" disabled={busy || !trial.trim()} onClick={() => void inspect(true)}>{busy ? '处理中…' : '调用模型试用'}</button>
        {preview && <details open><summary>实际加载的技能指令</summary><pre>{preview}</pre></details>}
        {result && <div className="skill-result" role="status"><h3>试用结果</h3><pre>{result}</pre></div>}
      </section>
    </div>
  )
}
