import React, { useState } from 'react'
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
  Button
} from 'antd'
import {
  ArrowUpOutlined,
  ArrowDownOutlined,
  ApiOutlined,
  ThunderboltOutlined,
  DollarOutlined,
  ClockCircleOutlined,
  DownloadOutlined
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
  ResponsiveContainer
} from 'recharts'
import dayjs from 'dayjs'
import './index.css'

const { RangePicker } = DatePicker
const { Option } = Select

const Monitor = () => {
  const [dateRange, setDateRange] = useState('today')
  const [moduleFilter, setModuleFilter] = useState('all')

  // 趋势数据
  const trendData = [
    { time: '00:00', calls: 45, tokens: 12500 },
    { time: '04:00', calls: 32, tokens: 8900 },
    { time: '08:00', calls: 98, tokens: 28300 },
    { time: '12:00', calls: 156, tokens: 45600 },
    { time: '16:00', calls: 134, tokens: 39200 },
    { time: '20:00', calls: 87, tokens: 24100 }
  ]

  // 功能使用分布
  const moduleData = [
    { name: '对话', value: 450, color: '#1890ff' },
    { name: '知识库', value: 230, color: '#52c41a' },
    { name: 'RAG检索', value: 180, color: '#faad14' },
    { name: '审查修正', value: 120, color: '#722ed1' }
  ]

  // Token消耗统计
  const tokenData = [
    { module: '对话', input: 45000, output: 68000 },
    { module: '知识库', input: 12000, output: 8000 },
    { module: 'RAG检索', input: 28000, output: 42000 },
    { module: '审查修正', input: 35000, output: 52000 }
  ]

  // 调用日志
  const logColumns = [
    {
      title: '时间',
      dataIndex: 'timestamp',
      key: 'timestamp',
      width: 180,
      render: (text) => (
        <Space>
          <ClockCircleOutlined />
          {text}
        </Space>
      )
    },
    {
      title: '功能模块',
      dataIndex: 'module',
      key: 'module',
      width: 120,
      filters: [
        { text: '对话', value: '对话' },
        { text: '知识库', value: '知识库' },
        { text: 'RAG检索', value: 'RAG检索' },
        { text: '审查修正', value: '审查修正' }
      ],
      onFilter: (value, record) => record.module === value,
      render: (text) => {
        const colorMap = {
          '对话': 'blue',
          '知识库': 'green',
          'RAG检索': 'orange',
          '审查修正': 'purple'
        }
        return <Tag color={colorMap[text]}>{text}</Tag>
      }
    },
    {
      title: '操作类型',
      dataIndex: 'operation',
      key: 'operation',
      width: 150
    },
    {
      title: 'Token消耗',
      dataIndex: 'tokens',
      key: 'tokens',
      width: 120,
      align: 'right',
      sorter: (a, b) => a.tokens - b.tokens,
      render: (text) => text.toLocaleString()
    },
    {
      title: '响应时间',
      dataIndex: 'responseTime',
      key: 'responseTime',
      width: 120,
      align: 'right',
      sorter: (a, b) => a.responseTime - b.responseTime,
      render: (text) => `${text}ms`
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      align: 'center',
      filters: [
        { text: '成功', value: '成功' },
        { text: '失败', value: '失败' }
      ],
      onFilter: (value, record) => record.status === value,
      render: (text) => (
        <Tag color={text === '成功' ? 'success' : 'error'}>{text}</Tag>
      )
    }
  ]

  const logData = [
    {
      key: 1,
      timestamp: '2024-01-20 14:35:21',
      module: '对话',
      operation: '发送消息',
      tokens: 2450,
      responseTime: 1250,
      status: '成功'
    },
    {
      key: 2,
      timestamp: '2024-01-20 14:33:45',
      module: 'RAG检索',
      operation: '文档检索',
      tokens: 1820,
      responseTime: 850,
      status: '成功'
    },
    {
      key: 3,
      timestamp: '2024-01-20 14:32:10',
      module: '知识库',
      operation: '上传文档',
      tokens: 320,
      responseTime: 2100,
      status: '成功'
    },
    {
      key: 4,
      timestamp: '2024-01-20 14:30:55',
      module: '对话',
      operation: '发送消息',
      tokens: 3120,
      responseTime: 1680,
      status: '成功'
    },
    {
      key: 5,
      timestamp: '2024-01-20 14:28:30',
      module: 'RAG检索',
      operation: '生成答案',
      tokens: 4250,
      responseTime: 2300,
      status: '成功'
    },
    {
      key: 6,
      timestamp: '2024-01-20 14:25:18',
      module: '对话',
      operation: '发送消息',
      tokens: 1950,
      responseTime: 980,
      status: '失败'
    },
    {
      key: 7,
      timestamp: '2024-01-20 14:22:42',
      module: '知识库',
      operation: '删除文档',
      tokens: 150,
      responseTime: 420,
      status: '成功'
    },
    {
      key: 8,
      timestamp: '2024-01-20 14:20:05',
      module: '审查修正',
      operation: '文档审查',
      tokens: 8500,
      responseTime: 4200,
      status: '成功'
    },
    {
      key: 9,
      timestamp: '2024-01-20 14:15:30',
      module: '审查修正',
      operation: '生成修正报告',
      tokens: 6200,
      responseTime: 3500,
      status: '成功'
    },
    {
      key: 10,
      timestamp: '2024-01-20 14:10:18',
      module: '审查修正',
      operation: '合规性检查',
      tokens: 5800,
      responseTime: 2800,
      status: '成功'
    }
  ]

  // 导出数据
  const handleExport = () => {
    console.log('导出数据')
    // 实际项目中会导出 Excel 文件
  }

  return (
    <div className="monitor-container">
      {/* 统计概览 */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={6}>
          <Card>
            <Statistic
              title="今日调用次数"
              value={940}
              prefix={<ApiOutlined />}
              suffix={
                <span style={{ fontSize: 14, color: '#52c41a' }}>
                  <ArrowUpOutlined /> 12.5%
                </span>
              }
              valueStyle={{ color: '#3f8600' }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="Token使用量"
              value={226000}
              prefix={<ThunderboltOutlined />}
              suffix={
                <span style={{ fontSize: 14, color: '#52c41a' }}>
                  <ArrowUpOutlined /> 8.3%
                </span>
              }
              valueStyle={{ color: '#1890ff' }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="今日成本"
              value={4.52}
              prefix={<DollarOutlined />}
              precision={2}
              suffix={
                <span style={{ fontSize: 14, color: '#f5222d' }}>
                  <ArrowUpOutlined /> 15.2%
                </span>
              }
              valueStyle={{ color: '#cf1322' }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="平均响应时间"
              value={1.25}
              prefix={<ClockCircleOutlined />}
              suffix="s"
              precision={2}
              valueStyle={{ color: '#722ed1' }}
            />
          </Card>
        </Col>
      </Row>

      {/* 筛选器 */}
      <Card style={{ marginBottom: 16 }}>
        <Space size="large">
          <span>时间范围:</span>
          <Select
            value={dateRange}
            onChange={setDateRange}
            style={{ width: 120 }}
          >
            <Option value="today">今日</Option>
            <Option value="week">本周</Option>
            <Option value="month">本月</Option>
            <Option value="custom">自定义</Option>
          </Select>

          {dateRange === 'custom' && (
            <RangePicker
              defaultValue={[dayjs().subtract(7, 'day'), dayjs()]}
            />
          )}

          <span style={{ marginLeft: 24 }}>功能模块:</span>
          <Select
            value={moduleFilter}
            onChange={setModuleFilter}
            style={{ width: 120 }}
          >
            <Option value="all">全部</Option>
            <Option value="chat">对话</Option>
            <Option value="knowledge">知识库</Option>
            <Option value="rag">RAG检索</Option>
            <Option value="review">审查修正</Option>
          </Select>

          <Button
            type="primary"
            icon={<DownloadOutlined />}
            onClick={handleExport}
            style={{ marginLeft: 'auto' }}
          >
            导出数据
          </Button>
        </Space>
      </Card>

      {/* 图表区域 */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        {/* 调用趋势 */}
        <Col span={12}>
          <Card title="调用趋势" className="chart-card">
            <ResponsiveContainer width="100%" height={300}>
              <LineChart data={trendData}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="time" />
                <YAxis yAxisId="left" />
                <YAxis yAxisId="right" orientation="right" />
                <Tooltip />
                <Legend />
                <Line
                  yAxisId="left"
                  type="monotone"
                  dataKey="calls"
                  stroke="#1890ff"
                  name="调用次数"
                  strokeWidth={2}
                />
                <Line
                  yAxisId="right"
                  type="monotone"
                  dataKey="tokens"
                  stroke="#52c41a"
                  name="Token消耗"
                  strokeWidth={2}
                />
              </LineChart>
            </ResponsiveContainer>
          </Card>
        </Col>

        {/* 功能使用分布 */}
        <Col span={12}>
          <Card title="功能使用分布" className="chart-card">
            <ResponsiveContainer width="100%" height={300}>
              <PieChart>
                <Pie
                  data={moduleData}
                  cx="50%"
                  cy="50%"
                  labelLine={false}
                  label={({ name, percent }) =>
                    `${name}: ${(percent * 100).toFixed(0)}%`
                  }
                  outerRadius={100}
                  fill="#8884d8"
                  dataKey="value"
                >
                  {moduleData.map((entry, index) => (
                    <Cell key={`cell-${index}`} fill={entry.color} />
                  ))}
                </Pie>
                <Tooltip />
              </PieChart>
            </ResponsiveContainer>
          </Card>
        </Col>
      </Row>

      {/* Token消耗统计 */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={24}>
          <Card title="Token消耗统计" className="chart-card">
            <ResponsiveContainer width="100%" height={300}>
              <BarChart data={tokenData}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="module" />
                <YAxis />
                <Tooltip />
                <Legend />
                <Bar dataKey="input" fill="#1890ff" name="输入Token" />
                <Bar dataKey="output" fill="#52c41a" name="输出Token" />
              </BarChart>
            </ResponsiveContainer>
          </Card>
        </Col>
      </Row>

      {/* 调用日志 */}
      <Card title="调用日志">
        <Table
          columns={logColumns}
          dataSource={logData}
          pagination={{
            pageSize: 10,
            showSizeChanger: true,
            showQuickJumper: true,
            showTotal: (total) => `共 ${total} 条记录`
          }}
          scroll={{ x: 1000 }}
        />
      </Card>
    </div>
  )
}

export default Monitor
