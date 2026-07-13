import React, { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Card,
  Row,
  Col,
  Statistic,
  Table,
  DatePicker,
  Select,
  Space,
  Tag,
  Button,
  Empty,
  message,
} from 'antd'
import {
  ApiOutlined,
  ThunderboltOutlined,
  DollarOutlined,
  ClockCircleOutlined,
  DownloadOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import {
  LineChart,
  Line,
  BarChart,
  Bar,
  PieChart,
  Pie,
  Cell,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from 'recharts'
import dayjs from 'dayjs'
import './index.css'

const { RangePicker } = DatePicker
const { Option } = Select

const MODULE_META = {
  chat: { label: '对话', color: '#1890ff', tag: 'blue' },
  knowledge: { label: '知识库', color: '#52c41a', tag: 'green' },
  rag: { label: 'RAG检索', color: '#faad14', tag: 'orange' },
  review: { label: '审查修正', color: '#722ed1', tag: 'purple' },
  v9_review: { label: '多智能体审查', color: '#eb2f96', tag: 'magenta' },
}

const STATUS_META = {
  success: { label: '成功', color: 'success' },
  failed: { label: '失败', color: 'error' },
}

function moduleMeta(module) {
  return MODULE_META[module] || { label: module || '未知模块', color: '#8c8c8c', tag: 'default' }
}

function statusMeta(status) {
  return STATUS_META[status] || { label: status || '未知', color: 'default' }
}

function formatNumber(value) {
  return Number(value || 0).toLocaleString()
}

function formatTime(value) {
  if (!value) return '-'
  return dayjs(value).format('YYYY-MM-DD HH:mm:ss')
}

function buildRange(dateRange, customRange) {
  const now = dayjs()
  if (dateRange === 'today') return [now.startOf('day'), now.endOf('day')]
  if (dateRange === 'week') return [now.subtract(6, 'day').startOf('day'), now.endOf('day')]
  if (dateRange === 'month') return [now.subtract(29, 'day').startOf('day'), now.endOf('day')]
  if (dateRange === 'custom' && customRange?.[0] && customRange?.[1]) {
    return [customRange[0].startOf('day'), customRange[1].endOf('day')]
  }
  return [now.startOf('day'), now.endOf('day')]
}

function buildQuery(params) {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') query.set(key, value)
  })
  return query.toString()
}

async function fetchJson(url) {
  const response = await fetch(url)
  if (!response.ok) {
    const text = await response.text()
    throw new Error(text || `请求失败：${response.status}`)
  }
  return response.json()
}

