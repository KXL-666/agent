import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { AGENT_PROFILES } from '@/data/agentProfiles'
import { AGENT_ROSTER } from '@/scene/layout/officeLayout'

type Props = {
  roster: number
  name: string
  avatar: string
  status: string
  task: string
  onClose: () => void
  onChangeMember: (roster: number) => void
  onChat: () => void
  onSkills: () => void
}

export function AgentProfileDialog({ roster, name, avatar, status, task, onClose, onChangeMember, onChat, onSkills }: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const headingRef = useRef<HTMLHeadingElement>(null)
  const bodyRef = useRef<HTMLDivElement>(null)
  const entry = AGENT_ROSTER[roster - 1]
  const profile = AGENT_PROFILES[roster - 1]
  useEffect(() => {
    const dialog = dialogRef.current
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    dialog?.showModal()
    return () => { dialog?.close(); if (opener?.isConnected) opener.focus() }
  }, [])
  useEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = 0
    headingRef.current?.focus()
  }, [roster])

  return createPortal(
    <dialog ref={dialogRef} className="agent-profile-dialog" aria-labelledby="agent-profile-title"
      onCancel={event => { event.preventDefault(); onClose() }}
      onClick={event => {
        if (event.target !== event.currentTarget) return
        const bounds = event.currentTarget.getBoundingClientRect()
        if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) onClose()
      }}>
      <header className="agent-profile-header">
        <span className="agent-avatar" style={{ background: `#${entry.color.toString(16).padStart(6, '0')}` }}>{avatar || name.slice(0, 1)}</span>
        <div><span className="agent-profile-eyebrow">团队成员 {roster} / 6 · {entry.role}</span><h2 id="agent-profile-title" ref={headingRef} tabIndex={-1}>{name}的详细介绍</h2></div>
        <button type="button" className="agent-profile-close" aria-label="关闭详细介绍" onClick={onClose}>×</button>
      </header>
      <div className="agent-profile-body" ref={bodyRef}>
        <p className="agent-profile-summary">{profile.summary}</p>
        <div className="agent-profile-status"><span>当前状态：{status}</span><span>当前任务：{task || '暂无任务'}</span></div>
        <div className="agent-skill-row">{entry.skills.map(skill => <span className="view-chip small" key={skill}>{skill}</span>)}</div>
        <section><h3>主要职责</h3><ul>{profile.responsibilities.map(item => <li key={item}>{item}</li>)}</ul></section>
        <section><h3>通常交付什么</h3><div className="agent-profile-deliverables">{profile.deliverables.map(item => <span key={item}>{item}</span>)}</div></section>
        <div className="agent-profile-columns"><section><h3>在流程中怎么工作</h3><p>{profile.workflow}</p></section><section><h3>与谁协作</h3><p>{profile.collaboration}</p></section></div>
        <section className="agent-profile-example"><h3>可以这样交给 TA</h3><p>“{profile.example}”</p></section>
        <section className="agent-profile-scope"><h3>当前能力说明</h3><p>{profile.scope}</p><p>在「团队 → 技能」分配并启用的技能，会在该成员下一次真实模型调用时加载。</p></section>
      </div>
      <footer className="agent-profile-footer">
        <nav aria-label="切换成员介绍"><button type="button" onClick={() => onChangeMember(roster === 1 ? 6 : roster - 1)}>← 上一位</button><button type="button" onClick={() => onChangeMember(roster === 6 ? 1 : roster + 1)}>下一位 →</button></nav>
        <div><button type="button" onClick={onSkills}>配置技能</button><button type="button" className="agent-profile-primary" onClick={onChat}>找 TA 聊聊</button></div>
      </footer>
    </dialog>, document.body,
  )
}
