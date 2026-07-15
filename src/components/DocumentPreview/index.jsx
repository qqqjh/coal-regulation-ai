import { useState, useEffect, memo } from 'react'
import { Modal, Tabs, Typography, Tag, Space, Divider, List, Spin } from 'antd'
import {
  FileTextOutlined,
  FilePdfOutlined,
  FileWordOutlined,
  FileUnknownOutlined,
  ClockCircleOutlined,
  DatabaseOutlined
} from '@ant-design/icons'
import useUserStore from '../../store/userStore'
import './index.css'

const { Title, Paragraph, Text } = Typography

// 根据文件名获取图标
const getFileIcon = (fileName) => {
  const ext = fileName.split('.').pop().toLowerCase()
  switch (ext) {
    case 'pdf':
      return <FilePdfOutlined style={{ color: '#ff4d4f', fontSize: 48 }} />
    case 'doc':
    case 'docx':
      return <FileWordOutlined style={{ color: '#1890ff', fontSize: 48 }} />
    case 'txt':
      return <FileTextOutlined style={{ color: '#52c41a', fontSize: 48 }} />
    default:
      return <FileUnknownOutlined style={{ color: '#999', fontSize: 48 }} />
  }
}

const APPLICABILITY_REASON_LABELS = {
  document_override: '上传时人工指定整份文档的适用范围',
  document_name: '规则文档名称表明其为突出矿井专用',
  structure_heading: '章节或上级标题表明该规则块为突出矿井专用',
  no_outburst_scope_in_structure: '文档名和章节结构未限定为突出矿井专用',
  stored_metadata: '使用入库时保存的适用范围'
}

const getApplicabilityTag = (metadata = {}) => {
  const reason = APPLICABILITY_REASON_LABELS[metadata.applicability_reason]
    || metadata.applicability_reason
  if (metadata.applicability === 'outburst_only') {
    return (
      <Tag color="volcano" title={reason || '突出矿井专用规则'}>
        突出专用
      </Tag>
    )
  }
  if (metadata.applicability === 'general') {
    return (
      <Tag color="green" title={reason || '通用规则'}>
        通用
      </Tag>
    )
  }
  return null
}

