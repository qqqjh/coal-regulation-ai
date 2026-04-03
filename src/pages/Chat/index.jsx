import { useState, useRef, useEffect } from 'react'
import { Input, Button, List, Card, Space, message, Select } from 'antd'
import { SendOutlined, PlusOutlined, DeleteOutlined, EditOutlined, CheckOutlined, CloseOutlined } from '@ant-design/icons'
import ReactMarkdown from 'react-markdown'
import useUserStore from '../../store/userStore'
import './index.css'

const { TextArea } = Input
const { Option } = Select

const Chat = () => {
  const user = useUserStore((state) => state.user)

  // 从 localStorage 加载会话数据
  const loadSessions = () => {
    try {
      const saved = localStorage.getItem('chat_sessions')
      if (saved) {
        const parsed = JSON.parse(saved)
        return parsed.length > 0 ? parsed : [{ id: 1, name: '新对话', createdAt: new Date().toISOString(), messages: [] }]
      }
    } catch (e) {
      console.error('加载会话失败:', e)
    }
    return [{ id: 1, name: '新对话', createdAt: new Date().toISOString(), messages: [] }]
  }

  const loadCurrentSessionId = () => {
    try {
      const saved = localStorage.getItem('current_session_id')
      return saved ? parseInt(saved) : 1
    } catch (e) {
      return 1
    }
  }

  const [sessions, setSessions] = useState(loadSessions)
  const [currentSessionId, setCurrentSessionId] = useState(loadCurrentSessionId)
  const [inputValue, setInputValue] = useState('')
  const [isGenerating, setIsGenerating] = useState(false)
  const [streamingContent, setStreamingContent] = useState('')
  const [editingSessionId, setEditingSessionId] = useState(null)
  const [editingName, setEditingName] = useState('')
  const [knowledgeBases, setKnowledgeBases] = useState([])
  const [selectedKbId, setSelectedKbId] = useState(null)
  const [selectedModel, setSelectedModel] = useState('gpt-3.5-turbo')
  const messagesEndRef = useRef(null)
  const streamingRef = useRef(null)

  // 可用的模型列表
  const availableModels = [
    { value: 'gpt-3.5-turbo', label: 'GPT-3.5 Turbo (快速)' },
    { value: 'gpt-4', label: 'GPT-4 (强大)' },
    { value: 'gpt-4-turbo', label: 'GPT-4 Turbo (平衡)' },
    { value: 'gpt-4o', label: 'GPT-4o (最新)' },
    { value: 'gpt-4o-mini', label: 'GPT-4o Mini (经济)' },
  ]

  const currentSession = sessions.find(s => s.id === currentSessionId)

  // 加载知识库列表
  useEffect(() => {
    const fetchKnowledgeBases = async () => {
      try {
        const response = await fetch('/api/knowledge/bases')
        if (response.ok) {
          const data = await response.json()
          setKnowledgeBases(data)
          // 如果有知识库且没有选中，默认选中第一个
          if (data.length > 0 && !selectedKbId) {
            setSelectedKbId(data[0].id)
          }
        }
      } catch (error) {
        console.error('加载知识库列表失败:', error)
      }
    }
    fetchKnowledgeBases()
  }, [])

  // 保存会话到 localStorage
  useEffect(() => {
    localStorage.setItem('chat_sessions', JSON.stringify(sessions))
  }, [sessions])

  // 保存当前会话ID
  useEffect(() => {
    localStorage.setItem('current_session_id', currentSessionId.toString())
  }, [currentSessionId])

  // 滚动到底部
  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }

  useEffect(() => {
    scrollToBottom()
  }, [currentSession?.messages, streamingContent])

  // 创建新会话
  const handleNewSession = () => {
    const newSession = {
      id: Date.now(),
      name: `对话 ${sessions.length + 1}`,
      createdAt: new Date().toISOString(),
      messages: []
    }
    setSessions([...sessions, newSession])
    setCurrentSessionId(newSession.id)
    message.success('创建新对话成功')
  }

  // 删除会话
  const handleDeleteSession = (sessionId) => {
    if (sessions.length === 1) {
      message.warning('至少保留一个对话')
      return
    }
    const newSessions = sessions.filter(s => s.id !== sessionId)
    setSessions(newSessions)
    if (currentSessionId === sessionId) {
      setCurrentSessionId(newSessions[0].id)
    }
    message.success('删除对话成功')
  }

  // 开始编辑会话名称
  const handleStartEdit = (e, session) => {
    e.stopPropagation()
    setEditingSessionId(session.id)
    setEditingName(session.name)
  }

  // 保存会话名称
  const handleSaveEdit = (e) => {
    e.stopPropagation()
    if (!editingName.trim()) {
      message.warning('对话名称不能为空')
      return
    }
    const newSessions = sessions.map(s =>
      s.id === editingSessionId ? { ...s, name: editingName.trim() } : s
    )
    setSessions(newSessions)
    setEditingSessionId(null)
    setEditingName('')
    message.success('重命名成功')
  }

  // 取消编辑
  const handleCancelEdit = (e) => {
    e.stopPropagation()
    setEditingSessionId(null)
    setEditingName('')
  }

  // 编辑输入框回车保存
  const handleEditKeyDown = (e) => {
    if (e.key === 'Enter') {
      handleSaveEdit(e)
    } else if (e.key === 'Escape') {
      handleCancelEdit(e)
    }
  }

  // 发送消息
  const handleSend = async () => {
    if (!inputValue.trim()) {
      message.warning('请输入内容')
      return
    }

    if (!selectedKbId) {
      message.warning('请先选择知识库')
      return
    }

    const userMessage = {
      id: Date.now(),
      role: 'user',
      content: inputValue,
      timestamp: new Date().toISOString()
    }

    // 更新当前会话的消息
    const updatedSessions = sessions.map(session => {
      if (session.id === currentSessionId) {
        return {
          ...session,
          messages: [...session.messages, userMessage]
        }
      }
      return session
    })
    setSessions(updatedSessions)
    const currentInput = inputValue
    setInputValue('')
    setIsGenerating(true)
    setStreamingContent('')

    try {
      // 调用后端流式 API
      const response = await fetch('/api/chat/completions', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          messages: [
            ...currentSession.messages.map(m => ({ role: m.role, content: m.content })),
            { role: 'user', content: currentInput }
          ],
          useRAG: true,
          kbId: selectedKbId,  // 传递选中的知识库ID
          model: selectedModel  // 传递选中的模型
        })
      })

      if (!response.ok) {
        throw new Error('请求失败')
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let fullResponse = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        const chunk = decoder.decode(value, { stream: true })
        fullResponse += chunk
        setStreamingContent(fullResponse)
      }

      // 流式输出完成，保存 AI 消息
      const aiMessage = {
        id: Date.now() + 1,
        role: 'assistant',
        content: fullResponse,
        timestamp: new Date().toISOString()
      }

      const newSessions = updatedSessions.map(session => {
        if (session.id === currentSessionId) {
          return {
            ...session,
            messages: [...session.messages, aiMessage]
          }
        }
        return session
      })
      setSessions(newSessions)
      setStreamingContent('')
      setIsGenerating(false)
    } catch (error) {
      console.error('发送消息失败:', error)
      message.error('发送失败: ' + error.message)
      setIsGenerating(false)
      setStreamingContent('')
    }
  }

  // 清理流式输出定时器
  useEffect(() => {
    return () => {
      if (streamingRef.current) {
        clearInterval(streamingRef.current)
      }
    }
  }, [])

  // 回车发送
  const handleKeyPress = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  return (
    <div className="chat-container">
      {/* 左侧会话列表 */}
      <div className="session-list">
        <div className="session-list-header">
          <h3>对话列表</h3>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={handleNewSession}
            size="small"
          >
            新建
          </Button>
        </div>
        <div className="session-list-content">
          {sessions.map(session => (
            <div
              key={session.id}
              className={`session-item ${currentSessionId === session.id ? 'active' : ''}`}
              onClick={() => setCurrentSessionId(session.id)}
            >
              <div className="session-item-content">
                {editingSessionId === session.id ? (
                  <Input
                    value={editingName}
                    onChange={(e) => setEditingName(e.target.value)}
                    onKeyDown={handleEditKeyDown}
                    onClick={(e) => e.stopPropagation()}
                    size="small"
                    autoFocus
                    className="session-name-input"
                  />
                ) : (
                  <div className="session-name">{session.name}</div>
                )}
                <div className="session-info">
                  {session.messages.length} 条消息
                </div>
              </div>
              <div className="session-actions">
                {editingSessionId === session.id ? (
                  <>
                    <Button
                      type="text"
                      size="small"
                      icon={<CheckOutlined />}
                      onClick={handleSaveEdit}
                      style={{ color: '#52c41a' }}
                    />
                    <Button
                      type="text"
                      size="small"
                      icon={<CloseOutlined />}
                      onClick={handleCancelEdit}
                    />
                  </>
                ) : (
                  <>
                    <Button
                      type="text"
                      size="small"
                      icon={<EditOutlined />}
                      onClick={(e) => handleStartEdit(e, session)}
                    />
                    <Button
                      type="text"
                      danger
                      size="small"
                      icon={<DeleteOutlined />}
                      onClick={(e) => {
                        e.stopPropagation()
                        handleDeleteSession(session.id)
                      }}
                    />
                  </>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* 右侧对话区域 */}
      <div className="chat-main">
        <Card
          title={
            <Space>
              <span>{currentSession?.name}</span>
              <Select
                style={{ width: 200 }}
                value={selectedKbId}
                onChange={setSelectedKbId}
                placeholder="选择知识库"
                disabled={isGenerating}
              >
                {knowledgeBases.map(kb => (
                  <Option key={kb.id} value={kb.id}>
                    {kb.name} ({kb.document_count}个文档)
                  </Option>
                ))}
              </Select>
              <Select
                style={{ width: 200 }}
                value={selectedModel}
                onChange={setSelectedModel}
                placeholder="选择模型"
                disabled={isGenerating}
              >
                {availableModels.map(model => (
                  <Option key={model.value} value={model.value}>
                    {model.label}
                  </Option>
                ))}
              </Select>
            </Space>
          }
          className="chat-card"
          extra={
            <Space>
              <Button
                size="small"
                onClick={() => {
                  const newSessions = sessions.map(s =>
                    s.id === currentSessionId ? { ...s, messages: [] } : s
                  )
                  setSessions(newSessions)
                  message.success('清空对话成功')
                }}
              >
                清空对话
              </Button>
            </Space>
          }
        >
          <div className="messages-container">
            {currentSession?.messages.length === 0 ? (
              <div className="welcome-container">
                <h1 className="welcome-title">
                  欢迎您，<span className="welcome-username">{user?.name || '用户'}</span>
                </h1>
                <p className="welcome-subtitle">使用煤炭规程智能体</p>
                <p className="welcome-hint">请在下方输入您的问题，开始对话</p>
              </div>
            ) : (
              <List
                dataSource={currentSession?.messages}
                renderItem={(msg) => (
                  <div className={`message-item ${msg.role}`}>
                    <div className="message-avatar">
                      {msg.role === 'user' ? '你' : 'AI'}
                    </div>
                    <div className="message-content">
                      <div className="message-text">
                        {msg.role === 'assistant' ? (
                          <ReactMarkdown>{msg.content}</ReactMarkdown>
                        ) : (
                          msg.content
                        )}
                      </div>
                      <div className="message-time">
                        {new Date(msg.timestamp).toLocaleTimeString()}
                      </div>
                    </div>
                  </div>
                )}
              />
            )}
            {isGenerating && (
              <div className="message-item assistant">
                <div className="message-avatar">AI</div>
                <div className="message-content">
                  <div className="message-text streaming">
                    {streamingContent ? (
                      <>
                        <ReactMarkdown>{streamingContent}</ReactMarkdown>
                        <span className="streaming-cursor">|</span>
                      </>
                    ) : (
                      <span className="typing-indicator">
                        <span></span>
                        <span></span>
                        <span></span>
                      </span>
                    )}
                  </div>
                </div>
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>

          <div className="input-container">
            <TextArea
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              onKeyPress={handleKeyPress}
              placeholder="输入您的问题（Shift+Enter 换行，Enter 发送）"
              autoSize={{ minRows: 2, maxRows: 6 }}
              disabled={isGenerating}
            />
            <Button
              type="primary"
              icon={<SendOutlined />}
              onClick={handleSend}
              loading={isGenerating}
              size="large"
            >
              发送
            </Button>
          </div>
        </Card>
      </div>
    </div>
  )
}

export default Chat
