import React, { useState } from 'react'
import { Form, Input, Button, Card, message, Checkbox } from 'antd'
import { UserOutlined, LockOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import useUserStore from '../../store/userStore'
import './index.css'

const Login = () => {
  const [loading, setLoading] = useState(false)
  const navigate = useNavigate()
  const login = useUserStore((state) => state.login)

  const onFinish = async (values) => {
    setLoading(true)

    // 模拟登录验证（延迟1秒）
    setTimeout(() => {
      // Mock 用户验证
      if (values.username === 'admin' && values.password === '123456') {
        const userInfo = {
          id: 1,
          username: values.username,
          name: '管理员',
          role: 'admin',
          avatar: null
        }
        login(userInfo)
        message.success('登录成功！')
        navigate('/')
      } else if (values.username && values.password) {
        // 允许任意用户名密码登录（演示用）
        const userInfo = {
          id: Date.now(),
          username: values.username,
          name: values.username,
          role: 'user',
          avatar: null
        }
        login(userInfo)
        message.success('登录成功！')
        navigate('/')
      } else {
        message.error('请输入用户名和密码')
      }
      setLoading(false)
    }, 1000)
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
          <p>管理平台</p>
        </div>

        <Form
          name="login"
          onFinish={onFinish}
          autoComplete="off"
          size="large"
          initialValues={{ remember: true }}
        >
          <Form.Item
            name="username"
            rules={[{ required: true, message: '请输入用户名' }]}
          >
            <Input
              prefix={<UserOutlined />}
              placeholder="用户名"
            />
          </Form.Item>

          <Form.Item
            name="password"
            rules={[{ required: true, message: '请输入密码' }]}
          >
            <Input.Password
              prefix={<LockOutlined />}
              placeholder="密码"
            />
          </Form.Item>

          <Form.Item>
            <div className="login-options">
              <Form.Item name="remember" valuePropName="checked" noStyle>
                <Checkbox>记住我</Checkbox>
              </Form.Item>
              <a className="login-forgot" href="#">
                忘记密码？
              </a>
            </div>
          </Form.Item>

          <Form.Item>
            <Button
              type="primary"
              htmlType="submit"
              loading={loading}
              block
              className="login-button"
            >
              登 录
            </Button>
          </Form.Item>
        </Form>

        <div className="login-footer">
          <p>演示账号：admin / 123456</p>
          <p>或输入任意用户名密码登录</p>
        </div>
      </Card>
    </div>
  )
}

export default Login
