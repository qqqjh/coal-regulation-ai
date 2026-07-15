import { memo } from 'react'
import { Link } from 'react-router-dom'
import { Card, Row, Col, Typography } from 'antd'
import {
  MessageOutlined,
  DatabaseOutlined,
  SearchOutlined,
  SafetyCertificateOutlined,
  DashboardOutlined,
  SettingOutlined,
  UserOutlined,
  TeamOutlined,
  SyncOutlined,
  RightOutlined
} from '@ant-design/icons'
import useUserStore from '../../store/userStore'
import './index.css'

const { Title, Paragraph } = Typography

// 抽取为独立的 memo 组件，避免重复渲染
const FeatureCard = memo(({ feature }) => (
  <Link
    to={feature.path}
    className="feature-card-link"
    data-feature-path={feature.path}
    aria-label={`进入${feature.title}`}
  >
    <Card className="feature-card" hoverable>
      <div className="feature-icon" style={{ backgroundColor: feature.color }}>
        {feature.icon}
      </div>
      <div className="feature-content">
        <Title level={4} className="feature-title">
          {feature.title}
        </Title>
        <Paragraph className="feature-description">
          {feature.description}
        </Paragraph>
      </div>
      <div className="feature-arrow">
        <RightOutlined />
      </div>
    </Card>
  </Link>
))

FeatureCard.displayName = 'FeatureCard'

const baseFeatures = [
  {
    key: 'chat',
    path: '/chat',
    icon: <MessageOutlined />,
    title: '智能对话',
    description: '基于煤炭规程知识库的智能问答系统，快速获取规程解读、安全指导等专业信息',
    color: '#1890ff'
  },
  {
    key: 'knowledge',
    path: '/knowledge',
    icon: <DatabaseOutlined />,
    title: '知识库管理',
    description: '管理煤炭规程文档，支持文档上传、分类、检索，构建专业知识体系',
    color: '#52c41a'
  },
  {
    key: 'rag',
    path: '/rag',
    icon: <SearchOutlined />,
    title: 'RAG 检索增强',
    description: '配置检索增强生成参数，优化知识检索效果，提升回答准确性',
    color: '#722ed1'
  },
  {
    key: 'review',
    path: '/review',
    icon: <SafetyCertificateOutlined />,
    title: '审查修正',
    description: '多智能体协同的文档合规性审查，实现从错误发现到一致性修正的完整闭环',
    color: '#fa8c16'
  },
  {
    key: 'monitor',
    path: '/monitor',
    icon: <DashboardOutlined />,
    title: '用量监控',
    description: '实时监控系统使用情况，包括API调用、Token消耗、用户活跃度等数据',
    color: '#13c2c2'
  },
  {
    key: 'settings',
    path: '/settings',
    icon: <SettingOutlined />,
    title: '模型与系统设置',
    description: '添加和切换大模型 API，配置界面偏好、默认矿井类型与审查提醒',
    color: '#2f54eb'
  },
  {
    key: 'profile',
    path: '/profile',
    icon: <UserOutlined />,
    title: '个人中心',
    description: '查看当前账户资料、角色和个人使用信息',
    color: '#eb2f96'
  }
]

const adminFeatures = [
  {
    key: 'admin-review-kb',
    path: '/admin/review-kb',
    icon: <TeamOutlined />,
    title: '审查知识库权限',
    description: '为不同用户配置审查时可选择、可检索的规程知识库范围',
    color: '#08979c'
  },
  {
    key: 'admin-flywheel',
    path: '/admin/flywheel',
    icon: <SyncOutlined />,
    title: '数据飞轮',
    description: '管理人工裁决样本，沉淀高质量反馈并导出审查黄金数据集',
    color: '#d4380d'
  }
]

