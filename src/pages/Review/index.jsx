import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import useUserStore from '../../store/userStore'
import {
  Button, Spin, Empty, Radio, Input, message, Progress, Tag, Tooltip,
} from 'antd'
import {
  DeleteOutlined, FileTextOutlined, CloudUploadOutlined,
  CheckCircleFilled, CloseCircleFilled, LoadingOutlined,
  CheckOutlined, CloseOutlined, EditOutlined, DownloadOutlined,
} from '@ant-design/icons'
import axios from 'axios'
import { Document as PdfDocument, Page, pdfjs } from 'react-pdf'
import 'react-pdf/dist/Page/AnnotationLayer.css'
import 'react-pdf/dist/Page/TextLayer.css'
import './index.css'

const { TextArea } = Input
const V9 = '/api/v9'
pdfjs.GlobalWorkerOptions.workerSrc = new URL('pdfjs-dist/build/pdf.worker.min.mjs', import.meta.url).toString()

// ── v9 issue type metadata ────────────────────────────────────────
const TYPE_CFG = {
  compliance:  { label: '合规', color: '#2f54eb', bg: '#e8eeff' },
  typo:        { label: '错别字', color: '#e53935', bg: '#fce4e4' },
  redundancy:  { label: '重复', color: '#ff9800', bg: '#fff3e0' },
  numeric:     { label: '数值', color: '#9c27b0', bg: '#f3e5f5' },
  escalation:  { label: '升级', color: '#607d8b', bg: '#eceff1' },
}
const TYPE_ORDER = ['compliance', 'typo', 'redundancy', 'numeric', 'escalation']

function typeCfg(t) {
  return TYPE_CFG[t] || { label: t, color: '#666', bg: '#f5f5f5' }
}

const JOB_STATUS = {
  pending:   { label: '排队中',   color: '#faad14' },
  parsing:   { label: '解析文档', color: '#1890ff' },
  reviewing: { label: '审查中',   color: '#1890ff' },
  done:      { label: '审查完成', color: '#52c41a' },
  failed:    { label: '失败',     color: '#f5222d' },
  cancelled: { label: '已取消',   color: '#8c8c8c' },
}

const ACTION_LABEL = {
  pending: { text: '待处理', color: 'default' },
  accept:  { text: '已采纳', color: 'success' },
  reject:  { text: '已驳回', color: 'warning' },
  custom:  { text: '已改写', color: 'processing' },
}

function normalizeText(text = '') {
  return String(text).normalize('NFKC').replace(/\s+/g, '')
}

