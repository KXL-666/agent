import { useCallback, useEffect, useState, useSyncExternalStore } from 'react'
import { OFFICE_API_BASE } from '@/config/officeMode'
import {
  getArtifactsSnapshot,
  setArtifacts,
  subscribeArtifacts,
  type OfficeArtifact,
} from '@/store/officeArtifactStore'
import { assigneeName, statusMeta, type SprintTask } from '@/store/officeTaskStore'
import { showToast } from '@/store/officeUiStore'
import { getViewedSprint, subscribeView } from '@/store/officeViewStore'
import {
  ARTIFACT_KIND_LABELS,
  downloadArtifact,
  fileExt,
  timeLabel,
  type FilesPayload,
} from './officeViewUtils'

function ArtifactCard({ artifact }: { artifact: OfficeArtifact }) {
  const ext = fileExt(artifact)
  return (
    <details className="artifact-card">
      <summary>
        <span className={`artifact-kind kind-${artifact.kind}`}>
          {ARTIFACT_KIND_LABELS[artifact.kind] ?? '产物'}
        </span>
        <strong>{artifact.title}</strong>
        {ext ? <span className="artifact-file-badge">{ext}</span> : null}
        <em>
          {artifact.sprint_id === 'solo' ? '单飞 · ' : ''}
          {assigneeName(artifact.author_roster)}
          {artifact.task_id ? ` · ${artifact.task_id}` : ''}
        </em>
      </summary>
      <pre className="view-md artifact-content">{artifact.content}</pre>
      {artifact.file_name ? (
        <footer className="artifact-card-actions">
          <button type="button" onClick={() => downloadArtifact(artifact)}>
            下载 {ext}
          </button>
        </footer>
      ) : null}
    </details>
  )
}

export function FilesView() {
  const [data, setData] = useState<FilesPayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const viewedSprint = useSyncExternalStore(subscribeView, getViewedSprint)
  const artifacts = useSyncExternalStore(
    subscribeArtifacts,
    getArtifactsSnapshot,
  )

  const load = useCallback(async () => {
    setLoading(true)
    try {
      if (!viewedSprint) {
        const res = await fetch(`${OFFICE_API_BASE}/api/files`)
        if (!res.ok) throw new Error(String(res.status))
        const payload = (await res.json()) as FilesPayload
        setData(payload)
        setArtifacts(payload.artifacts ?? [])
      } else {
        // 历史 Sprint：无日报，需求/任务/产物取自详情接口
        const res = await fetch(`${OFFICE_API_BASE}/api/sprints/${viewedSprint}`)
        if (!res.ok) throw new Error(String(res.status))
        const payload = (await res.json()) as {
          sprint?: { requirement?: string; started_at?: number }
          tasks?: SprintTask[]
          artifacts?: OfficeArtifact[]
        }
        setData({
          requirement: payload.sprint?.requirement ?? '',
          running: false,
          updated_at: payload.sprint?.started_at ?? 0,
          tasks: payload.tasks ?? [],
          artifacts: payload.artifacts ?? [],
          report_md: '',
        })
        setArtifacts(payload.artifacts ?? [])
      }
      setError(null)
    } catch {
      setError('无法连接后端，请确认服务已启动')
    } finally {
      setLoading(false)
    }
  }, [viewedSprint])

  useEffect(() => {
    void load()
  }, [load])

  function downloadReport() {
    if (!data?.report_md) return
    const blob = new Blob([data.report_md], {
      type: 'text/markdown;charset=utf-8',
    })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = 'sprint-report.md'
    anchor.click()
    URL.revokeObjectURL(url)
    showToast('Sprint 日报已下载')
  }

  return (
    <div className="view-page">
      <header className="view-head">
        <div>
          <h1>过程文件</h1>
          <p>Sprint 产物（PRD / 开发说明 / 测试报告 / 评审记录 / 总结）、任务清单与日报</p>
        </div>
        <div className="view-head-actions">
          <button type="button" className="view-ghost-btn" onClick={load}>
            {loading ? '刷新中…' : '刷新'}
          </button>
          <button
            type="button"
            className="view-primary-btn"
            onClick={downloadReport}
            disabled={!data?.report_md}
          >
            下载日报
          </button>
        </div>
      </header>

      {error ? <div className="view-empty">{error}</div> : null}

      {!error && data ? (
        data.requirement || data.tasks.length > 0 || artifacts.length > 0 ? (
          <>
            <section className="view-card">
              <h2>需求描述</h2>
              <p className="view-card-text">
                {data.requirement || '（未记录）'}
              </p>
              <p className="view-card-meta">
                状态：{data.running ? 'Sprint 进行中' : '空闲/已完成'} ·
                更新于 {timeLabel(data.updated_at)}
              </p>
            </section>

            <section className="view-card">
              <h2>Sprint 产物（{artifacts.length}）</h2>
              {artifacts.length === 0 ? (
                <p className="view-card-meta">
                  暂无产物。Sprint 运行中，各环节（拆解 / 开发 / 测试 / 评审 / 复盘）完成后会实时出现在这里。
                </p>
              ) : (
                <>
                  <div className="artifact-list">
                    {artifacts.map((artifact) => (
                      <ArtifactCard key={artifact.id} artifact={artifact} />
                    ))}
                  </div>
                  <p className="view-card-meta">
                    <a
                      href={`${OFFICE_API_BASE}/api/artifacts/code.zip`}
                    >
                      下载本次 Sprint 全部代码（zip）
                    </a>
                  </p>
                </>
              )}
            </section>

            <section className="view-card">
              <h2>任务清单（{data.tasks.length}）</h2>
              {data.tasks.length === 0 ? (
                <p className="view-card-meta">暂无任务</p>
              ) : (
                <ul className="view-task-rows">
                  {data.tasks.map((task) => {
                    const meta = statusMeta(task.status)
                    return (
                      <li key={task.id}>
                        <span className={`tone-dot ${meta.tone}`} />
                        <strong>{task.title || task.id}</strong>
                        <em>{assigneeName(task.assignee_roster)}</em>
                        <span>{task.status}</span>
                      </li>
                    )
                  })}
                </ul>
              )}
            </section>

            <section className="view-card">
              <h2>Sprint 日报预览</h2>
              <pre className="view-md">{data.report_md}</pre>
            </section>
          </>
        ) : (
          <div className="view-empty">
            还没有 Sprint 产物。发起一次 Sprint 后，需求、任务、各环节产物与日报会出现在这里。
          </div>
        )
      ) : null}
    </div>
  )
}
