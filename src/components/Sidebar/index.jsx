import { memo } from 'react'
import { Layout, Menu, Avatar, Dropdown, Switch, message } from 'antd'
import { useNavigate, useLocation } from 'react-router-dom'
import {
  MessageOutlined,
  DatabaseOutlined,
  SearchOutlined,
  DashboardOutlined,
  UserOutlined,
  LogoutOutlined,
  SettingOutlined,
  SafetyCertificateOutlined,
  HomeOutlined,
  SunOutlined,
  MoonOutlined
} from '@ant-design/icons'
import useUserStore from '../../store/userStore'
import useThemeStore from '../../store/themeStore'
import './index.css'

const { Sider } = Layout

const menuItems = [
  {
    key: '/',
    icon: <HomeOutlined />,
    label: '首页'
  },
  {
    key: '/chat',
    icon: <MessageOutlined />,
    label: '对话'
  },
  {
    key: '/knowledge',
    icon: <DatabaseOutlined />,
    label: '知识库'
  },
  {
    key: '/rag',
    icon: <SearchOutlined />,
    label: 'RAG'
  },
  {
    key: '/review',
    icon: <SafetyCertificateOutlined />,
    label: '审查修正'
  },
  {
    key: '/monitor',
    icon: <DashboardOutlined />,
    label: '用量监控'
  }
]

const Sidebar = memo(() => {
  const navigate = useNavigate()
  const location = useLocation()
  const user = useUserStore((state) => state.user)
  const logout = useUserStore((state) => state.logout)
  const theme = useThemeStore((state) => state.theme)
  const toggleTheme = useThemeStore((state) => state.toggleTheme)

  const handleMenuClick = ({ key }) => {
    navigate(key)
  }

  const handleLogout = () => {
    logout()
    message.success('已退出登录')
    navigate('/login')
  }

  // 用户下拉菜单
  const userMenuItems = [
    {
      key: 'profile',
      icon: <UserOutlined />,
      label: '个人信息'
    },
    {
      key: 'settings',
      icon: <SettingOutlined />,
      label: '设置'
    },
    {
      type: 'divider'
    },
    {
      key: 'logout',
      icon: <LogoutOutlined />,
      label: '退出登录',
      danger: true
    }
  ]

  const handleUserMenuClick = ({ key }) => {
    if (key === 'logout') {
      handleLogout()
    }
  }

  // 获取当前路径
  const selectedKey = location.pathname

  return (
    <Sider width={220} className="sidebar">
      <div className="logo">
        <h2>煤炭规程智能体</h2>
      </div>

      <Menu
        mode="inline"
        selectedKeys={[selectedKey]}
        items={menuItems}
        onClick={handleMenuClick}
        className="sidebar-menu"
      />

      {/* 主题切换 */}
      <div className="sidebar-theme">
        <SunOutlined className={`theme-icon ${theme === 'light' ? 'active' : ''}`} />
        <Switch
          checked={theme === 'dark'}
          onChange={toggleTheme}
          size="small"
        />
        <MoonOutlined className={`theme-icon ${theme === 'dark' ? 'active' : ''}`} />
      </div>

      {/* 用户信息区域 */}
      <div className="sidebar-user">
        <Dropdown
          menu={{
            items: userMenuItems,
            onClick: handleUserMenuClick
          }}
          placement="topRight"
          trigger={['click']}
        >
          <div className="user-info">
            <Avatar
              size={36}
              icon={<UserOutlined />}
              style={{ backgroundColor: '#1890ff' }}
            />
            <div className="user-detail">
              <span className="user-name">{user?.name || '用户'}</span>
              <span className="user-role">
                {user?.role === 'admin' ? '管理员' : '普通用户'}
              </span>
            </div>
          </div>
        </Dropdown>
      </div>
    </Sider>
  )
})

Sidebar.displayName = 'Sidebar'

export default Sidebar
