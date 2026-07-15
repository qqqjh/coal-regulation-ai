import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import useUserStore from '../../store/userStore'
import {
  Button, Spin, Empty, Radio, Input, message, Progress, Tag, Tooltip, Popconfirm,
  Select,
} from 'antd'
import {
  DeleteOutlined, FileTextOutlined, CloudUploadOutlined,
  CheckCircleFilled, CloseCircleFilled, LoadingOutlined,
  CheckOutlined, CloseOutlined, EditOutlined, DownloadOutlined, ReloadOutlined,
  RedoOutlined, PlayCircleOutlined, EnvironmentOutlined, SwapOutlined,
} from '@ant-design/icons'
import axios from 'axios'
import { Document as PdfDocument, Page, pdfjs } from 'react-pdf'
import 'react-pdf/dist/Page/AnnotationLayer.css'
import 'react-pdf/dist/Page/TextLayer.css'
import './index.css'

const { TextArea } = Input
const REVIEW_API = '/api/v10'
pdfjs.GlobalWorkerOptions.workerSrc = new URL('pdfjs-dist/build/pdf.worker.min.mjs', import.meta.url).toString()

// ── v10 issue type metadata ───────────────────────────────────────
const TYPE_CFG = {
  compliance:  { label: '合规', color: '#d9363e', bg: '#fff1f0' },
  typo:        { label: '错别字', color: '#7b3ff2', bg: '#f3edff' },
  redundancy:  { label: '重复', color: '#f59f00', bg: '#fff7e6' },
  numeric:     { label: '数值', color: '#0b7285', bg: '#e6fcff' },
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
  accept:  { text: '人工已采纳', color: 'success' },
  reject:  { text: '人工已驳回', color: 'warning' },
  custom:  { text: '人工已改写', color: 'processing' },
}

function normalizeText(text = '') {
  return String(text).normalize('NFKC').replace(/\s+/g, '')
}

