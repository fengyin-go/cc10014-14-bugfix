import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/transform'
const columns = [
  "模板编号",
  "关联服务",
  "输入字段",
  "输出字段",
  "格式转换",
  "默认填充",
  "数组展开",
  "嵌套打平"
]
const actions = [
  "提交校验",
  "启用模板",
  "停用模板"
]
const statuses = [
  "草稿",
  "待校验",
  "已启用",
  "已停用"
]

type Row = Record<string, string | number | null>

type TransformRecord = {
  id: number
  记录编号: string
  输出版本: number
  输出列: string[]
  行数: number
  数据集: Row[]
  冲突说明: string[]
  转换备注: string[]
  生成时间: string
}

type Contract = {
  模板编号: string
  状态: string
  契约版本: number
  最近输出列: string
  接口文档: { 定义编号: string; 参数列表: string }[]
}

const emptyForm: Record<string, string> = Object.fromEntries(columns.map((c) => [c, '']))

export default function TransformView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const [formOpen, setFormOpen] = useState(false)
  const [form, setForm] = useState<Record<string, string>>(emptyForm)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const [detail, setDetail] = useState<Row | null>(null)
  const [records, setRecords] = useState<TransformRecord[]>([])
  const [contract, setContract] = useState<Contract | null>(null)
  const [sampleInput, setSampleInput] = useState('')
  const [sampleResult, setSampleResult] = useState<TransformRecord | null>(null)

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('转换模板列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '响应转换列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  const stats = useMemo(() => [
    { label: '转换模板总数', value: total },
    { label: '已启用模板', value: rows.filter((row) => row.status === '已启用').length },
    { label: '待校验模板', value: rows.filter((row) => row.status === '待校验').length },
  ], [rows, total])

  function openCreate() {
    setEditingId(null)
    setForm(emptyForm)
    setFormOpen(true)
    setNotice('')
  }

  async function openEdit(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}`)
      if (!response.ok) throw new Error('模板详情读取失败')
      const entry = await response.json()
      setEditingId(Number(row.id))
      setForm(Object.fromEntries(columns.map((c) => [c, String(entry[c] ?? '')])))
      setFormOpen(true)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '模板详情读取失败')
    }
  }

  async function submitForm() {
    if (submitting) return // 连续提交两次只生成一条有效记录
    setSubmitting(true)
    setErrorMessage('')
    setNotice('')
    try {
      const isEdit = editingId !== null
      const response = await request(isEdit ? `${ENDPOINT}/${editingId}` : ENDPOINT, {
        method: isEdit ? 'PUT' : 'POST',
        body: JSON.stringify({ values: form }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? payload.detail ?? '转换模板保存失败')
      }
      setNotice(payload.message)
      setFormOpen(false)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '转换模板保存失败')
    } finally {
      setSubmitting(false)
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? '响应转换动作未生效，请检查后重试')
      }
      setNotice(payload.message)
      await reload()
      if (detail && detail.id === row.id) await openDetail(row)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '响应转换操作失败')
    }
  }

  async function openDetail(row: Row) {
    setErrorMessage('')
    setSampleResult(null)
    try {
      const [entryResp, recordsResp, contractResp] = await Promise.all([
        request(`${ENDPOINT}/${row.id}`),
        request(`${ENDPOINT}/${row.id}/records`),
        request(`${ENDPOINT}/${row.id}/contract`),
      ])
      if (!entryResp.ok) throw new Error('模板详情读取失败')
      setDetail(await entryResp.json())
      setRecords(recordsResp.ok ? (await recordsResp.json()).items ?? [] : [])
      setContract(contractResp.ok ? await contractResp.json() : null)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '模板详情读取失败')
    }
  }

  async function runSample() {
    if (!detail) return
    setErrorMessage('')
    setNotice('')
    try {
      const recordsInput = JSON.parse(sampleInput)
      const list = Array.isArray(recordsInput) ? recordsInput : [recordsInput]
      const response = await request(`${ENDPOINT}/${detail.id}/apply`, {
        method: 'POST',
        body: JSON.stringify({ values: { records: list } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? '转换执行失败')
      }
      setNotice(payload.message)
      setSampleResult(payload.entry as TransformRecord)
      await openDetail(detail)
      setSampleResult(payload.entry as TransformRecord)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '转换执行失败，请检查输入 JSON')
    }
  }

  return (
    <section className="page" data-module="transform">
      <header className="page-head">
        <div>
          <h2>响应转换管理</h2>
          <p className="page-desc">维护响应转换模板的输入、执行约束与结果记录，为响应转换提供可验证的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={openCreate}>
            登记转换模板
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出响应转换清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      {formOpen && (
        <form
          className="filter-bar edit-panel"
          onSubmit={(event) => { event.preventDefault(); void submitForm() }}
        >
          {columns.map((field) => (
            <label key={field} className="filter-item">
              <span>{field}</span>
              <input
                value={form[field] ?? ''}
                placeholder={`请输入${field}`}
                onChange={(event) => setForm((prev) => ({ ...prev, [field]: event.target.value }))}
              />
            </label>
          ))}
          <button className="btn primary" type="submit" disabled={submitting}>
            {submitting ? '提交中…' : editingId !== null ? '保存模板' : '提交登记'}
          </button>
          <button className="btn ghost" type="button" onClick={() => setFormOpen(false)}>取消</button>
        </form>
      )}

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>状态</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>{row.status ?? '—'}</td>
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void openDetail(row)}>详情</button>
                <button className="link" type="button" onClick={() => void openEdit(row)}>编辑</button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无响应转换数据，可先登记转换模板</td>
            </tr>
          )}
        </tbody>
      </table>

      {detail && (
        <section className="edit-panel">
          <h3>模板详情：{String(detail['模板编号'] ?? '')}（{String(detail.status ?? '')}）</h3>
          <table className="data-table">
            <tbody>
              {columns.map((column) => (
                <tr key={column}>
                  <th>{column}</th>
                  <td>{detail[column] ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>

          {contract && (
            <p className="page-desc">
              契约版本 v{contract.契约版本 || 0}，最近输出列：{contract.最近输出列 || '—'}；
              接口文档：{contract.接口文档.length > 0
                ? contract.接口文档.map((doc) => `${doc.定义编号}（${doc.参数列表}）`).join('、')
                : '尚未同步'}
            </p>
          )}

          <h4>转换记录（{records.length}）</h4>
          <table className="data-table">
            <thead>
              <tr><th>记录编号</th><th>输出版本</th><th>输出列</th><th>行数</th><th>生成时间</th></tr>
            </thead>
            <tbody>
              {records.map((item) => (
                <tr key={item.id}>
                  <td>{item.记录编号}</td>
                  <td>v{item.输出版本}</td>
                  <td>{(item.输出列 ?? []).join(',')}</td>
                  <td>{item.行数}</td>
                  <td>{item.生成时间}</td>
                </tr>
              ))}
              {records.length === 0 && (
                <tr><td className="empty-state" colSpan={5}>暂无转换记录，可在下方粘贴查询结果执行转换</td></tr>
              )}
            </tbody>
          </table>

          <h4>转换执行</h4>
          <textarea
            className="sample-input"
            rows={4}
            placeholder='粘贴查询结果 JSON，如 [{"id":"1","customer":{"address":{"city":"杭州"}}}]'
            value={sampleInput}
            onChange={(event) => setSampleInput(event.target.value)}
          />
          <div className="page-actions">
            <button className="btn primary" type="button" onClick={() => void runSample()}>执行转换</button>
            <button className="btn ghost" type="button" onClick={() => setDetail(null)}>关闭详情</button>
          </div>

          {sampleResult && (
            <table className="data-table">
              <thead>
                <tr>{sampleResult.输出列.map((column) => <th key={column}>{column}</th>)}</tr>
              </thead>
              <tbody>
                {sampleResult.数据集.map((dataRow, index) => (
                  <tr key={index}>
                    {sampleResult.输出列.map((column) => <td key={column}>{String(dataRow[column] ?? '—')}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条响应转换记录</span>
        {notice && <span>{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
