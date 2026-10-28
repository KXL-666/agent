import { useEffect, useState, useSyncExternalStore } from 'react'
import { OFFICE_API_BASE } from '@/config/officeMode'
import { getSprintMetaSnapshot, subscribeSprintData } from '@/store/officeTaskStore'
import {
  getSprintHistory,
  getViewedSprint,
  subscribeView,
} from '@/store/officeViewStore'
import { showToast } from '@/store/officeUiStore'

type UsageMember = {
  roster: number
  name?: string
  model: string
  calls: number
  prompt_tokens: number
  completion_tokens: number
  cost: number
}

type UsageReport = {
  members: UsageMember[]
  total_cost: number
  estimated: boolean
}

type PricingPayload = {
  prices: Record<string, [number, number]>
  default: [number, number]
  builtin: string[]
}

/** 单价编辑行：reset=true 表示保存时提交 null 恢复内置价 */
type PriceRow = { model: string; input: string; output: string; reset: boolean }

export function UsagePanel() {
  const [usage, setUsage] = useState<UsageReport | null>(null)
  const sprint = useSyncExternalStore(subscribeSprintData, getSprintMetaSnapshot)
  const viewedSprint = useSyncExternalStore(subscribeView, getViewedSprint)
  const history = useSyncExternalStore(subscribeView, getSprintHistory)
  const [priceRows, setPriceRows] = useState<PriceRow[]>([])
  const [pricingLoaded, setPricingLoaded] = useState(false)
  const [pricingSaving, setPricingSaving] = useState(false)

  async function loadUsage() {
    try {
      const res = await fetch(`${OFFICE_API_BASE}/api/usage`)
      if (!res.ok) return
      setUsage((await res.json()) as UsageReport)
    } catch {
      /* 用量面板静默失败，不影响主流程 */
    }
  }

  useEffect(() => {
    void loadUsage()
  }, [])

  async function loadPricing() {
    try {
      const res = await fetch(`${OFFICE_API_BASE}/api/pricing`)
      if (!res.ok) return
      const payload = (await res.json()) as PricingPayload
      setPriceRows(
        Object.entries(payload.prices ?? {}).map(([model, pair]) => ({
          model,
          input: String(pair[0]),
          output: String(pair[1]),
          reset: false,
        })),
      )
      setPricingLoaded(true)
    } catch {
      showToast('加载单价配置失败')
    }
  }

  function updateRow(index: number, patch: Partial<PriceRow>) {
    setPriceRows((rows) =>
      rows.map((row, i) => (i === index ? { ...row, ...patch } : row)),
    )
  }

  async function savePricing() {
    if (pricingSaving) return
    const prices: Record<string, [number, number] | null> = {}
    for (const row of priceRows) {
      const model = row.model.trim()
      if (!model) continue
      if (row.reset) {
        prices[model] = null
        continue
      }
      const input = Number(row.input)
      const output = Number(row.output)
      if (!Number.isFinite(input) || !Number.isFinite(output)) continue
      prices[model] = [input, output]
    }
    setPricingSaving(true)
    try {
      const res = await fetch(`${OFFICE_API_BASE}/api/pricing`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prices }),
      })
      if (!res.ok) {
        const detail = (await res.json().catch(() => null)) as {
          detail?: string
        } | null
        showToast(detail?.detail ?? '保存单价失败')
        return
      }
      showToast('单价已保存，金额已按新价重算')
      await loadPricing()
      await loadUsage()
    } catch {
      showToast('保存单价失败，请确认后端已启动')
    } finally {
      setPricingSaving(false)
    }
  }

  const totals = usage?.members.reduce(
    (acc, m) => ({
      calls: acc.calls + m.calls,
      prompt: acc.prompt + m.prompt_tokens,
      completion: acc.completion + m.completion_tokens,
    }),
    { calls: 0, prompt: 0, completion: 0 },
  )
  const viewedCost = viewedSprint
    ? history.find((item) => item.id === viewedSprint)?.usage_cost
    : sprint.id ? sprint.cost : undefined

  return (
    <details
      className="usage-panel"
      onToggle={(event) => {
        if (event.currentTarget.open) void loadUsage()
      }}
    >
      <summary>
        <span>费用统计（估算）</span>
        <span className="usage-total">
          {viewedCost == null ? '本次费用暂无记录' : `${viewedSprint ? '所选历史' : '当前'} Sprint ¥${viewedCost.toFixed(4)}`}
          {' · '}
          {usage ? `所有历史累计 ¥${usage.total_cost.toFixed(4)}` : '历史累计加载中'}
        </span>
      </summary>
      {usage && usage.members.length > 0 && totals ? (
        <>
          <table>
            <thead>
              <tr>
                <th>成员</th>
                <th>模型</th>
                <th>调用次数</th>
                <th>输入 tokens</th>
                <th>输出 tokens</th>
                <th>估算花费</th>
              </tr>
            </thead>
            <tbody>
              {usage.members.map((m) => (
                <tr key={m.roster}>
                  <td>{m.name ?? `#${m.roster}`}</td>
                  <td>{m.model}</td>
                  <td>{m.calls}</td>
                  <td>{m.prompt_tokens}</td>
                  <td>{m.completion_tokens}</td>
                  <td>¥{m.cost.toFixed(4)}</td>
                </tr>
              ))}
              <tr className="usage-total-row">
                <td>所有历史合计</td>
                <td>—</td>
                <td>{totals.calls}</td>
                <td>{totals.prompt}</td>
                <td>{totals.completion}</td>
                <td>¥{usage.total_cost.toFixed(4)}</td>
              </tr>
            </tbody>
          </table>
          <div className="usage-note">
            上表为所有历史模型调用的累计用量；本次 Sprint 花费见标题。费用按各服务商公开刊例约值估算，以实际账单为准。
            <a
              className="usage-export-link"
              href={`${OFFICE_API_BASE}/api/usage/export.csv`}
              download
            >
              ⇩ 导出 CSV
            </a>
          </div>
          <details
            className="pricing-panel"
            onToggle={(event) => {
              if (event.currentTarget.open && !pricingLoaded) void loadPricing()
            }}
          >
            <summary>自定义单价（元/百万tokens）</summary>
            {pricingLoaded ? (
              <>
                <table>
                  <thead>
                    <tr>
                      <th>模型</th>
                      <th>输入价</th>
                      <th>输出价</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {priceRows.map((row, index) => (
                      <tr key={index} className={row.reset ? 'pricing-row-reset' : ''}>
                        <td>
                          <input
                            value={row.model}
                            placeholder="模型名，如 gpt-4o"
                            disabled={row.reset}
                            onChange={(event) =>
                              updateRow(index, { model: event.target.value })
                            }
                          />
                        </td>
                        <td>
                          <input
                            type="number"
                            min="0"
                            step="0.01"
                            value={row.input}
                            disabled={row.reset}
                            onChange={(event) =>
                              updateRow(index, { input: event.target.value })
                            }
                          />
                        </td>
                        <td>
                          <input
                            type="number"
                            min="0"
                            step="0.01"
                            value={row.output}
                            disabled={row.reset}
                            onChange={(event) =>
                              updateRow(index, { output: event.target.value })
                            }
                          />
                        </td>
                        <td>
                          {row.reset ? (
                            <button
                              type="button"
                              onClick={() => updateRow(index, { reset: false })}
                            >
                              撤销
                            </button>
                          ) : (
                            <button
                              type="button"
                              onClick={() => updateRow(index, { reset: true })}
                            >
                              恢复内置价
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <div className="pricing-actions">
                  <button
                    type="button"
                    onClick={() =>
                      setPriceRows((rows) => [
                        ...rows,
                        { model: '', input: '', output: '', reset: false },
                      ])
                    }
                  >
                    + 新增模型
                  </button>
                  <button
                    type="button"
                    className="pricing-save-btn"
                    disabled={pricingSaving}
                    onClick={() => void savePricing()}
                  >
                    {pricingSaving ? '保存中…' : '保存单价'}
                  </button>
                </div>
              </>
            ) : (
              <p className="usage-empty">展开后加载单价配置…</p>
            )}
          </details>
        </>
      ) : (
        <div className="usage-empty">暂无用量记录，Sprint 运行后自动统计。</div>
      )}
    </details>
  )
}
