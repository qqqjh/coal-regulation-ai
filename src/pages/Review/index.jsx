import React, { useState } from 'react'
import {
  Card,
  Upload,
  Button,
  message,
  Row,
  Col,
  Divider,
  Tag,
  Space,
  Statistic,
  List,
  Checkbox,
  Modal,
  Spin,
  Typography,
  Steps
} from 'antd'
import {
  UploadOutlined,
  CheckOutlined,
  CloseOutlined,
  DownloadOutlined,
  FileTextOutlined,
  LoadingOutlined,
  CheckCircleOutlined,
  SyncOutlined
} from '@ant-design/icons'
import './index.css'

const { Dragger } = Upload
const { Text } = Typography

const Review = () => {
  const [uploading, setUploading] = useState(false)
  const [analyzing, setAnalyzing] = useState(false)
  const [taskId, setTaskId] = useState(null)
  const [reviewResult, setReviewResult] = useState(null)
  const [selectedChanges, setSelectedChanges] = useState([])
  const [currentStep, setCurrentStep] = useState(0)
  const [currentAgent, setCurrentAgent] = useState('')
  const [knowledgeBases, setKnowledgeBases] = useState([])
  const [selectedKbId, setSelectedKbId] = useState(3)

  // 智能体步骤配置
  const agentSteps = [
    { title: '错别字检测', description: '识别拼写错误和标点问题', icon: '📝' },
    { title: '语义通顺性', description: '检测语法和表达问题', icon: '💬' },
    { title: '重复检测', description: '发现冗余和重复内容', icon: '🔍' },
    { title: '合规性验证', description: '验证技术标准符合性', icon: '✅' }
  ]

  // 加载知识库列表
  React.useEffect(() => {
    fetchKnowledgeBases()
  }, [])

  const fetchKnowledgeBases = async () => {
    try {
      const response = await fetch('/api/knowledge/bases')
      if (response.ok) {
        const data = await response.json()
        setKnowledgeBases(data)
      }
    } catch (error) {
      console.error('获取知识库列表失败:', error)
    }
  }

  // 上传文档
  const handleUpload = async (file) => {
    setUploading(true)
    const formData = new FormData()
    formData.append('file', file)
    formData.append('kb_id', selectedKbId)

    try {
      const response = await fetch(`/api/review/upload?kb_id=${selectedKbId}`, {
        method: 'POST',
        body: formData
      })

      if (response.ok) {
        const data = await response.json()
        setTaskId(data.task_id)
        message.success('文档上传成功，开始分析...')

        // 自动开始分析
        startAnalysis(data.task_id)
      } else {
        const error = await response.json()
        message.error('文档上传失败: ' + (error.detail || '未知错误'))
      }
    } catch (error) {
      console.error('上传失败:', error)
      message.error('上传失败: ' + error.message)
    } finally {
      setUploading(false)
    }

    return false // 阻止默认上传行为
  }

  // 开始分析
  const startAnalysis = async (id) => {
    setAnalyzing(true)
    setCurrentStep(0)

    try {
      const response = await fetch(`/api/review/analyze/${id}`, {
        method: 'POST'
      })

      if (response.ok) {
        // 轮询获取结果
        pollResult(id)
      } else {
        message.error('启动分析失败')
        setAnalyzing(false)
      }
    } catch (error) {
      console.error('分析失败:', error)
      message.error('分析失败: ' + error.message)
      setAnalyzing(false)
    }
  }

  // 轮询获取结果
  const pollResult = async (id) => {
    const maxAttempts = 60 // 最多轮询60次（5分钟）
    let attempts = 0

    const poll = setInterval(async () => {
      attempts++

      try {
        const response = await fetch(`/api/review/result/${id}`)
        if (response.ok) {
          const data = await response.json()

          // 更新进度
          if (data.result && data.result.progress) {
            const progress = data.result.progress
            if (progress >= 25 && progress < 50) setCurrentStep(1)
            else if (progress >= 50 && progress < 75) setCurrentStep(2)
            else if (progress >= 75 && progress < 100) setCurrentStep(3)
            else if (progress === 100) setCurrentStep(4)
          }

          if (data.status === 'completed') {
            clearInterval(poll)
            setAnalyzing(false)
            setCurrentStep(4)
            setReviewResult(data.result)
            message.success('文档分析完成！')
          } else if (data.status === 'failed') {
            clearInterval(poll)
            setAnalyzing(false)
            message.error('文档分析失败: ' + data.error)
          }
        }

        if (attempts >= maxAttempts) {
          clearInterval(poll)
          setAnalyzing(false)
          message.error('分析超时，请重试')
        }
      } catch (error) {
        console.error('获取结果失败:', error)
      }
    }, 5000) // 每5秒轮询一次
  }

  // 选择/取消选择修改
  const handleSelectChange = (changeId, checked) => {
    if (checked) {
      setSelectedChanges([...selectedChanges, changeId])
    } else {
      setSelectedChanges(selectedChanges.filter(id => id !== changeId))
    }
  }

  // 全选
  const handleSelectAll = () => {
    if (reviewResult && reviewResult.all_changes) {
      setSelectedChanges(reviewResult.all_changes.map(c => c.id))
    }
  }

  // 取消全选
  const handleDeselectAll = () => {
    setSelectedChanges([])
  }

  // 接受修改
  const handleAcceptChanges = () => {
    if (selectedChanges.length === 0) {
      message.warning('请先选择要接受的修改')
      return
    }

    Modal.confirm({
      title: '确认接受修改',
      content: `确定要接受选中的 ${selectedChanges.length} 处修改吗？`,
      onOk: async () => {
        try {
          const response = await fetch('/api/review/apply-changes', {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json'
            },
            body: JSON.stringify({
              file_id: taskId,
              accepted_change_ids: selectedChanges
            })
          })

          if (response.ok) {
            message.success('修改已应用，可以下载修改后的文档')
          } else {
            message.error('应用修改失败')
          }
        } catch (error) {
          console.error('应用修改失败:', error)
          message.error('应用修改失败: ' + error.message)
        }
      }
    })
  }

  // 下载修改后的文档
  const handleDownload = () => {
    if (!taskId) {
      message.warning('没有可下载的文档')
      return
    }

    const downloadUrl = `/api/review/download/${taskId}`
    window.open(downloadUrl, '_blank')
    message.success('开始下载文档')
  }

  // 获取修改类型的颜色
  const getChangeTypeColor = (type) => {
    const colors = {
      typo: 'red',
      fluency: 'orange',
      duplicate: 'purple',
      compliance: 'volcano'
    }
    return colors[type] || 'default'
  }

  // 获取修改类型的标签
  const getChangeTypeLabel = (type) => {
    const labels = {
      typo: '错别字',
      fluency: '语义通顺性',
      duplicate: '内容重复',
      compliance: '技术标准合规性'
    }
    return labels[type] || type
  }

  const uploadProps = {
    accept: '.docx,.doc',
    beforeUpload: handleUpload,
    showUploadList: false
  }

  return (
    <div className="review-container">
      {/* 上传区域 - 始终显示 */}
      <Card title="文档审查修正" style={{ marginBottom: 16 }}>
        {/* 知识库选择 */}
        <div style={{ marginBottom: 16 }}>
          <Text strong>选择知识库：</Text>
          <Space style={{ marginLeft: 8 }}>
            {knowledgeBases.map(kb => (
              <Button
                key={kb.id}
                type={selectedKbId === kb.id ? 'primary' : 'default'}
                onClick={() => setSelectedKbId(kb.id)}
                disabled={uploading || analyzing}
              >
                {kb.name}
              </Button>
            ))}
          </Space>
        </div>

        <Dragger {...uploadProps} disabled={uploading || analyzing}>
          <p className="ant-upload-drag-icon">
            <FileTextOutlined />
          </p>
          <p className="ant-upload-text">点击或拖拽Word文档到此区域上传</p>
          <p className="ant-upload-hint">
            支持 .docx 和 .doc 格式，系统将自动进行错别字识别、语义通顺性优化、内容重复检测和技术标准合规性验证
          </p>
        </Dragger>
      </Card>

      {/* 智能体流程图 - 始终显示 */}
      <Card title="多智能体审查流程" style={{ marginBottom: 16 }}>
        <Steps
          current={currentStep}
          items={agentSteps.map((step, index) => ({
            title: (
              <span>
                <span style={{ fontSize: '20px', marginRight: '8px' }}>{step.icon}</span>
                {step.title}
              </span>
            ),
            description: step.description,
            status: analyzing && index === currentStep ? 'process' :
                    index < currentStep ? 'finish' : 'wait',
            icon: analyzing && index === currentStep ? <SyncOutlined spin /> :
                  index < currentStep ? <CheckCircleOutlined /> : null
          }))}
        />

        {analyzing && (
          <div style={{ textAlign: 'center', marginTop: 24 }}>
            <Spin indicator={<LoadingOutlined style={{ fontSize: 48 }} spin />} />
            <p style={{ marginTop: 16, fontSize: '16px', fontWeight: 'bold' }}>
              {agentSteps[currentStep]?.title} 正在工作中...
            </p>
            <p style={{ color: '#999' }}>多智能体协同分析，预计需要1-3分钟</p>
          </div>
        )}
      </Card>

      {/* 审查结果 */}
      {reviewResult && (
        <>
          {/* 统计信息 */}
          <Row gutter={16} style={{ marginBottom: 16 }}>
            <Col span={6}>
              <Card>
                <Statistic
                  title="总修改建议"
                  value={reviewResult.total_changes}
                  prefix={<FileTextOutlined />}
                  valueStyle={{ color: '#1890ff' }}
                />
              </Card>
            </Col>
            <Col span={6}>
              <Card>
                <Statistic
                  title="错别字"
                  value={reviewResult.typo_count}
                  valueStyle={{ color: '#f5222d' }}
                />
              </Card>
            </Col>
            <Col span={6}>
              <Card>
                <Statistic
                  title="语义问题"
                  value={reviewResult.fluency_count}
                  valueStyle={{ color: '#fa8c16' }}
                />
              </Card>
            </Col>
            <Col span={6}>
              <Card>
                <Statistic
                  title="合规性问题"
                  value={reviewResult.compliance_count}
                  valueStyle={{ color: '#722ed1' }}
                />
              </Card>
            </Col>
          </Row>

          {/* 操作按钮 */}
          <Card style={{ marginBottom: 16 }}>
            <Space>
              <Button
                type="primary"
                icon={<CheckOutlined />}
                onClick={handleAcceptChanges}
                disabled={selectedChanges.length === 0}
              >
                接受选中修改 ({selectedChanges.length})
              </Button>
              <Button onClick={handleSelectAll}>
                全选
              </Button>
              <Button onClick={handleDeselectAll}>
                取消全选
              </Button>
              <Button
                icon={<DownloadOutlined />}
                onClick={handleDownload}
              >
                下载修改后文档
              </Button>
              <Button
                onClick={() => {
                  setReviewResult(null)
                  setSelectedChanges([])
                  setCurrentStep(0)
                }}
              >
                审查新文档
              </Button>
            </Space>
          </Card>

          {/* 对比视图 */}
          <Row gutter={16}>
            {/* 左侧：原文档 */}
            <Col span={12}>
              <Card
                title="原文档"
                extra={<Tag color="red">待修改</Tag>}
                style={{ height: '600px', overflow: 'auto' }}
              >
                <div className="document-content" style={{ whiteSpace: 'pre-wrap', lineHeight: '1.8' }}>
                  {reviewResult.original_content}
                </div>
              </Card>
            </Col>

            {/* 右侧：修改建议列表 */}
            <Col span={12}>
              <Card
                title="修改建议"
                extra={<Tag color="green">共 {reviewResult.total_changes} 处</Tag>}
                style={{ height: '600px', overflow: 'auto' }}
              >
                <List
                  dataSource={reviewResult.all_changes}
                  renderItem={(change) => (
                    <List.Item
                      key={change.id}
                      className="change-item"
                    >
                      <div style={{ width: '100%' }}>
                        <Space style={{ marginBottom: 8 }}>
                          <Checkbox
                            checked={selectedChanges.includes(change.id)}
                            onChange={(e) => handleSelectChange(change.id, e.target.checked)}
                          />
                          <Tag color={getChangeTypeColor(change.type)}>
                            {getChangeTypeLabel(change.type)}
                          </Tag>
                          {change.severity && (
                            <Tag color={change.severity === '严重' ? 'red' : 'orange'}>
                              {change.severity}
                            </Tag>
                          )}
                        </Space>

                        {change.original && (
                          <div style={{ marginBottom: 8 }}>
                            <div style={{ color: '#999', fontSize: 12 }}>原文：</div>
                            <div style={{
                              background: '#fff1f0',
                              padding: '8px',
                              borderRadius: '4px',
                              color: '#cf1322'
                            }}>
                              {change.original}
                            </div>
                          </div>
                        )}

                        {(change.corrected || change.improved || change.suggestion) && (
                          <div style={{ marginBottom: 8 }}>
                            <div style={{ color: '#999', fontSize: 12 }}>修改为：</div>
                            <div style={{
                              background: '#f6ffed',
                              padding: '8px',
                              borderRadius: '4px',
                              color: '#389e0d'
                            }}>
                              {change.corrected || change.improved || change.suggestion}
                            </div>
                          </div>
                        )}

                        {(change.reason || change.issue) && (
                          <div style={{ color: '#666', fontSize: 12 }}>
                            原因：{change.reason || change.issue}
                          </div>
                        )}

                        {change.reference && (
                          <div style={{ color: '#1890ff', fontSize: 12, marginTop: 4 }}>
                            参考：{change.reference}
                          </div>
                        )}
                      </div>
                    </List.Item>
                  )}
                />
              </Card>
            </Col>
          </Row>
        </>
      )}
    </div>
  )
}

export default Review