// Mock 文档内容数据
const mockDocumentContent = {
  1: {
    title: '煤矿安全规程',
    type: 'pdf',
    pages: 156,
    uploadTime: '2024-01-15 10:30',
    size: '2.3 MB',
    chunks: [
      {
        id: 1,
        page: 1,
        content: '第一章 总则\n\n第一条 为了保障煤矿安全生产和职工人身安全，防止煤矿事故，根据《中华人民共和国安全生产法》、《中华人民共和国矿山安全法》等有关法律、行政法规，制定本规程。\n\n第二条 本规程适用于在中华人民共和国领域及管辖海域内的所有煤矿。'
      },
      {
        id: 2,
        page: 2,
        content: '第三条 煤矿企业（矿井）必须遵守国家有关安全生产的法律、法规、规章、标准和技术规范。\n\n第四条 煤矿企业必须建立健全各级领导安全生产责任制、职能机构安全生产责任制、岗位人员安全生产责任制。\n\n第五条 煤矿企业必须对职工进行安全培训，未经培训合格的不得上岗作业。'
      },
      {
        id: 3,
        page: 15,
        content: '第二章 井工煤矿\n\n第一节 开拓与开采\n\n第二十一条 煤矿建设项目的安全设施必须与主体工程同时设计、同时施工、同时投入生产和使用。\n\n第二十二条 采用正规采煤方法，合理布置采区和工作面，保证安全出口畅通。'
      },
      {
        id: 4,
        page: 45,
        content: '第三节 瓦斯防治\n\n第一百一十条 矿井必须建立瓦斯、二氧化碳和其他有害气体检查制度，并遵守下列规定：\n\n（一）矿长、矿技术负责人、爆破工、采掘区队长、通风区队长、工程技术人员、班长、流动电钳工下井时，必须携带便携式甲烷检测报警仪。'
      },
      {
        id: 5,
        page: 78,
        content: '第四节 防灭火\n\n第一百四十五条 煤矿必须制定井上、下防灭火措施。\n\n第一百四十六条 进风井口应有防止烟雾侵入井下的设施。井下必须设消防材料库，储存足够数量的消防材料。'
      }
    ],
    summary: '本规程共分十二章，包含总则、井工煤矿、露天煤矿、通风与瓦斯防治、防灭火、防治水、爆破作业、电气设备、运输与提升、职业病危害防治等内容，是煤矿安全生产的基本规范。'
  },
  2: {
    title: '瓦斯防治细则',
    type: 'pdf',
    pages: 86,
    uploadTime: '2024-01-16 14:20',
    size: '1.8 MB',
    chunks: [
      {
        id: 1,
        page: 1,
        content: '第一章 总则\n\n第一条 为加强煤矿瓦斯防治工作，有效预防和遏制煤矿瓦斯事故，保障煤矿安全生产和从业人员的生命安全，根据《安全生产法》《矿山安全法》《煤矿安全规程》等法律法规和标准规范，制定本细则。'
      },
      {
        id: 2,
        page: 8,
        content: '第二章 瓦斯检测与监控\n\n第十五条 瓦斯检测与监控系统应当24小时连续运行，系统应当具备实时监测、超限报警、断电和馈电状态监测等功能。\n\n第十六条 井下所有采掘工作面、硐室、使用中的机电设备设置地点、有人员作业的地点都应当纳入监测范围。'
      },
      {
        id: 3,
        page: 25,
        content: '第三章 瓦斯抽采\n\n第三十条 高瓦斯矿井、煤与瓦斯突出矿井必须建立地面永久瓦斯抽采系统。\n\n第三十一条 瓦斯抽采系统应当独立、可靠，并实现抽采参数的连续监测和自动记录。'
      }
    ],
    summary: '本细则详细规定了煤矿瓦斯检测、监控、抽采等技术要求，是煤矿瓦斯防治工作的重要技术规范。'
  },
  3: {
    title: '通风系统管理',
    type: 'docx',
    pages: 42,
    uploadTime: '2024-01-17 09:15',
    size: '956 KB',
    chunks: [
      {
        id: 1,
        page: 1,
        content: '第一章 通风系统基本要求\n\n1.1 矿井必须建立完善的机械通风系统。主要通风机必须安装在地面；装有主要通风机的出风井口应当安装防爆门，防爆门每6个月检查维修1次。'
      },
      {
        id: 2,
        page: 8,
        content: '第二章 通风设施管理\n\n2.1 通风设施包括风门、风桥、风窗、密闭等。所有通风设施必须按照设计要求建设，并定期检查维护。\n\n2.2 主要风门必须实现联锁，防止风流短路。'
      },
      {
        id: 3,
        page: 20,
        content: '第三章 局部通风管理\n\n3.1 掘进工作面必须采用独立通风。\n\n3.2 局部通风机应当实现"三专两闭锁"，即专用变压器、专用开关、专用线路，以及风电闭锁、瓦斯电闭锁。'
      }
    ],
    summary: '本文档规定了煤矿通风系统的设计、建设、管理和维护要求，确保矿井通风安全可靠。'
  }
}