const Monitor = () => {
  const [dateRange, setDateRange] = useState('today')
  const [customRange, setCustomRange] = useState([dayjs().subtract(7, 'day'), dayjs()])
  const [moduleFilter, setModuleFilter] = useState('all')
  const [statusFilter, setStatusFilter] = useState('all')
  const [loading, setLoading] = useState(false)
  const [stats, setStats] = useState({
    total_calls: 0,
    total_tokens: 0,
    avg_latency: 0,
    total_cost: 0,
    error_rate: 0,
    failed_calls: 0,
  })
  const [trendData, setTrendData] = useState([])
  const [moduleData, setModuleData] = useState([])
  const [moduleUsage, setModuleUsage] = useState([])
  const [logData, setLogData] = useState([])

  const [startTime, endTime] = useMemo(
    () => buildRange(dateRange, customRange),
    [customRange, dateRange]
  )

  const moduleParam = moduleFilter === 'all' ? undefined : moduleFilter
  const statusParam = statusFilter === 'all' ? undefined : statusFilter

  const rangeParams = useMemo(() => ({
    start_time: startTime.format('YYYY-MM-DDTHH:mm:ss'),
    end_time: endTime.format('YYYY-MM-DDTHH:mm:ss'),
  }), [endTime, startTime])

  const loadData = useCallback(async () => {
    setLoading(true)
    try {
      const statsQuery = buildQuery({ ...rangeParams, module: moduleParam })
      const commonQuery = buildQuery(rangeParams)
      const logQuery = buildQuery({ limit: 100, module: moduleParam, status: statusParam })
      const hours = Math.max(1, Math.min(24 * 31, Math.ceil(endTime.diff(startTime, 'hour', true))))
      const trendQuery = buildQuery({ hours, module: moduleParam })

      const [statsRes, trendRes, distributionRes, modulesRes, logsRes] = await Promise.all([
        fetchJson(`/api/monitor/stats?${statsQuery}`),
        fetchJson(`/api/monitor/trend?${trendQuery}`),
        fetchJson(`/api/monitor/distribution?${commonQuery}`),
        fetchJson(`/api/monitor/modules?${commonQuery}`),
        fetchJson(`/api/monitor/logs?${logQuery}`),
      ])

      setStats(statsRes)
      setTrendData((trendRes.data || []).map(item => ({
        ...item,
        time: dayjs(item.time).isValid() ? dayjs(item.time).format('MM-DD HH:mm') : item.time,
        calls: Number(item.calls || 0),
        tokens: Number(item.tokens || 0),
      })))
      setModuleData((distributionRes.data || [])
        .filter(item => !moduleParam || item.name === moduleParam)
        .map(item => ({
          ...item,
          name: moduleMeta(item.name).label,
          module: item.name,
          color: moduleMeta(item.name).color,
          value: Number(item.value || 0),
        })))
      setModuleUsage((modulesRes.data || [])
        .filter(item => !moduleParam || item.module === moduleParam)
        .map(item => ({
          ...item,
          module_label: moduleMeta(item.module).label,
          calls: Number(item.calls || 0),
          tokens: Number(item.tokens || 0),
          cost: Number(item.cost || 0),
          avg_latency: Number(item.avg_latency || 0),
        })))
      setLogData((logsRes.logs || []).map(item => ({
        key: item.id,
        ...item,
        tokens: Number(item.tokens || 0),
        latency: Number(item.latency || 0),
        cost: Number(item.cost || 0),
      })))
    } catch (error) {
      message.error(`加载用量监控失败：${error.message}`)
      setStats({
        total_calls: 0,
        total_tokens: 0,
        avg_latency: 0,
        total_cost: 0,
        error_rate: 0,
        failed_calls: 0,
      })
      setTrendData([])
      setModuleData([])
      setModuleUsage([])
      setLogData([])
    } finally {
      setLoading(false)
    }
  }, [endTime, moduleParam, rangeParams, startTime, statusParam])

  useEffect(() => {
    loadData()
  }, [loadData])

  const logColumns = [
    {
      title: '时间',
      dataIndex: 'timestamp',
      key: 'timestamp',
      width: 180,
      render: (text) => (
        <Space>
          <ClockCircleOutlined />
          {formatTime(text)}
        </Space>
      ),
    },
    {
      title: '功能模块',
      dataIndex: 'module',
      key: 'module',
      width: 130,
      render: (text) => {
        const meta = moduleMeta(text)
        return <Tag color={meta.tag}>{meta.label}</Tag>
      },
    },
    {
      title: '操作类型',
      dataIndex: 'operation',
      key: 'operation',
      width: 150,
      ellipsis: true,
    },
    {
      title: 'Token消耗',
      dataIndex: 'tokens',
      key: 'tokens',
      width: 120,
      align: 'right',
      sorter: (a, b) => a.tokens - b.tokens,
      render: formatNumber,
    },
    {
      title: '响应时间',
      dataIndex: 'latency',
      key: 'latency',
      width: 120,
      align: 'right',
      sorter: (a, b) => a.latency - b.latency,
      render: (value) => `${Number(value || 0).toFixed(3)}s`,
    },
    {
      title: '成本',
      dataIndex: 'cost',
      key: 'cost',
      width: 100,
      align: 'right',
      render: (value) => `$${Number(value || 0).toFixed(4)}`,
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      align: 'center',
      render: (text) => {
        const meta = statusMeta(text)
        return <Tag color={meta.color}>{meta.label}</Tag>
      },
    },
    {
      title: '错误信息',
      dataIndex: 'error_msg',
      key: 'error_msg',
      ellipsis: true,
      render: (text) => text || '-',
    },
  ]

  const handleExport = () => {
    const headers = ['时间', '模块', '操作', '状态', 'Token', '响应时间(s)', '成本(USD)', '错误信息']
    const rows = logData.map(item => [
      formatTime(item.timestamp),
      moduleMeta(item.module).label,
      item.operation || '',
      statusMeta(item.status).label,
      item.tokens || 0,
      item.latency || 0,
      item.cost || 0,
      item.error_msg || '',
    ])
    const csv = [headers, ...rows]
      .map(row => row.map(cell => `"${String(cell).replace(/"/g, '""')}"`).join(','))
      .join('\n')
    const blob = new Blob([`\uFEFF${csv}`], { type: 'text/csv;charset=utf-8;' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `usage-monitor-${dayjs().format('YYYYMMDD-HHmmss')}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="monitor-container">
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={6}>
          <Card loading={loading}>
            <Statistic
              title="调用次数"
              value={stats.total_calls}
              prefix={<ApiOutlined />}
              valueStyle={{ color: '#3f8600' }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card loading={loading}>
            <Statistic
              title="Token使用量"
              value={stats.total_tokens}
              prefix={<ThunderboltOutlined />}
              valueStyle={{ color: '#1890ff' }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card loading={loading}>
            <Statistic
              title="成本"
              value={stats.total_cost}
              prefix={<DollarOutlined />}
              precision={4}
              valueStyle={{ color: '#cf1322' }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card loading={loading}>
            <Statistic
              title={`平均响应时间 / 错误率 ${Number(stats.error_rate || 0).toFixed(2)}%`}
              value={stats.avg_latency}
              prefix={<ClockCircleOutlined />}
              suffix="s"
              precision={3}
              valueStyle={{ color: '#722ed1' }}
            />
          </Card>
        </Col>
      </Row>

      <Card className="monitor-filter-card">
        <Space size="middle" wrap>
          <span>时间范围:</span>
          <Select value={dateRange} onChange={setDateRange} style={{ width: 120 }}>
            <Option value="today">今日</Option>
            <Option value="week">近7天</Option>
            <Option value="month">近30天</Option>
            <Option value="custom">自定义</Option>
          </Select>

          {dateRange === 'custom' && (
            <RangePicker value={customRange} onChange={setCustomRange} allowClear={false} />
          )}

          <span>功能模块:</span>
          <Select value={moduleFilter} onChange={setModuleFilter} style={{ width: 150 }}>
            <Option value="all">全部</Option>
            {Object.entries(MODULE_META).map(([value, meta]) => (
              <Option value={value} key={value}>{meta.label}</Option>
            ))}
          </Select>

          <span>日志状态:</span>
          <Select value={statusFilter} onChange={setStatusFilter} style={{ width: 110 }}>
            <Option value="all">全部</Option>
            <Option value="success">成功</Option>
            <Option value="failed">失败</Option>
          </Select>

          <Button icon={<ReloadOutlined />} onClick={loadData} loading={loading}>
            刷新
          </Button>
          <Button type="primary" icon={<DownloadOutlined />} onClick={handleExport} disabled={!logData.length}>
            导出日志
          </Button>
        </Space>
      </Card>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={12}>
          <Card title="调用趋势" className="chart-card" loading={loading}>
            {trendData.length ? (
              <ResponsiveContainer width="100%" height={300}>
                <LineChart data={trendData}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="time" minTickGap={20} />
                  <YAxis yAxisId="left" />
                  <YAxis yAxisId="right" orientation="right" />
                  <Tooltip formatter={(value) => formatNumber(value)} />
                  <Legend />
                  <Line yAxisId="left" type="monotone" dataKey="calls" stroke="#1890ff" name="调用次数" strokeWidth={2} dot={false} />
                  <Line yAxisId="right" type="monotone" dataKey="tokens" stroke="#52c41a" name="Token消耗" strokeWidth={2} dot={false} />
                </LineChart>
              </ResponsiveContainer>
            ) : (
              <Empty description="暂无趋势数据" />
            )}
          </Card>
        </Col>

        <Col span={12}>
          <Card title="功能使用分布" className="chart-card" loading={loading}>
            {moduleData.length ? (
              <ResponsiveContainer width="100%" height={300}>
                <PieChart>
                  <Pie
                    data={moduleData}
                    cx="50%"
                    cy="50%"
                    labelLine={false}
                    label={({ name, percent }) => `${name}: ${(percent * 100).toFixed(0)}%`}
                    outerRadius={100}
                    fill="#8884d8"
                    dataKey="value"
                  >
                    {moduleData.map((entry) => (
                      <Cell key={entry.module} fill={entry.color} />
                    ))}
                  </Pie>
                  <Tooltip formatter={(value) => `${formatNumber(value)} 次`} />
                </PieChart>
              </ResponsiveContainer>
            ) : (
              <Empty description="暂无模块分布数据" />
            )}
          </Card>
        </Col>
      </Row>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={24}>
          <Card title="模块用量汇总" className="chart-card" loading={loading}>
            {moduleUsage.length ? (
              <ResponsiveContainer width="100%" height={300}>
                <BarChart data={moduleUsage}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="module_label" />
                  <YAxis yAxisId="left" />
                  <YAxis yAxisId="right" orientation="right" />
                  <Tooltip formatter={(value) => formatNumber(value)} />
                  <Legend />
                  <Bar yAxisId="left" dataKey="tokens" fill="#1890ff" name="Token总量" />
                  <Bar yAxisId="right" dataKey="calls" fill="#52c41a" name="调用次数" />
                </BarChart>
              </ResponsiveContainer>
            ) : (
              <Empty description="暂无模块用量数据" />
            )}
          </Card>
        </Col>
      </Row>

      <Card title="调用日志">
        <Table
          loading={loading}
          columns={logColumns}
          dataSource={logData}
          pagination={{
            pageSize: 10,
            showSizeChanger: true,
            showQuickJumper: true,
            showTotal: (total) => `共 ${total} 条记录`,
          }}
          scroll={{ x: 1100 }}
        />
      </Card>
    </div>
  )
}

export default Monitor
