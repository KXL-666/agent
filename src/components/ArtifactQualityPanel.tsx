import { useEffect, useState } from 'react'
import { OFFICE_API_BASE } from '@/config/officeMode'

type QualityReport = {
  status: 'passed' | 'failed' | 'unchecked'
  checks: { label: string; ok: boolean | null; detail: string }[]
  metrics?: Record<string, number>
  attempts?: { attempt: number; stage?: string; status: string }[]
  review?: { status: string; visual?: boolean; sampled_pages?: number[]; issues?: unknown[] }
  pages?: string[]
}

type VerificationItem = { requirement_id: string; status: string; evidence?: string; location?: string; problem?: string; fix?: string }
type Provenance = {
  execution_style: 'collaborative' | 'quick' | 'legacy_unknown'
  overall_status: 'passed' | 'failed' | 'unchecked'
  failure_reason?: string
  contract: { status?: string; unchecked_reasons?: string[]; requirements?: { requirement_id: string; text: string; status: string; source?: { type: string; name?: string; location?: string } }[] }
  tasks: { id: string; title: string; assignee_roster?: number; role_scope?: string; requirement_ids?: string[] }[]
  handoffs: { id: number; task_id: string; from_roster: number; to_roster: number; status: string; reason?: string; output_refs: string[] }[]
  contributions: { task_id: string; roster: number; valid: boolean; reason?: string; preview?: string; files: { logical_path: string; sha256: string }[] }[]
  candidate: { id?: string; version?: number; sha256?: string; parent_id?: string }
  qa?: { status: string; reason?: string; candidate_sha256?: string; items?: VerificationItem[] }
  lead?: { status: string; reason?: string; candidate_sha256?: string; issues?: { location?: string; problem?: string; fix?: string }[] }
  usage: { usage_calls?: number; usage_tokens?: number; usage_cost?: number }
}

const STATUS_LABEL: Record<string, string> = { passed: '通过', failed: '失败', unchecked: '未验证', ready: '待接收', accepted: '已接收', rejected: '已拒收', blocked: '受阻' }
const CONTRACT_STATUS_LABEL: Record<string, string> = { ...STATUS_LABEL, ready: '已就绪' }
const ROSTER_LABEL: Record<number, string> = { 1: 'PO', 2: 'BE', 3: 'FE', 4: 'QA', 5: 'LEAD', 6: 'SM' }

