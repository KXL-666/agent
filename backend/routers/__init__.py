"""路由模块包。

按业务域拆分 main.py 中的 REST/WebSocket 路由：
- sprint: Sprint 生命周期与任务管理
- uploads: 附件上传管理
- state: 状态查询、系统资源、搜索、文件中心、健康检查
- artifacts: 产物中心（下载/预览/质量/版本/重试/修订/编辑等）
- chat: 角色对话与流式对话
- agents: 成员配置与 API 测试
- usage: 用量统计与模型单价
- backup: 数据备份导入导出
- privacy: 本地模式能力声明
- ws: 办公室实时 WebSocket
"""