function escapeHtml(text = '') {
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function isMeaningfulPdfFragment(str = '', normStr = normalizeText(str)) {
  if (normStr.length < 6) return false
  if (!/[\u4e00-\u9fa5A-Za-z0-9]/.test(normStr)) return false
  if (/^[\s，。；、：！？,.!?;:()[\]（）【】《》“”"'‘’\-—~·]+$/.test(str)) return false
  return true
}

function makePdfTextRenderer(issues, selectedId) {
  const sortedIssues = [...issues]
    .map(issue => ({ issue, candidates: issueCandidates(issue) }))
    .filter(item => item.candidates.length > 0)
    .sort((a, b) => Math.max(...b.candidates.map(c => c.length)) - Math.max(...a.candidates.map(c => c.length)))

  return ({ str }) => {
    if (!str) return ''
    const normStr = normalizeText(str)
    const exactMatches = []

    for (const { issue, candidates } of sortedIssues) {
      for (const candidate of candidates) {
        const normCandidate = normalizeText(candidate)
        if (!normCandidate) continue
        const isShortNeedle = candidate.length <= 3
        if (str.includes(candidate)) {
          exactMatches.push({ issue, candidate })
          break
        }
        if (!isShortNeedle && isMeaningfulPdfFragment(str, normStr) && normCandidate.includes(normStr)) {
          return `<mark class="rv-pdf-mark ${issueHighlightClass(issue)} ${issue.id === selectedId ? 'selected' : ''}" data-issue-id="${issue.id}">${escapeHtml(str)}</mark>`
        }
      }
    }

    if (exactMatches.length === 0) return escapeHtml(str)
    let html = escapeHtml(str)
    exactMatches.slice(0, 3).forEach(({ issue, candidate }) => {
      const safeCandidate = escapeHtml(candidate)
      if (!safeCandidate) return
      const cls = `rv-pdf-mark ${issueHighlightClass(issue)} ${issue.id === selectedId ? 'selected' : ''}`
      html = html.split(safeCandidate).join(`<mark class="${cls}" data-issue-id="${issue.id}">${safeCandidate}</mark>`)
    })
    return html
  }
}
function issueHighlightClass(issue) {
  if (issue.agent_applied === 1) return 'fixed'
  if (issue.human_action === 'reject' || issue.agent_applied === 2) return 'rejected'
  if (['accept', 'custom'].includes(issue.human_action) && [0, 3].includes(issue.agent_applied)) return 'applying'
  return 'error'
}

function issueCandidates(issue) {
  const values = issue.agent_applied === 1
    ? [issue.human_text, issue.suggestion, issue.original_text]
    : [issue.original_text]
  return values.map(v => (v || '').trim()).filter(Boolean)
}

function issueLocateCandidates(issue) {
  return [
    issue.detail?.context,
    issue.original_text,
    issue.human_text,
    issue.suggestion,
  ].map(v => (v || '').trim()).filter(Boolean)
}

function findIssueNeedle(text, issue) {
  if (!text) return ''
  return issueCandidates(issue).find(candidate => text.includes(candidate)) || ''
}

function renderHighlightedText(text = '', blockIssues = [], onSelectIssue) {
  if (!text || blockIssues.length === 0) return text
  const ranges = []
  const occupied = new Array(text.length).fill(false)
  const sorted = [...blockIssues].sort((a, b) => {
    const aNeedle = findIssueNeedle(text, a)
    const bNeedle = findIssueNeedle(text, b)
    return bNeedle.length - aNeedle.length
  })

  sorted.forEach(issue => {
    const needle = findIssueNeedle(text, issue)
    if (!needle) return
    const start = text.indexOf(needle)
    const end = start + needle.length
    if (start < 0 || occupied.slice(start, end).some(Boolean)) return
    for (let i = start; i < end; i += 1) occupied[i] = true
    ranges.push({ start, end, issue })
  })

  if (ranges.length === 0) return text
  ranges.sort((a, b) => a.start - b.start)
  const nodes = []
  let cursor = 0
  ranges.forEach(range => {
    if (range.start > cursor) nodes.push(text.slice(cursor, range.start))
    nodes.push(
      <mark
        key={`${range.issue.id}-${range.start}`}
        className={`rv-doc-mark ${issueHighlightClass(range.issue)}`}
        data-issue-id={range.issue.id}
        title={`#${range.issue.id} ${range.issue.title || range.issue.status || ''}`}
        onClick={(e) => { e.stopPropagation(); onSelectIssue(range.issue.id) }}
      >
        {text.slice(range.start, range.end)}
      </mark>
    )
    cursor = range.end
  })
  if (cursor < text.length) nodes.push(text.slice(cursor))
  return nodes
}

function AnnotatedDocument({ blocks, loading, issuesByBlock, onSelectIssue }) {
  if (loading) {
    return <div className="rv-doc-loading"><Spin /><span>正在加载高亮文档...</span></div>
  }
  if (!blocks.length) {
    return <div className="rv-doc-loading"><Empty description="文档段落模型尚未生成" /></div>
  }

  return (
    <div className="rv-doc-scroll">
      <div className="rv-a4-page rv-highlight-page">
        {blocks.map(block => {
          const blockIssues = issuesByBlock.get(Number(block.block_index)) || []
          const issueIds = blockIssues.map(issue => String(issue.id)).join(' ')
          const fixedWithoutExactMatch = blockIssues.some(issue =>
            issue.agent_applied === 1 && !findIssueNeedle(block.text || '', issue)
          )
          const classes = [
            'rv-doc-block',
            block.is_heading ? 'heading' : '',
            block.kind === 'table' ? 'table' : '',
            blockIssues.length ? 'has-issues' : '',
            fixedWithoutExactMatch ? 'fixed-block' : '',
          ].filter(Boolean).join(' ')
          return (
            <div
              key={block.block_index}
              className={classes}
              data-block-index={block.block_index}
              data-issue-ids={issueIds}
              onClick={() => blockIssues[0] && onSelectIssue(blockIssues[0].id)}
            >
              {renderHighlightedText(block.text || '', blockIssues, onSelectIssue)}
            </div>
          )
        })}
      </div>
    </div>
  )
}
// ── 主组件 ───────────────────────────────────────────────────────
export default function Review() {
  const user = useUserStore(s => s.user)
  const uid  = user?.id ?? 'guest'
  const activeJobKey = `review-active-job:${uid}`

  // ── 上传 & 任务状态 ───────────────────────────────────────────
  const [dragging,   setDragging]   = useState(false)
  const [uploading,  setUploading]  = useState(false)
  const [preprocessStage, setPreprocessStage] = useState('')
  const [mineType,   setMineType]   = useState('non_outburst')
  const [jobId,      setJobId]      = useState(null)
  const [jobInfo,    setJobInfo]    = useState(null)
  const [docName,    setDocName]    = useState(null)
  const [pdfUrl,     setPdfUrl]     = useState(null)
  const [pdfBlobUrl, setPdfBlobUrl] = useState(null)
  const [pdfLoading, setPdfLoading] = useState(false)
  const [pdfError,   setPdfError]   = useState("")
  const [pdfNumPages, setPdfNumPages] = useState(0)
  const [pdfPage, setPdfPage] = useState(1)
  const [pdfScale, setPdfScale] = useState(1.35)
  const [pdfTextIndex, setPdfTextIndex] = useState({})
  const [previewMode, setPreviewMode] = useState('pdf')
  const [documentBlocks, setDocumentBlocks] = useState([])
  const [documentLoading, setDocumentLoading] = useState(false)

  // ── 问题列表 ──────────────────────────────────────────────────
  const [issues,     setIssues]     = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [filterType, setFilterType] = useState('all')

  // ── 反馈 ──────────────────────────────────────────────────────
  const [feedbackMap,  setFeedbackMap]  = useState({})
  const [customText,   setCustomText]   = useState('')

  const sseRef      = useRef(null)
  const fileInputRef = useRef(null)

  const loadDocument = useCallback(async (jid) => {
    if (!jid) return
    setDocumentLoading(true)
    try {
      const res = await axios.get(`${V9}/document/${jid}`)
      setDocumentBlocks(res.data.blocks || [])
    } catch {
      setDocumentBlocks([])
    } finally {
      setDocumentLoading(false)
    }
  }, [])

  // ── SSE ───────────────────────────────────────────────────────
  const connectSSE = useCallback((jid) => {
    if (sseRef.current) sseRef.current.close()
    const es = new EventSource(`${V9}/stream/${jid}`)
    es.onmessage = (e) => {
      try {
        const d = JSON.parse(e.data)
        if (d.type === 'status') {
          setJobInfo(d.job)
          if (d.job?.paragraphs_ready) loadDocument(jid)
        } else if (d.type === 'issue') {
          setIssues(prev => prev.some(i => i.id === d.issue.id) ? prev : [...prev, d.issue])
        } else if (d.type === 'end') {
          loadDocument(jid)
          es.close()
        } else if (d.type === 'timeout') {
          es.close()
          setJobInfo(prev => {
            if (prev && !['done', 'failed', 'cancelled'].includes(prev.status)) connectSSE(jid)
            return prev
          })
        } else if (d.type === 'error') {
          message.error(d.msg || 'SSE 错误')
          es.close()
        }
      } catch {
        // Ignore malformed SSE payloads and wait for the next event.
      }
    }
    es.onerror = () => {}
    sseRef.current = es
  }, [loadDocument])

  useEffect(() => () => sseRef.current?.close(), [])

  // 刷新页面后恢复当前审查任务、已有问题与 SSE 连接
  useEffect(() => {
    const saved = localStorage.getItem(activeJobKey)
    if (!saved) return
    let active = true
    let task
    try {
      task = JSON.parse(saved)
    } catch {
      localStorage.removeItem(activeJobKey)
      return
    }
    Promise.all([
      axios.get(`${V9}/status/${task.jobId}`),
      axios.get(`${V9}/issues/${task.jobId}`),
    ]).then(([statusRes, issuesRes]) => {
      if (!active) return
      setJobId(task.jobId)
      setDocName(task.docName || statusRes.data.doc_name)
      if (task.pdfReady) {
        setPdfUrl(`${V9}/preview-pdf/${task.jobId}?t=${Date.now()}`)
        setPdfLoading(true)
        setPreviewMode('pdf')
      } else {
        setPdfUrl(null)
        setPdfLoading(false)
        setPreviewMode('annotated')
      }
      setPdfError("")
      setJobInfo(statusRes.data)
      setIssues(issuesRes.data.issues || [])
      loadDocument(task.jobId)
      if (statusRes.data.status === 'cancelled') {
        localStorage.removeItem(activeJobKey)
        return
      }
      if (!['done', 'failed', 'cancelled'].includes(statusRes.data.status)) connectSSE(task.jobId)
    }).catch(() => localStorage.removeItem(activeJobKey))
    return () => { active = false }
  }, [activeJobKey, connectSSE, loadDocument])


  useEffect(() => {
    if (!pdfUrl) return
    let active = true
    setPdfLoading(true)
    setPdfError("")
    setPdfPage(1)
    setPdfBlobUrl(null)
    axios.get(pdfUrl, { responseType: 'blob', timeout: 45000 }).then(res => {
      if (!active) return
      const contentType = res.headers?.['content-type'] || ''
      if (!contentType.includes('pdf') && res.data?.type && !res.data.type.includes('pdf')) {
        throw new Error('后端返回的不是 PDF 文件')
      }
      const objectUrl = URL.createObjectURL(res.data)
      setPdfBlobUrl(objectUrl)
    }).catch(err => {
      if (!active) return
      setPdfLoading(false)
      setPdfError(`PDF 预览加载失败：${err.response?.data?.detail || err.message || '未知错误'}`)
    })
    return () => {
      active = false
    }
  }, [pdfUrl])

  useEffect(() => () => {
    if (pdfBlobUrl) URL.revokeObjectURL(pdfBlobUrl)
  }, [pdfBlobUrl])

  function refreshPdfPreview(force = false) {
    if (!jobId) return
    setPreviewMode('pdf')
    setPdfError("")
    setPdfLoading(true)
    setPdfPage(1)
    setPdfTextIndex({})
    setPdfUrl(`${V9}/preview-pdf/${jobId}?t=${Date.now()}${force ? '&refresh=1' : ''}`)
  }

  async function handlePdfLoadSuccess(pdf) {
    setPdfNumPages(pdf.numPages)
    setPdfLoading(false)
    setPdfError('')
    try {
      const entries = await Promise.all(
        Array.from({ length: pdf.numPages }, async (_, i) => {
          const pageNo = i + 1
          const page = await pdf.getPage(pageNo)
          const content = await page.getTextContent()
          return [pageNo, content.items.map(item => item.str || '').join('')]
        })
      )
      setPdfTextIndex(Object.fromEntries(entries))
    } catch {
      setPdfTextIndex({})
    }
  }
  // ── 上传 ──────────────────────────────────────────────────────
  async function handleFile(file) {
    if (!file) return
    if (!file.name.match(/\.(docx|doc)$/i)) {
      message.error('只支持 Word 文档（.docx / .doc）')
      return
    }
    setUploading(true)
    setIssues([])
    setSelectedId(null)
    setFeedbackMap({})
    setJobInfo(null)
    setDocName(null)
    try {
      const fd = new FormData()
      fd.append('file', file)
      setPreprocessStage('正在预处理文档：保存原件、统一 .docx、准备 PDF 预览')
      const prepRes = await axios.post(`${V9}/preprocess`, fd, { timeout: 180000 })
      const prep = prepRes.data
      if (prep.pdf_error) {
        message.warning('文档已可审查，但 PDF 预览未准备成功，可先使用文本视图')
      }
      setPreprocessStage('预处理完成，正在创建审查任务')
      const res = await axios.post(`${V9}/start/${prep.preprocess_id}?mine_type=${mineType}`)
      const pdfReady = !!res.data.preprocess?.pdf_ready
      setJobId(res.data.job_id)
      setDocName(res.data.doc_name)
      localStorage.setItem(activeJobKey, JSON.stringify({
        jobId: res.data.job_id, docName: res.data.doc_name, pdfReady,
      }))
      setJobInfo({ status: 'pending', progress: 0, n_chunks: 0, n_done: 0 })
      setDocumentBlocks([])
      setPreviewMode(pdfReady ? 'pdf' : 'annotated')
      if (pdfReady) {
        setPdfUrl(`${V9}/preview-pdf/${res.data.job_id}?t=${Date.now()}`)
        setPdfLoading(true)
        setPdfError("")
      } else {
        setPdfUrl(null)
        setPdfLoading(false)
        setPdfError("")
      }
      connectSSE(res.data.job_id)
      message.success(pdfReady ? '预处理完成，审查任务已创建' : '预处理完成，审查任务已创建；PDF 可稍后手动生成')
    } catch (err) {
      message.error('预处理/上传失败：' + (err.response?.data?.detail || err.message))
    } finally {
      setPreprocessStage('')
      setUploading(false)
    }
  }

  const onDragOver  = (e) => { e.preventDefault(); setDragging(true) }
  const onDragLeave = ()  => setDragging(false)
  const onDrop      = (e) => { e.preventDefault(); setDragging(false); handleFile(e.dataTransfer.files[0]) }
  const onInputChange = (e) => handleFile(e.target.files[0])

  // ── 清除 ──────────────────────────────────────────────────────
  async function clearResults() {
    const currentJobId = jobId
    sseRef.current?.close()
    if (currentJobId) {
      try {
        await axios.post(`${V9}/cancel/${currentJobId}`)
      } catch {
        // Cancellation is best-effort; local UI state should still reset.
      }
    }
    localStorage.removeItem(activeJobKey)
    setJobId(null); setJobInfo(null); setDocName(null); setPreprocessStage('')
    setPdfUrl(null); setPdfBlobUrl(null); setPdfLoading(false); setPdfError("")
    setDocumentBlocks([]); setPreviewMode('pdf')
    setIssues([]); setSelectedId(null)
    setFeedbackMap({}); setCustomText('')
    setFilterType('all')
  }

  // ── 下载 ──────────────────────────────────────────────────────
  async function handleDownload() {
    try {
      const res = await axios.get(`${V9}/download/${jobId}`, { responseType: 'blob' })
      const url = URL.createObjectURL(res.data)
      const a = document.createElement('a')
      a.href = url
      a.download = `审查后_${docName || jobId}.docx`
      a.click()
      URL.revokeObjectURL(url)
    } catch { message.error('下载失败') }
  }

  // ── 反馈 ──────────────────────────────────────────────────────
  async function submitFeedback(issueId, action, text = '') {
    setFeedbackMap(prev => ({ ...prev, [issueId]: { loading: true } }))
    try {
      await axios.post(`${V9}/feedback`, { issue_id: issueId, action, text })
      const nextApplied = action === 'reject' ? 2 : 3
      setIssues(prev => prev.map(i =>
        i.id === issueId ? { ...i, human_action: action, human_text: text, agent_applied: nextApplied } : i
      ))
      setCustomText('')
      if (action === 'reject') {
        setFeedbackMap(prev => ({ ...prev, [issueId]: { loading: false, done: true, action } }))
        message.success('已驳回，原文保留')
      } else {
        message.success('反馈已提交，正在改写文档')
        pollFeedbackResult(issueId)
      }
    } catch (err) {
      message.error('提交失败：' + (err.response?.data?.detail || err.message))
      setFeedbackMap(prev => ({ ...prev, [issueId]: { loading: false } }))
    }
  }

  async function pollFeedbackResult(issueId) {
    for (let i = 0; i < 80; i += 1) {
      await new Promise(resolve => setTimeout(resolve, 1500))
      try {
        const res = await axios.get(`${V9}/feedback-status/${issueId}`)
        const data = res.data
        setIssues(prev => prev.map(item =>
          item.id === issueId
            ? { ...item, human_action: data.human_action, agent_applied: data.agent_applied, agent_note: data.agent_note }
            : item
        ))
        if (![0, 3].includes(data.agent_applied)) {
          setFeedbackMap(prev => ({ ...prev, [issueId]: { loading: false, done: true, action: data.human_action } }))
          if (data.agent_applied === 1) {
            message.success('文档已改写，高亮已刷新')
            loadDocument(jobId)
            refreshPdfPreview(true)
          } else if (data.agent_applied === -1) {
            message.error(`改写失败：${data.agent_note || '未能定位原文'}`)
          }
          return
        }
      } catch {
        // Feedback polling is best-effort; timeout below reports the unresolved state.
      }
    }
    setFeedbackMap(prev => ({ ...prev, [issueId]: { loading: false } }))
    message.warning('改写仍在处理中，请稍后刷新查看')
  }

  // ── 派生状态 ──────────────────────────────────────────────────
  const selectedIssue = issues.find(i => i.id === selectedId) || null
  const isRunning = jobInfo && ['pending', 'parsing', 'reviewing'].includes(jobInfo.status)
  const isDone    = jobInfo?.status === 'done'
  const isFailed  = jobInfo?.status === 'failed'
  const agentStage = jobInfo?.agent_stage || ''
  const isVectorizing = agentStage === 'vectorizing'
  const isRetrievalRerank = agentStage === 'retrieval_rerank'
  const isReviewingChunks = agentStage === 'reviewing'
  const phaseDone = Number(jobInfo?.phase_done ?? 0)
  const phaseTotal = Number(jobInfo?.phase_total ?? jobInfo?.n_chunks ?? 0)
  const progressDone = (isVectorizing || isRetrievalRerank)
    ? phaseDone
    : Number(jobInfo?.n_done ?? 0)
  const progressTotal = (isVectorizing || isRetrievalRerank)
    ? phaseTotal
    : Number(jobInfo?.n_chunks ?? phaseTotal ?? 0)
  const timingText = useMemo(() => {
    const timings = jobInfo?.timings || {}
    const items = [
      ['parse', '解析'],
      ['vectorize', '向量化'],
      ['retrieval_rerank', '检索重排'],
      ['review', '审查'],
      ['repetition', '重复性'],
      ['total', '总计'],
    ].filter(([key]) => typeof timings[key] === 'number' && timings[key] > 0)
    return items.map(([key, label]) => `${label} ${timings[key].toFixed(1)}s`).join(' · ')
  }, [jobInfo?.timings])

  const phaseProgressText = useMemo(() => {
    if (isDone) return `共发现 ${issues.length} 个问题`
    if (isVectorizing) return `已向量化 ${progressDone} / ${progressTotal} 段`
    if (isRetrievalRerank) return `已检索重排 ${progressDone} / ${progressTotal} 段`
    if (agentStage === 'repetition') return '正在检查文档级重复内容'
    if (isReviewingChunks) return `已审查 ${progressDone} / ${progressTotal} 段`
    return `${progressDone} / ${progressTotal} 段`
  }, [agentStage, isDone, isRetrievalRerank, isReviewingChunks, isVectorizing, issues.length, progressDone, progressTotal])

  const countByType = issues.reduce((acc, i) => {
    acc[i.issue_type] = (acc[i.issue_type] || 0) + 1; return acc
  }, {})

  const filteredIssues = filterType === 'all'
    ? issues
    : issues.filter(i => i.issue_type === filterType)

  const issuesByBlock = useMemo(() => {
    const map = new Map()
    issues.forEach(issue => {
      (issue.block_indices || []).forEach(bi => {
        const key = Number(bi)
        if (!map.has(key)) map.set(key, [])
        map.get(key).push(issue)
      })
    })
    return map
  }, [issues])

  const issuePageMap = useMemo(() => {
    const pages = Object.keys(pdfTextIndex).map(Number).sort((a, b) => a - b)
    if (!pages.length) return {}
    const map = {}
    const normalizedPages = Object.fromEntries(
      pages.map(page => [page, normalizeText(pdfTextIndex[page] || '')])
    )
    issues.forEach(issue => {
      for (const candidate of issueLocateCandidates(issue)) {
        const normalized = normalizeText(candidate)
        if (normalized.length < 4) continue
        for (const page of pages) {
          if (normalizedPages[page].includes(normalized)) {
            map[issue.id] = page
            return
          }
        }
        for (let len = Math.min(normalized.length, 30); len >= 10; len -= 5) {
          const prefix = normalized.slice(0, len)
          for (const page of pages) {
            if (normalizedPages[page].includes(prefix)) {
              map[issue.id] = page
              return
            }
          }
        }
      }
    })
    return map
  }, [issues, pdfTextIndex])

  const visiblePdfIssues = useMemo(() => {
    const hasIndex = Object.keys(pdfTextIndex).length > 0
    if (!hasIndex) return issues
    return issues.filter(issue => issuePageMap[issue.id] === pdfPage)
  }, [issues, issuePageMap, pdfPage, pdfTextIndex])

  const pdfTextRenderer = useMemo(
    () => makePdfTextRenderer(visiblePdfIssues, selectedId),
    [visiblePdfIssues, selectedId]
  )

  useEffect(() => {
    if (!selectedId) return
    const mappedPage = issuePageMap[selectedId]
    if (previewMode === 'pdf' && mappedPage && mappedPage !== pdfPage) {
      setPdfPage(mappedPage)
      return
    }
    const timer = setTimeout(() => {
      const selector = previewMode === 'pdf'
        ? `.rv-pdf-mark[data-issue-id="${selectedId}"]`
        : `.rv-doc-mark[data-issue-id="${selectedId}"], .rv-doc-block[data-issue-ids~="${selectedId}"]`
      const target = document.querySelector(selector)
      target?.scrollIntoView({ block: 'center', behavior: 'smooth' })
    }, 120)
    return () => clearTimeout(timer)
  }, [selectedId, previewMode, documentBlocks, issues, pdfPage, issuePageMap])

  useEffect(() => {
    const handler = (event) => {
      const mark = event.target.closest?.('.rv-pdf-mark[data-issue-id]')
      if (mark?.dataset?.issueId) setSelectedId(Number(mark.dataset.issueId))
    }
    document.addEventListener('click', handler)
    return () => document.removeEventListener('click', handler)
  }, [])

  const st = JOB_STATUS[jobInfo?.status] || { label: jobInfo?.status || '', color: '#666' }

  // ── render ────────────────────────────────────────────────────
  return (
    <div className="rv-root">
      {/* ── 顶栏 ── */}
      <div className="rv-topbar">
        <span className="rv-title">规程合规审查</span>

        {/* 无任务时：矿井类型选择 */}
        {!jobId && (
          <Radio.Group value={mineType} onChange={e => setMineType(e.target.value)} size="small">
            <Radio.Button value="non_outburst">非突出矿井</Radio.Button>
            <Radio.Button value="outburst">突出矿井</Radio.Button>
          </Radio.Group>
        )}

        {/* 有任务时：状态 + 进度 */}
        {jobId && (
          <>
            <Tag color={st.color} style={{ fontWeight: 600 }}>{st.label}</Tag>
            {jobInfo?.agent_status && (
              <span style={{ fontSize: 12, color: 'rgba(255,255,255,.7)', maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {jobInfo.agent_status}
              </span>
            )}
            {isRunning && jobInfo?.n_chunks > 0 && (
              <span className="rv-stats">
                {progressDone}/{progressTotal} 段
              </span>
            )}
          </>
        )}

        <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
          {isDone && (
            <Button icon={<DownloadOutlined />} onClick={handleDownload}
              style={{ background: 'rgba(46,160,67,.85)', borderColor: 'rgba(46,160,67,.6)', color: '#fff', fontWeight: 600 }}>
              下载审查后文档
            </Button>
          )}
          {jobId && (
            <Button icon={<DeleteOutlined />} onClick={clearResults}
              style={{ background: 'rgba(255,255,255,.15)', borderColor: 'rgba(255,255,255,.35)', color: '#fff' }}>
              清除
            </Button>
          )}
        </div>
      </div>

      {/* 进度条（审查中）*/}
      {isRunning && jobInfo?.n_chunks > 0 && (
        <div style={{ padding: '0 0 2px 0', background: '#1a1a2e' }}>
          <Progress
            percent={progressTotal > 0 ? Math.round((progressDone / progressTotal) * 100) : 0}
            status="active"
            strokeColor={{ '0%': '#2f54eb', '100%': '#52c41a' }}
            showInfo={false}
            style={{ margin: 0 }}
          />
        </div>
      )}

      <div className="rv-body">
        {/* ── 左栏：问题列表 ── */}
        <div className="rv-left">
          <div className="rv-tab-bar">
            {/* 全部 tab */}
            <button
              className={`rv-tab-btn ${filterType === 'all' ? 'active' : ''}`}
              onClick={() => setFilterType('all')}
            >
              全部
              {issues.length > 0 && <span className="rv-filter-count">{issues.length}</span>}
              {isRunning && issues.length === 0 && (
                <LoadingOutlined style={{ marginLeft: 4, fontSize: 11, color: '#2f54eb' }} />
              )}
            </button>
            {/* 按类型 tabs */}
            {TYPE_ORDER.filter(t => countByType[t]).map(t => {
              const cfg = typeCfg(t)
              return (
                <button
                  key={t}
                  className={`rv-tab-btn ${filterType === t ? 'active' : ''}`}
                  style={filterType === t ? { color: cfg.color, borderBottomColor: cfg.color } : {}}
                  onClick={() => setFilterType(t)}
                >
                  {cfg.label}
                  <span className="rv-filter-count">{countByType[t]}</span>
                </button>
              )
            })}
          </div>

          <div className="rv-issue-list">
            {isRunning && filteredIssues.length === 0 && (
              <div style={{ padding: '16px 0', textAlign: 'center' }}>
                <Spin size="small" />
                <span style={{ marginLeft: 8, fontSize: '.82em', color: '#aaa' }}>审查中…</span>
              </div>
            )}
            {!isRunning && !jobId && (
              <Empty description="上传文档后开始审查" style={{ marginTop: 48 }} />
            )}
            {!isRunning && jobId && filteredIssues.length === 0 && (
              <Empty description="暂无问题" style={{ marginTop: 48 }} />
            )}

            {filteredIssues.map(issue => {
              const cfg = typeCfg(issue.issue_type)
              const isSelected = issue.id === selectedId
              return (
                <div
                  key={issue.id}
                  className={`rv-issue-item ${isSelected ? 'selected' : ''}`}
                  style={{ borderLeft: `4px solid ${cfg.color}` }}
                  onClick={() => { setSelectedId(issue.id); setCustomText('') }}
                >
                  <div className="rv-issue-item-header">
                    <span className="rv-dec-dot">
                      {issue.human_action === 'accept' && <CheckCircleFilled style={{ color: '#52c41a', fontSize: 14 }} />}
                      {issue.human_action === 'reject' && <CloseCircleFilled style={{ color: '#f44336', fontSize: 14 }} />}
                      {issue.human_action === 'custom' && <CheckCircleFilled style={{ color: '#1890ff', fontSize: 14 }} />}
                      {issue.human_action === 'pending' && <span className="rv-dec-empty" />}
                    </span>
                    <span className="rv-badge" style={{ background: cfg.color }}>{cfg.label}</span>
                    <span className="rv-issue-type">{issue.title || issue.status}</span>
                    <span className="rv-issue-no">#{issue.id}</span>
                  </div>
                  {issue.original_text && (
                    <div className="rv-issue-orig">
                      「{issue.original_text.slice(0, 40)}{issue.original_text.length > 40 ? '…' : ''}」
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </div>

        {/* ── 中栏：上传区 / 进度面板 ── */}
        <div className="rv-doc-viewer">
          {!jobId ? (
            /* 上传区 */
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '100%', gap: 20, padding: '0 40px' }}>
              <div
                style={{
                  width: '100%', maxWidth: 480, height: 200,
                  border: `2px dashed ${dragging ? '#2f54eb' : '#adb5bd'}`,
                  borderRadius: 12,
                  display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
                  cursor: 'pointer', transition: 'all .2s',
                  background: dragging ? '#e8eeff' : 'var(--bg-primary, #fff)',
                  gap: 8,
                }}
                onDragOver={onDragOver} onDragLeave={onDragLeave} onDrop={onDrop}
                onClick={() => fileInputRef.current?.click()}
              >
                {uploading
                  ? <>
                    <Spin indicator={<LoadingOutlined style={{ fontSize: 36 }} spin />} />
                    <div style={{ fontSize: 14, fontWeight: 500, color: '#444', maxWidth: 360, textAlign: 'center' }}>
                      {preprocessStage || '正在处理文档'}
                    </div>
                  </>
                  : <>
                    <CloudUploadOutlined style={{ fontSize: 40, color: '#2f54eb' }} />
                    <div style={{ fontSize: 15, fontWeight: 500, color: '#444' }}>拖拽 Word 文档到此，或点击上传</div>
                    <div style={{ fontSize: 12, color: '#aaa' }}>.docx / .doc，上传后先预处理再审查</div>
                  </>
                }
              </div>
              <input ref={fileInputRef} type="file" accept=".docx,.doc" hidden onChange={onInputChange} />
              <Radio.Group value={mineType} onChange={e => setMineType(e.target.value)}>
                <Radio value="non_outburst">非突出矿井</Radio>
                <Radio value="outburst">突出矿井</Radio>
              </Radio.Group>
            </div>
          ) : (
            <div className="rv-pdf-panel">
              <div className="rv-pdf-toolbar">
                <FileTextOutlined style={{ color: '#2f54eb' }} />
                <span className="rv-pdf-doc-name">{docName}</span>
                <Tag color={st.color}>{st.label}</Tag>
                <Radio.Group
                  size="small"
                  value={previewMode}
                  onChange={e => {
                    const next = e.target.value
                    setPreviewMode(next)
                    if (next === 'pdf' && jobId && !pdfUrl && !pdfBlobUrl) refreshPdfPreview(false)
                  }}
                  className="rv-preview-switch"
                >
                  <Radio.Button value="pdf">PDF 高亮</Radio.Button>
                  <Radio.Button value="annotated">文本视图</Radio.Button>
                </Radio.Group>
                {(isRunning || isDone) && (
                  <span className="rv-pdf-progress-text">
                    {phaseProgressText}
                  </span>
                )}
              </div>
              {(isRunning || isDone) && (
                <Progress
                  percent={isDone ? 100 : (progressTotal > 0 ? Math.round((progressDone / progressTotal) * 100) : 0)}
                  status={isFailed ? 'exception' : isDone ? 'success' : 'active'}
                  strokeColor={{ '0%': '#2f54eb', '100%': '#52c41a' }}
                  showInfo={false}
                  size="small"
                />
              )}
              {isFailed && (
                <div className="rv-pdf-error">审查失败：{jobInfo?.error || '未知错误'}</div>
              )}
              {jobInfo?.agent_status && (
                <div className="rv-pdf-status">{jobInfo.agent_status}</div>
              )}
              {timingText && (
                <div className="rv-pdf-status">耗时：{timingText}</div>
              )}
              <div className="rv-pdf-frame-wrap">
                {previewMode === 'annotated' ? (
                  <AnnotatedDocument
                    blocks={documentBlocks}
                    loading={documentLoading}
                    issuesByBlock={issuesByBlock}
                    onSelectIssue={setSelectedId}
                  />
                ) : (
                  <>
                    {pdfLoading && !pdfError && (
                      <div className="rv-pdf-loading">
                        <Spin />
                        <span>正在加载 PDF 预览...</span>
                      </div>
                    )}
                    {pdfError && (
                      <div className="rv-pdf-loading">
                        <span>{pdfError}</span>
                        <Button size="small" onClick={() => refreshPdfPreview(true)}>重试预览</Button>
                      </div>
                    )}
                    {!pdfUrl && !pdfBlobUrl && !pdfError && (
                      <div className="rv-pdf-loading">
                        <span>PDF 预览尚未生成。文本视图可直接使用。</span>
                        <Button size="small" type="primary" onClick={() => refreshPdfPreview(false)}>生成 PDF 预览</Button>
                      </div>
                    )}
                    {pdfBlobUrl && !pdfError && (
                      <div className="rv-pdf-doc-scroll">
                        <div className="rv-pdf-page-controls">
                          <Button size="small" disabled={pdfPage <= 1} onClick={() => setPdfPage(p => Math.max(1, p - 1))}>上一页</Button>
                          <span>{pdfPage} / {pdfNumPages || '-'}</span>
                          <Button size="small" disabled={!pdfNumPages || pdfPage >= pdfNumPages} onClick={() => setPdfPage(p => Math.min(pdfNumPages, p + 1))}>下一页</Button>
                          <Button size="small" onClick={() => setPdfScale(s => Math.max(0.9, +(s - 0.1).toFixed(2)))}>-</Button>
                          <span>{Math.round(pdfScale * 100)}%</span>
                          <Button size="small" onClick={() => setPdfScale(s => Math.min(2.4, +(s + 0.1).toFixed(2)))}>+</Button>
                        </div>
                        <PdfDocument
                          key={pdfBlobUrl}
                          file={pdfBlobUrl}
                          loading={null}
                          onLoadSuccess={handlePdfLoadSuccess}
                          onLoadError={(err) => { setPdfLoading(false); setPdfError(`PDF 预览加载失败：${err?.message || '未知错误'}`) }}
                        >
                          <Page
                            className="rv-pdf-page"
                            pageNumber={pdfPage}
                            scale={pdfScale}
                            renderAnnotationLayer={false}
                            renderTextLayer={true}
                            customTextRenderer={pdfTextRenderer}
                          />
                        </PdfDocument>
                      </div>
                    )}
                  </>
                )}
              </div>
            </div>
          )}
        </div>

        {/* ── 右栏：问题详情 + 反馈 ── */}
        <div className="rv-right">
          {selectedIssue ? (
            <IssueDetail
              issue={selectedIssue}
              feedbackState={feedbackMap[selectedIssue.id] || {}}
              customText={customText}
              onCustomTextChange={setCustomText}
              onFeedback={submitFeedback}
            />
          ) : (
            <div className="rv-center" style={{ height: '100%', flexDirection: 'column', gap: 12 }}>
              <Empty description={jobId ? '点击左侧问题查看详情' : '上传文档开始审查'} />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// ── 问题详情面板 ──────────────────────────────────────────────────
function IssueDetail({ issue, feedbackState, customText, onCustomTextChange, onFeedback }) {
  const cfg = typeCfg(issue.issue_type)
  const alreadyFed = issue.human_action !== 'pending'
  const { loading } = feedbackState

  return (
    <div className="rv-detail">
      {/* 标题行 */}
      <div className="rv-detail-header" style={{ borderBottom: `3px solid ${cfg.color}` }}>
        <span className="rv-badge" style={{ background: cfg.color }}>{cfg.label}</span>
        <span className="rv-detail-type">{issue.status}</span>
        <span className="rv-issue-no">#{issue.id}</span>
      </div>

      {issue.title && (
        <div style={{ fontWeight: 700, fontSize: 14, marginBottom: 12, lineHeight: 1.5, color: 'var(--text-primary, #1a1a2e)' }}>
          {issue.title}
        </div>
      )}

      {/* 原文片段 */}
      {issue.original_text && (
        <div className="rv-detail-block">
          <div className="rv-detail-label">原文片段</div>
          <div className="rv-detail-orig" style={{ borderLeft: '3px solid #ffc107', background: '#fff8e1', padding: '8px 12px', borderRadius: 4 }}>
            {issue.original_text}
          </div>
        </div>
      )}

      {/* 修改建议 */}
      {issue.suggestion && (
        <div className="rv-detail-block">
          <div className="rv-detail-label">修改建议</div>
          <div style={{ background: '#e8f5e9', borderLeft: '3px solid #4caf50', padding: '8px 12px', borderRadius: 4, fontSize: 13, color: '#1b5e20', lineHeight: 1.7 }}>
            {issue.suggestion}
          </div>
        </div>
      )}

      {/* 法规依据 */}
      {issue.regulation && (
        <div className="rv-detail-block">
          <div className="rv-detail-label">法规依据</div>
          <div style={{ background: '#e3f2fd', padding: '6px 10px', borderRadius: 4, fontSize: 13, color: '#1565c0' }}>
            {issue.regulation}
          </div>
        </div>
      )}

      {/* 分析说明 */}
      {issue.reason && (
        <div className="rv-detail-block">
          <div className="rv-detail-label">分析说明</div>
          <div className="rv-detail-text" style={{ fontSize: 13, lineHeight: 1.7, color: '#555' }}>{issue.reason}</div>
        </div>
      )}

      {/* 数值核验明细 */}
      {issue.issue_type === 'numeric' && issue.detail && Object.keys(issue.detail).length > 0 && (
        <div className="rv-detail-block">
          <div className="rv-detail-label">数值核验</div>
          <pre style={{ fontSize: 11, background: '#1a1a2e', color: '#a8d8b9', padding: '8px 12px', borderRadius: 6, overflow: 'auto', fontFamily: 'Cascadia Code, Consolas, monospace' }}>
            {JSON.stringify(issue.detail, null, 2)}
          </pre>
        </div>
      )}

      {/* worker 改写状态 */}
      {issue.agent_applied === 1 && (
        <div style={{ background: '#f6ffed', border: '1px solid #b7eb8f', borderRadius: 6, padding: '8px 12px', fontSize: 13, color: '#389e0d', marginBottom: 8 }}>
          ✓ 主智能体已改写文档{issue.agent_note ? `：${issue.agent_note}` : ''}
        </div>
      )}
      {issue.agent_applied === -1 && (
        <div style={{ background: '#fff2f0', border: '1px solid #ffa39e', borderRadius: 6, padding: '8px 12px', fontSize: 13, color: '#cf1322', marginBottom: 8 }}>
          ✗ 改写失败{issue.agent_note ? `：${issue.agent_note}` : ''}
        </div>
      )}

      {/* ── 人工裁决 ── */}
      <div className="rv-detail-block rv-hitl-block">
        <div className="rv-detail-label">人工裁决</div>

        {alreadyFed ? (
          <div>
            <Tag color={ACTION_LABEL[issue.human_action]?.color || 'default'} style={{ fontSize: 13 }}>
              {ACTION_LABEL[issue.human_action]?.text}
            </Tag>
            {issue.human_text && (
              <div style={{ fontSize: 13, color: '#555', background: '#f5f5f5', padding: '6px 10px', borderRadius: 4, marginTop: 6 }}>
                {issue.human_text}
              </div>
            )}
          </div>
        ) : (
          <>
            <div className="rv-hitl-btns">
              <Tooltip title="确认问题属实，采纳系统建议（主智能体将改写文档）">
                <Button
                  icon={<CheckOutlined />}
                  className="rv-hitl-accept"
                  loading={loading}
                  onClick={() => onFeedback(issue.id, 'accept')}
                >采纳</Button>
              </Tooltip>
              <Tooltip title="驳回：系统误报，原文合规">
                <Button
                  icon={<CloseOutlined />}
                  className="rv-hitl-reject"
                  loading={loading}
                  onClick={() => onFeedback(issue.id, 'reject')}
                >驳回</Button>
              </Tooltip>
            </div>
            <div style={{ marginTop: 10 }}>
              <TextArea
                placeholder="输入自定义修改意见（可选），然后点击「按此改写」…"
                value={customText}
                onChange={e => onCustomTextChange(e.target.value)}
                rows={3}
              />
              <Button
                icon={<EditOutlined />}
                style={{ marginTop: 6 }}
                disabled={!customText.trim()}
                loading={loading}
                onClick={() => onFeedback(issue.id, 'custom', customText)}
              >按此意见改写</Button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
















