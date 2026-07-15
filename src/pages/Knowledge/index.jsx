import { useState, useEffect, useCallback } from 'react'
import {
  Card,
  Button,
  Table,
  Modal,
  Form,
  Input,
  Upload,
  message,
  Space,
  Tag,
  Popconfirm,
  Statistic,
  Row,
  Col,
  Progress,
  List,
  Select
} from 'antd'
import {
  PlusOutlined,
  UploadOutlined,
  DeleteOutlined,
  EditOutlined,
  FileTextOutlined,
  DatabaseOutlined,
  CloudUploadOutlined,
  ClockCircleOutlined,
  EyeOutlined,
  InboxOutlined,
  CheckCircleOutlined,
  LoadingOutlined,
  CloseCircleOutlined,
  DownloadOutlined
} from '@ant-design/icons'
import DocumentPreview from '../../components/DocumentPreview'
import useUserStore from '../../store/userStore'
import './index.css'

const { TextArea } = Input
const { Dragger } = Upload

const APPLICABILITY_OPTIONS = [
  {
    value: 'auto',
    label: '自动识别',
    description: '根据规则文档名称和章节标题判断，正文偶然出现“突出”不会误标。'
  },
  {
    value: 'general',
    label: '整份通用',
    description: '强制将本次上传文档的全部规则块标记为通用。'
  },
  {
    value: 'outburst_only',
    label: '整份突出专用',
    description: '强制将本次上传文档的全部规则块标记为突出矿井专用。'
  }
]

const applicabilityMessage = (summary = {}) => {
  if (summary.available === false) return '已入库，适用性统计稍后刷新'
  const general = Number(summary.general || 0)
  const outburst = Number(summary.outburst_only || 0)
  return `通用 ${general} 块，突出专用 ${outburst} 块`
}

const SUPPORTED_RULE_EXTENSIONS = ['pdf', 'doc', 'docx', 'txt']
const isSupportedRuleFile = (file) => {
  const extension = String(file?.name || '').split('.').pop().toLowerCase()
  return SUPPORTED_RULE_EXTENSIONS.includes(extension)
}
const isWithinUploadLimit = (file) => Number(file?.size || 0) / 1024 / 1024 < 10