const agentLinks = [
  { key: 'retrieval', path: '/rag', icon: <SearchOutlined />, label: '检索' },
  { key: 'review', path: '/review', icon: <SafetyCertificateOutlined />, label: '判别' },
  { key: 'knowledge', path: '/knowledge', icon: <DatabaseOutlined />, label: '关联' },
  { key: 'chat', path: '/chat', icon: <MessageOutlined />, label: '生成' },
  { key: 'monitor', path: '/monitor', icon: <DashboardOutlined />, label: '监控' },
]

const Home = memo(() => {
  const user = useUserStore((state) => state.user)
  const features = user?.role === 'admin'
    ? [...baseFeatures, ...adminFeatures]
    : baseFeatures

  return (
    <div className="home-container">
      {/* 顶部欢迎区域 */}
      <div className="home-header">
        <div className="home-header-content">
          <Title level={1} className="home-title">
            煤炭规程智能体
          </Title>
          <Paragraph className="home-subtitle">
            基于大语言模型的煤炭行业规程智能助手，提供规程检索、智能问答、文档审查等一站式服务
          </Paragraph>
          <div className="home-welcome">
            欢迎您，<span className="home-username">{user?.name || '用户'}</span>
          </div>
        </div>
        <div className="home-header-decoration">
          <div className="decoration-circle circle-1"></div>
          <div className="decoration-circle circle-2"></div>
          <div className="decoration-circle circle-3"></div>
        </div>
      </div>

      {/* 功能介绍区域 */}
      <div className="home-features">
        <Title level={3} className="features-title">
          核心功能
        </Title>
        <Row gutter={[24, 24]}>
          {features.map(feature => (
            <Col xs={24} sm={12} lg={8} key={feature.key}>
              <FeatureCard
                feature={feature}
              />
            </Col>
          ))}
        </Row>
      </div>

      {/* 系统介绍区域 */}
      <div className="home-intro">
        <Row gutter={48} align="middle">
          <Col span={12}>
            <div className="intro-content">
              <Title level={3}>多智能体协同架构</Title>
              <Paragraph>
                系统采用“检索-判别-关联-生成-复核”五维分工的多智能体协同架构，
                通过智能体间的信息流转与约束制衡，实现从局部错误发现到全局一致性修正的完整闭环。
              </Paragraph>
              <ul className="intro-list">
                <li>全域知识检索 - 跨文档背景知识召回</li>
                <li>合规性判别 - 识别参数/程序违规</li>
                <li>全局依赖分析 - 预测连锁反应</li>
                <li>标准内容生成 - 综合考量修正方案</li>
                <li>安全合规复核 - 模拟专家校验</li>
              </ul>
            </div>
          </Col>
          <Col span={12}>
            <div className="intro-visual">
              <div className="agent-flow-visual">
                {agentLinks.map((agent, index) => (
                  <Link
                    key={agent.key}
                    to={agent.path}
                    className={`agent-node node-${index + 1}`}
                    data-agent-path={agent.path}
                    aria-label={`进入${agent.label}功能`}
                  >
                    {agent.icon}
                    <span>{agent.label}</span>
                  </Link>
                ))}
                <svg className="agent-lines" viewBox="0 0 300 200">
                  <path d="M60,100 Q150,30 240,100" stroke="#1890ff" strokeWidth="2" fill="none" strokeDasharray="5,5">
                    <animate attributeName="stroke-dashoffset" from="10" to="0" dur="1s" repeatCount="indefinite" />
                  </path>
                  <path d="M60,100 Q150,170 240,100" stroke="#52c41a" strokeWidth="2" fill="none" strokeDasharray="5,5">
                    <animate attributeName="stroke-dashoffset" from="10" to="0" dur="1s" repeatCount="indefinite" />
                  </path>
                </svg>
              </div>
            </div>
          </Col>
        </Row>
      </div>

      {/* 底部信息 */}
      <div className="home-footer">
        <Paragraph type="secondary">
          煤炭规程智能体 v1.0.0 | 基于大语言模型技术
        </Paragraph>
      </div>
    </div>
  )
})

Home.displayName = 'Home'

export default Home
