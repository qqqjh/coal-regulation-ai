import { useState, useEffect } from 'react'
import {
  Card,
  Row,
  Col,
  Form,
  Select,
  Slider,
  InputNumber,
  Button,
  Input,
  List,
  Tag,
  Divider,
  Space,
  message,
  Empty
} from 'antd'
import {
  SearchOutlined,
  SaveOutlined,
  ThunderboltOutlined,
  FileTextOutlined,
  LoadingOutlined
} from '@ant-design/icons'
import ReactMarkdown from 'react-markdown'
import useUserStore from '../../store/userStore'
import './index.css'

const { TextArea } = Input
const { Option } = Select

const INITIAL_RAG_CONFIG = {
  knowledgeBase: null,
  retrievalMethod: 'hybrid',
  topK: 5,
  similarityThreshold: 0.7,
  model: 'qwen-plus',
  temperature: 0.7,
  maxTokens: 2000,
  contextWindow: 4000
}

const QWEN_MODELS = new Set(['qwen-plus', 'qwen-turbo', 'qwen-max', 'qwen-long'])

function normalizeRagConfig(config = {}) {
  return {
    ...INITIAL_RAG_CONFIG,
    ...config,
    model: QWEN_MODELS.has(config.model) ? config.model : INITIAL_RAG_CONFIG.model,
  }
}

