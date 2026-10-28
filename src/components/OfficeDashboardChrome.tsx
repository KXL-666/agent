/** 仪表盘外壳组件的聚合入口
 *
 * 各组件已按职责拆分到 dashboard/ 目录，本文件仅做 re-export，
 * 保持外部 import 路径（@/components/OfficeDashboardChrome）不变。
 *
 * 组件分布：
 * - dashboard/officeSidebar.tsx        侧边栏导航
 * - dashboard/officeHeader.tsx         顶部统计、系统资源、进度条、历史选择器
 * - dashboard/taskPanels.tsx           任务卡、任务详情弹窗、右侧任务面板
 * - dashboard/sprintDialog.tsx         新建 Sprint 弹窗
 * - dashboard/officeBottomToolbar.tsx  底部工具栏与快捷工具
 * - dashboard/officeToast.tsx          全局 Toast 提示
 */

export { OfficeSidebar } from './dashboard/officeSidebar'
export { OfficeHeaderStats } from './dashboard/officeHeader'
export { TaskDetailDialog, TaskCard, OfficeRightPanel } from './dashboard/taskPanels'
export { OfficeBottomToolbar } from './dashboard/officeBottomToolbar'
export { OfficeToast } from './dashboard/officeToast'
