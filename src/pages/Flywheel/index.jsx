import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Button,
  Empty,
  Input,
  Popconfirm,
  Select,
  Spin,
  Tag,
  message,
} from 'antd'
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  EditOutlined,
  ExportOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  SearchOutlined,
  SyncOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import axios from 'axios'
import './index.css'

const API = '/api/v10/flywheel'

const ACTION_META = {
  accept: { label: '人工采纳', color: 'success', icon: <CheckCircleOutlined /> },
  reject: { label: '人工驳回', color: 'warning', icon: <CloseCircleOutlined /> },
  custom: { label: '人工改写', color: 'processing', icon: <EditOutlined /> },
}

const ISSUE_META = {
  compliance: '合规',
  typo: '错别字',
  redundancy: '重复',
  numeric: '数值',
  escalation: '人工升级',
}

function compactText(value = '', limit = 260) {
  const text = String(value || '').trim()
  return text.length > limit ? `${text.slice(0, limit)}…` : text
}

function Kpi({ label, value, suffix, tone, hint }) {
  return (
    <div className={`fw-kpi ${tone || ''}`}>
      <span className="fw-kpi-label">{label}</span>
      <div className="fw-kpi-value">
        {value}<small>{suffix}</small>
      </div>
      <span className="fw-kpi-hint">{hint}</span>
    </div>
  )
}

