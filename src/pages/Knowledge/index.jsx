import { useState, useEffect } from 'react'
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
  List
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
import './index.css'

const { TextArea } = Input
const { Dragger } = Upload

const Knowledge = () => {
  const [knowledgeBases, setKnowledgeBases] = useState([])
  const [documents, setDocuments] = useState([])
  const [loading, setLoading] = useState(false)

  const [isKbModalVisible, setIsKbModalVisible] = useState(false)
  const [selectedKbId, setSelectedKbId] = useState(null)
  const [editingKb, setEditingKb] = useState(null)
  const [form] = Form.useForm()

  // 文档预览相关状态
  const [previewVisible, setPreviewVisible] = useState(false)
  const [previewDocument, setPreviewDocument] = useState(null)

  // 文档编辑相关状态
  const [isDocModalVisible, setIsDocModalVisible] = useState(false)
  const [editingDoc, setEditingDoc] = useState(null)
  const [docForm] = Form.useForm()

  // 批量上传相关状态
  const [batchUploadVisible, setBatchUploadVisible] = useState(false)
  const [uploadingFiles, setUploadingFiles] = useState([]) // {uid, name, size, status: 'uploading'|'success'|'error', progress}

  // 加载知识库列表
  const loadKnowledgeBases = async () => {
    setLoading(true)
    try {
      const response = await fetch('/api/knowledge/bases')
      if (response.ok) {
        const data = await response.json()
        setKnowledgeBases(data.map(kb => ({
          ...kb,
          documentCount: kb.document_count || 0,
          totalSize: kb.total_size || 0,
          status: '已索引',
          createdAt: kb.created_at?.split('T')[0] || '',
          updatedAt: kb.updated_at?.split('T')[0] || kb.created_at?.split('T')[0] || ''
        })))
      }
    } catch (error) {
      console.error('加载知识库失败:', error)
      message.error('加载知识库失败')
    } finally {
      setLoading(false)
    }
  }

  // 加载文档列表
  const loadDocuments = async (kbId) => {
    if (!kbId) return
    try {
      const response = await fetch(`/api/knowledge/bases/${kbId}/documents`)
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
  }

  // 组件加载时获取数据
  useEffect(() => {
    loadKnowledgeBases()
  }, [])

  // 当选中知识库时加载文档
  useEffect(() => {
    if (selectedKbId) {
      loadDocuments(selectedKbId)
    }
  }, [selectedKbId])

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
        <Tag color={status === '已索引' ? 'success' : 'processing'}>{status}</Tag>
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
          body: new URLSearchParams(values)
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
          body: new URLSearchParams(values)
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
      const response = await fetch(`/api/knowledge/bases/${id}`, {
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

      try {
        const response = await fetch(`/api/knowledge/bases/${selectedKbId}/documents`, {
          method: 'POST',
          body: formData
        })

        if (response.ok) {
          const result = await response.json()
          message.success(`${file.name} 上传成功`)
          onSuccess(result)
          // 重新加载文档列表和知识库列表
          loadDocuments(selectedKbId)
          loadKnowledgeBases()
        } else {
          message.error(`${file.name} 上传失败`)
          onError(new Error('上传失败'))
        }
      } catch (error) {
        console.error('上传失败:', error)
        message.error(`${file.name} 上传失败`)
        onError(error)
      }
    },
    beforeUpload: (file) => {
      const isValidType = ['application/pdf', 'application/msword', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'text/plain'].includes(file.type)
      if (!isValidType) {
        message.error('只支持 PDF、Word、TXT 格式文件')
        return false
      }
      const isLt10M = file.size / 1024 / 1024 < 10
      if (!isLt10M) {
        message.error('文件大小不能超过 10MB')
        return false
      }
      return true
    }
  }

  // 下载文档
  const handleDownloadDoc = (record) => {
    const downloadUrl = `/api/knowledge/documents/${record.id}/download`
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
    setEditingDoc(record)
    docForm.setFieldsValue({
      name: record.name
    })
    setIsDocModalVisible(true)
  }

  // 提交文档编辑
  const handleDocSubmit = async () => {
    try {
      const values = await docForm.validateFields()
      // 这里可以添加更新文档名称的API调用
      // 暂时只是关闭弹窗
      message.info('文档编辑功能待实现')
      setIsDocModalVisible(false)
      docForm.resetFields()
      setEditingDoc(null)
    } catch (error) {
      console.error('提交失败:', error)
    }
  }

  // 删除文档
  const handleDeleteDoc = async (id) => {
    try {
      const response = await fetch(`/api/knowledge/documents/${id}`, {
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
    beforeUpload: (file, fileList) => {
      // 验证文件类型
      const isValidType = ['application/pdf', 'application/msword', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'text/plain'].includes(file.type)
      if (!isValidType) {
        message.error(`${file.name} 格式不支持，只支持 PDF、Word、TXT 格式`)
        return false
      }
      // 验证文件大小
      const isLt10M = file.size / 1024 / 1024 < 10
      if (!isLt10M) {
        message.error(`${file.name} 超过10MB限制`)
        return false
      }
      return false // 阻止自动上传
    },
    onChange: (info) => {
      const { fileList } = info
      // 过滤有效文件
      const validFiles = fileList.filter(file => {
        const isValidType = ['application/pdf', 'application/msword', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'text/plain'].includes(file.type)
        const isLt10M = file.size / 1024 / 1024 < 10
        return isValidType && isLt10M
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
  const handleStartBatchUpload = () => {
    if (uploadingFiles.length === 0) {
      message.warning('请先选择要上传的文件')
      return
    }

    // 模拟上传每个文件
    uploadingFiles.forEach((fileInfo, index) => {
      // 设置为上传中状态
      setTimeout(() => {
        setUploadingFiles(prev =>
          prev.map(f => f.uid === fileInfo.uid ? { ...f, status: 'uploading', progress: 0 } : f)
        )

        // 模拟上传进度
        let progress = 0
        const progressInterval = setInterval(() => {
          progress += Math.random() * 30
          if (progress >= 100) {
            progress = 100
            clearInterval(progressInterval)

            // 上传完成
            setUploadingFiles(prev =>
              prev.map(f => f.uid === fileInfo.uid ? { ...f, status: 'success', progress: 100 } : f)
            )

            // 添加到文档列表
            const newDoc = {
              id: Date.now() + index,
              knowledgeBaseId: selectedKbId,
              name: fileInfo.name,
              size: (fileInfo.size / 1024 / 1024).toFixed(2) + ' MB',
              uploadTime: new Date().toLocaleString('zh-CN'),
              status: '索引中'
            }
            setDocuments(prev => [...prev, newDoc])

            // 模拟索引完成
            setTimeout(() => {
              setDocuments(docs =>
                docs.map(doc =>
                  doc.id === newDoc.id ? { ...doc, status: '已索引' } : doc
                )
              )
              setKnowledgeBases(kbs =>
                kbs.map(kb =>
                  kb.id === selectedKbId
                    ? { ...kb, documentCount: kb.documentCount + 1 }
                    : kb
                )
              )
            }, 2000)
          } else {
            setUploadingFiles(prev =>
              prev.map(f => f.uid === fileInfo.uid ? { ...f, progress: Math.floor(progress) } : f)
            )
          }
        }, 200)
      }, index * 500) // 错开每个文件的上传时间
    })
  }

  // 移除待上传文件
  const handleRemoveUploadFile = (uid) => {
    setUploadingFiles(prev => prev.filter(f => f.uid !== uid))
  }

  // 关闭批量上传弹窗
  const handleCloseBatchUpload = () => {
    setBatchUploadVisible(false)
    setUploadingFiles([])
  }

  // 检查是否所有文件都上传完成
  const allUploaded = uploadingFiles.length > 0 && uploadingFiles.every(f => f.status === 'success')

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
            <Space>
              <Upload {...uploadProps}>
                <Button icon={<UploadOutlined />}>上传文档</Button>
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
          setEditingDoc(null)
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
        title="批量上传文档"
        open={batchUploadVisible}
        onCancel={handleCloseBatchUpload}
        width={600}
        footer={[
          <Button key="cancel" onClick={handleCloseBatchUpload}>
            {allUploaded ? '完成' : '取消'}
          </Button>,
          !allUploaded && (
            <Button
              key="upload"
              type="primary"
              onClick={handleStartBatchUpload}
              disabled={uploadingFiles.length === 0 || uploadingFiles.some(f => f.status === 'uploading')}
            >
              开始上传
            </Button>
          )
        ]}
      >
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
                      <Progress percent={item.progress} size="small" />
                    ) : item.status === 'success' ? (
                      <span style={{ color: '#52c41a' }}>上传成功</span>
                    ) : item.status === 'error' ? (
                      <span style={{ color: '#ff4d4f' }}>上传失败</span>
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
