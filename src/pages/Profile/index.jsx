import { useState } from 'react'
import { Avatar, Button, Card, Col, Form, Input, Row, Tag, Upload, message } from 'antd'
import { CameraOutlined, SaveOutlined, UserOutlined } from '@ant-design/icons'
import useUserStore from '../../store/userStore'
import './index.css'

function readAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result)
    reader.onerror = reject
    reader.readAsDataURL(file)
  })
}

export default function Profile() {
  const user = useUserStore(state => state.user)
  const updateUser = useUserStore(state => state.updateUser)
  const [form] = Form.useForm()
  const [avatar, setAvatar] = useState(user?.avatar || '')

  const beforeUpload = async (file) => {
    if (!file.type.startsWith('image/')) {
      message.error('只能上传图片文件')
      return Upload.LIST_IGNORE
    }
    if (file.size > 2 * 1024 * 1024) {
      message.error('头像图片不能超过 2MB')
      return Upload.LIST_IGNORE
    }
    const dataUrl = await readAsDataUrl(file)
    setAvatar(dataUrl)
    updateUser({ avatar: dataUrl })
    message.success('头像已更新')
    return false
  }

  const handleSave = (values) => {
    updateUser({ ...values, avatar })
    message.success('个人信息已保存')
  }

  return (
    <div className="profile-page">
      <div className="profile-header">
        <div>
          <h1>个人信息</h1>
          <p>管理当前登录用户的基础资料和头像。</p>
        </div>
        <Tag color={user?.role === 'admin' ? 'blue' : 'default'}>
          {user?.role === 'admin' ? '管理员' : '普通用户'}
        </Tag>
      </div>

      <Row gutter={[20, 20]}>
        <Col xs={24} lg={8}>
          <Card className="profile-card">
            <div className="profile-avatar-panel">
              <Avatar size={104} src={avatar || undefined} icon={<UserOutlined />} />
              <Upload showUploadList={false} beforeUpload={beforeUpload} accept="image/*">
                <Button icon={<CameraOutlined />}>修改头像</Button>
              </Upload>
              <span className="profile-avatar-tip">支持 JPG、PNG，大小不超过 2MB</span>
            </div>
          </Card>
        </Col>

        <Col xs={24} lg={16}>
          <Card className="profile-card" title="基础资料">
            <Form
              form={form}
              layout="vertical"
              initialValues={{
                username: user?.username || '',
                name: user?.name || '',
                role: user?.role || 'user',
              }}
              onFinish={handleSave}
            >
              <Form.Item label="用户 ID">
                <Input value={user?.id ?? ''} disabled />
              </Form.Item>
              <Form.Item label="用户名" name="username">
                <Input disabled />
              </Form.Item>
              <Form.Item
                label="显示名称"
                name="name"
                rules={[{ required: true, message: '请输入显示名称' }]}
              >
                <Input placeholder="请输入显示名称" />
              </Form.Item>
              <Form.Item label="角色" name="role">
                <Input disabled />
              </Form.Item>
              <Button type="primary" htmlType="submit" icon={<SaveOutlined />}>
                保存资料
              </Button>
            </Form>
          </Card>
        </Col>
      </Row>
    </div>
  )
}