export default function Flywheel() {
  const [stats, setStats] = useState(null)
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(false)
  const [action, setAction] = useState('')
  const [issueType, setIssueType] = useState('')
  const [query, setQuery] = useState('')

  const loadData = useCallback(async () => {
    setLoading(true)
    try {
      const response = await axios.get(API, {
        params: { limit: 500, action, issue_type: issueType, query: query.trim() },
      })
      setStats(response.data.stats || {})
      setItems(response.data.items || [])
    } catch (error) {
      message.error(`数据飞轮加载失败：${error.response?.data?.detail || error.message}`)
    } finally {
      setLoading(false)
    }
  }, [action, issueType, query])

  useEffect(() => {
    const timer = setTimeout(loadData, query ? 260 : 0)
    return () => clearTimeout(timer)
  }, [loadData, query])

  const maxDaily = useMemo(
    () => Math.max(1, ...(stats?.daily || []).map(item => Number(item.count || 0))),
    [stats?.daily]
  )

  async function exportSamples() {
    try {
      const response = await axios.get(`${API}/export`, { responseType: 'blob' })
      const url = URL.createObjectURL(response.data)
      const link = document.createElement('a')
      link.href = url
      link.download = `human_feedback_gold_${new Date().toISOString().slice(0, 10)}.json`
      link.click()
      URL.revokeObjectURL(url)
      message.success('人工反馈黄金样本已导出')
    } catch (error) {
      message.error(`导出失败：${error.response?.data?.detail || error.message}`)
    }
  }

  async function deleteSample(id) {
    try {
      await axios.delete(`${API}/${id}`)
      message.success('样本已从飞轮中移除')
      loadData()
    } catch (error) {
      message.error(`删除失败：${error.response?.data?.detail || error.message}`)
    }
  }

  const confirmationRate = Math.round(Number(stats?.confirmation_rate || 0) * 100)

  return (
    <div className="flywheel-page">
      <section className="fw-hero">
        <div>
          <div className="fw-eyebrow"><SyncOutlined /> HUMAN FEEDBACK LOOP</div>
          <h1>人工反馈数据飞轮</h1>
          <p>
            每次采纳、驳回和人工改写都会形成可追溯样本；后续审查检索相似样本，
            作为少样本经验辅助判断。Prompt、Skill 和规则版本仍由人工维护，不会被自动改写。
          </p>
        </div>
        <div className="fw-hero-actions">
          <Button icon={<ReloadOutlined />} onClick={loadData} loading={loading}>刷新</Button>
          <Button type="primary" icon={<ExportOutlined />} onClick={exportSamples}>
            导出黄金样本
          </Button>
        </div>
      </section>

      <section className="fw-kpi-grid">
        <Kpi label="有效人工样本" value={stats?.total_samples || 0} suffix="条" tone="ink" hint={`${stats?.document_count || 0} 份文档`} />
        <Kpi label="确认问题属实" value={stats?.confirmed_samples || 0} suffix="条" tone="green" hint={`确认率 ${confirmationRate}%`} />
        <Kpi label="纠正系统误报" value={stats?.rejected_samples || 0} suffix="条" tone="amber" hint="驳回样本会抑制同类误报" />
        <Kpi label="后续审查复用" value={stats?.reuse_events || 0} suffix="次" tone="blue" hint={`${stats?.reused_samples || 0} 条样本被命中`} />
      </section>

      <section className="fw-loop-panel">
        <div className="fw-section-heading">
          <div>
            <span>闭环状态</span>
            <h2>反馈不是归档终点，而是下一次审查的输入</h2>
          </div>
          <Tag color="cyan">当前启用：相似样本提示复用</Tag>
        </div>
        <div className="fw-loop-track">
          <div className="fw-loop-node">
            <SafetyCertificateOutlined />
            <strong>系统审查</strong>
            <span>输出问题、证据与建议</span>
          </div>
          <div className="fw-loop-arrow">→</div>
          <div className="fw-loop-node active">
            <CheckCircleOutlined />
            <strong>人工裁决</strong>
            <span>{stats?.total_samples || 0} 条有效反馈</span>
          </div>
          <div className="fw-loop-arrow">→</div>
          <div className="fw-loop-node">
            <DatabaseOutlined />
            <strong>黄金样本</strong>
            <span>撤回即失效，可筛选导出</span>
          </div>
          <div className="fw-loop-arrow">→</div>
          <div className="fw-loop-node reused">
            <ThunderboltOutlined />
            <strong>相似复用</strong>
            <span>{stats?.reuse_events || 0} 次进入后续审查</span>
          </div>
        </div>

        <div className="fw-activity-row">
          <div className="fw-daily-card">
            <span className="fw-mini-title">近 14 天反馈增量</span>
            <div className="fw-bars">
              {(stats?.daily || []).map(item => (
                <div className="fw-bar-column" key={item.date} title={`${item.date}：${item.count} 条`}>
                  <span>{item.count || ''}</span>
                  <i style={{ height: `${Math.max(4, Number(item.count || 0) / maxDaily * 72)}px` }} />
                  <small>{item.date.slice(5)}</small>
                </div>
              ))}
            </div>
          </div>
          <div className="fw-type-card">
            <span className="fw-mini-title">样本类型构成</span>
            <div className="fw-type-list">
              {Object.entries(stats?.issue_type_counts || {}).map(([key, value]) => (
                <div key={key}>
                  <span>{ISSUE_META[key] || key}</span>
                  <strong>{value}</strong>
                </div>
              ))}
              {!Object.keys(stats?.issue_type_counts || {}).length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无人工样本" />}
            </div>
          </div>
        </div>
      </section>

      <section className="fw-samples-panel">
        <div className="fw-sample-toolbar">
          <div>
            <span className="fw-mini-title">反馈样本库</span>
            <strong>{items.length} 条当前结果</strong>
          </div>
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder="搜索文档、原文或反馈理由"
            value={query}
            onChange={event => setQuery(event.target.value)}
          />
          <Select
            value={action}
            onChange={setAction}
            options={[
              { label: '全部裁决', value: '' },
              { label: '人工采纳', value: 'accept' },
              { label: '人工驳回', value: 'reject' },
              { label: '人工改写', value: 'custom' },
            ]}
          />
          <Select
            value={issueType}
            onChange={setIssueType}
            options={[
              { label: '全部类型', value: '' },
              ...Object.entries(ISSUE_META).map(([value, label]) => ({ value, label })),
            ]}
          />
        </div>

        {loading ? (
          <div className="fw-loading"><Spin /><span>正在读取人工反馈样本…</span></div>
        ) : items.length === 0 ? (
          <Empty description="还没有符合筛选条件的人工反馈" />
        ) : (
          <div className="fw-sample-list">
            {items.map(item => {
              const meta = ACTION_META[item.action] || { label: item.action || '人工反馈', color: 'default' }
              const extra = item.extra || {}
              return (
                <article className="fw-sample" key={item.id}>
                  <div className="fw-sample-rail" />
                  <div className="fw-sample-main">
                    <div className="fw-sample-head">
                      <div>
                        <Tag color={meta.color} icon={meta.icon}>{meta.label}</Tag>
                        <Tag>{ISSUE_META[item.issue_type] || item.issue_type || '未分类'}</Tag>
                        <span className="fw-sample-id">样本 #{item.id}</span>
                      </div>
                      <span>{item.created_at}</span>
                    </div>
                    <div className="fw-sample-doc">{item.doc_name} · Chunk #{Number(item.chunk_index) + 1}</div>
                    <div className="fw-sample-copy">{compactText(item.pending_content) || '未保存原文片段'}</div>
                    <div className="fw-verdict-row">
                      <span>模型：<strong>{item.model_verdict || '未记录'}</strong></span>
                      <b>→</b>
                      <span>人工：<strong>{item.final_verdict || '未记录'}</strong></span>
                    </div>
                    {(extra.final_text || item.reason) && (
                      <div className="fw-human-note">
                        {extra.final_text && <span><b>最终文本</b>{compactText(extra.final_text, 180)}</span>}
                        {item.reason && <span><b>裁决依据</b>{compactText(item.reason, 180)}</span>}
                      </div>
                    )}
                  </div>
                  <div className="fw-sample-side">
                    <span className="fw-reuse-count"><ThunderboltOutlined /> 复用 {item.reused_count || 0} 次</span>
                    {item.last_reused_at && <small>最近 {item.last_reused_at}</small>}
                    <Popconfirm
                      title="删除这条飞轮样本？"
                      description="删除后不再用于相似经验复用，也不会出现在黄金样本导出中。"
                      okText="删除"
                      cancelText="取消"
                      onConfirm={() => deleteSample(item.id)}
                    >
                      <Button type="text" danger size="small" icon={<DeleteOutlined />}>移除</Button>
                    </Popconfirm>
                  </div>
                </article>
              )
            })}
          </div>
        )}
      </section>
    </div>
  )
}
