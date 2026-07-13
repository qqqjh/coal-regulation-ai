import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Col,
  Form,
  Input,
  message,
  Modal,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
} from 'antd'
import {
  DatabaseOutlined,
  PlusOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  SaveOutlined,
  TeamOutlined,
} from '@ant-design/icons'
import useUserStore from '../../store/userStore'
import './index.css'

const { Text } = Typography

const roleLabel = {
  admin: '管理员',
  user: '普通用户',
}

const AdminReviewKb = () => {
  const user = useUserStore((state) => state.user)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [users, setUsers] = useState([])
  const [knowledgeBases, setKnowledgeBases] = useState([])
  const [draft, setDraft] = useState({})
  const [addOpen, setAddOpen] = useState(false)
  const [form] = Form.useForm()

  const kbOptions = useMemo(
    () => knowledgeBases.map(kb => ({
      label: `${kb.name}${kb.document_count ? `（${kb.document_count} 文档）` : ''}`,
      value: kb.id,
    })),
    [knowledgeBases],
  )

  const configuredCount = users.filter(u => u.configured).length

  const loadData = async () => {
    setLoading(true)
    try {
      const response = await fetch(`/api/admin/review-kb-permissions?role=${encodeURIComponent(user?.role || '')}`)
      if (!response.ok) {
        const err = await response.json().catch(() => ({}))
        throw new Error(err.detail || '加载失败')
      }
      const data = await response.json()
      const nextUsers = data.users || []
      setUsers(nextUsers)
      setKnowledgeBases(data.knowledge_bases || [])
      setDraft(Object.fromEntries(nextUsers.map(item => [item.user_id, item.kb_ids || []])))
    } catch (error) {
      message.error(`加载审查知识库权限失败：${error.message}`)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadData()
  }, [])

  const updateDraft = (userId, kbIds) => {
    setDraft(prev => ({ ...prev, [userId]: kbIds }))
  }

  const savePermissions = async () => {
    setSaving(true)
    try {
      const payload = {
        permissions: users.map(item => ({
          user_id: item.user_id,
          user_name: item.user_name,
          user_role: item.user_role,
          kb_ids: draft[item.user_id] || [],
        })),
      }
      const response = await fetch(`/api/admin/review-kb-permissions?role=${encodeURIComponent(user?.role || '')}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!response.ok) {
        const err = await response.json().catch(() => ({}))
        throw new Error(err.detail || '保存失败')
      }
      message.success('审查知识库权限已保存')
      await loadData()
    } catch (error) {
      message.error(`保存失败：${error.message}`)
    } finally {
      setSaving(false)
    }
  }

  const addUser = async () => {
    const values = await form.validateFields()
    const normalizedId = String(values.user_id).trim()
    if (users.some(item => item.user_id === normalizedId)) {
      message.warning('该用户已经在列表中')
      return
    }
    const kbIds = values.kb_ids || knowledgeBases.map(kb => kb.id)
    const nextUser = {
      user_id: normalizedId,
      user_name: values.user_name || normalizedId,
      user_role: values.user_role || 'user',
      configured: true,
      kb_ids: kbIds,
    }
    setUsers(prev => [...prev, nextUser])
    setDraft(prev => ({ ...prev, [normalizedId]: kbIds }))
    form.resetFields()
    setAddOpen(false)
  }

  const columns = [
    {
      title: '用户',
      dataIndex: 'user_name',
      width: 220,
      render: (_, record) => (
        <Space direction="vertical" size={2}>
          <Space size={8}>
            <Text strong>{record.user_name || record.user_id}</Text>
            <Tag color={record.user_role === 'admin' ? 'geekblue' : 'default'}>
              {roleLabel[record.user_role] || record.user_role || '用户'}
            </Tag>
          </Space>
          <Text type="secondary">ID: {record.user_id}</Text>
        </Space>
      ),
    },
    {
      title: '审查可用规程知识库',
      dataIndex: 'kb_ids',
      render: (_, record) => (
        <Select
          mode="multiple"
          allowClear
          showSearch
          maxTagCount="responsive"
          placeholder="选择该用户审查时可用的规程知识库"
          className="admin-review-kb-select"
          value={draft[record.user_id] || []}
          options={kbOptions}
          optionFilterProp="label"
          onChange={(values) => updateDraft(record.user_id, values)}
        />
      ),
    },
    {
      title: '状态',
      width: 130,
      render: (_, record) => {
        const count = (draft[record.user_id] || []).length
        if (count === 0) return <Tag color="red">禁止审查检索</Tag>
        if (count === knowledgeBases.length) return <Tag color="green">全部可用</Tag>
        return <Tag color="gold">限制 {count} 个</Tag>
      },
    },
  ]

  return (
    <div className="admin-review-kb-page">
      <div className="admin-review-kb-header">
        <div>
          <div className="admin-review-kb-kicker">
            <SafetyCertificateOutlined />
            管理员配置
          </div>
          <h1>审查规程知识库权限</h1>
          <p>设置所有用户在多智能体审查时可以选择和检索的规程知识库。普通用户不会看到该页面。</p>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={loadData} loading={loading}>刷新</Button>
          <Button icon={<PlusOutlined />} onClick={() => setAddOpen(true)}>新增用户</Button>
          <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={savePermissions}>保存权限</Button>
        </Space>
      </div>

      <Row gutter={12} className="admin-review-kb-stats">
        <Col span={8}>
          <Card>
            <Statistic title="纳入配置用户" value={users.length} prefix={<TeamOutlined />} />
          </Card>
        </Col>
        <Col span={8}>
          <Card>
            <Statistic title="已显式保存权限" value={configuredCount} prefix={<SafetyCertificateOutlined />} />
          </Card>
        </Col>
        <Col span={8}>
          <Card>
            <Statistic title="规程知识库" value={knowledgeBases.length} prefix={<DatabaseOutlined />} />
          </Card>
        </Col>
      </Row>

      <Alert
        className="admin-review-kb-note"
        type="info"
        showIcon
        message="权限规则"
        description="未保存过配置的用户按兼容策略默认可使用全部规程知识库；保存后会严格按表格中的选择限制。若选择为空，该用户无法启动带知识库限制的审查任务。"
      />

      <Card className="admin-review-kb-card">
        <Table
          rowKey="user_id"
          loading={loading}
          columns={columns}
          dataSource={users}
          pagination={false}
        />
      </Card>

      <Modal
        title="新增可配置用户"
        open={addOpen}
        onCancel={() => setAddOpen(false)}
        onOk={addUser}
        okText="加入列表"
        cancelText="取消"
      >
        <Form form={form} layout="vertical">
          <Form.Item
            label="用户 ID"
            name="user_id"
            rules={[{ required: true, message: '请输入用户 ID' }]}
          >
            <Input placeholder="例如 user、zhangsan 或登录后显示的用户 ID" />
          </Form.Item>
          <Form.Item label="用户名称" name="user_name">
            <Input placeholder="显示名称，可选" />
          </Form.Item>
          <Form.Item label="角色" name="user_role" initialValue="user">
            <Select
              options={[
                { label: '普通用户', value: 'user' },
                { label: '管理员', value: 'admin' },
              ]}
            />
          </Form.Item>
          <Form.Item label="可用规程知识库" name="kb_ids">
            <Select mode="multiple" allowClear options={kbOptions} placeholder="默认全部可用" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}

export default AdminReviewKb