function issuePdfLocations(issue) {
  const detail = issue?.detail || {}
  const groups = [
    ...(detail.pdf_locations || []).map(location => ({ ...location, role: 'issue' })),
    ...(detail.source_pdf_locations || []).map(location => ({ ...location, role: 'source' })),
    ...(detail.duplicate_pdf_locations || []).map(location => ({ ...location, role: 'duplicate' })),
  ]
  const seen = new Set()
  return groups.filter(location => {
    const page = Number(location?.page)
    const bbox = Array.isArray(location?.bbox) ? location.bbox.map(Number) : []
    if (!Number.isFinite(page) || page < 1 || bbox.length !== 4 || bbox.some(value => !Number.isFinite(value))) {
      return false
    }
    if (bbox[2] <= bbox[0] || bbox[3] <= bbox[1]) return false
    const key = `${page}:${bbox.join(',')}:${location.role}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function issuePdfPages(issue) {
  return [...new Set(issuePdfLocations(issue).map(location => Number(location.page)))]
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

const PDF_AREA_HIGHLIGHT_TYPES = new Set(['compliance', 'redundancy', 'escalation'])

function shouldUsePdfAreaHighlight(issue) {
  return issue.agent_applied === 1 || PDF_AREA_HIGHLIGHT_TYPES.has(issue.issue_type)
}

function pdfMarkClass(issue, selectedId, forceArea = false) {
  return [
    'rv-pdf-mark',
    issueHighlightClass(issue),
    forceArea || shouldUsePdfAreaHighlight(issue) ? 'area' : 'text',
    issue.id === selectedId ? 'selected' : '',
  ].filter(Boolean).join(' ')
}

function canUsePdfAreaMatch(issue, normStr, normCandidate, isSelected = false) {
  if (!shouldUsePdfAreaHighlight(issue)) return false
  const minFragmentLength = isSelected ? 3 : 6
  const minCandidateLength = issue.agent_applied === 1 ? 3 : 10
  if (normCandidate.length < minCandidateLength || normStr.length < minFragmentLength) return false
  if (!/[\u4e00-\u9fa5A-Za-z0-9]/.test(normStr)) return false
  if (/^[\d.%％Ω]+$/.test(normStr) && normStr.length < 4) return false
  return normCandidate.includes(normStr)
}

function percentile(values, p) {
  if (!values.length) return 0
  const sorted = [...values].sort((a, b) => a - b)
  const index = Math.min(sorted.length - 1, Math.max(0, Math.floor((sorted.length - 1) * p)))
  return sorted[index]
}

function getPdfTextColumnBounds(page, pageRect) {
  const spans = Array.from(page.querySelectorAll('.react-pdf__Page__textContent span'))
    .map(span => span.getBoundingClientRect())
    .filter(rect => rect.width > 2 && rect.height > 2)
  if (!spans.length) return null
  const lefts = spans.map(rect => rect.left)
  const rights = spans.map(rect => rect.right)
  return {
    left: Math.max(pageRect.left, percentile(lefts, 0.05) - 8),
    right: Math.min(pageRect.right, percentile(rights, 0.95) + 8),
  }
}

function mergePdfAreaRects(rects, bounds, columnBounds = null, options = {}) {
  const paddingX = 14
  const paddingY = 6
  const minBlockHeight = options.minBlockHeight || 8
  const lines = []
  rects
    .filter(rect => rect.width > 0 && rect.height > 0)
    .sort((a, b) => a.top - b.top || a.left - b.left)
    .forEach(rect => {
      const center = rect.top + rect.height / 2
      const line = lines.find(item => Math.abs(item.center - center) <= Math.max(7, Math.min(item.height, rect.height) * 0.7))
      if (line) {
        line.left = Math.min(line.left, rect.left)
        line.top = Math.min(line.top, rect.top)
        line.right = Math.max(line.right, rect.right)
        line.bottom = Math.max(line.bottom, rect.bottom)
        line.height = Math.max(line.height, rect.height)
        line.center = (line.top + line.bottom) / 2
      } else {
        lines.push({
          left: rect.left,
          top: rect.top,
          right: rect.right,
          bottom: rect.bottom,
          height: rect.height,
          center,
        })
      }
    })

  const blocks = []
  lines
    .sort((a, b) => a.top - b.top || a.left - b.left)
    .forEach(line => {
      const last = blocks[blocks.length - 1]
      const gap = last ? line.top - last.bottom : Infinity
      const mergeGap = Math.max(30, Math.min(48, Math.max(last?.height || 0, line.height) * 2.2))
      if (last && gap <= mergeGap) {
        last.left = Math.min(last.left, line.left)
        last.top = Math.min(last.top, line.top)
        last.right = Math.max(last.right, line.right)
        last.bottom = Math.max(last.bottom, line.bottom)
        last.height = Math.max(last.height, line.height)
      } else {
        blocks.push({ ...line })
      }
    })

  const columnLeft = columnBounds?.left ?? null
  const columnRight = columnBounds?.right ?? null

  return blocks.map(block => {
    const rawLeft = columnLeft !== null ? Math.min(block.left, columnLeft) : block.left
    const rawRight = columnRight !== null ? Math.max(block.right, columnRight) : block.right
    const left = Math.max(0, rawLeft - bounds.left - paddingX)
    const top = Math.max(0, block.top - bounds.top - paddingY)
    const right = Math.min(bounds.width, rawRight - bounds.left + paddingX)
    let bottom = Math.min(bounds.height, block.bottom - bounds.top + paddingY)
    if (bottom - top < minBlockHeight) {
      bottom = Math.min(bounds.height, top + minBlockHeight)
    }
    return {
      left,
      top,
      width: Math.max(8, right - left),
      height: Math.max(8, bottom - top),
    }
  })
}

function roundPdfRectValue(value) {
  return Math.round(Number(value || 0) * 10) / 10
}

function samePdfAreaLayerBox(a, b) {
  if (!a || !b) return a === b
  return ['left', 'top', 'width', 'height'].every(key => roundPdfRectValue(a[key]) === roundPdfRectValue(b[key]))
}

function samePdfAreaRects(a = [], b = []) {
  if (a.length !== b.length) return false
  return a.every((rect, index) => {
    const other = b[index]
    return other
      && rect.key === other.key
      && rect.issueId === other.issueId
      && rect.issueType === other.issueType
      && rect.stateClass === other.stateClass
      && rect.selected === other.selected
      && ['left', 'top', 'width', 'height'].every(key => roundPdfRectValue(rect[key]) === roundPdfRectValue(other[key]))
  })
}

function pdfIssueCandidates(issue) {
  const items = [...issueCandidates(issue), issue.detail?.context].filter(Boolean)
  const out = []
  items.forEach(text => {
    out.push(text)
    String(text)
      .split(/[。；;！!？?\n\r]/)
      .map(v => v.trim())
      .filter(v => normalizeText(v).length >= 6)
      .forEach(v => out.push(v))
  })
  return [...new Set(out)]
}

function makePdfTextRenderer(issues, selectedId) {
  const sortedIssues = [...issues]
    .map(issue => ({ issue, candidates: pdfIssueCandidates(issue) }))
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
        const canMatchTinyNumeric = normStr.length >= 2 && /[\d.%％]/.test(normStr)
        if (canUsePdfAreaMatch(issue, normStr, normCandidate, issue.id === selectedId)) {
          return `<mark class="${pdfMarkClass(issue, selectedId, true)}" data-issue-id="${issue.id}">${escapeHtml(str)}</mark>`
        }
        if (!isShortNeedle && (isMeaningfulPdfFragment(str, normStr) || canMatchTinyNumeric) && normCandidate.includes(normStr)) {
          return `<mark class="${pdfMarkClass(issue, selectedId)}" data-issue-id="${issue.id}">${escapeHtml(str)}</mark>`
        }
      }
    }

    if (exactMatches.length === 0) return escapeHtml(str)
    let html = escapeHtml(str)
    exactMatches.slice(0, 3).forEach(({ issue, candidate }) => {
      const safeCandidate = escapeHtml(candidate)
      if (!safeCandidate) return
      const cls = pdfMarkClass(issue, selectedId)
      html = html.split(safeCandidate).join(`<mark class="${cls}" data-issue-id="${issue.id}">${safeCandidate}</mark>`)
    })
    return html
  }
}
function issueHighlightClass(issue) {
  const typeClass = `issue-${issue.issue_type || 'other'}`
  if (issue.agent_applied === 1) return 'fixed'
  if (issue.human_action === 'reject' || issue.agent_applied === 2) return 'rejected'
  if (['accept', 'custom'].includes(issue.human_action) && [0, 3].includes(issue.agent_applied)) return 'applying'
  return `error ${typeClass}`
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

function buildBlockPageCandidates(issue, blockByIndex) {
  const texts = (issue.block_indices || [])
    .map(idx => blockByIndex.get(Number(idx))?.text || '')
    .filter(Boolean)
  const anchors = [
    issue.detail?.context,
    issue.original_text,
    issue.human_text,
    issue.suggestion,
  ].map(v => normalizeText(v || '')).filter(v => v.length >= 4)

  const candidates = []
  texts.forEach(text => {
    const normalized = normalizeText(text)
    if (normalized.length < 8) return
    let addedWindow = false
    anchors.forEach(anchor => {
      const pos = normalized.indexOf(anchor)
      if (pos >= 0) {
        candidates.push(normalized.slice(Math.max(0, pos - 80), Math.min(normalized.length, pos + anchor.length + 120)))
        addedWindow = true
      }
    })
    if (!addedWindow) {
      candidates.push(normalized.length > 260 ? normalized.slice(0, 260) : normalized)
    }
  })
  return [...new Set(candidates)]
}

function formatHistoryTime(value) {
  if (!value) return ''
  const text = String(value)
  return text.length > 16 ? text.slice(5, 16) : text
}

function formatEscalationType(value) {
  if (!value) return '未标明'
  if (typeof value === 'string') return value
  return value.type || value.reason || JSON.stringify(value)
}

function applyStatusMeta(issue) {
  if (issue.human_action === 'pending') return null
  if (issue.human_action === 'reject') {
    return { color: 'default', text: '原文保留', note: '该问题已被人工驳回，不会改写 Word/PDF。' }
  }
  if (issue.agent_applied === 1) {
    return { color: 'success', text: '已改入文档', note: issue.agent_note || 'Word 已改写，PDF 预览应显示最新内容。' }
  }
  if (issue.agent_applied === 3) {
    return { color: 'processing', text: '正在改写文档', note: '后台 worker 正在把该建议写入 Word，完成后会自动刷新 PDF。' }
  }
  if (issue.agent_applied === 0) {
    return { color: 'warning', text: '等待后台改写', note: '人工裁决已记录，但还没有写入 Word/PDF。请确认 v10_worker.py 正在运行。' }
  }
  if (issue.agent_applied === 2) {
    return { color: 'default', text: '未改写文档', note: issue.agent_note || '主智能体判断无需改写，PDF 会保持原文。' }
  }
  if (issue.agent_applied === -1) {
    return { color: 'error', text: '改写失败', note: issue.agent_note || '未能定位原文或改写失败。' }
  }
  return { color: 'default', text: '状态未知', note: issue.agent_note || '' }
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

function AnnotatedDocument({ blocks, loading, issuesByBlock, onSelectIssue, focusedBlockIndex }) {
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
          const dominantIssueType = blockIssues[0]?.issue_type ? `issue-${blockIssues[0].issue_type}` : ''
          const fixedWithoutExactMatch = blockIssues.some(issue =>
            issue.agent_applied === 1 && !findIssueNeedle(block.text || '', issue)
          )
          const classes = [
            'rv-doc-block',
            dominantIssueType,
            block.is_heading ? 'heading' : '',
            block.kind === 'table' ? 'table' : '',
            blockIssues.length ? 'has-issues' : '',
            fixedWithoutExactMatch ? 'fixed-block' : '',
            Number(block.block_index) === Number(focusedBlockIndex) ? 'repeat-focus' : '',
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
  const isAdmin = user?.role === 'admin'
  const activeJobKey = `review-active-job:${uid}`

  // ── 上传 & 任务状态 ───────────────────────────────────────────
  const [dragging,   setDragging]   = useState(false)
  const [uploading,  setUploading]  = useState(false)
  const [preprocessStage, setPreprocessStage] = useState('')
  const [mineType,   setMineType]   = useState('non_outburst')
  const [reviewKbs, setReviewKbs] = useState([])
  const [reviewKbLoading, setReviewKbLoading] = useState(false)
  const [selectedReviewKbId, setSelectedReviewKbId] = useState(null)
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
  const [pdfAreaRects, setPdfAreaRects] = useState([])
  const [pdfAreaLayerBox, setPdfAreaLayerBox] = useState(null)
  const [previewMode, setPreviewMode] = useState('pdf')
  const [repeatFocus, setRepeatFocus] = useState(null)
  const [repeatPdfFocus, setRepeatPdfFocus] = useState(null)
  const [documentBlocks, setDocumentBlocks] = useState([])
  const [documentLoading, setDocumentLoading] = useState(false)
  const [historyDocs, setHistoryDocs] = useState([])
  const [historyLoading, setHistoryLoading] = useState(false)

  // ── 问题列表 ──────────────────────────────────────────────────
  const [issues,     setIssues]     = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [filterType, setFilterType] = useState('all')

  // ── 反馈 ──────────────────────────────────────────────────────
  const [feedbackMap,  setFeedbackMap]  = useState({})
  const [customText,   setCustomText]   = useState('')

  const sseRef      = useRef(null)
  const fileInputRef = useRef(null)
  const pdfPageWrapRef = useRef(null)
  const pdfRefreshSeqRef = useRef(0)

  useEffect(() => {
    setRepeatFocus(null)
    setRepeatPdfFocus(null)
  }, [jobId])

  const loadDocument = useCallback(async (jid) => {
    if (!jid) return
    setDocumentLoading(true)
    try {
      const res = await axios.get(`${REVIEW_API}/document/${jid}`)
      setDocumentBlocks(res.data.blocks || [])
    } catch {
      setDocumentBlocks([])
    } finally {
      setDocumentLoading(false)
    }
  }, [])

  const loadHistory = useCallback(async () => {
    setHistoryLoading(true)
    try {
      const res = await axios.get(`${REVIEW_API}/jobs`, {
        params: { user_id: uid, limit: 20, scope: user?.role === 'admin' ? 'all' : 'mine' },
      })
      setHistoryDocs(res.data.items || [])
    } catch {
      setHistoryDocs([])
    } finally {
      setHistoryLoading(false)
    }
  }, [uid, user?.role])

  const loadReviewKbs = useCallback(async () => {
    if (isAdmin) {
      setReviewKbs([])
      setSelectedReviewKbId(null)
      setReviewKbLoading(false)
      return
    }
    setReviewKbLoading(true)
    try {
      const res = await axios.get('/api/admin/review-kb-permissions/allowed', {
        params: { user_id: uid },
      })
      const items = res.data.knowledge_bases || []
      setReviewKbs(items)
      setSelectedReviewKbId(prev => {
        if (prev && items.some(kb => kb.id === prev)) return prev
        return items[0]?.id || null
      })
    } catch {
      setReviewKbs([])
      setSelectedReviewKbId(null)
    } finally {
      setReviewKbLoading(false)
    }
  }, [uid, isAdmin])

  useEffect(() => {
    loadHistory()
  }, [loadHistory])

  useEffect(() => {
    loadReviewKbs()
  }, [loadReviewKbs])

  // ── SSE ───────────────────────────────────────────────────────
  const connectSSE = useCallback((jid) => {
    if (sseRef.current) sseRef.current.close()
    const es = new EventSource(`${REVIEW_API}/stream/${jid}`)
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
          loadHistory()
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
  }, [loadDocument, loadHistory])

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
      axios.get(`${REVIEW_API}/status/${task.jobId}`),
      axios.get(`${REVIEW_API}/issues/${task.jobId}`),
    ]).then(([statusRes, issuesRes]) => {
      if (!active) return
      setJobId(task.jobId)
      setDocName(task.docName || statusRes.data.doc_name)
      if (task.pdfReady) {
        setPdfUrl(`${REVIEW_API}/preview-pdf/${task.jobId}?t=${Date.now()}`)
        setPdfLoading(true)
      } else {
        setPdfUrl(null)
        setPdfLoading(false)
      }
      setPreviewMode('pdf')
      setPdfError("")
      setJobInfo(statusRes.data)
      if (statusRes.data.kb_id) setSelectedReviewKbId(statusRes.data.kb_id)
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
    axios.get(pdfUrl, {
      responseType: 'blob',
      timeout: 45000,
      headers: {
        'Cache-Control': 'no-cache',
        Pragma: 'no-cache',
      },
    }).then(res => {
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
    pdfRefreshSeqRef.current += 1
    setPreviewMode('pdf')
    setPdfError("")
    setPdfLoading(true)
    setPdfPage(1)
    setPdfTextIndex({})
    setPdfAreaRects([])
    setPdfAreaLayerBox(null)
    setPdfUrl(`${REVIEW_API}/preview-pdf/${jobId}?t=${Date.now()}&v=${pdfRefreshSeqRef.current}${force ? '&refresh=1' : ''}`)
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
    if (!file.name.match(/\.(docx|doc|pdf)$/i)) {
      message.error('只支持 Word/PDF 文档（.docx / .doc / .pdf）')
      return
    }
    if (!isAdmin && !selectedReviewKbId) {
      message.error('当前账号没有可用于审查的规程知识库，请联系管理员配置')
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
      setPreprocessStage(file.name.toLowerCase().endsWith('.pdf')
        ? '正在保存原始 PDF；审查阶段将由 MinerU 直接解析'
        : '正在预处理文档：保存原件、统一 .docx、准备 PDF 预览')
      const prepRes = await axios.post(`${REVIEW_API}/preprocess`, fd, { timeout: 180000 })
      const prep = prepRes.data
      if (prep.pdf_error) {
        message.warning('文档已可审查，但 PDF 预览未准备成功，可稍后手动生成')
      }
      setPreprocessStage('预处理完成，正在创建审查任务')
      const startParams = new URLSearchParams({
        mine_type: mineType,
        user_id: String(uid),
      })
      if (!isAdmin && selectedReviewKbId) {
        startParams.set('kb_id', String(selectedReviewKbId))
      }
      const res = await axios.post(`${REVIEW_API}/start/${prep.preprocess_id}?${startParams.toString()}`)
      const pdfReady = !!res.data.preprocess?.pdf_ready
      setJobId(res.data.job_id)
      setDocName(res.data.doc_name)
      localStorage.setItem(activeJobKey, JSON.stringify({
        jobId: res.data.job_id, docName: res.data.doc_name, pdfReady,
      }))
      setJobInfo({
        status: 'pending',
        progress: 0,
        n_chunks: 0,
        n_done: 0,
        source_format: prep.source_format || 'docx',
      })
      setDocumentBlocks([])
      setPreviewMode('pdf')
      if (pdfReady) {
        setPdfUrl(`${REVIEW_API}/preview-pdf/${res.data.job_id}?t=${Date.now()}`)
        setPdfLoading(true)
        setPdfError("")
      } else {
        setPdfUrl(null)
        setPdfLoading(false)
        setPdfError("")
      }
      connectSSE(res.data.job_id)
      loadHistory()
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
  const onInputChange = (e) => {
    handleFile(e.target.files[0])
    e.target.value = ''
  }

  // ── 清除 ──────────────────────────────────────────────────────
  async function clearResults() {
    const currentJobId = jobId
    sseRef.current?.close()
    if (currentJobId) {
      try {
        await axios.post(`${REVIEW_API}/cancel/${currentJobId}`)
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
    loadHistory()
  }

  async function openHistoryJob(item) {
    if (!item?.job_id) return
    sseRef.current?.close()
    setUploading(false)
    setPreprocessStage('')
    setSelectedId(null)
    setFeedbackMap({})
    setCustomText('')
    setFilterType('all')
    try {
      const [statusRes, issuesRes] = await Promise.all([
        axios.get(`${REVIEW_API}/status/${item.job_id}`),
        axios.get(`${REVIEW_API}/issues/${item.job_id}`),
      ])
      const statusData = statusRes.data
      setJobId(item.job_id)
      setDocName(statusData.doc_name || item.doc_name)
      setJobInfo(statusData)
      if (statusData.kb_id) setSelectedReviewKbId(statusData.kb_id)
      setIssues(issuesRes.data.issues || [])
      setPdfError('')
      setPdfPage(1)
      setPdfTextIndex({})
      setPdfUrl(`${REVIEW_API}/preview-pdf/${item.job_id}?t=${Date.now()}`)
      setPdfLoading(true)
      setPreviewMode('pdf')
      loadDocument(item.job_id)
      localStorage.setItem(activeJobKey, JSON.stringify({
        jobId: item.job_id,
        docName: statusData.doc_name || item.doc_name,
        pdfReady: true,
      }))
      if (!['done', 'failed', 'cancelled'].includes(statusData.status)) {
        connectSSE(item.job_id)
      }
    } catch (err) {
      message.error('打开历史任务失败：' + (err.response?.data?.detail || err.message))
    }
  }

  async function rerunHistoryJob(item) {
    if (!item?.job_id) return
    try {
      const res = await axios.post(`${REVIEW_API}/jobs/${item.job_id}/rerun`, null, {
        params: { user_id: uid },
      })
      message.success('已创建重新审查任务')
      await loadHistory()
      await openHistoryJob({ job_id: res.data.job_id, doc_name: res.data.doc_name })
    } catch (err) {
      message.error('重新审查失败：' + (err.response?.data?.detail || err.message))
    }
  }

  async function deleteHistoryJob(item) {
    if (!item?.job_id) return
    try {
      await axios.delete(`${REVIEW_API}/jobs/${item.job_id}`)
      if (item.job_id === jobId) {
        sseRef.current?.close()
        localStorage.removeItem(activeJobKey)
        setJobId(null); setJobInfo(null); setDocName(null); setPreprocessStage('')
        setPdfUrl(null); setPdfBlobUrl(null); setPdfLoading(false); setPdfError("")
        setDocumentBlocks([]); setPreviewMode('pdf')
        setIssues([]); setSelectedId(null)
        setFeedbackMap({}); setCustomText('')
        setFilterType('all')
      }
      await loadHistory()
      message.success('历史任务已删除')
    } catch (err) {
      message.error('删除失败：' + (err.response?.data?.detail || err.message))
    }
  }

  // ── 下载 ──────────────────────────────────────────────────────
  async function handleDownload() {
    try {
      const res = await axios.get(`${REVIEW_API}/download/${jobId}`, { responseType: 'blob' })
      const url = URL.createObjectURL(res.data)
      const a = document.createElement('a')
      a.href = url
      const baseName = String(docName || jobId).replace(/\.(docx|doc|pdf)$/i, '')
      a.download = `审查后_${baseName}.docx`
      a.click()
      URL.revokeObjectURL(url)
    } catch { message.error('下载失败') }
  }

  // ── 反馈 ──────────────────────────────────────────────────────
  async function submitFeedback(issueId, action, text = '') {
    setFeedbackMap(prev => ({ ...prev, [issueId]: { loading: true } }))
    try {
      const feedbackResponse = await axios.post(`${REVIEW_API}/feedback`, {
        issue_id: issueId,
        action,
        text,
        user_id: String(uid),
      })
      const nextApplied = action === 'reject' ? 2 : 3
      setIssues(prev => prev.map(i =>
        i.id === issueId
          ? {
              ...i,
              human_action: action,
              human_text: text,
              agent_applied: nextApplied,
              flywheel_annotation_id: feedbackResponse.data.annotation_id,
            }
          : i
      ))
      setCustomText('')
      if (action === 'reject') {
        setFeedbackMap(prev => ({ ...prev, [issueId]: { loading: false, done: true, action } }))
        message.success('已驳回，原文保留；该误报已进入人工反馈飞轮')
      } else {
        message.success('反馈已进入数据飞轮，正在改写文档')
        pollFeedbackResult(issueId)
      }
    } catch (err) {
      message.error('提交失败：' + (err.response?.data?.detail || err.message))
      setFeedbackMap(prev => ({ ...prev, [issueId]: { loading: false } }))
    }
  }

  async function revokeFeedback(issueId) {
    setFeedbackMap(prev => ({ ...prev, [issueId]: { loading: true } }))
    try {
      const res = await axios.post(`${REVIEW_API}/feedback/${issueId}/revoke`)
      const latest = res.data.issue || {
        id: issueId,
        human_action: 'pending',
        human_text: '',
        agent_applied: 0,
        agent_note: '',
      }
      setIssues(prev => prev.map(item => (
        item.id === issueId
          ? {
              ...item,
              human_action: latest.human_action,
              human_text: latest.human_text,
              agent_applied: latest.agent_applied,
              agent_note: latest.agent_note,
            }
          : item
      )))
      setFeedbackMap(prev => ({ ...prev, [issueId]: { loading: false } }))
      if (res.data.rolled_back) {
        await loadDocument(jobId)
        setTimeout(() => refreshPdfPreview(true), 350)
        message.success('已撤回人工裁决，并回滚文档改写')
      } else {
        message.success('已撤回人工裁决')
      }
    } catch (err) {
      setFeedbackMap(prev => ({ ...prev, [issueId]: { loading: false } }))
      message.error('撤回失败：' + (err.response?.data?.detail || err.message))
    }
  }

  async function pollFeedbackResult(issueId) {
    for (let i = 0; i < 80; i += 1) {
      await new Promise(resolve => setTimeout(resolve, 1500))
      try {
        const res = await axios.get(`${REVIEW_API}/feedback-status/${issueId}`)
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
            await loadDocument(jobId)
            setTimeout(() => refreshPdfPreview(true), 350)
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

  const pendingFeedbackIds = useMemo(() => (
    issues
      .filter(item => ['accept', 'custom'].includes(item.human_action) && [0, 3].includes(Number(item.agent_applied)))
      .map(item => item.id)
      .join(',')
  ), [issues])

  useEffect(() => {
    if (!jobId || !pendingFeedbackIds) return undefined
    let stopped = false
    let timer = null
    const ids = pendingFeedbackIds.split(',').map(Number).filter(Boolean)

    const tick = async () => {
      if (stopped || ids.length === 0) return
      try {
        const results = await Promise.all(
          ids.map(id => axios.get(`${REVIEW_API}/feedback-status/${id}`).then(res => res.data).catch(() => null))
        )
        let shouldRefreshPdf = false
        setIssues(prev => prev.map(item => {
          const data = results.find(result => result && Number(result.issue_id) === Number(item.id))
          if (!data) return item
          if (item.agent_applied !== data.agent_applied && data.agent_applied === 1) {
            shouldRefreshPdf = true
          }
          return {
            ...item,
            human_action: data.human_action,
            agent_applied: data.agent_applied,
            agent_note: data.agent_note,
          }
        }))
        if (shouldRefreshPdf && !stopped) {
          await loadDocument(jobId)
          setTimeout(() => {
            if (!stopped) refreshPdfPreview(true)
          }, 350)
        }
      } finally {
        if (!stopped) timer = setTimeout(tick, 3000)
      }
    }

    timer = setTimeout(tick, 500)
    return () => {
      stopped = true
      if (timer) clearTimeout(timer)
    }
  }, [jobId, pendingFeedbackIds, loadDocument])

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
      ['persist_review_vectors', '向量入库'],
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

  const blockByIndex = useMemo(() => {
    const map = new Map()
    documentBlocks.forEach(block => map.set(Number(block.block_index), block))
    return map
  }, [documentBlocks])

  const issuePageMap = useMemo(() => {
    const pages = Object.keys(pdfTextIndex).map(Number).sort((a, b) => a - b)
    if (!pages.length) return {}
    const map = {}
    const normalizedPages = Object.fromEntries(
      pages.map(page => [page, normalizeText(pdfTextIndex[page] || '')])
    )
    issues.forEach(issue => {
      const nativePages = issuePdfPages(issue)
      if (nativePages.length) {
        map[issue.id] = nativePages[0]
        return
      }
      const candidates = [
        ...buildBlockPageCandidates(issue, blockByIndex),
        ...issueLocateCandidates(issue).map(candidate => normalizeText(candidate)),
      ].filter(Boolean)
      for (const normalized of candidates) {
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
  }, [blockByIndex, issues, pdfTextIndex])

  const visiblePdfIssues = useMemo(() => {
    const hasIndex = Object.keys(pdfTextIndex).length > 0
    return issues.filter(issue => {
      const nativePages = issuePdfPages(issue)
      if (nativePages.length) return nativePages.includes(pdfPage)
      if (!hasIndex) return true
      return !issuePageMap[issue.id] || issuePageMap[issue.id] === pdfPage
    })
  }, [issues, issuePageMap, pdfPage, pdfTextIndex])

  const visibleTextPdfIssues = useMemo(
    () => visiblePdfIssues.filter(issue => issuePdfLocations(issue).length === 0),
    [visiblePdfIssues]
  )

  const pdfTextRenderer = useMemo(
    () => makePdfTextRenderer(visibleTextPdfIssues, selectedId),
    [visibleTextPdfIssues, selectedId]
  )

  const historyPanelItems = useMemo(() => {
    const items = [...historyDocs]
    if (jobId && !items.some(item => item.job_id === jobId)) {
      items.unshift({
        job_id: jobId,
        doc_name: docName || jobInfo?.doc_name || '当前待审文档',
        status: jobInfo?.status || 'pending',
        issue_count: issues.length,
        n_chunks: jobInfo?.n_chunks || progressTotal || 0,
        n_done: jobInfo?.n_done || progressDone || 0,
        updated_at: jobInfo?.updated_at || '',
        created_at: jobInfo?.created_at || '',
      })
    }
    return items.slice(0, 20)
  }, [docName, historyDocs, issues.length, jobId, jobInfo, progressDone, progressTotal])

  const locateRepeatBlocks = useCallback((issueId, blockIndices = [], pdfLocations = []) => {
    const nativeLocation = pdfLocations.find(location => (
      Number(location?.page) > 0
      && Array.isArray(location?.bbox)
      && location.bbox.length === 4
    ))
    if (nativeLocation) {
      setSelectedId(Number(issueId))
      setRepeatFocus(null)
      setRepeatPdfFocus({
        issueId: Number(issueId),
        page: Number(nativeLocation.page),
        sourceUnitId: nativeLocation.source_unit_id || '',
      })
      setPdfPage(Number(nativeLocation.page))
      setPreviewMode('pdf')
      return
    }
    const firstBlock = blockIndices
      .map(value => Number(value))
      .find(value => Number.isFinite(value))
    if (firstBlock === undefined) {
      message.warning('该重复位置缺少 Word 段落索引')
      return
    }
    setSelectedId(Number(issueId))
    setRepeatPdfFocus(null)
    setRepeatFocus({ issueId: Number(issueId), blockIndex: firstBlock })
    setPreviewMode('blocks')
  }, [])

  useEffect(() => {
    if (!selectedId) return
    const focusedPdfPage = repeatPdfFocus?.issueId === Number(selectedId)
      ? Number(repeatPdfFocus.page)
      : null
    const mappedPage = focusedPdfPage || issuePageMap[selectedId]
    if (previewMode === 'pdf' && mappedPage && mappedPage !== pdfPage) {
      setPdfPage(mappedPage)
      return
    }
    const timer = setTimeout(() => {
      const focusedBlock = repeatFocus?.issueId === Number(selectedId)
        ? repeatFocus.blockIndex
        : null
      const selector = previewMode === 'pdf'
        ? `.rv-pdf-mark[data-issue-id="${selectedId}"], .rv-pdf-area[data-issue-id="${selectedId}"]`
        : focusedBlock !== null
          ? `.rv-doc-block[data-block-index="${focusedBlock}"]`
          : `.rv-doc-mark[data-issue-id="${selectedId}"], .rv-doc-block[data-issue-ids~="${selectedId}"]`
      const target = document.querySelector(selector)
      target?.scrollIntoView({ block: 'center', behavior: 'smooth' })
    }, 120)
    return () => clearTimeout(timer)
  }, [selectedId, previewMode, documentBlocks, issues, pdfPage, issuePageMap, repeatFocus, repeatPdfFocus])

  useEffect(() => {
    const handler = (event) => {
      const mark = event.target.closest?.('.rv-pdf-mark[data-issue-id]')
      if (mark?.dataset?.issueId) {
        setSelectedId(Number(mark.dataset.issueId))
        setRepeatFocus(null)
        setRepeatPdfFocus(null)
      }
    }
    document.addEventListener('click', handler)
    return () => document.removeEventListener('click', handler)
  }, [])

  useEffect(() => {
    if (!pdfBlobUrl || !visiblePdfIssues.length) {
      setPdfAreaRects([])
      setPdfAreaLayerBox(null)
      return undefined
    }
    let timer = null
    let tries = 0
    let disposed = false
    const calculate = () => {
      if (disposed) return
      const wrap = pdfPageWrapRef.current
      const page = wrap?.querySelector?.('.rv-pdf-page')
      if (!wrap || !page) {
        setPdfAreaRects([])
        setPdfAreaLayerBox(null)
        return
      }

      const wrapRect = wrap.getBoundingClientRect()
      const pageRect = page.getBoundingClientRect()
      const nextLayerBox = {
        left: pageRect.left - wrapRect.left,
        top: pageRect.top - wrapRect.top,
        width: pageRect.width,
        height: pageRect.height,
      }
      setPdfAreaLayerBox(prev => samePdfAreaLayerBox(prev, nextLayerBox) ? prev : nextLayerBox)
      const textColumnBounds = getPdfTextColumnBounds(page, pageRect)
      const issueById = new Map(visiblePdfIssues.map(issue => [Number(issue.id), issue]))
      const marks = Array.from(page.querySelectorAll('.rv-pdf-mark.area[data-issue-id]'))
      const grouped = new Map()

      marks.forEach(mark => {
        const issueId = Number(mark.dataset.issueId)
        if (!issueById.has(issueId)) return
        const rect = mark.getBoundingClientRect()
        if (rect.width < 1 || rect.height < 1) return
        if (!grouped.has(issueId)) grouped.set(issueId, [])
        grouped.get(issueId).push(rect)
      })

      const next = []
      grouped.forEach((rects, issueId) => {
        const issue = issueById.get(issueId)
        mergePdfAreaRects(rects, pageRect, textColumnBounds, {
          minBlockHeight: issue.agent_applied === 1 ? 48 : 8,
        }).forEach((rect, index) => {
          next.push({
            ...rect,
            key: `${issueId}-${index}`,
            issueId,
            issueType: issue.issue_type || 'other',
            stateClass: issueHighlightClass(issue).split(' ')[0],
            selected: issue.id === selectedId,
          })
        })
      })
      setPdfAreaRects(prev => samePdfAreaRects(prev, next) ? prev : next)
      if (next.length === 0 && tries < 8) {
        tries += 1
        timer = setTimeout(calculate, 160)
      }
    }
    timer = setTimeout(calculate, 80)
    return () => {
      disposed = true
      if (timer) clearTimeout(timer)
    }
  }, [pdfBlobUrl, pdfPage, pdfScale, selectedId, visiblePdfIssues, pdfTextRenderer])

  const pdfNativeRects = useMemo(() => {
    if (!pdfAreaLayerBox?.width || !pdfAreaLayerBox?.height) return []
    const result = []
    visiblePdfIssues.forEach(issue => {
      issuePdfLocations(issue)
        .filter(location => Number(location.page) === Number(pdfPage))
        .forEach((location, index) => {
          const bbox = location.bbox.map(Number)
          const pageWidth = Number(location.page_width)
          const pageHeight = Number(location.page_height)
          if (!(pageWidth > 0) || !(pageHeight > 0)) return
          const left = Math.max(0, bbox[0] / pageWidth * pdfAreaLayerBox.width - 4)
          const top = Math.max(0, bbox[1] / pageHeight * pdfAreaLayerBox.height - 3)
          const right = Math.min(pdfAreaLayerBox.width, bbox[2] / pageWidth * pdfAreaLayerBox.width + 4)
          const bottom = Math.min(pdfAreaLayerBox.height, bbox[3] / pageHeight * pdfAreaLayerBox.height + 3)
          if (right - left < 2 || bottom - top < 2) return
          const sourceUnitId = location.source_unit_id || ''
          result.push({
            left,
            top,
            width: right - left,
            height: bottom - top,
            key: `native-${issue.id}-${location.role}-${sourceUnitId || index}`,
            issueId: Number(issue.id),
            issueType: issue.issue_type || 'other',
            stateClass: issueHighlightClass(issue).split(' ')[0],
            role: location.role,
            sourceUnitId,
            selected: Number(issue.id) === Number(selectedId),
            focused: (
              repeatPdfFocus?.issueId === Number(issue.id)
              && repeatPdfFocus?.sourceUnitId
              && repeatPdfFocus.sourceUnitId === sourceUnitId
            ),
          })
        })
    })
    return result
  }, [pdfAreaLayerBox, pdfPage, repeatPdfFocus, selectedId, visiblePdfIssues])

  const st = JOB_STATUS[jobInfo?.status] || { label: jobInfo?.status || '', color: '#666' }

  // ── render ────────────────────────────────────────────────────
  return (
    <div className="rv-root">
      {/* ── 顶栏 ── */}
      <div className="rv-topbar">
        <span className="rv-title">规程合规审查</span>
        <span className="rv-engine-badge" title="当前前端通过 /api/v10 提交审查任务">
          <span className="rv-engine-dot" />V10
        </span>

        {/* 无任务时：矿井类型选择 */}
        {!jobId && (
          <>
            <Radio.Group value={mineType} onChange={e => setMineType(e.target.value)} size="small">
              <Radio.Button value="non_outburst">非突出矿井</Radio.Button>
              <Radio.Button value="outburst">突出矿井</Radio.Button>
            </Radio.Group>
            {isAdmin ? (
              <Tag className="rv-default-kb-tag">管理员：系统默认规程</Tag>
            ) : (
              <Select
                size="small"
                loading={reviewKbLoading}
                value={selectedReviewKbId}
                placeholder="选择规程知识库"
                className="rv-review-kb-select"
                options={reviewKbs.map(kb => ({ label: kb.name, value: kb.id }))}
                onChange={setSelectedReviewKbId}
                disabled={reviewKbs.length === 0}
              />
            )}
          </>
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

      <input ref={fileInputRef} type="file" accept=".docx,.doc,.pdf,application/pdf" hidden onChange={onInputChange} />

      <div className="rv-workbench">
        <div
          className={`rv-workbench-upload ${dragging ? 'dragging' : ''}`}
          onDragOver={onDragOver}
          onDragLeave={onDragLeave}
          onDrop={onDrop}
          onClick={() => fileInputRef.current?.click()}
        >
          {uploading ? <LoadingOutlined className="rv-workbench-upload-icon" /> : <CloudUploadOutlined className="rv-workbench-upload-icon" />}
          <div>
            <div className="rv-workbench-upload-title">{uploading ? '正在处理文档' : '上传待审文档'}</div>
            <div className="rv-workbench-upload-sub">{preprocessStage || '.docx / .doc / .pdf，预处理后进入审查'}</div>
          </div>
        </div>

        <div className="rv-workbench-current">
          <div className="rv-workbench-section-title">
            <FileTextOutlined />
            <span>当前待审</span>
          </div>
          {jobId ? (
            <>
              <div className="rv-current-row">
                <span className="rv-current-name" title={docName || jobId}>{docName || jobId}</span>
                <Tag color={st.color}>{st.label}</Tag>
              </div>
              <div className="rv-current-meta">
                <span>{issues.length} 个问题</span>
                <span>{progressTotal ? `${progressDone}/${progressTotal} 段` : '等待分段'}</span>
                {timingText && <span>{timingText}</span>}
              </div>
            </>
          ) : (
            <div className="rv-current-empty">尚未选择待审文档</div>
          )}
        </div>

        <div className="rv-workbench-history">
          <div className="rv-history-head">
            <div className="rv-workbench-section-title">
              <FileTextOutlined />
              <span>最近历史</span>
            </div>
            <Button
              size="small"
              icon={<ReloadOutlined />}
              loading={historyLoading}
              onClick={loadHistory}
            >
              刷新
            </Button>
          </div>
          <div className="rv-history-list">
            {historyPanelItems.length === 0 && (
              <span className="rv-history-empty">
                {historyLoading ? '正在加载历史…' : '还没有历史任务，上传后会显示在这里'}
              </span>
            )}
            {historyPanelItems.map(item => {
              const status = JOB_STATUS[item.status] || { label: item.status || '未知', color: '#8c8c8c' }
              const active = item.job_id === jobId
              return (
                <div
                  key={item.job_id}
                  role="button"
                  tabIndex={0}
                  className={`rv-history-item ${active ? 'active' : ''}`}
                  onClick={() => openHistoryJob(item)}
                  onKeyDown={(e) => { if (e.key === 'Enter') openHistoryJob(item) }}
                  title={`${item.doc_name || item.job_id}\n${status.label}`}
                >
                  <div className="rv-history-name-row">
                    <span className="rv-history-name">{item.doc_name || item.job_id}</span>
                  </div>
                  <div className="rv-history-bottom">
                    <span className="rv-history-meta">
                      <Tag color={status.color}>{status.label}</Tag>
                      <span>{Number(item.issue_count || 0)} 问题</span>
                      {formatHistoryTime(item.updated_at || item.created_at) && <span>{formatHistoryTime(item.updated_at || item.created_at)}</span>}
                    </span>
                    <span className="rv-history-actions" onClick={(e) => e.stopPropagation()}>
                      <Tooltip title={['pending', 'parsing', 'reviewing'].includes(item.status) ? '继续查看审查进度' : '打开历史结果'}>
                        <Button
                          size="small"
                          icon={<PlayCircleOutlined />}
                          onClick={() => openHistoryJob(item)}
                        />
                      </Tooltip>
                      <Tooltip title="基于该文档重新审查">
                        <Button
                          size="small"
                          icon={<RedoOutlined />}
                          onClick={() => rerunHistoryJob(item)}
                        />
                      </Tooltip>
                      <Popconfirm
                        title="删除这个历史任务？"
                        description="会删除任务记录和问题列表，不会删除原始上传文件。"
                        okText="删除"
                        cancelText="取消"
                        onConfirm={() => deleteHistoryJob(item)}
                      >
                        <Button size="small" danger icon={<DeleteOutlined />} />
                      </Popconfirm>
                    </span>
                  </div>
                </div>
              )
            })}
          </div>
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
                  onClick={() => {
                    setSelectedId(issue.id)
                    setRepeatFocus(null)
                    setRepeatPdfFocus(null)
                    setCustomText('')
                  }}
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
                    <div style={{ fontSize: 15, fontWeight: 500, color: '#444' }}>拖拽 Word/PDF 文档到此，或点击上传</div>
                    <div style={{ fontSize: 12, color: '#aaa' }}>.docx / .doc / .pdf，上传后先预处理再审查</div>
                  </>
                }
              </div>
              <Radio.Group value={mineType} onChange={e => setMineType(e.target.value)}>
                <Radio value="non_outburst">非突出矿井</Radio>
                <Radio value="outburst">突出矿井</Radio>
              </Radio.Group>
              {isAdmin ? (
                <Tag className="rv-upload-default-kb-tag">管理员使用系统默认规程库，不受用户权限限制</Tag>
              ) : (
                <Select
                  loading={reviewKbLoading}
                  value={selectedReviewKbId}
                  placeholder="选择审查使用的规程知识库"
                  style={{ width: 360, maxWidth: '100%' }}
                  options={reviewKbs.map(kb => ({
                    label: `${kb.name}${kb.document_count ? `（${kb.document_count} 文档）` : ''}`,
                    value: kb.id,
                  }))}
                  onChange={setSelectedReviewKbId}
                  disabled={reviewKbs.length === 0}
                />
              )}
              {!isAdmin && reviewKbs.length === 0 && (
                <div style={{ fontSize: 12, color: '#cf1322' }}>
                  当前账号没有可用于审查的规程知识库，请联系管理员配置
                </div>
              )}
            </div>
          ) : (
            <div className="rv-pdf-panel">
              <div className="rv-pdf-toolbar">
                <FileTextOutlined style={{ color: '#2f54eb' }} />
                <span className="rv-pdf-doc-name">{docName}</span>
                <Tag color={st.color}>{st.label}</Tag>
                <Radio.Group
                  size="small"
                  optionType="button"
                  buttonStyle="solid"
                  value={previewMode}
                  onChange={event => setPreviewMode(event.target.value)}
                  options={[
                    { label: 'PDF 高亮', value: 'pdf' },
                    { label: '段落定位', value: 'blocks' },
                  ]}
                />
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
              {previewMode === 'blocks' ? (
                <AnnotatedDocument
                  blocks={documentBlocks}
                  loading={documentLoading}
                  issuesByBlock={issuesByBlock}
                  focusedBlockIndex={
                    repeatFocus?.issueId === Number(selectedId)
                      ? repeatFocus.blockIndex
                      : null
                  }
                  onSelectIssue={(issueId) => {
                    setSelectedId(issueId)
                    setRepeatFocus(null)
                    setRepeatPdfFocus(null)
                  }}
                />
              ) : (
              <div className="rv-pdf-frame-wrap">
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
                    <span>PDF 预览尚未生成。</span>
                    <Button size="small" type="primary" onClick={() => refreshPdfPreview(false)}>生成 PDF 预览</Button>
                  </div>
                )}
                {pdfBlobUrl && !pdfError && (
                  <div className="rv-pdf-doc-scroll">
                    <div className="rv-pdf-page-controls">
                      <span className="rv-pdf-source-badge">
                        {jobInfo?.source_format === 'pdf' ? '原始 PDF · MinerU 坐标' : 'Word PDF 预览'}
                      </span>
                      <Button size="small" disabled={pdfPage <= 1} onClick={() => {
                        setRepeatPdfFocus(null)
                        setPdfPage(p => Math.max(1, p - 1))
                      }}>上一页</Button>
                      <span>{pdfPage} / {pdfNumPages || '-'}</span>
                      <Button size="small" disabled={!pdfNumPages || pdfPage >= pdfNumPages} onClick={() => {
                        setRepeatPdfFocus(null)
                        setPdfPage(p => Math.min(pdfNumPages, p + 1))
                      }}>下一页</Button>
                      <Button size="small" onClick={() => setPdfScale(s => Math.max(0.9, +(s - 0.1).toFixed(2)))}>-</Button>
                      <span>{Math.round(pdfScale * 100)}%</span>
                      <Button size="small" onClick={() => setPdfScale(s => Math.min(2.4, +(s + 0.1).toFixed(2)))}>+</Button>
                    </div>
                    <div className={`rv-pdf-page-shell ${pdfAreaRects.length ? 'has-area-overlay' : ''}`} ref={pdfPageWrapRef}>
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
                      <div className="rv-pdf-area-layer" style={pdfAreaLayerBox || undefined}>
                        {[...pdfAreaRects, ...pdfNativeRects].map(rect => (
                          <button
                            key={rect.key}
                            type="button"
                            data-issue-id={rect.issueId}
                            className={[
                              'rv-pdf-area',
                              `issue-${rect.issueType}`,
                              rect.stateClass,
                              rect.key.startsWith('native-') ? 'native' : '',
                              rect.role ? `location-${rect.role}` : '',
                              rect.selected ? 'selected' : '',
                              rect.focused ? 'focused' : '',
                            ].filter(Boolean).join(' ')}
                            style={{
                              left: rect.left,
                              top: rect.top,
                              width: rect.width,
                              height: rect.height,
                            }}
                            onClick={() => {
                              setSelectedId(rect.issueId)
                              setRepeatFocus(null)
                              setRepeatPdfFocus(rect.key.startsWith('native-') ? {
                                issueId: rect.issueId,
                                page: pdfPage,
                                sourceUnitId: rect.sourceUnitId || '',
                              } : null)
                            }}
                          />
                        ))}
                      </div>
                    </div>
                  </div>
                )}
              </div>
              )}
            </div>
          )}
        </div>

        {/* ── 右栏：问题详情 + 反馈 ── */}
        <div className="rv-right">
          {selectedIssue ? (
            <IssueDetail
              issue={selectedIssue}
              jobId={jobId}
              userId={uid}
              feedbackState={feedbackMap[selectedIssue.id] || {}}
              customText={customText}
              onCustomTextChange={setCustomText}
              onFeedback={submitFeedback}
              onRevoke={revokeFeedback}
              documentBlocks={documentBlocks}
              onLocateBlocks={locateRepeatBlocks}
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

function buildRepeatLocation(blockIndices = [], documentBlocks = [], fallbackText = '') {
  const indices = [...new Set(
    blockIndices.map(value => Number(value)).filter(value => Number.isFinite(value))
  )].sort((a, b) => a - b)
  const ordered = [...documentBlocks].sort(
    (a, b) => Number(a.block_index) - Number(b.block_index)
  )
  const byIndex = new Map(ordered.map(block => [Number(block.block_index), block]))
  const primary = indices[0]
  const orderPosition = ordered.findIndex(block => Number(block.block_index) === primary)
  const blocks = indices.map(index => byIndex.get(index)).filter(Boolean)
  const heading = orderPosition >= 0
    ? [...ordered.slice(0, orderPosition + 1)].reverse().find(block => block.is_heading && block.text)?.text
    : ''
  const previous = orderPosition > 0 ? ordered[orderPosition - 1]?.text || '' : ''
  const next = orderPosition >= 0 ? ordered[orderPosition + 1]?.text || '' : ''
  return {
    indices,
    heading: heading || '未识别章节标题',
    text: blocks.map(block => block.text || '').filter(Boolean).join('\n') || fallbackText,
    previous,
    next,
  }
}

function DuplicateComparison({ issue, documentBlocks, onLocateBlocks }) {
  const detail = issue.detail || {}
  const allIndices = issue.block_indices || []
  const sourceIndices = detail.source_block_indices?.length
    ? detail.source_block_indices
    : allIndices.slice(0, 1)
  const duplicateIndices = detail.duplicate_block_indices?.length
    ? detail.duplicate_block_indices
    : allIndices.slice(1)
  const sourcePdfLocations = detail.source_pdf_locations || []
  const duplicatePdfLocations = detail.duplicate_pdf_locations || []
  const source = buildRepeatLocation(sourceIndices, documentBlocks, issue.original_text)
  const duplicate = buildRepeatLocation(duplicateIndices, documentBlocks, issue.original_text)
  const locations = [
    { key: 'source', label: '首次出现', tone: 'source', data: source, pdfLocations: sourcePdfLocations },
    { key: 'duplicate', label: '重复出现', tone: 'duplicate', data: duplicate, pdfLocations: duplicatePdfLocations },
  ]
  const hasNativePdfLocations = sourcePdfLocations.length > 0 || duplicatePdfLocations.length > 0

  return (
    <div className="rv-repeat-compare">
      <div className="rv-repeat-summary">
        <span><SwapOutlined /> 两处内容完全一致</span>
        <span>{Number(detail.occurrence_count || 2)} 次出现</span>
        <span>{detail.match_method === 'exact_paragraph' ? '段落级精确匹配' : '重复内容匹配'}</span>
        {hasNativePdfLocations && <span>原始 PDF 坐标已关联</span>}
      </div>
      <div className="rv-repeat-grid">
        {locations.map(location => (
          <div className={`rv-repeat-card ${location.tone}`} key={location.key}>
            <div className="rv-repeat-card-head">
              <span className="rv-repeat-order">{location.label}</span>
              <span className="rv-repeat-blocks">
                {[
                  ...location.pdfLocations.map(item => `原PDF第 ${item.page} 页`),
                  ...location.data.indices.map(index => `Word段落 ${index + 1}`),
                ].join('、') || '位置缺失'}
              </span>
            </div>
            <div className="rv-repeat-heading">{location.data.heading}</div>
            <div className="rv-repeat-text">{location.data.text || '未读取到该位置文本'}</div>
            {(location.data.previous || location.data.next) && (
              <div className="rv-repeat-context">
                {location.data.previous && <span>上文：{location.data.previous.slice(0, 64)}</span>}
                {location.data.next && <span>下文：{location.data.next.slice(0, 64)}</span>}
              </div>
            )}
            <Button
              size="small"
              icon={<EnvironmentOutlined />}
              disabled={!location.data.indices.length && !location.pdfLocations.length}
              onClick={() => onLocateBlocks(issue.id, location.data.indices, location.pdfLocations)}
            >
              定位到{location.label}
            </Button>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── 问题详情面板 ──────────────────────────────────────────────────
function IssueDetail({
  issue,
  jobId,
  userId,
  feedbackState,
  customText,
  onCustomTextChange,
  onFeedback,
  onRevoke,
  documentBlocks,
  onLocateBlocks,
}) {
  const cfg = typeCfg(issue.issue_type)
  const alreadyFed = issue.human_action !== 'pending'
  const { loading } = feedbackState
  const adjudication = issue.detail?.adjudication
  const applyMeta = applyStatusMeta(issue)
  const [similarLoading, setSimilarLoading] = useState(false)
  const [similarItems, setSimilarItems] = useState([])

  async function loadSimilarChunks() {
    if (!jobId || issue.chunk_index === undefined || issue.chunk_index === null) return
    setSimilarLoading(true)
    try {
      const res = await axios.get(`${REVIEW_API}/review-similar/${jobId}/${issue.chunk_index}`, {
        params: { user_id: userId || 'guest', top_k: 5 },
      })
      setSimilarItems(res.data.items || [])
      if (!(res.data.items || []).length) message.info('未找到历史相似片段')
    } catch (err) {
      message.error('历史相似检索失败：' + (err.response?.data?.detail || err.message))
    } finally {
      setSimilarLoading(false)
    }
  }

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

      {issue.issue_type === 'redundancy' && (
        <div className="rv-detail-block">
          <div className="rv-detail-label">重复位置对照</div>
          <DuplicateComparison
            issue={issue}
            documentBlocks={documentBlocks || []}
            onLocateBlocks={onLocateBlocks}
          />
        </div>
      )}

      {/* 原文片段 */}
      {issue.original_text && issue.issue_type !== 'redundancy' && (
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

      {adjudication?.called && (
        <div className="rv-detail-block">
          <div className="rv-detail-label">主智能体裁决</div>
          <div className="rv-adjudication-card">
            <div className="rv-adjudication-summary">
              <span>分歧 {adjudication.conflict_count || 0}</span>
              <span className="is-keep">保留初审 {adjudication.kept_initial || 0}</span>
              <span className="is-drop">采纳复核 {adjudication.accepted_verifier || 0}</span>
              {(adjudication.kept_llm || 0) > 0 && (
                <span className="is-llm">保留 LLM {adjudication.kept_llm}</span>
              )}
              {(adjudication.accepted_numeric_tool || 0) > 0 && (
                <span className="is-tool">采纳数值工具 {adjudication.accepted_numeric_tool}</span>
              )}
              {(adjudication.numeric_issues_added || 0) > 0 && (
                <span className="is-tool">数值新增 {adjudication.numeric_issues_added}</span>
              )}
              {(adjudication.numeric_issues_removed || 0) > 0 && (
                <span className="is-drop">数值删除 {adjudication.numeric_issues_removed}</span>
              )}
              {(adjudication.needs_human || 0) > 0 && (
                <span className="is-human">转人工 {adjudication.needs_human}</span>
              )}
            </div>
            {adjudication.summary && (
              <div className="rv-adjudication-copy">{adjudication.summary}</div>
            )}
            {(adjudication.decisions || []).map(item => (
              <div className="rv-adjudication-decision" key={item.conflict_id}>
                <strong>
                  {item.conflict_id} · {item.decision}
                  {item.conflict_type?.startsWith('numeric_') ? ' · 数值分歧' : ''}
                </strong>
                {item.reason && <span>{item.reason}</span>}
              </div>
            ))}
          </div>
        </div>
      )}

      {issue.detail?.feedback_experience?.length > 0 && (
        <div className="rv-detail-block">
          <div className="rv-detail-label">数据飞轮复用记录</div>
          <div className="rv-feedback-experience-trace">
            {issue.detail.feedback_experience.map(item => (
              <Tag key={item.annotation_id} color={item.action === 'reject' ? 'orange' : 'cyan'}>
                样本 #{item.annotation_id} · {item.action || '反馈'} · 相似度 {Number(item.similarity || 0).toFixed(2)}
              </Tag>
            ))}
          </div>
        </div>
      )}

      {issue.issue_type === 'escalation' && (
        <div className="rv-detail-block rv-escalation-detail">
          <div className="rv-detail-label">升级原因</div>
          <div className="rv-escalation-grid">
            <span>类型</span>
            <strong>{formatEscalationType(issue.escalation_type)}</strong>
            <span>当前结论</span>
            <strong>{issue.status || '不确定'}</strong>
            <span>说明</span>
            <strong>{issue.detail?.summary || issue.reason || '审查链认为该项需要人工复核后才能稳定裁决'}</strong>
          </div>
          {issue.detail?.numeric?.length > 0 && (
            <pre className="rv-detail-json">{JSON.stringify(issue.detail.numeric, null, 2)}</pre>
          )}
        </div>
      )}

      <div className="rv-detail-block">
        <div className="rv-detail-label">历史相似待审片段</div>
        <Button size="small" loading={similarLoading} onClick={loadSimilarChunks}>
          查历史相似片段
        </Button>
        {similarItems.length > 0 && (
          <div className="rv-similar-list">
            {similarItems.map(item => (
              <div className="rv-similar-item" key={`${item.job_id}-${item.chunk_index}-${item.milvus_id}`}>
                <div className="rv-similar-head">
                  <span className="rv-similar-doc">{item.doc_name || item.job_id}</span>
                  <span className="rv-similar-score">{Number(item.score || 0).toFixed(3)}</span>
                </div>
                <div className="rv-similar-meta">
                  Chunk#{Number(item.chunk_index) + 1}
                  {item.chapter ? ` · ${item.chapter}` : ''}
                  {item.section ? ` / ${item.section}` : ''}
                </div>
                <div className="rv-similar-content">
                  {(item.content || '').slice(0, 180)}{(item.content || '').length > 180 ? '…' : ''}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

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
      {applyMeta && (
        <div className={`rv-apply-status rv-apply-${applyMeta.color}`}>
          <Tag color={applyMeta.color}>文档状态：{applyMeta.text}</Tag>
          <span>{applyMeta.note}</span>
        </div>
      )}

      {/* ── 人工裁决 ── */}
      <div className="rv-detail-block rv-hitl-block">
        <div className="rv-detail-label">人工裁决</div>
        <div className={`rv-flywheel-note ${alreadyFed ? 'captured' : ''}`}>
          <span className="rv-flywheel-dot" />
          {alreadyFed
            ? '这次人工裁决已沉淀为反馈样本，后续相似片段审查会检索复用。'
            : '采纳、驳回或人工改写后，将形成可追溯的反馈样本；不会自动修改 Prompt 或 Skill。'}
        </div>

        {alreadyFed ? (
          <div>
            <div className="rv-feedback-applied-row">
              <Tag color={ACTION_LABEL[issue.human_action]?.color || 'default'} style={{ fontSize: 13 }}>
                {ACTION_LABEL[issue.human_action]?.text}
              </Tag>
              {Number(issue.agent_applied) !== 3 ? (
                <Popconfirm
                  title="撤回这次人工裁决？"
                  description={Number(issue.agent_applied) === 1
                    ? '撤回后会回滚已写入 Word 的修改，并恢复为待处理。'
                    : '撤回后该问题会恢复为待处理，可重新采纳、驳回或按意见改写。'}
                  okText="撤回"
                  cancelText="取消"
                  onConfirm={() => onRevoke(issue.id)}
                >
                  <Button size="small" loading={loading}>撤回裁决</Button>
                </Popconfirm>
              ) : (
                <Tooltip title="后台正在改写，完成或失败后才能撤回">
                  <Button size="small" disabled>撤回裁决</Button>
                </Tooltip>
              )}
            </div>
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
















