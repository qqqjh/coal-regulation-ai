import React, { useState, useRef, useEffect, useCallback } from 'react'
import {
  Upload, Button, message, Tag, Space, Progress, Input, Tooltip,
  Empty, Segmented, Badge, Spin, Drawer, Table, Popconfirm
} from 'antd'
import {
  UploadOutlined, CheckOutlined, CloseOutlined, EditOutlined,
  DownloadOutlined, RobotOutlined, FileWordOutlined, AimOutlined,
  ReloadOutlined, DatabaseOutlined, DeleteOutlined
} from '@ant-design/icons'
import './index.css'

const { TextArea } = Input

// 问题类型 → 颜色/标签
const TYPE_META = {
  compliance: { color: 'volcano', label: '合规' },
  numeric: { color: 'geekblue', label: '数值' },
  typo: { color: 'orange', label: '错别字' },
  redundancy: { color: 'purple', label: '重复' },
  escalation: { color: 'magenta', label: '待裁决' }
}
const STATUS_COLOR = { 不合规: '#cf1322', 不确定: '#d48806', 错别字: '#d46b08', 重复: '#531dab' }

const Review = () => {
  const [jobId, setJobId] = useState(null)
  const [docName, setDocName] = useState('')
  const [mineType, setMineType] = useState('non_outburst')
  const [blocks, setBlocks] = useState([])
  const [issues, setIssues] = useState([])
  const [job, setJob] = useState({ status: 'idle', progress: 0, agent_status: '' })
  const [selectedId, setSelectedId] = useState(null)
  const [customFor, setCustomFor] = useState(null)   // 正在写自定义意见的 issue id
  const [customText, setCustomText] = useState('')
  const [uploading, setUploading] = useState(false)
  const [fwOpen, setFwOpen] = useState(false)
  const [fwData, setFwData] = useState({ counts: {}, items: [] })
  const [fwLoading, setFwLoading] = useState(false)

  const esRef = useRef(null)
  const docRef = useRef(null)
  const blockRefs = useRef({})

  // ---------- SSE ----------
  const closeStream = () => {
    if (esRef.current) { esRef.current.close(); esRef.current = null }
  }

  const startStream = useCallback((id) => {
    closeStream()
    const es = new EventSource(`/api/v9/stream/${id}`)
    esRef.current = es
    es.onmessage = (e) => {
      const msg = JSON.parse(e.data)
      if (msg.type === 'status') {
        setJob(j => ({ ...j, ...msg.job }))
        if (msg.job.paragraphs_ready) fetchDocument(id)
      } else if (msg.type === 'issue') {
        setIssues(prev => prev.some(x => x.id === msg.issue.id) ? prev : [...prev, msg.issue])
      } else if (msg.type === 'end' || msg.type === 'timeout' || msg.type === 'error') {
        closeStream()
        fetchDocument(id)
      }
    }
    es.onerror = () => { /* 浏览器会自动重连；done 时已主动 close */ }
  }, [])

  useEffect(() => () => closeStream(), [])

  // ---------- 文档段落 ----------
  const fetchDocument = async (id) => {
    try {
      const r = await fetch(`/api/v9/document/${id}`)
      if (r.ok) {
        const data = await r.json()
        if (data.blocks?.length) setBlocks(data.blocks)
      }
    } catch (err) { console.error('加载文档失败', err) }
  }

  // ---------- 上传 ----------
  const handleUpload = async (file) => {
    setUploading(true)
    setBlocks([]); setIssues([]); setSelectedId(null)
    setJob({ status: 'pending', progress: 0, agent_status: '已上传，等待 worker 取走...' })
    const form = new FormData()
    form.append('file', file)
    try {
      const r = await fetch(`/api/v9/upload?mine_type=${mineType}`, { method: 'POST', body: form })
      if (!r.ok) { message.error('上传失败'); setUploading(false); return false }
      const data = await r.json()
      setJobId(data.job_id)
      setDocName(data.doc_name)
      message.success('上传成功，审查已排队')
      startStream(data.job_id)
      // 等段落就绪
      const t = setInterval(async () => {
        const jr = await fetch(`/api/v9/status/${data.job_id}`)
        if (jr.ok) {
          const j = await jr.json()
          if (j.paragraphs_path || j.status !== 'pending') { fetchDocument(data.job_id); clearInterval(t) }
        }
      }, 1500)
    } catch (err) {
      message.error('上传出错: ' + err.message)
    } finally {
      setUploading(false)
    }
    return false
  }

  // ---------- 反馈 ----------
  const pollFeedback = (issueId) => {
    const t = setInterval(async () => {
      const r = await fetch(`/api/v9/feedback-status/${issueId}`)
      if (r.ok) {
        const s = await r.json()
        if (s.agent_applied === 1) { // 已改写
          clearInterval(t)
          message.success('主智能体已改写文档：' + (s.agent_note || ''))
          fetchDocument(jobId)
          setIssues(prev => prev.map(x => x.id === issueId
            ? { ...x, _resolved: 'applied', agent_note: s.agent_note } : x))
        } else if (s.agent_applied === 2 || s.agent_applied === -1) {
          clearInterval(t)
          setIssues(prev => prev.map(x => x.id === issueId
            ? { ...x, _resolved: s.agent_applied === 2 ? 'noop' : 'failed', agent_note: s.agent_note } : x))
        }
      }
    }, 1500)
  }

  const sendFeedback = async (issue, action, text = '') => {
    try {
      const r = await fetch('/api/v9/feedback', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ issue_id: issue.id, action, text })
      })
      if (!r.ok) { message.error('反馈失败'); return }
      if (action === 'reject') {
        message.success('已驳回，记入数据飞轮（人工层）')
        setIssues(prev => prev.map(x => x.id === issue.id ? { ...x, _resolved: 'rejected' } : x))
      } else {
        message.loading({ content: '已提交，主智能体改写中...', key: `fb${issue.id}` })
        setIssues(prev => prev.map(x => x.id === issue.id ? { ...x, _resolved: 'processing' } : x))
        pollFeedback(issue.id)
      }
      setCustomFor(null); setCustomText('')
    } catch (err) { message.error('反馈出错: ' + err.message) }
  }

  // ---------- 数据飞轮 ----------
  const openFlywheel = async () => {
    setFwOpen(true)
    setFwLoading(true)
    try {
      const r = await fetch('/api/v9/flywheel?limit=300')
      if (r.ok) setFwData(await r.json())
    } catch (err) { message.error('加载飞轮失败: ' + err.message) }
    finally { setFwLoading(false) }
  }
  const deleteFlywheel = async (id) => {
    try {
      const r = await fetch(`/api/v9/flywheel/${id}`, { method: 'DELETE' })
      if (r.ok) {
        message.success('已删除')
        setFwData(d => ({ ...d, items: d.items.filter(x => x.id !== id) }))
      } else message.error('删除失败')
    } catch (err) { message.error('删除出错: ' + err.message) }
  }

  // ---------- 定位 ----------
  const locateIssue = (issue) => {
    setSelectedId(issue.id)
    const bi = (issue.block_indices || [])[0]
    if (bi != null && blockRefs.current[bi]) {
      blockRefs.current[bi].scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
  }

  // 当前选中问题对应的高亮 block 集合 + 原文片段
  const selected = issues.find(x => x.id === selectedId)
  const hlBlocks = new Set(selected?.block_indices || [])
  const hlText = selected?.original_text || ''

  const renderParagraph = (b) => {
    const isHl = hlBlocks.has(b.block_index)
    let content = b.text || ' '
    if (isHl && hlText && b.text?.includes(hlText)) {
      const i = b.text.indexOf(hlText)
      content = (<>
        {b.text.slice(0, i)}
        <mark className="hl-text">{hlText}</mark>
        {b.text.slice(i + hlText.length)}
      </>)
    }
    return (
      <p
        key={b.block_index}
        ref={el => (blockRefs.current[b.block_index] = el)}
        className={`doc-para ${isHl ? 'doc-para-hl' : ''} ${b.is_heading ? 'doc-heading' : ''}`}
      >
        {content}
      </p>
    )
  }

  const renderTable = (b) => (
    <table key={b.block_index} ref={el => (blockRefs.current[b.block_index] = el)}
      className={`doc-table ${hlBlocks.has(b.block_index) ? 'doc-para-hl' : ''}`}>
      <tbody>
        {(b.rows || []).map((row, ri) => (
          <tr key={ri}>{row.map((c, ci) => <td key={ci}>{c}</td>)}</tr>
        ))}
      </tbody>
    </table>
  )

  const activeIssues = issues.filter(x => !x._resolved)
  const isRunning = ['pending', 'parsing', 'reviewing'].includes(job.status)

  return (
    <div className="v9-review">
      {/* ===== 顶部：上传 + 主智能体状态 ===== */}
      <div className="v9-topbar">
        <div className="v9-topbar-left">
          <Upload accept=".docx,.doc" beforeUpload={handleUpload} showUploadList={false}
            disabled={uploading || isRunning}>
            <Button type="primary" icon={<UploadOutlined />} loading={uploading} disabled={isRunning}>
              上传待审 Word
            </Button>
          </Upload>
          <Segmented
            value={mineType}
            onChange={setMineType}
            disabled={isRunning}
            options={[
              { label: '非突出矿井', value: 'non_outburst' },
              { label: '突出矿井', value: 'outburst' }
            ]}
          />
          {docName && <span className="v9-docname"><FileWordOutlined /> {docName}</span>}
        </div>
        <div className="v9-topbar-right">
          <Button icon={<DatabaseOutlined />} onClick={openFlywheel}>
            数据飞轮
          </Button>
          <Button icon={<DownloadOutlined />} disabled={!jobId}
            onClick={() => window.open(`/api/v9/download/${jobId}`, '_blank')}>
            下载改写后文档
          </Button>
        </div>
      </div>

      {/* 主智能体状态条 */}
      <div className="v9-agentbar">
        <div className="v9-agent-head">
          <RobotOutlined className="v9-agent-icon" />
          <span className="v9-agent-label">主智能体</span>
          {isRunning && <Spin size="small" />}
          <span className={`v9-agent-state-tag ${isRunning ? 'running' : job.status === 'failed' ? 'failed' : ''}`}>
            {job.status === 'idle' ? '待命' : isRunning ? '工作中' : job.status === 'done' ? '已完成' : job.status === 'failed' ? '失败' : job.status}
          </span>
          {job.n_chunks > 0 && (
            <span className="v9-agent-count">已审 {job.n_done}/{job.n_chunks} 块</span>
          )}
          <span className="v9-agent-issues">发现 {issues.length} 处问题</span>
          <span className="v9-agent-progress">{job.progress || 0}%</span>
        </div>
        <div className="v9-agent-statusline">
          {job.agent_status || (job.status === 'idle' ? '等待上传文档…审查将自动开始，问题会实时出现在左侧。' : job.status)}
        </div>
        <Progress percent={job.progress || 0} size="small" showInfo={false}
          status={job.status === 'failed' ? 'exception' : isRunning ? 'active' : 'normal'} />
      </div>

      {/* 数据飞轮抽屉 */}
      <Drawer title={<span><DatabaseOutlined /> 数据飞轮（人工/主智能体裁决沉淀）</span>}
        open={fwOpen} onClose={() => setFwOpen(false)} width={920}>
        <Space style={{ marginBottom: 12 }} wrap>
          <Tag color="red">人工 {fwData.counts?.human || 0}</Tag>
          <Tag color="blue">主智能体 {fwData.counts?.agent || 0}</Tag>
          <Button size="small" icon={<ReloadOutlined />} onClick={openFlywheel}>刷新</Button>
          <span style={{ color: '#999', fontSize: 12 }}>人工裁决为最高层，用于扩充评估集 / 后续动态判例参考</span>
        </Space>
        <Table
          rowKey="id" size="small" loading={fwLoading} dataSource={fwData.items}
          pagination={{ pageSize: 12 }}
          columns={[
            { title: '来源', dataIndex: 'source', width: 80,
              render: s => <Tag color={s === 'human' ? 'red' : 'blue'}>{s === 'human' ? '人工' : '主智能体'}</Tag> },
            { title: '裁决', dataIndex: 'final_verdict', width: 110,
              render: (v, r) => <span><span style={{ color: '#999' }}>{r.model_verdict || '?'}→</span><b>{v}</b></span> },
            { title: '文档/块', width: 140, render: (_, r) => <span style={{ fontSize: 12 }}>{(r.doc_name || '').slice(0, 12)}<br />ck{r.chunk_index}</span> },
            { title: '原文', dataIndex: 'pending_content', ellipsis: true,
              render: t => <Tooltip title={t}><span style={{ fontSize: 12 }}>{(t || '').slice(0, 40)}</span></Tooltip> },
            { title: '理由', dataIndex: 'reason', ellipsis: true,
              render: t => <Tooltip title={t}><span style={{ fontSize: 12, color: '#666' }}>{(t || '').slice(0, 40)}</span></Tooltip> },
            { title: '操作', width: 70, render: (_, r) => (
              <Popconfirm title="删除这条飞轮记录？" onConfirm={() => deleteFlywheel(r.id)} okText="删除" cancelText="保留">
                <Button size="small" danger type="text" icon={<DeleteOutlined />} />
              </Popconfirm>
            ) }
          ]}
        />
      </Drawer>

      {/* ===== 主体：左问题 + 中文档 ===== */}
      <div className="v9-body">
        {/* 左侧：实时问题 */}
        <div className="v9-issues">
          <div className="v9-issues-head">
            <span>发现问题</span>
            <Badge count={activeIssues.length} showZero color="#cf1322" />
            <Tooltip title="点击问题可在右侧文档定位">
              <AimOutlined style={{ color: '#999', marginLeft: 'auto' }} />
            </Tooltip>
          </div>
          <div className="v9-issues-list">
            {issues.length === 0 && (
              <Empty description={isRunning ? '审查中，问题将实时出现…' : '暂无问题'}
                image={Empty.PRESENTED_IMAGE_SIMPLE} style={{ marginTop: 40 }} />
            )}
            {issues.map(issue => {
              const meta = TYPE_META[issue.issue_type] || { color: 'default', label: issue.issue_type }
              return (
                <div key={issue.id}
                  className={`v9-issue ${selectedId === issue.id ? 'v9-issue-sel' : ''} ${issue._resolved ? 'v9-issue-done' : ''}`}
                  onClick={() => locateIssue(issue)}>
                  <div className="v9-issue-top">
                    <Tag color={meta.color}>{meta.label}</Tag>
                    {issue.status && (
                      <span className="v9-issue-status" style={{ color: STATUS_COLOR[issue.status] || '#555' }}>
                        {issue.status}
                      </span>
                    )}
                    {issue.escalation_type && <Tag>{issue.escalation_type}</Tag>}
                    {issue._resolved && (
                      <Tag color={issue._resolved === 'applied' ? 'green'
                        : issue._resolved === 'rejected' ? 'default'
                          : issue._resolved === 'processing' ? 'processing' : 'red'}>
                        {{ applied: '已改写', rejected: '已驳回', processing: '改写中',
                          noop: '未改', failed: '改写失败' }[issue._resolved]}
                      </Tag>
                    )}
                  </div>

                  {issue.original_text && (
                    <div className="v9-issue-orig" title={issue.original_text}>
                      原文：{issue.original_text}
                    </div>
                  )}
                  {issue.suggestion && (
                    <div className="v9-issue-sug">建议：{issue.suggestion}</div>
                  )}
                  {issue.reason && <div className="v9-issue-reason">{issue.reason}</div>}
                  {issue.regulation && <div className="v9-issue-reg">依据：{issue.regulation}</div>}
                  {(issue.detail?.numeric?.length > 0) && (
                    <div className="v9-issue-numeric">
                      {issue.detail.numeric.map((n, i) => (
                        <div key={i}>🧮 [{n.verdict}] {n.explanation}</div>
                      ))}
                    </div>
                  )}

                  {!issue._resolved && (
                    <div className="v9-issue-actions" onClick={e => e.stopPropagation()}>
                      <Button size="small" type="primary" icon={<CheckOutlined />}
                        onClick={() => sendFeedback(issue, 'accept')}>接受</Button>
                      <Button size="small" icon={<CloseOutlined />}
                        onClick={() => sendFeedback(issue, 'reject')}>驳回</Button>
                      <Button size="small" icon={<EditOutlined />}
                        onClick={() => { setCustomFor(issue.id); setCustomText('') }}>改写</Button>
                    </div>
                  )}
                  {customFor === issue.id && (
                    <div className="v9-issue-custom" onClick={e => e.stopPropagation()}>
                      <TextArea rows={2} value={customText} placeholder="写下你的修改意见，主智能体将据此改写原文…"
                        onChange={e => setCustomText(e.target.value)} />
                      <Space style={{ marginTop: 6 }}>
                        <Button size="small" type="primary" disabled={!customText.trim()}
                          onClick={() => sendFeedback(issue, 'custom', customText)}>提交给主智能体</Button>
                        <Button size="small" onClick={() => setCustomFor(null)}>取消</Button>
                      </Space>
                    </div>
                  )}
                  {issue.agent_note && issue._resolved && (
                    <div className="v9-issue-agentnote">🤖 {issue.agent_note}</div>
                  )}
                </div>
              )
            })}
          </div>
        </div>

        {/* 中间：Word 文档 */}
        <div className="v9-doc" ref={docRef}>
          <div className="v9-doc-head">
            <FileWordOutlined /> {docName || '文档预览'}
            {jobId && <Button size="small" type="text" icon={<ReloadOutlined />}
              onClick={() => fetchDocument(jobId)}>刷新</Button>}
          </div>
          <div className="v9-doc-paper">
            {blocks.length === 0 ? (
              <Empty description={isRunning ? '正在解析文档…' : '上传 Word 文档后在此显示'}
                style={{ marginTop: 80 }} />
            ) : (
              blocks.map(b => b.kind === 'table' ? renderTable(b) : renderParagraph(b))
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

export default Review