const RAG = () => {
  const user = useUserStore((state) => state.user)
  const [form] = Form.useForm()
  const [query, setQuery] = useState('')
  const [retrievalResults, setRetrievalResults] = useState([])
  const [generatedAnswer, setGeneratedAnswer] = useState('')
  const [isSearching, setIsSearching] = useState(false)
  const [isGenerating, setIsGenerating] = useState(false)
  const [knowledgeBases, setKnowledgeBases] = useState([])
  const knowledgeAuthQuery = () => new URLSearchParams({
    user_id: String(user?.id || 'guest'),
    role: user?.role || 'user',
  }).toString()

  // 从localStorage加载保存的配置
  useEffect(() => {
    const savedConfig = localStorage.getItem('ragConfig')
    if (savedConfig) {
      try {
        const config = normalizeRagConfig(JSON.parse(savedConfig))
        localStorage.setItem('ragConfig', JSON.stringify(config))
        form.setFieldsValue(config)
      } catch (error) {
        console.error('加载配置失败:', error)
        form.setFieldsValue(INITIAL_RAG_CONFIG)
      }
    } else {
      form.setFieldsValue(INITIAL_RAG_CONFIG)
    }
  }, [form])

  // 加载知识库列表
  useEffect(() => {
    const fetchKnowledgeBases = async () => {
      try {
        const response = await fetch(`/api/knowledge/bases?${knowledgeAuthQuery()}`)
        if (response.ok) {
          const data = await response.json()
          setKnowledgeBases(data)

          // 检查是否有保存的配置
          const savedConfig = localStorage.getItem('ragConfig')
          if (savedConfig) {
            const config = JSON.parse(savedConfig)
            if (config.knowledgeBase && data.some(kb => kb.id === config.knowledgeBase)) {
              form.setFieldsValue({ knowledgeBase: config.knowledgeBase })
            } else if (data.length > 0) {
              form.setFieldsValue({ knowledgeBase: data[0].id })
            } else {
              form.setFieldsValue({ knowledgeBase: null })
            }
          } else if (data.length > 0) {
            form.setFieldsValue({ knowledgeBase: data[0].id })
          } else {
            form.setFieldsValue({ knowledgeBase: null })
          }
        }
      } catch (error) {
        console.error('加载知识库列表失败:', error)
      }
    }
    fetchKnowledgeBases()
  }, [form, user?.id, user?.role])

  // 处理检索
  const handleRetrieval = async () => {
    if (!query.trim()) {
      message.warning('请输入查询问题')
      return
    }

    const config = form.getFieldsValue()
    if (!config.knowledgeBase) {
      message.warning('请选择知识库')
      return
    }

    setIsSearching(true)
    setRetrievalResults([])
    setGeneratedAnswer('')

    try {
      const response = await fetch('/api/rag/retrieve', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          query: query,
          kb_id: config.knowledgeBase,
          top_k: config.topK,
          similarity_threshold: config.similarityThreshold,
          user_id: String(user?.id || 'guest'),
          role: user?.role || 'user',
        })
      })

      if (response.ok) {
        const data = await response.json()
        setRetrievalResults(data.results)
        message.success(`检索到 ${data.results.length} 条相关文档`)
      } else {
        message.error('检索失败')
      }
    } catch (error) {
      console.error('检索失败:', error)
      message.error('检索失败: ' + error.message)
    } finally {
      setIsSearching(false)
    }
  }

  // 处理生成
  const handleGenerate = async () => {
    if (retrievalResults.length === 0) {
      message.warning('请先进行检索')
      return
    }

    const config = form.getFieldsValue()
    setIsGenerating(true)
    setGeneratedAnswer('')

    try {
      const response = await fetch('/api/rag/generate', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          query: query,
          kb_id: config.knowledgeBase,
          model: config.model,
          temperature: config.temperature,
          top_k: config.topK,
          user_id: String(user?.id || 'guest'),
          role: user?.role || 'user',
        })
      })

      if (response.ok) {
        const data = await response.json()
        setGeneratedAnswer(data.answer)
        message.success('答案生成成功')
      } else {
        message.error('生成失败')
      }
    } catch (error) {
      console.error('生成失败:', error)
      message.error('生成失败: ' + error.message)
    } finally {
      setIsGenerating(false)
    }
  }

  // 保存配置
  const handleSaveConfig = () => {
    const config = form.getFieldsValue()
    try {
      localStorage.setItem('ragConfig', JSON.stringify(config))
      message.success('配置已保存，切换页面后仍会保留')
    } catch (error) {
      console.error('保存配置失败:', error)
      message.error('配置保存失败')
    }
  }

  return (
    <div className="rag-container">
      <Row gutter={16}>
        {/* 左侧配置区 */}
        <Col span={8}>
          <Card title="RAG 配置" className="config-card">
            <Form form={form} layout="vertical">
              <Divider orientation="left">检索配置</Divider>

              <Form.Item name="knowledgeBase" label="知识库选择">
                <Select placeholder="请选择知识库">
                  {knowledgeBases.map(kb => (
                    <Option key={kb.id} value={kb.id}>{kb.name}</Option>
                  ))}
                </Select>
              </Form.Item>

              <Form.Item name="retrievalMethod" label="检索方式">
                <Select>
                  <Option value="vector">向量检索</Option>
                  <Option value="keyword">关键词检索</Option>
                  <Option value="hybrid">混合检索</Option>
                </Select>
              </Form.Item>

              <Form.Item name="topK" label="返回结果数 (Top-K)">
                <Slider
                  min={1}
                  max={10}
                  marks={{ 1: '1', 5: '5', 10: '10' }}
                  tooltip={{ formatter: (value) => `${value} 条` }}
                />
              </Form.Item>

              <Form.Item name="similarityThreshold" label="相似度阈值">
                <Slider
                  min={0}
                  max={1}
                  step={0.05}
                  marks={{ 0: '0', 0.5: '0.5', 1: '1' }}
                  tooltip={{ formatter: (value) => value.toFixed(2) }}
                />
              </Form.Item>

              <Divider orientation="left">生成配置</Divider>

              <Form.Item name="model" label="模型选择">
                <Select>
                  <Option value="qwen-plus">Qwen Plus</Option>
                  <Option value="qwen-turbo">Qwen Turbo</Option>
                  <Option value="qwen-max">Qwen Max</Option>
                  <Option value="qwen-long">Qwen Long</Option>
                </Select>
              </Form.Item>

              <Form.Item name="temperature" label="温度参数">
                <Slider
                  min={0}
                  max={1}
                  step={0.1}
                  marks={{ 0: '精确', 0.5: '平衡', 1: '创造' }}
                  tooltip={{ formatter: (value) => value.toFixed(1) }}
                />
              </Form.Item>

              <Form.Item name="maxTokens" label="最大生成长度">
                <InputNumber
                  min={100}
                  max={4000}
                  step={100}
                  style={{ width: '100%' }}
                  formatter={(value) => `${value} tokens`}
                />
              </Form.Item>

              <Form.Item name="contextWindow" label="上下文窗口">
                <InputNumber
                  min={1000}
                  max={8000}
                  step={1000}
                  style={{ width: '100%' }}
                  formatter={(value) => `${value} tokens`}
                />
              </Form.Item>

              <Form.Item>
                <Button
                  type="primary"
                  icon={<SaveOutlined />}
                  block
                  onClick={handleSaveConfig}
                >
                  保存配置
                </Button>
              </Form.Item>
            </Form>
          </Card>
        </Col>

        {/* 右侧测试区 */}
        <Col span={16}>
          <Card title="RAG 测试" className="test-card">
            {/* 查询输入 */}
            <div className="query-section">
              <TextArea
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="请输入您的问题，例如：煤矿瓦斯检测有哪些要求？"
                autoSize={{ minRows: 3, maxRows: 5 }}
                style={{ marginBottom: 16 }}
              />
              <Space>
                <Button
                  type="primary"
                  icon={<SearchOutlined />}
                  onClick={handleRetrieval}
                  loading={isSearching}
                >
                  开始检索
                </Button>
                <Button
                  icon={<ThunderboltOutlined />}
                  onClick={handleGenerate}
                  loading={isGenerating}
                  disabled={retrievalResults.length === 0}
                >
                  生成答案
                </Button>
              </Space>
            </div>

            <Divider />

            {/* 检索结果 */}
            <div className="retrieval-section">
              <h3>检索结果 ({retrievalResults.length})</h3>
              {isSearching ? (
                <div style={{ textAlign: 'center', padding: 40 }}>
                  <LoadingOutlined style={{ fontSize: 48, color: '#1890ff' }} />
                  <p style={{ marginTop: 16 }}>正在检索中...</p>
                </div>
              ) : retrievalResults.length > 0 ? (
                <List
                  dataSource={retrievalResults}
                  renderItem={(item) => (
                    <List.Item key={item.id} className="retrieval-item">
                      <div className="retrieval-content">
                        <Space className="retrieval-header">
                          <FileTextOutlined />
                          <span className="doc-name">{item.document}</span>
                          <Tag color="blue">第 {item.page} 页</Tag>
                          <Tag color="green">
                            相似度: {(item.similarity * 100).toFixed(1)}%
                          </Tag>
                        </Space>
                        <div className="retrieval-text">{item.content}</div>
                      </div>
                    </List.Item>
                  )}
                />
              ) : (
                <Empty description="暂无检索结果" />
              )}
            </div>

            <Divider />

            {/* 生成答案 */}
            <div className="generation-section">
              <h3>生成答案</h3>
              {isGenerating ? (
                <div style={{ textAlign: 'center', padding: 40 }}>
                  <LoadingOutlined style={{ fontSize: 48, color: '#1890ff' }} />
                  <p style={{ marginTop: 16 }}>正在生成答案...</p>
                </div>
              ) : generatedAnswer ? (
                <Card className="answer-card">
                  <ReactMarkdown>{generatedAnswer}</ReactMarkdown>
                </Card>
              ) : (
                <Empty description="暂无生成结果" />
              )}
            </div>
          </Card>
        </Col>
      </Row>
    </div>
  )
}

export default RAG
