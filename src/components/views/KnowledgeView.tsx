import { UploadedKnowledge } from '../UploadedKnowledge'

export function KnowledgeView() {
  return (
    <div className="view-page">
      <header className="view-head">
        <div>
          <h1>参考资料</h1>
          <p>管理上传的资料，供团队在任务中查阅。</p>
        </div>
      </header>
      <UploadedKnowledge />
    </div>
  )
}
