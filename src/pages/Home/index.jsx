import { memo, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { Card, Row, Col, Typography } from 'antd'
import {
  MessageOutlined,
  DatabaseOutlined,
  SearchOutlined,
  SafetyCertificateOutlined,
  DashboardOutlined,
  RightOutlined
} from '@ant-design/icons'
import useUserStore from '../../store/userStore'
import './index.css'

const { Title, Paragraph } = Typography

// 抽取为独立的 memo 组件，避免重复渲染
const FeatureCard = memo(({ feature, onClick }) => (
  <Card
    className="feature-card"
    hoverable
    onClick={onClick}
  >
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
))

FeatureCard.displayName = 'FeatureCard'

const features = [
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
  }
]

const Home = memo(() => {
  const navigate = useNavigate()
  const user = useUserStore((state) => state.user)

  const handleFeatureClick = useCallback((path) => {
    navigate(path)
  }, [navigate])

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
                onClick={() => handleFeatureClick(feature.path)}
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
                系统采用"检索-判别-关联-生成-复核"五维分工的多智能体协同架构，
                通过智能体间的信息流转与约束制衡，实现从局部错误发现到全局一致性修正的完整闭环。
              </Paragraph>
              <ul className="intro-list">
                <li>全域知识检索 - 跨文档背景知识召回</li>
                <li>合规性判别 - 识别参数/程序违规</li>
                <li>全局依赖分析 - 预测连锁反应</li>
                <li>标准内容生成 - 综合考量修正方案</li>
                <li>安全合规复合 - 模拟专家校验</li>
              </ul>
            </div>
          </Col>
          <Col span={12}>
            <div className="intro-visual">
              <div className="agent-flow-visual">
                <div className="agent-node node-1">
                  <SearchOutlined />
                  <span>检索</span>
                </div>
                <div className="agent-node node-2">
                  <SafetyCertificateOutlined />
                  <span>判别</span>
                </div>
                <div className="agent-node node-3">
                  <DatabaseOutlined />
                  <span>关联</span>
                </div>
                <div className="agent-node node-4">
                  <MessageOutlined />
                  <span>生成</span>
                </div>
                <div className="agent-node node-5">
                  <DashboardOutlined />
                  <span>复核</span>
                </div>
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