const DocumentPreview = memo(({ visible, document, onClose }) => {
  const user = useUserStore((state) => state.user)
  const [loading, setLoading] = useState(false)
  const [activeTab, setActiveTab] = useState('content')
  const [docContent, setDocContent] = useState(null)

  // 加载真实文档内容
  useEffect(() => {
    const loadDocumentContent = async () => {
      if (!document || !visible) return

      setLoading(true)
      try {
        // 先尝试从API加载，最多获取10个块
        const params = new URLSearchParams({
          limit: '10',
          user_id: String(user?.id || 'guest'),
          role: user?.role || 'user',
        })
        let response = await fetch(`/api/knowledge/document-content/${document.id}?${params.toString()}`)

        // 如果API失败，尝试从本地JSON文件加载（包含真实的煤矿安全规程内容）
        if (!response.ok) {
          console.log('API failed, loading from local JSON file')
          response = await fetch('/document_preview.json')
        }

        if (response.ok) {
          const data = await response.json()
          setDocContent({
            title: data.name,
            type: data.type,
            pages: data.total_chunks,
            uploadTime: data.uploadTime?.replace('T', ' ').split('.')[0] || document.uploadTime,
            size: document.size,
            chunks: data.chunks.map((chunk, idx) => ({
              id: chunk.id,
              page: chunk.page || idx + 1,
              content: chunk.content,
              metadata: chunk.metadata || {}
            })),
            totalChunks: data.total_chunks
          })
        } else {
          // 如果API失败，使用mock数据作为后备
          setDocContent(mockDocumentContent[document.id] || {
            title: document.name,
            type: document.name.split('.').pop(),
            pages: 10,
            uploadTime: document.uploadTime,
            size: document.size,
            chunks: [
              {
                id: 1,
                page: 1,
                content: '无法加载文档内容。请确保文档已正确索引到向量数据库。'
              }
            ],
            summary: '文档内容加载失败。'
          })
        }
      } catch (error) {
        console.error('加载文档内容失败:', error)
        setDocContent({
          title: document.name,
          type: document.name.split('.').pop(),
          pages: 0,
          uploadTime: document.uploadTime,
          size: document.size,
          chunks: [
            {
              id: 1,
              page: 1,
              content: '加载文档内容时发生错误。'
            }
          ],
          summary: '加载失败'
        })
      } finally {
        setLoading(false)
      }
    }

    loadDocumentContent()
  }, [document, visible, user?.id, user?.role])

  if (!document) return null
  if (!docContent) return null

  const tabItems = [
    {
      key: 'content',
      label: '文档内容',
      children: (
        <div className="preview-content">
          {loading ? (
            <div className="preview-loading">
              <Spin size="large" />
              <p>正在加载文档内容...</p>
            </div>
          ) : (
            <List
              dataSource={docContent.chunks}
              renderItem={(chunk) => (
                <div className="chunk-item" key={chunk.id}>
                  <div className="chunk-header">
                    <Tag color="blue">第 {chunk.page} 页</Tag>
                    {getApplicabilityTag(chunk.metadata)}
                    <Text type="secondary">分块 #{chunk.id}</Text>
                  </div>
                  <div className="chunk-content">
                    <Paragraph>
                      {chunk.content.split('\n').map((line, i) => (
                        <span key={i}>
                          {line}
                          <br />
                        </span>
                      ))}
                    </Paragraph>
                  </div>
                </div>
              )}
            />
          )}
        </div>
      )
    },
    {
      key: 'info',
      label: '文档信息',
      children: (
        <div className="preview-info">
          <div className="info-header">
            {getFileIcon(document.name)}
            <div className="info-title">
              <Title level={4}>{docContent.title}</Title>
              <Space>
                <Tag>{docContent.type.toUpperCase()}</Tag>
                <Tag color="green">{docContent.pages} 页</Tag>
              </Space>
            </div>
          </div>

          <Divider />

          <div className="info-details">
            <div className="info-item">
              <Text type="secondary">文件大小</Text>
              <Text strong>{docContent.size}</Text>
            </div>
            <div className="info-item">
              <Text type="secondary">上传时间</Text>
              <Text strong>
                <ClockCircleOutlined /> {docContent.uploadTime}
              </Text>
            </div>
            <div className="info-item">
              <Text type="secondary">分块数量</Text>
              <Text strong>
                <DatabaseOutlined /> {docContent.totalChunks || docContent.chunks.length} 个
              </Text>
            </div>
          </div>

          <Divider />
        </div>
      )
    }
  ]

  return (
    <Modal
      title={
        <Space>
          {getFileIcon(document.name)}
          <span>{document.name}</span>
        </Space>
      }
      open={visible}
      onCancel={onClose}
      footer={null}
      width={800}
      className="document-preview-modal"
    >
      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={tabItems}
      />
    </Modal>
  )
})

DocumentPreview.displayName = 'DocumentPreview'

export default DocumentPreview