const Knowledge = () => {
  const user = useUserStore((state) => state.user)
  const [knowledgeBases, setKnowledgeBases] = useState([])
  const [documents, setDocuments] = useState([])

  const [isKbModalVisible, setIsKbModalVisible] = useState(false)
  const [selectedKbId, setSelectedKbId] = useState(null)
  const [editingKb, setEditingKb] = useState(null)
  const [form] = Form.useForm()

  // 文档预览相关状态
  const [previewVisible, setPreviewVisible] = useState(false)
  const [previewDocument, setPreviewDocument] = useState(null)

  // 文档编辑相关状态
  const [isDocModalVisible, setIsDocModalVisible] = useState(false)
  const [docForm] = Form.useForm()

  // 批量上传相关状态
  const [batchUploadVisible, setBatchUploadVisible] = useState(false)
  const [uploadingFiles, setUploadingFiles] = useState([]) // {uid, name, size, status: 'uploading'|'success'|'error', progress}
  const [batchUploading, setBatchUploading] = useState(false)
  const [applicabilityMode, setApplicabilityMode] = useState('auto')

  const authParams = useCallback(() => new URLSearchParams({
    user_id: String(user?.id || 'guest'),
    role: user?.role || 'user',
  }), [user?.id, user?.role])

  const appendAuthToForm = (values = {}) => new URLSearchParams({
    ...values,
    user_id: String(user?.id || 'guest'),
    role: user?.role || 'user',
    user_name: user?.name || user?.username || '',
  })

  // 加载知识库列表
  const loadKnowledgeBases = useCallback(async () => {
    try {
      const response = await fetch(`/api/knowledge/bases?${authParams().toString()}`)
      if (response.ok) {
        const data = await response.json()
        const nextKnowledgeBases = data.map(kb => ({
          ...kb,
          documentCount: kb.document_count || 0,
          totalSize: kb.total_size || 0,
          status: '已索引',
          createdAt: kb.created_at?.split('T')[0] || '',
          updatedAt: kb.updated_at?.split('T')[0] || kb.created_at?.split('T')[0] || ''
        }))
        setKnowledgeBases(nextKnowledgeBases)
        if (selectedKbId && !nextKnowledgeBases.some(kb => kb.id === selectedKbId)) {
          setSelectedKbId(null)
          setDocuments([])
        }
      }
    } catch (error) {
      console.error('加载知识库失败:', error)
      message.error('加载知识库失败')
    }
  }, [authParams, selectedKbId])

  // 加载文档列表
  const loadDocuments = useCallback(async (kbId) => {
    if (!kbId) return
    try {
      const response = await fetch(`/api/knowledge/bases/${kbId}/documents?${authParams().toString()}`)
      if (response.ok) {
        const data = await response.json()
        setDocuments(data.map(doc => ({
          ...doc,
          knowledgeBaseId: kbId,
          size: `${(doc.size / 1024 / 1024).toFixed(2)} MB`,
          uploadTime: doc.uploadTime?.replace('T', ' ').split('.')[0] || '',
          status: doc.status === 'indexed' ? '已索引' : doc.status
        })))
      }
    } catch (error) {
      console.error('加载文档列表失败:', error)
      message.error('加载文档列表失败')
    }
  }, [authParams])

  // 组件加载时获取数据
  useEffect(() => {
    loadKnowledgeBases()
  }, [loadKnowledgeBases])

  // 当选中知识库时加载文档
  useEffect(() => {
    if (selectedKbId) {
      loadDocuments(selectedKbId)
    }
  }, [loadDocuments, selectedKbId])

  // 打开文档预览
  const handlePreviewDocument = (document) => {
    setPreviewDocument(document)
    setPreviewVisible(true)
  }

  // 关闭文档预览
  const handleClosePreview = () => {
    setPreviewVisible(false)
    setPreviewDocument(null)
  }

  // 知识库表格列
  const kbColumns = [
    {
      title: '知识库名称',
      dataIndex: 'name',
      key: 'name',
      width: 200,
      render: (text) => (
        <Space>
          <DatabaseOutlined style={{ color: '#1890ff' }} />
          <span style={{ fontWeight: 500 }}>{text}</span>
        </Space>
      )
    },
    {
      title: '描述',
      dataIndex: 'description',
      key: 'description',
      ellipsis: true
    },
    {
      title: '文档数量',
      dataIndex: 'documentCount',
      key: 'documentCount',
      width: 100,
      align: 'center',
      render: (count) => <Tag color="blue">{count}</Tag>
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      align: 'center',
      render: (status) => (
        <Tag color={status === '已索引' ? 'success' : status === 'failed' ? 'error' : 'processing'}>
          {status === 'failed' ? '失败' : status}
        </Tag>
      )
    },
    {
      title: '更新时间',
      dataIndex: 'updatedAt',
      key: 'updatedAt',
      width: 120
    },
    {
      title: '操作',
      key: 'action',
      width: 240,
      fixed: 'right',
      render: (_, record) => (
        <Space size="small">
          <Button
            type="link"
            size="small"
            onClick={() => handleViewDocuments(record.id)}
          >
            查看
          </Button>
          <Button
            type="link"
            size="small"
            icon={<EditOutlined />}
            onClick={() => handleEditKb(record)}
          >
            编辑
          </Button>
          <Popconfirm
            title="确定删除此知识库吗？"
            onConfirm={() => handleDeleteKb(record.id)}
            okText="确定"
            cancelText="取消"
          >
            <Button type="link" size="small" danger icon={<DeleteOutlined />}>
              删除
            </Button>
          </Popconfirm>
        </Space>
      )
    }
  ]

  // 文档表格列
  const docColumns = [
    {
      title: '文档名称',
      dataIndex: 'name',
      key: 'name',
      render: (text) => (
        <Space>
          <FileTextOutlined />
          {text}
        </Space>
      )
    },
    {
      title: '大小',
      dataIndex: 'size',
      key: 'size',
      width: 100
    },
    {
      title: '上传时间',
      dataIndex: 'uploadTime',
      key: 'uploadTime',
      width: 180
    },
    {
      title: '适用范围',
      dataIndex: 'applicabilitySummary',
      key: 'applicabilitySummary',
      width: 210,
      render: (summary = {}) => {
        const total = Number(summary.total || 0)
        const general = Number(summary.general || 0)
        const outburst = Number(summary.outburst_only || 0)
        if (summary.available === false || (total > 0 && general + outburst === 0)) {
          return <Tag>适用性统计暂不可用</Tag>
        }
        return (
          <Space size={[4, 4]} wrap>
            <Tag color="green">通用 {general}</Tag>
            <Tag color="volcano">突出专用 {outburst}</Tag>
          </Space>
        )
      }
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      align: 'center',
      render: (status) => (
        <Tag color={status === '已索引' ? 'success' : 'processing'}>{status}</Tag>
      )
    },
    {
      title: '操作',
      key: 'action',
      width: 250,
      render: (_, record) => (
        <Space size="small">
          <Button
            type="link"
            size="small"
            icon={<EyeOutlined />}
            onClick={() => handlePreviewDocument(record)}
          >
            预览
          </Button>
          <Button
            type="link"
            size="small"
            icon={<DownloadOutlined />}
            onClick={() => handleDownloadDoc(record)}
          >
            下载
          </Button>
          <Button
            type="link"
            size="small"
            icon={<EditOutlined />}
            onClick={() => handleEditDoc(record)}
          >
            编辑
          </Button>
          <Popconfirm
            title="确定删除此文档吗？"
            onConfirm={() => handleDeleteDoc(record.id)}
            okText="确定"
            cancelText="取消"
          >
            <Button type="link" size="small" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      )
    }
  ]

  // 创建/编辑知识库
  const handleKbSubmit = async () => {
    try {
      const values = await form.validateFields()
      if (editingKb) {
        // 编辑
        const response = await fetch(`/api/knowledge/bases/${editingKb.id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: appendAuthToForm(values)
        })
        if (response.ok) {
          message.success('知识库更新成功')
          loadKnowledgeBases() // 重新加载列表
        } else {
          message.error('更新失败')
        }
      } else {
        // 新建
        const response = await fetch('/api/knowledge/bases', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: appendAuthToForm(values)
        })
        if (response.ok) {
          message.success('知识库创建成功')
          loadKnowledgeBases() // 重新加载列表
        } else {
          message.error('创建失败')
        }
      }
      setIsKbModalVisible(false)
      form.resetFields()
      setEditingKb(null)
    } catch (error) {
      console.error('提交失败:', error)
      message.error('操作失败')
    }
  }

  // 编辑知识库
  const handleEditKb = (record) => {
    setEditingKb(record)
    form.setFieldsValue(record)
    setIsKbModalVisible(true)
  }

  // 删除知识库
  const handleDeleteKb = async (id) => {
    try {
      const response = await fetch(`/api/knowledge/bases/${id}?${authParams().toString()}`, {
        method: 'DELETE'
      })
      if (response.ok) {
        message.success('知识库删除成功')
        loadKnowledgeBases() // 重新加载列表
        if (selectedKbId === id) {
          setSelectedKbId(null)
          setDocuments([])
        }
      } else {
        message.error('删除失败')
      }
    } catch (error) {
      console.error('删除失败:', error)
      message.error('删除失败')
    }
  }

  // 查看文档
  const handleViewDocuments = (kbId) => {
    setSelectedKbId(kbId)
  }

  // 上传文档
  const uploadProps = {
    customRequest: async ({ file, onSuccess, onError }) => {
      if (!selectedKbId) {
        message.error('请先选择知识库')
        onError(new Error('未选择知识库'))
        return
      }

      const formData = new FormData()
      formData.append('file', file)
      formData.append('user_id', String(user?.id || 'guest'))
      formData.append('role', user?.role || 'user')
      formData.append('applicability', applicabilityMode)

      try {
        const response = await fetch(`/api/knowledge/bases/${selectedKbId}/documents`, {
          method: 'POST',
          body: formData
        })

        if (response.ok) {
          const result = await response.json()
          message.success(
            `${file.name} 上传并索引成功：${applicabilityMessage(result.applicability_summary)}`
          )
          onSuccess(result)
          // 重新加载文档列表和知识库列表
          loadDocuments(selectedKbId)
          loadKnowledgeBases()
        } else {
          const errorData = await response.json().catch(() => ({}))
          const detail = errorData.detail || '上传失败'
          message.error(`${file.name} 上传失败：${detail}`)
          onError(new Error(detail))
        }
      } catch (error) {
        console.error('上传失败:', error)
        message.error(`${file.name} 上传失败`)
        onError(error)
      }
    },
    beforeUpload: (file) => {
      if (!isSupportedRuleFile(file)) {
        message.error('只支持 PDF、Word、TXT 格式文件')
        return Upload.LIST_IGNORE
      }
      if (!isWithinUploadLimit(file)) {
        message.error('文件大小不能超过 10MB')
        return Upload.LIST_IGNORE
      }
      return true
    }
  }

  // 下载文档
  const handleDownloadDoc = (record) => {
    const downloadUrl = `/api/knowledge/documents/${record.id}/download?${authParams().toString()}`
    const link = document.createElement('a')
    link.href = downloadUrl
    link.download = record.name
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    message.success('开始下载文档')
  }

  // 编辑文档
  const handleEditDoc = (record) => {
    docForm.setFieldsValue({
      name: record.name
    })
    setIsDocModalVisible(true)
  }

  // 提交文档编辑
  const handleDocSubmit = async () => {
    try {
      await docForm.validateFields()
      // 这里可以添加更新文档名称的API调用
      // 暂时只是关闭弹窗
      message.info('文档编辑功能待实现')
      setIsDocModalVisible(false)
      docForm.resetFields()
    } catch (error) {
      console.error('提交失败:', error)
    }
  }

  // 删除文档
  const handleDeleteDoc = async (id) => {
    try {
      const response = await fetch(`/api/knowledge/documents/${id}?${authParams().toString()}`, {
        method: 'DELETE'
      })
      if (response.ok) {
        message.success('文档删除成功')
        loadDocuments(selectedKbId) // 重新加载文档列表
        loadKnowledgeBases() // 更新知识库文档计数
      } else {
        message.error('删除失败')
      }
    } catch (error) {
      console.error('删除失败:', error)
      message.error('删除失败')
    }
  }

  // 批量上传配置
  const batchUploadProps = {
    name: 'file',
    multiple: true,
    accept: '.pdf,.doc,.docx,.txt',
    showUploadList: false,
    beforeUpload: (file) => {
      // 验证文件类型
      if (!isSupportedRuleFile(file)) {
        message.error(`${file.name} 格式不支持，只支持 PDF、Word、TXT 格式`)
        return Upload.LIST_IGNORE
      }
      // 验证文件大小
      if (!isWithinUploadLimit(file)) {
        message.error(`${file.name} 超过10MB限制`)
        return Upload.LIST_IGNORE
      }
      return false // 阻止自动上传
    },
    onChange: (info) => {
      const { fileList } = info
      // 过滤有效文件
      const validFiles = fileList.filter(file => {
        const source = file.originFileObj || file
        return isSupportedRuleFile(source) && isWithinUploadLimit(source)
      })

      // 更新上传文件列表
      const newUploadingFiles = validFiles.map(file => ({
        uid: file.uid,
        name: file.name,
        size: file.size,
        status: 'waiting',
        progress: 0,
        file: file.originFileObj || file
      }))
      setUploadingFiles(newUploadingFiles)
    }
  }

  // 开始批量上传
  const handleStartBatchUpload = async () => {
    if (uploadingFiles.length === 0) {
      message.warning('请先选择要上传的文件')
      return
    }
    if (!selectedKbId) {
      message.error('请先选择知识库')
      return
    }

    setBatchUploading(true)
    let successCount = 0
    try {
      // 串行提交，避免多个 MinerU 解析任务同时争抢显存和内存。
      for (const fileInfo of uploadingFiles) {
        setUploadingFiles(prev =>
          prev.map(f => f.uid === fileInfo.uid
            ? { ...f, status: 'uploading', progress: 35, error: '' }
            : f)
        )

        const formData = new FormData()
        formData.append('file', fileInfo.file)
        formData.append('user_id', String(user?.id || 'guest'))
        formData.append('role', user?.role || 'user')
        formData.append('applicability', applicabilityMode)

        try {
          const response = await fetch(`/api/knowledge/bases/${selectedKbId}/documents`, {
            method: 'POST',
            body: formData
          })
          const result = await response.json().catch(() => ({}))
          if (!response.ok) {
            throw new Error(result.detail || '上传或规则解析失败')
          }
          successCount += 1
          setUploadingFiles(prev =>
            prev.map(f => f.uid === fileInfo.uid
              ? {
                  ...f,
                  status: 'success',
                  progress: 100,
                  applicabilitySummary: result.applicability_summary
                }
              : f)
          )
        } catch (error) {
          setUploadingFiles(prev =>
            prev.map(f => f.uid === fileInfo.uid
              ? { ...f, status: 'error', progress: 0, error: error.message }
              : f)
          )
        }
      }
    } finally {
      setBatchUploading(false)
      await Promise.all([loadDocuments(selectedKbId), loadKnowledgeBases()])
      if (successCount === uploadingFiles.length) {
        message.success(`批量上传完成，共索引 ${successCount} 份规则文档`)
      } else {
        message.warning(`批量上传完成：成功 ${successCount} 份，失败 ${uploadingFiles.length - successCount} 份`)
      }
    }
  }

  // 移除待上传文件
  const handleRemoveUploadFile = (uid) => {
    setUploadingFiles(prev => prev.filter(f => f.uid !== uid))
  }

  // 关闭批量上传弹窗
  const handleCloseBatchUpload = () => {
    if (batchUploading) return
    setBatchUploadVisible(false)
    setUploadingFiles([])
  }

  // 检查是否所有文件都上传完成
  const allUploaded = uploadingFiles.length > 0 && uploadingFiles.every(
    f => f.status === 'success' || f.status === 'error'
  )

  // 计算统计数据
  const totalDocuments = knowledgeBases.reduce((sum, kb) => sum + (kb.document_count || 0), 0)

  // 存储空间：所有知识库的总大小（从后端返回的total_size累加）
  const totalStorageMB = knowledgeBases.reduce((sum, kb) => sum + (kb.totalSize || 0), 0) / 1024 / 1024

  // 最近更新时间：取所有知识库中最新的更新时间
  const lastUpdatedKb = knowledgeBases.length > 0
    ? Math.max(...knowledgeBases.map(kb => {
        const timestamp = kb.updated_at || kb.updatedAt
        return timestamp ? new Date(timestamp).getTime() : 0
      }))
    : 0
  const lastUpdatedTime = lastUpdatedKb > 0
    ? (() => {
        const minutes = Math.floor((Date.now() - lastUpdatedKb) / 1000 / 60)
        if (minutes < 1) return '刚刚'
        if (minutes < 60) return `${minutes}分钟前`
        const hours = Math.floor(minutes / 60)
        if (hours < 24) return `${hours}小时前`
        const days = Math.floor(hours / 24)
        return `${days}天前`
      })()
    : '无更新'

  const currentKb = knowledgeBases.find(kb => kb.id === selectedKbId)
  const currentDocs = documents.filter(doc => doc.knowledgeBaseId === selectedKbId)

  return (
    <div className="knowledge-container">
      {/* 统计卡片 */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={6}>
          <Card>
            <Statistic
              title="知识库总数"
              value={knowledgeBases.length}
              prefix={<DatabaseOutlined />}
              valueStyle={{ color: '#3f8600' }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="文档总数"
              value={totalDocuments}
              prefix={<FileTextOutlined />}
              valueStyle={{ color: '#1890ff' }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="存储空间"
              value={totalStorageMB.toFixed(2)}
              suffix="MB"
              prefix={<CloudUploadOutlined />}
              valueStyle={{ color: '#cf1322' }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="最近更新"
              value={lastUpdatedTime}
              prefix={<ClockCircleOutlined />}
            />
          </Card>
        </Col>
      </Row>

      {!selectedKbId ? (
        // 知识库列表视图
        <Card
          title="知识库管理"
          extra={
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => {
                setEditingKb(null)
                form.resetFields()
                setIsKbModalVisible(true)
              }}
            >
              创建知识库
            </Button>
          }
        >
          <Table
            columns={kbColumns}
            dataSource={knowledgeBases}
            rowKey="id"
            pagination={{ pageSize: 10 }}
            scroll={{ x: 1000 }}
          />
        </Card>
      ) : (
        // 文档列表视图
        <Card
          title={
            <Space>
              <Button type="link" onClick={() => setSelectedKbId(null)}>
                返回
              </Button>
              <span>/ {currentKb?.name}</span>
            </Space>
          }
          extra={
            <Space wrap>
              <div className="kb-applicability-control">
                <span className="kb-applicability-label">新规则适用性</span>
                <Select
                  value={applicabilityMode}
                  options={APPLICABILITY_OPTIONS}
                  onChange={setApplicabilityMode}
                  optionRender={(option) => (
                    <div>
                      <div>{option.data.label}</div>
                      <div className="kb-applicability-option-help">
                        {option.data.description}
                      </div>
                    </div>
                  )}
                  popupMatchSelectWidth={360}
                  style={{ width: 150 }}
                />
              </div>
              <Upload {...uploadProps}>
                <Button icon={<UploadOutlined />}>上传规则文档</Button>
              </Upload>
              <Button
                type="primary"
                icon={<CloudUploadOutlined />}
                onClick={() => setBatchUploadVisible(true)}
              >
                批量上传
              </Button>
            </Space>
          }
        >
          <Table
            columns={docColumns}
            dataSource={currentDocs}
            rowKey="id"
            pagination={{ pageSize: 10 }}
            scroll={{ x: 1200 }}
          />
        </Card>
      )}

      {/* 创建/编辑知识库弹窗 */}
      <Modal
        title={editingKb ? '编辑知识库' : '创建知识库'}
        open={isKbModalVisible}
        onOk={handleKbSubmit}
        onCancel={() => {
          setIsKbModalVisible(false)
          form.resetFields()
          setEditingKb(null)
        }}
        okText="确定"
        cancelText="取消"
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="name"
            label="知识库名称"
            rules={[{ required: true, message: '请输入知识库名称' }]}
          >
            <Input placeholder="请输入知识库名称" />
          </Form.Item>
          <Form.Item
            name="description"
            label="描述"
            rules={[{ required: true, message: '请输入描述' }]}
          >
            <TextArea rows={4} placeholder="请输入知识库描述" />
          </Form.Item>
        </Form>
      </Modal>

      {/* 文档预览弹窗 */}
      <DocumentPreview
        visible={previewVisible}
        document={previewDocument}
        onClose={handleClosePreview}
      />

      {/* 编辑文档弹窗 */}
      <Modal
        title="编辑文档"
        open={isDocModalVisible}
        onOk={handleDocSubmit}
        onCancel={() => {
          setIsDocModalVisible(false)
          docForm.resetFields()
        }}
        okText="确定"
        cancelText="取消"
      >
        <Form form={docForm} layout="vertical">
          <Form.Item
            name="name"
            label="文档名称"
            rules={[{ required: true, message: '请输入文档名称' }]}
          >
            <Input placeholder="请输入文档名称" />
          </Form.Item>
        </Form>
      </Modal>

      {/* 批量上传弹窗 */}
      <Modal
        title="批量上传规则文档"
        open={batchUploadVisible}
        onCancel={handleCloseBatchUpload}
        maskClosable={!batchUploading}
        closable={!batchUploading}
        width={600}
        footer={[
          <Button key="cancel" onClick={handleCloseBatchUpload} disabled={batchUploading}>
            {allUploaded ? '完成' : '取消'}
          </Button>,
          !allUploaded && (
            <Button
              key="upload"
              type="primary"
              onClick={handleStartBatchUpload}
              loading={batchUploading}
              disabled={uploadingFiles.length === 0 || batchUploading}
            >
              开始上传
            </Button>
          )
        ]}
      >
        <div className="kb-batch-scope">
          <span>
            本批规则适用性：
            <strong>{APPLICABILITY_OPTIONS.find(item => item.value === applicabilityMode)?.label}</strong>
          </span>
          <span className="kb-batch-scope-help">
            {APPLICABILITY_OPTIONS.find(item => item.value === applicabilityMode)?.description}
          </span>
        </div>
        <Dragger {...batchUploadProps} style={{ marginBottom: 16 }}>
          <p className="ant-upload-drag-icon">
            <InboxOutlined />
          </p>
          <p className="ant-upload-text">点击或拖拽文件到此区域上传</p>
          <p className="ant-upload-hint">
            支持 PDF、Word、TXT 格式，单个文件不超过 10MB，可批量上传多个文件
          </p>
        </Dragger>

        {uploadingFiles.length > 0 && (
          <List
            size="small"
            header={<div>待上传文件 ({uploadingFiles.length})</div>}
            bordered
            dataSource={uploadingFiles}
            renderItem={(item) => (
              <List.Item
                actions={[
                  item.status === 'waiting' && (
                    <Button
                      type="link"
                      size="small"
                      danger
                      onClick={() => handleRemoveUploadFile(item.uid)}
                    >
                      移除
                    </Button>
                  )
                ].filter(Boolean)}
              >
                <List.Item.Meta
                  avatar={
                    item.status === 'success' ? (
                      <CheckCircleOutlined style={{ color: '#52c41a', fontSize: 20 }} />
                    ) : item.status === 'uploading' ? (
                      <LoadingOutlined style={{ color: '#1890ff', fontSize: 20 }} />
                    ) : item.status === 'error' ? (
                      <CloseCircleOutlined style={{ color: '#ff4d4f', fontSize: 20 }} />
                    ) : (
                      <FileTextOutlined style={{ color: '#999', fontSize: 20 }} />
                    )
                  }
                  title={item.name}
                  description={
                    item.status === 'uploading' ? (
                      <Space direction="vertical" size={0} style={{ width: '100%' }}>
                        <span>上传并解析规则中，请勿关闭窗口</span>
                        <Progress percent={item.progress} size="small" status="active" showInfo={false} />
                      </Space>
                    ) : item.status === 'success' ? (
                      <span style={{ color: '#52c41a' }}>
                        上传成功 · {applicabilityMessage(item.applicabilitySummary)}
                      </span>
                    ) : item.status === 'error' ? (
                      <span style={{ color: '#ff4d4f' }}>上传失败：{item.error || '未知错误'}</span>
                    ) : (
                      <span>{(item.size / 1024 / 1024).toFixed(2)} MB</span>
                    )
                  }
                />
              </List.Item>
            )}
          />
        )}
      </Modal>
    </div>
  )
}

export default Knowledge