export function ArtifactQualityPanel({ artifactId }: { artifactId: string }) {
  const [report, setReport] = useState<QualityReport | null>(null)
  const [provenance, setProvenance] = useState<Provenance | null>(null)
  const [provenanceError, setProvenanceError] = useState('')
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    fetch(`${OFFICE_API_BASE}/api/artifacts/${encodeURIComponent(artifactId)}/quality`, { signal: controller.signal })
      .then(async response => {
        if (response.status === 404) return { status: 'unchecked', checks: [] } as QualityReport
        if (!response.ok) throw new Error('暂时无法读取验收记录')
        return response.json() as Promise<QualityReport>
      })
      .then(setReport)
      .catch((reason: Error) => { if (reason.name !== 'AbortError') setError(reason.message) })
    fetch(`${OFFICE_API_BASE}/api/artifacts/${encodeURIComponent(artifactId)}/provenance`, { signal: controller.signal })
      .then(response => {
        if (!response.ok) throw new Error(`协作追溯读取失败（HTTP ${response.status}）`)
        return response.json() as Promise<Provenance>
      })
      .then(setProvenance)
      .catch((reason: Error) => { if (reason.name !== 'AbortError') setProvenanceError(reason.message) })
    return () => controller.abort()
  }, [artifactId])

  if (error) return <p role="alert">{error}</p>
  if (!report) return <p role="status">正在读取文件检查与页面渲染结果…</p>
  const overall = provenance?.overall_status ?? 'unchecked'
  return (
    <section className="artifact-quality-panel" aria-label="成品验收详情">
      <h3>成品验收：{STATUS_LABEL[overall] ?? '未验证'}</h3>
      {provenance?.failure_reason ? <p role="alert">未通过原因：{provenance.failure_reason}</p> : null}
      <p>文件结构检查：{STATUS_LABEL[report.status]}。最终状态以需求条款、QA 和 LEAD 对同一候选版本的证据为准。</p>
      {provenanceError ? <p role="alert">{provenanceError}；协作记录暂不可见，不能据此判断为没有协作。</p> : null}
      {provenance ? <div className="artifact-provenance">
        <h4>生成流程：{provenance.execution_style === 'collaborative' ? '六智能体协作' : provenance.execution_style === 'quick' ? '快速生成' : '旧版记录，协作模式未记录'}</h4>
        {provenance.execution_style === 'collaborative' ? <>
          <p>候选版本 v{provenance.candidate.version ?? 1} · SHA-256：<code>{provenance.candidate.sha256 || '未记录'}</code></p>
          <h4>PO 需求条款：{CONTRACT_STATUS_LABEL[provenance.contract.status ?? 'unchecked']}</h4>
          <ul>{provenance.contract.requirements?.map(clause => <li key={clause.requirement_id}>
            <strong>{clause.requirement_id} · {CONTRACT_STATUS_LABEL[clause.status] ?? clause.status}</strong> {clause.text}
            {clause.source?.type === 'attachment' ? <small>（{clause.source.name} {clause.source.location}）</small> : null}
          </li>)}</ul>
          {provenance.contract.unchecked_reasons?.map((reason, index) => <p key={index}>待核验：{reason}</p>)}
          <h4>分工与实际贡献</h4>
          <ul>{provenance.tasks.map(task => <li key={task.id}>
            <strong>{task.id} · {ROSTER_LABEL[task.assignee_roster ?? 0] ?? '未分派'} · {task.title}</strong>
            <p>{task.role_scope || '职责未记录'} · 条款 {task.requirement_ids?.join('、') || '未关联'}</p>
            {provenance.contributions.filter(item => item.task_id === task.id).map(item => <div key={item.task_id}>
              <p>贡献：{item.valid ? '文件与哈希一致' : `未核验：${item.reason || '文件缺失'}`} · {item.files.map(file => file.logical_path).join('、')}</p>
              {item.preview ? <details><summary>查看提交内容节选</summary><pre>{item.preview}</pre></details> : null}
            </div>)}
          </li>)}</ul>
          <details><summary>查看交接记录（{provenance.handoffs.length} 条）</summary>
            <ul>{provenance.handoffs.map(item => <li key={item.id}>
              {ROSTER_LABEL[item.from_roster]} → {ROSTER_LABEL[item.to_roster]} · {item.task_id || '全局'} · {STATUS_LABEL[item.status] ?? item.status}
              {item.reason ? `：${item.reason}` : ''} · 输出 {item.output_refs.join('、') || '无'}
            </li>)}</ul>
          </details>
          <h4>QA 逐条验证：{STATUS_LABEL[provenance.qa?.status ?? 'unchecked']}</h4>
          <p>QA 候选哈希：{provenance.qa?.candidate_sha256 || '未记录'} · {provenance.qa?.reason || '无附加说明'}</p>
          <ul>{provenance.qa?.items?.map((item, index) => <li key={`${item.requirement_id}-${index}`}>
            {item.requirement_id} · {STATUS_LABEL[item.status] ?? item.status} · {item.location || '未定位'} · {item.evidence || item.problem || '无证据'}
          </li>)}</ul>
          <h4>LEAD 独立决定：{STATUS_LABEL[provenance.lead?.status ?? 'unchecked']}</h4>
          <p>LEAD 候选哈希：{provenance.lead?.candidate_sha256 || '未记录'} · {provenance.lead?.reason || '无附加说明'}</p>
          {provenance.lead?.issues?.map((issue, index) => <p key={index}>{issue.location || '未定位'}：{issue.problem}；修复：{issue.fix}</p>)}
        </> : <p>此路线没有六人交接、QA 与 LEAD 的协作证据。</p>}
        <p>本轮用量：{provenance.usage.usage_calls ?? 0} 次调用，{provenance.usage.usage_tokens ?? 0} tokens，约 ¥{(provenance.usage.usage_cost ?? 0).toFixed(4)}。</p>
      </div> : null}
      <ul>{report.checks.map((check, index) => (
        <li key={index}>
          <strong>{check.ok === null ? '未验证' : check.ok ? '通过' : '未通过'} · {check.label}</strong>
          <p>{check.detail}</p>
        </li>
      ))}</ul>
      {report.review?.status === 'passed' || report.review?.status === 'failed' ? (
        <div>
          <h4>模型成品评审：{report.review.status === 'passed' ? '通过' : '需修正'}</h4>
          {report.review.visual ? <p>已检查截图，抽样页：{report.review.sampled_pages?.join('、')}。全部页面已单独完成渲染与边界检查。</p> : <p>本次评审依据实际文件内容，未完成视觉模型检查。</p>}
          {report.review.issues?.map((issue, index) => <p key={index}>{typeof issue === 'string' ? issue : JSON.stringify(issue)}</p>)}
        </div>
      ) : <p>本次没有有效的模型成品评审结论。</p>}
      {report.attempts?.length ? <p>制作与修订共 {report.attempts.length} 轮，保留检查结果较好的版本。</p> : null}
      {report.pages?.length ? (
        <details>
          <summary>查看真实页面（{report.pages.length} 页）</summary>
          <div className="artifact-quality-pages">
            {report.pages.map((_, index) => <img loading="lazy" key={index} alt={`第 ${index + 1} 页`} src={`${OFFICE_API_BASE}/api/artifacts/${encodeURIComponent(artifactId)}/quality/pages/${index + 1}`} />)}
          </div>
        </details>
      ) : null}
      <p className="view-card-meta">以上列出实际执行的检查范围；自动检查通过后仍可按实际使用场景继续验收。</p>
    </section>
  )
}
