import { useCallback, useEffect, useState } from 'react'
import axios from 'axios'
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Form,
  Input,
  List,
  Modal,
  Popconfirm,
  Row,
  Select,
  Space,
  Switch,
  Tag,
  message,
} from 'antd'
import {
  ApiOutlined,
  BellOutlined,
  CheckCircleFilled,
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  SafetyCertificateOutlined,
  SettingOutlined,
  SkinOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import useThemeStore from '../../store/themeStore'
import useUserStore from '../../store/userStore'
import './index.css'


const PROFILE_API = '/api/llm-profiles'


export default function Settings() {
  const user = useUserStore(state => state.user)
  const accessToken = useUserStore(state => state.accessToken)
  const theme = useThemeStore(state => state.theme)
  const setTheme = useThemeStore(state => state.setTheme)

  const [profiles, setProfiles] = useState([])
  const [profilesLoading, setProfilesLoading] = useState(false)
  const [profileModalOpen, setProfileModalOpen] = useState(false)
  const [editingProfile, setEditingProfile] = useState(null)
  const [savingProfile, setSavingProfile] = useState(false)
  const [switchingId, setSwitchingId] = useState('')
  const [testingId, setTestingId] = useState('')
  const [form] = Form.useForm()

  const loadProfiles = useCallback(async () => {
    if (!accessToken) {
      setProfiles([])
      return
    }
    setProfilesLoading(true)
    try {
      const res = await axios.get(PROFILE_API)
      setProfiles(res.data.profiles || [])
    } catch (err) {
      message.error(err.response?.data?.detail || '模型配置加载失败')
      setProfiles([])
    } finally {
      setProfilesLoading(false)
    }
  }, [accessToken])

  useEffect(() => {
    loadProfiles()
  }, [loadProfiles])

  function openCreateProfile() {
    setEditingProfile(null)
    form.setFieldsValue({
      name: '',
      base_url: 'http://127.0.0.1:8000/v1',
      model: '',
      api_key: '',
    })
    setProfileModalOpen(true)
  }

  function openEditProfile(profile) {
    setEditingProfile(profile)
    form.setFieldsValue({
      name: profile.name,
      base_url: profile.base_url,
      model: profile.model,
      api_key: '',
    })
    setProfileModalOpen(true)
  }

  async function saveProfile() {
    let values
    try {
      values = await form.validateFields()
    } catch {
      return
    }
    setSavingProfile(true)
    try {
      if (editingProfile) {
        await axios.put(`${PROFILE_API}/${editingProfile.id}`, values)
      } else {
        await axios.post(PROFILE_API, values)
      }
      message.success(editingProfile ? '模型配置已更新' : '模型配置已添加')
      setProfileModalOpen(false)
      await loadProfiles()
    } catch (err) {
      message.error(err.response?.data?.detail || err.message || '模型配置保存失败')
    } finally {
      setSavingProfile(false)
    }
  }

  async function activateProfile(profile) {
    setSwitchingId(profile.id)
    try {
      await axios.put(`${PROFILE_API}/${profile.id}/activate`)
      message.success(`已切换到：${profile.name}。之后启动的新审查任务将使用该模型`)
      await loadProfiles()
    } catch (err) {
      message.error(err.response?.data?.detail || '模型切换失败')
    } finally {
      setSwitchingId('')
    }
  }

  async function testProfile(profile) {
    setTestingId(profile.id)
    try {
      const res = await axios.post(`${PROFILE_API}/${profile.id}/test`)
      message.success(
        `连接成功：${res.data.model}，耗时 ${res.data.latency_seconds}s`
      )
    } catch (err) {
      message.error(err.response?.data?.detail || '连接测试失败')
    } finally {
      setTestingId('')
    }
  }

  async function deleteProfile(profile) {
    try {
      await axios.delete(`${PROFILE_API}/${profile.id}`)
      message.success('模型配置已删除')
      await loadProfiles()
    } catch (err) {
      message.error(err.response?.data?.detail || '模型配置删除失败')
    }
  }

  const activeProfile = profiles.find(profile => profile.is_active)

  return (
    <div className="settings-page">
      <div className="settings-header">
        <h1>设置</h1>
        <p>配置模型 API、界面偏好、审查默认项和账户视图。</p>
      </div>

      <Row gutter={[20, 20]}>
        <Col span={24}>
          <Card
            className="settings-card model-settings-card"
            title={<span><ApiOutlined /> 大模型 API 与模型切换</span>}
            extra={(
              <Button type="primary" icon={<PlusOutlined />} onClick={openCreateProfile} disabled={!accessToken}>
                添加模型配置
              </Button>
            )}
          >
            {!accessToken && (
              <Alert
                type="warning"
                showIcon
                message="当前登录状态来自旧版本，请退出后重新登录，才能安全管理 API Key。"
                className="model-auth-alert"
              />
            )}
            <Alert
              type="info"
              showIcon
              message="支持 OpenAI 兼容接口"
              description="可接入 DashScope、OpenAI、Ollama、vLLM、LM Studio、LocalAI 等服务。切换后，新启动的审查任务使用新模型；已运行中的任务保持原模型。API Key 加密保存在服务器 MongoDB，前端不会返回明文。"
              className="model-info-alert"
            />

            {activeProfile && (
              <div className="active-model-summary">
                <CheckCircleFilled />
                <span>当前模型</span>
                <strong>{activeProfile.name}</strong>
                <Tag color="blue">{activeProfile.model}</Tag>
                <code>{activeProfile.base_url}</code>
              </div>
            )}

            <List
              loading={profilesLoading}
              dataSource={profiles}
              locale={{ emptyText: accessToken ? '暂无模型配置' : '请重新登录后加载模型配置' }}
              renderItem={profile => (
                <List.Item
                  className={`model-profile-item ${profile.is_active ? 'active' : ''}`}
                  actions={[
                    <Button
                      key="test"
                      size="small"
                      icon={<ThunderboltOutlined />}
                      loading={testingId === profile.id}
                      onClick={() => testProfile(profile)}
                    >
                      测试连接
                    </Button>,
                    profile.is_active ? (
                      <Tag key="active" color="success">当前使用</Tag>
                    ) : (
                      <Button
                        key="activate"
                        size="small"
                        type="primary"
                        ghost
                        loading={switchingId === profile.id}
                        onClick={() => activateProfile(profile)}
                      >
                        切换使用
                      </Button>
                    ),
                    !profile.is_system && (
                      <Button
                        key="edit"
                        size="small"
                        icon={<EditOutlined />}
                        onClick={() => openEditProfile(profile)}
                      >
                        编辑
                      </Button>
                    ),
                    !profile.is_system && (
                      <Popconfirm
                        key="delete"
                        title="确定删除这个模型配置吗？"
                        onConfirm={() => deleteProfile(profile)}
                      >
                        <Button size="small" danger icon={<DeleteOutlined />}>删除</Button>
                      </Popconfirm>
                    ),
                  ].filter(Boolean)}
                >
                  <List.Item.Meta
                    title={(
                      <Space wrap>
                        <span>{profile.name}</span>
                        {profile.is_system && <Tag>系统内置</Tag>}
                        {profile.is_active && <Tag color="success">已启用</Tag>}
                      </Space>
                    )}
                    description={(
                      <div className="model-profile-detail">
                        <div><span>模型</span><code>{profile.model}</code></div>
                        <div><span>Base URL</span><code>{profile.base_url}</code></div>
                        <div><span>API Key</span><code>{profile.api_key_masked}</code></div>
                      </div>
                    )}
                  />
                </List.Item>
              )}
            />
          </Card>
        </Col>

        <Col xs={24} lg={12}>
          <Card className="settings-card" title={<span><SkinOutlined /> 界面偏好</span>}>
            <div className="settings-row">
              <div>
                <div className="settings-row-title">主题模式</div>
                <div className="settings-row-desc">切换浅色或深色界面</div>
              </div>
              <Select
                value={theme}
                style={{ width: 140 }}
                onChange={setTheme}
                options={[
                  { value: 'light', label: '浅色模式' },
                  { value: 'dark', label: '深色模式' },
                ]}
              />
            </div>
            <div className="settings-row">
              <div>
                <div className="settings-row-title">紧凑信息密度</div>
                <div className="settings-row-desc">适合长文档审查的密集布局</div>
              </div>
              <Switch defaultChecked disabled />
            </div>
          </Card>
        </Col>

        <Col xs={24} lg={12}>
          <Card className="settings-card" title={<span><SafetyCertificateOutlined /> 审查默认项</span>}>
            <div className="settings-row">
              <div>
                <div className="settings-row-title">默认矿井类型</div>
                <div className="settings-row-desc">多智能体审查上传时的默认选项</div>
              </div>
              <Select
                defaultValue="non_outburst"
                style={{ width: 140 }}
                options={[
                  { value: 'non_outburst', label: '非突出矿井' },
                  { value: 'outburst', label: '突出矿井' },
                ]}
              />
            </div>
            <div className="settings-row">
              <div>
                <div className="settings-row-title">历史任务提醒</div>
                <div className="settings-row-desc">审查完成后在历史区域保留任务入口</div>
              </div>
              <Switch defaultChecked disabled />
            </div>
          </Card>
        </Col>

        <Col xs={24} lg={12}>
          <Card className="settings-card" title={<span><BellOutlined /> 通知</span>}>
            <div className="settings-row">
              <div>
                <div className="settings-row-title">前端消息提示</div>
                <div className="settings-row-desc">上传、审查、反馈状态使用消息提示</div>
              </div>
              <Switch defaultChecked disabled />
            </div>
          </Card>
        </Col>

        <Col xs={24} lg={12}>
          <Card className="settings-card" title={<span><SettingOutlined /> 当前账户</span>}>
            <Descriptions size="small" column={1}>
              <Descriptions.Item label="用户名">{user?.username || '-'}</Descriptions.Item>
              <Descriptions.Item label="角色">
                {user?.role === 'admin' ? '管理员' : '普通用户'}
              </Descriptions.Item>
              <Descriptions.Item label="可用模块">
                知识库、RAG、多智能体审查、用量监控
              </Descriptions.Item>
            </Descriptions>
          </Card>
        </Col>
      </Row>

      <Modal
        title={editingProfile ? '编辑模型配置' : '添加模型配置'}
        open={profileModalOpen}
        okText="保存"
        cancelText="取消"
        confirmLoading={savingProfile}
        onOk={saveProfile}
        onCancel={() => setProfileModalOpen(false)}
        destroyOnClose={false}
      >
        <Form form={form} layout="vertical" requiredMark="optional">
          <Form.Item
            name="name"
            label="配置名称"
            rules={[{ required: true, message: '请输入配置名称' }]}
          >
            <Input placeholder="例如：服务器 vLLM / 本地 Ollama" maxLength={80} />
          </Form.Item>
          <Form.Item
            name="base_url"
            label="OpenAI 兼容 Base URL"
            extra="例如：http://127.0.0.1:11434/v1 或 http://10.0.0.8:8000/v1。该地址必须能从后端服务器访问。"
            rules={[
              { required: true, message: '请输入 Base URL' },
              { pattern: /^https?:\/\//i, message: '必须以 http:// 或 https:// 开头' },
            ]}
          >
            <Input placeholder="http://127.0.0.1:8000/v1" />
          </Form.Item>
          <Form.Item
            name="model"
            label="模型名称"
            extra="填写接口实际接受的 model 参数，例如 qwen-plus、Qwen3-8B、llama3.1。"
            rules={[{ required: true, message: '请输入模型名称' }]}
          >
            <Input placeholder="Qwen3-8B" maxLength={200} />
          </Form.Item>
          <Form.Item
            name="api_key"
            label="API Key"
            extra={editingProfile
              ? '留空表示继续使用原 API Key；无需鉴权的本地服务可在首次创建时留空。'
              : '无需鉴权的本地 OpenAI 兼容服务可以留空。'}
          >
            <Input.Password placeholder={editingProfile ? '留空则不修改' : 'sk-...（可选）'} autoComplete="new-password" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
