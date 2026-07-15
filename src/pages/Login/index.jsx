import React, { useState } from 'react'
import { Form, Input, Button, Card, message, Checkbox, Segmented } from 'antd'
import { UserOutlined, LockOutlined, IdcardOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import useUserStore from '../../store/userStore'
import './index.css'

const Login = () => {
  const [loading, setLoading] = useState(false)
  const [mode, setMode] = useState('login')
  const [form] = Form.useForm()
  const navigate = useNavigate()
  const login = useUserStore((state) => state.login)

  const submitAuth = async (values) => {
    setLoading(true)
    try {
      const endpoint = mode === 'login' ? '/api/auth/login' : '/api/auth/register'
      const payload = mode === 'login'
        ? { username: values.username, password: values.password }
        : {
          username: values.username,
          password: values.password,
          display_name: values.display_name || values.username,
        }
      const response = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(data.detail || (mode === 'login' ? '登录失败' : '注册失败'))
      }
      login(data.user)
      message.success(mode === 'login' ? '登录成功' : '注册成功，已自动登录')
      navigate('/')
    } catch (error) {
      message.error(error.message)
    } finally {
      setLoading(false)
    }
  }

  const switchMode = (nextMode) => {
    setMode(nextMode)
    form.resetFields()
  }

  return (
    <div className="login-container">
      <div className="login-background">
        <div className="login-bg-shape shape-1"></div>
        <div className="login-bg-shape shape-2"></div>
        <div className="login-bg-shape shape-3"></div>
      </div>

      <Card className="login-card">
        <div className="login-header">
          <h1>煤炭规程智能体</h1>
          <p>{mode === 'login' ? '登录管理平台' : '注册普通用户账号'}</p>
        </div>

        <Segmented
          block
          className="login-mode-switch"
          value={mode}
          onChange={switchMode}
          options={[
            { label: '登录', value: 'login' },
            { label: '注册', value: 'register' },
          ]}
        />

        <Form
          form={form}
          name="auth"
          onFinish={submitAuth}
          autoComplete="off"
          size="large"
          initialValues={{ remember: true }}
        >
          <Form.Item
            name="username"
            rules={[{ required: true, message: '请输入用户名' }]}
          >
            <Input prefix={<UserOutlined />} placeholder="用户名" />
          </Form.Item>

          {mode === 'register' && (
            <Form.Item name="display_name">
              <Input prefix={<IdcardOutlined />} placeholder="显示名称（可选）" />
            </Form.Item>
          )}

          <Form.Item
            name="password"
            rules={[
              { required: true, message: '请输入密码' },
              { min: 6, message: '密码至少 6 位' },
            ]}
          >
            <Input.Password prefix={<LockOutlined />} placeholder="密码" />
          </Form.Item>

          {mode === 'register' && (
            <Form.Item
              name="confirm_password"
              dependencies={['password']}
              rules={[
                { required: true, message: '请再次输入密码' },
                ({ getFieldValue }) => ({
                  validator(_, value) {
                    if (!value || getFieldValue('password') === value) {
                      return Promise.resolve()
                    }
                    return Promise.reject(new Error('两次输入的密码不一致'))
                  },
                }),
              ]}
            >
              <Input.Password prefix={<LockOutlined />} placeholder="确认密码" />
            </Form.Item>
          )}

          {mode === 'login' && (
            <Form.Item>
              <div className="login-options">
                <Form.Item name="remember" valuePropName="checked" noStyle>
                  <Checkbox>记住我</Checkbox>
                </Form.Item>
              </div>
            </Form.Item>
          )}

          <Form.Item>
            <Button
              type="primary"
              htmlType="submit"
              loading={loading}
              block
              className="login-button"
            >
              {mode === 'login' ? '登 录' : '注 册'}
            </Button>
          </Form.Item>
        </Form>

        <div className="login-footer">
          <p>管理员默认账号：admin / 123456</p>
          <p>注册用户会自动进入管理员的审查知识库权限名单</p>
        </div>
      </Card>
    </div>
  )
}

export default Login
