import './style.css'
import { GlobalWorkerOptions, getDocument } from 'pdfjs-dist/legacy/build/pdf.mjs'
import workerUrl from 'pdfjs-dist/legacy/build/pdf.worker.mjs?url'

function installPdfJsCollectionPolyfills(): void {
  const mapPrototype = Map.prototype as Map<unknown, unknown> & {
    getOrInsertComputed?: (key: unknown, callbackfn: (key: unknown) => unknown) => unknown
    getOrInsert?: (key: unknown, value: unknown) => unknown
  }
  if (!mapPrototype.getOrInsertComputed) {
    mapPrototype.getOrInsertComputed = function (key, callbackfn) {
      if (!this.has(key)) {
        this.set(key, callbackfn(key))
      }
      return this.get(key)
    }
  }
  if (!mapPrototype.getOrInsert) {
    mapPrototype.getOrInsert = function (key, value) {
      if (!this.has(key)) {
        this.set(key, value)
      }
      return this.get(key)
    }
  }

  const weakMapPrototype = WeakMap.prototype as WeakMap<object, unknown> & {
    getOrInsertComputed?: (key: object, callbackfn: (key: object) => unknown) => unknown
    getOrInsert?: (key: object, value: unknown) => unknown
  }
  if (!weakMapPrototype.getOrInsertComputed) {
    weakMapPrototype.getOrInsertComputed = function (key, callbackfn) {
      if (!this.has(key)) {
        this.set(key, callbackfn(key))
      }
      return this.get(key)
    }
  }
  if (!weakMapPrototype.getOrInsert) {
    weakMapPrototype.getOrInsert = function (key, value) {
      if (!this.has(key)) {
        this.set(key, value)
      }
      return this.get(key)
    }
  }
}

installPdfJsCollectionPolyfills()

GlobalWorkerOptions.workerSrc = workerUrl

type BBox = {
  x0: number
  y0: number
  x1: number
  y1: number
  width: number
  height: number
}

type ReaderSource = {
  object_ref: string | null
  context_chain: string[]
  snippet: string
  highlight_start: number
  highlight_end: number
}

type RelatedObjectReference = {
  object_ref: string
  role: string
}

type ObjectDetail = {
  object_ref: string
  kind_label: string
  description: string
  raw_source: string | null
  decoded_stream_preview: string | null
}

type VectorItem = {
  id: string
  kind: 'vector_path'
  page_number: number
  paint_operator: string
  bbox: BBox
  commands: Array<Record<string, unknown>>
  line_width: number
  effective_line_width: number
  source: ReaderSource
  source_comment: string
  reference_chain: RelatedObjectReference[]
  summary: {
    command_count: number
    point_count: number
  }
}

type LinkItem = {
  id: string
  kind: 'link'
  page_number: number
  bbox: BBox
  link: {
    kind: string | null
    target: unknown
    action: unknown
  }
  source: ReaderSource
  source_comment: string
  reference_chain: RelatedObjectReference[]
}

type TextItem = {
  id: string
  kind: 'text'
  page_number: number
  bbox: BBox
  text: {
    content: string
    raw_glyph_text: string | null
    operator: string
    font: string | null
    font_size: number
    decoded_via_tounicode: boolean
  }
  source: ReaderSource
  source_comment: string
  reference_chain: RelatedObjectReference[]
  summary: {
    char_count: number
  }
}

type ImageItem = {
  id: string
  kind: 'image'
  page_number: number
  bbox: BBox
  image: {
    name: string
    pixel_width: number | null
    pixel_height: number | null
    filters: string[]
    object_ref: string
  }
  source: ReaderSource
  source_comment: string
  reference_chain: RelatedObjectReference[]
  summary: {
    draw_width: number
    draw_height: number
  }
}

type ReaderItem = VectorItem | LinkItem | TextItem | ImageItem

type OverlayKind = ReaderItem['kind']

const LAYER_STORAGE_KEY = 'pdf-reader-overlay-layers'

function loadLayerVisibility(): Record<OverlayKind, boolean> {
  const defaults: Record<OverlayKind, boolean> = {
    vector_path: true,
    text: true,
    image: true,
    link: true,
  }
  try {
    const raw = localStorage.getItem(LAYER_STORAGE_KEY)
    if (!raw) {
      return defaults
    }
    const parsed = JSON.parse(raw) as Partial<Record<OverlayKind, boolean>>
    return { ...defaults, ...parsed }
  } catch {
    return defaults
  }
}

function saveLayerVisibility(visibility: Record<OverlayKind, boolean>): void {
  try {
    localStorage.setItem(LAYER_STORAGE_KEY, JSON.stringify(visibility))
  } catch {
    /* ignore quota / private mode */
  }
}

let layerVisibility = loadLayerVisibility()

function isReaderItem(item: { kind: string }): item is ReaderItem {
  return (
    item.kind === 'vector_path' ||
    item.kind === 'link' ||
    item.kind === 'text' ||
    item.kind === 'image'
  )
}

function isLayerVisible(kind: ReaderItem['kind']): boolean {
  return layerVisibility[kind]
}

type PageManifest = {
  page_number: number
  page_size: {
    width_pt: number
    height_pt: number
  }
  item_counts: {
    vector_path: number
    text: number
    image: number
    link: number
  }
  warnings: string[]
  data_url: string
}

type ReaderDocument = {
  id: string
  title: string
  pdf_url: string
  page_count: number
  pages: PageManifest[]
  resolved_object_count: number
  header: string
}

type Manifest = {
  documents: ReaderDocument[]
}

type PageData = {
  page_number: number
  page_object_ref: string | null
  page_size: {
    width_pt: number
    height_pt: number
  }
  content_streams: string[]
  item_counts: {
    vector_path: number
    text: number
    image: number
    link: number
  }
  warnings: string[]
  object_details: Record<string, ObjectDetail>
  items: ReaderItem[]
}

type PageState = {
  pageNumber: number
  pageData: PageData
  canvas: HTMLCanvasElement
  overlay: HTMLDivElement
  scale: number
}

type ZoomAnchor = {
  documentX: number
  documentY: number
  viewportX: number
  viewportY: number
}

type RenderPageOptions = {
  preserveSelection?: boolean
  anchor?: ZoomAnchor | null
}

function mustQuery<T extends Element>(selector: string): T {
  const element = document.querySelector<T>(selector)
  if (!element) {
    throw new Error(`Missing required element: ${selector}`)
  }
  return element
}

const app = mustQuery<HTMLDivElement>('#app')

app.innerHTML = `
  <div class="layout">
    <aside class="sidebar">
      <div class="panel">
        <div class="panel-title-row">
          <div>
            <p class="eyebrow">PDF source reader</p>
            <h1 class="title">Eplan PDF Object Explorer</h1>
          </div>
        </div>
        <p class="muted">
          Choose any PDF from data/eplans. The viewer renders the original PDF and overlays clickable vector and hyperlink regions.
        </p>
      </div>

      <div class="panel">
        <label class="field-label" for="doc-select">Document</label>
        <select id="doc-select" class="select"></select>
        <div id="doc-meta" class="meta-list"></div>
      </div>

      <div class="panel">
        <div class="page-toolbar">
          <button id="prev-page" class="button" type="button">Previous</button>
          <button id="next-page" class="button" type="button">Next</button>
        </div>
        <label class="field-label" for="page-select">Page</label>
        <select id="page-select" class="select"></select>
        <div id="page-meta" class="meta-list"></div>
      </div>

      <div class="panel">
        <p class="field-label">Overlay layers</p>
        <div class="legend">
          <label class="legend-item">
            <input type="checkbox" class="layer-toggle" data-layer-kind="vector_path" checked />
            <i class="legend-swatch vector" aria-hidden="true"></i>
            <span>Vector path</span>
          </label>
          <label class="legend-item">
            <input type="checkbox" class="layer-toggle" data-layer-kind="text" checked />
            <i class="legend-swatch text" aria-hidden="true"></i>
            <span>Text block</span>
          </label>
          <label class="legend-item">
            <input type="checkbox" class="layer-toggle" data-layer-kind="image" checked />
            <i class="legend-swatch image" aria-hidden="true"></i>
            <span>Image</span>
          </label>
          <label class="legend-item">
            <input type="checkbox" class="layer-toggle" data-layer-kind="link" checked />
            <i class="legend-swatch link" aria-hidden="true"></i>
            <span>Hyperlink</span>
          </label>
          <div class="legend-note">
            <span><i class="legend-swatch selected" aria-hidden="true"></i>Selected (when clicking a visible region)</span>
          </div>
        </div>
        <p class="muted small">
          Uncheck a layer to hide its highlights on the page. Click a visible region to inspect PDF source on the right.
        </p>
      </div>
    </aside>

    <main class="workspace">
      <section class="viewer-shell">
        <div class="viewer-topbar">
          <div id="viewer-status" class="status">Loading manifest...</div>
          <div class="viewer-toolbar">
            <label class="zoom-control" for="zoom-input">
              <span>Zoom</span>
              <input id="zoom-input" class="zoom-input" type="number" min="50" max="400" step="5" value="100" />
              <span>%</span>
            </label>
            <span id="zoom-label" class="zoom-label">Fit width</span>
            <span class="viewer-hint">Wheel to zoom, drag with left mouse button to pan</span>
          </div>
        </div>
        <div id="viewer-scroll" class="viewer-scroll">
          <div id="viewer-stage" class="viewer-stage"></div>
        </div>
      </section>

      <aside class="inspector">
        <div class="panel inspector-panel">
          <p class="eyebrow">Source mapping</p>
          <h2 id="selection-title" class="section-title">No object selected</h2>
          <div id="selection-meta" class="meta-list"></div>
          <div id="source-comment" class="source-comment"></div>
          <pre id="source-code" class="source-code empty">Click a region in the PDF viewer to show the matching PDF source snippet here.</pre>
          <div id="reference-chain" class="reference-chain"></div>
          <div id="warnings" class="warnings"></div>
        </div>
      </aside>
    </main>
  </div>
`

const docSelect = mustQuery<HTMLSelectElement>('#doc-select')
const pageSelect = mustQuery<HTMLSelectElement>('#page-select')
const prevPageButton = mustQuery<HTMLButtonElement>('#prev-page')
const nextPageButton = mustQuery<HTMLButtonElement>('#next-page')
const docMeta = mustQuery<HTMLDivElement>('#doc-meta')
const pageMeta = mustQuery<HTMLDivElement>('#page-meta')
const viewerStatus = mustQuery<HTMLDivElement>('#viewer-status')
const viewerScroll = mustQuery<HTMLDivElement>('#viewer-scroll')
const viewerStage = mustQuery<HTMLDivElement>('#viewer-stage')
const zoomInput = mustQuery<HTMLInputElement>('#zoom-input')
const zoomLabel = mustQuery<HTMLSpanElement>('#zoom-label')
const selectionTitle = mustQuery<HTMLHeadingElement>('#selection-title')
const selectionMeta = mustQuery<HTMLDivElement>('#selection-meta')
const sourceComment = mustQuery<HTMLDivElement>('#source-comment')
const sourceCode = mustQuery<HTMLPreElement>('#source-code')
const referenceChain = mustQuery<HTMLDivElement>('#reference-chain')
const warningsEl = mustQuery<HTMLDivElement>('#warnings')

const pageCache = new Map<string, PageData>()
const pdfDocumentCache = new Map<string, Promise<Awaited<ReturnType<typeof loadPdfDocument>>>>()
let manifest: Manifest | null = null
let activeDocument: ReaderDocument | null = null
let activePageNumber = 1
let activeSelectionId: string | null = null
let currentPageState: PageState | null = null
let zoomFactor = 1
let fitScale = 1
let renderSequence = 0
let scheduledRenderTimer: number | null = null
let isPanning = false
let panStartX = 0
let panStartY = 0
let panScrollLeft = 0
let panScrollTop = 0

const MIN_ZOOM_FACTOR = 0.5
const MAX_ZOOM_FACTOR = 4

function escapeHtml(text: string): string {
  return text
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;')
}

function renderMeta(container: HTMLElement, items: Array<[string, string]>): void {
  if (!items.length) {
    container.innerHTML = ''
    return
  }
  container.innerHTML = items
    .map(([label, value]) => `<div class="meta-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`)
    .join('')
}

function formatJson(value: unknown): string {
  if (value == null) {
    return 'null'
  }
  if (typeof value === 'string') {
    return value
  }
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function highlightSource(source: ReaderSource): string {
  const start = Math.max(0, Math.min(source.highlight_start, source.snippet.length))
  const end = Math.max(start, Math.min(source.highlight_end, source.snippet.length))
  const before = escapeHtml(source.snippet.slice(0, start))
  const hit = escapeHtml(source.snippet.slice(start, end))
  const after = escapeHtml(source.snippet.slice(end))
  return `${before}<mark>${hit || ' '}</mark>${after}`
}

function getPageManifest(pageNumber: number): PageManifest | undefined {
  return activeDocument?.pages.find((page) => page.page_number === pageNumber)
}

function pageKey(documentId: string, pageNumber: number): string {
  return `${documentId}:${pageNumber}`
}

async function loadManifest(): Promise<Manifest> {
  const response = await fetch('/reader-data/manifest.json')
  if (!response.ok) {
    throw new Error(`Failed to load manifest: ${response.status}`)
  }
  return (await response.json()) as Manifest
}

async function loadPdfDocument(url: string) {
  const task = getDocument(url)
  return await task.promise
}

function normalizePageItems(data: PageData): PageData {
  return {
    ...data,
    items: data.items.filter((item): item is ReaderItem => isReaderItem(item)),
  }
}

async function loadPageData(documentId: string, page: PageManifest): Promise<PageData> {
  const key = pageKey(documentId, page.page_number)
  if (pageCache.has(key)) {
    return normalizePageItems(pageCache.get(key) as PageData)
  }
  const response = await fetch(page.data_url)
  if (!response.ok) {
    throw new Error(`Failed to load page data: ${response.status}`)
  }
  const raw = (await response.json()) as PageData
  const data = normalizePageItems(raw)
  pageCache.set(key, data)
  return data
}

async function getPdfDocument(url: string) {
  if (!pdfDocumentCache.has(url)) {
    pdfDocumentCache.set(url, loadPdfDocument(url))
  }
  return await (pdfDocumentCache.get(url) as Promise<Awaited<ReturnType<typeof loadPdfDocument>>>)
}

function setStatus(message: string): void {
  viewerStatus.textContent = message
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max)
}

function getCurrentRenderScale(): number {
  return currentPageState?.scale ?? fitScale * zoomFactor
}

function updateZoomLabel(): void {
  const zoomPercent = Math.round(zoomFactor * 100)
  zoomInput.value = String(zoomPercent)
  zoomLabel.textContent = Math.abs(zoomFactor - 1) < 0.001 ? 'Fit width' : `${zoomPercent}% of fit width`
}

function countOrZero(value: number | undefined): number {
  return typeof value === 'number' ? value : 0
}

function getAvailableViewerWidth(): number {
  const style = window.getComputedStyle(viewerScroll)
  const padding =
    Number.parseFloat(style.paddingLeft || '0') + Number.parseFloat(style.paddingRight || '0')
  return Math.max((viewerScroll.clientWidth || 960) - padding - 28, 320)
}

function scheduleRender(pageNumber: number, options: RenderPageOptions = {}): void {
  if (scheduledRenderTimer !== null) {
    window.clearTimeout(scheduledRenderTimer)
  }
  scheduledRenderTimer = window.setTimeout(() => {
    scheduledRenderTimer = null
    void renderPage(pageNumber, options)
  }, 40)
}

function getViewportAnchorFromClientPoint(clientX: number, clientY: number): ZoomAnchor | null {
  const rect = viewerScroll.getBoundingClientRect()
  const viewportX = clientX - rect.left
  const viewportY = clientY - rect.top
  if (viewportX < 0 || viewportY < 0 || viewportX > rect.width || viewportY > rect.height) {
    return null
  }
  const scale = getCurrentRenderScale()
  if (scale <= 0) {
    return null
  }
  return {
    documentX: (viewerScroll.scrollLeft + viewportX) / scale,
    documentY: (viewerScroll.scrollTop + viewportY) / scale,
    viewportX,
    viewportY,
  }
}

function getViewportCenterAnchor(): ZoomAnchor | null {
  const rect = viewerScroll.getBoundingClientRect()
  if (!rect.width || !rect.height) {
    return null
  }
  const scale = getCurrentRenderScale()
  if (scale <= 0) {
    return null
  }
  const viewportX = rect.width / 2
  const viewportY = rect.height / 2
  return {
    documentX: (viewerScroll.scrollLeft + viewportX) / scale,
    documentY: (viewerScroll.scrollTop + viewportY) / scale,
    viewportX,
    viewportY,
  }
}

function applyAnchorScroll(anchor: ZoomAnchor | null, scale: number): void {
  if (!anchor) {
    viewerScroll.scrollLeft = 0
    viewerScroll.scrollTop = 0
    return
  }

  viewerScroll.scrollLeft = Math.max(anchor.documentX * scale - anchor.viewportX, 0)
  viewerScroll.scrollTop = Math.max(anchor.documentY * scale - anchor.viewportY, 0)
}

function renderReferenceChain(item: ReaderItem, pageData: PageData): void {
  if (!item.reference_chain.length) {
    referenceChain.innerHTML = ''
    return
  }

  referenceChain.innerHTML = [
    '<div class="reference-section-title">Reference chain</div>',
    ...item.reference_chain.map((entry) => {
      const detail = pageData.object_details[entry.object_ref]
      const description = detail
        ? `<p class="reference-description"><strong>${escapeHtml(detail.kind_label)}.</strong> ${escapeHtml(detail.description)}</p>`
        : '<p class="reference-description">Object details are not available for this reference.</p>'
      const rawSource = detail?.raw_source
        ? `<pre class="reference-source">${escapeHtml(detail.raw_source)}</pre>`
        : '<div class="reference-empty">No raw object preview available.</div>'
      const decodedPreview = detail?.decoded_stream_preview
        ? `
          <details class="reference-details">
            <summary>Decoded stream preview</summary>
            <pre class="reference-source">${escapeHtml(detail.decoded_stream_preview)}</pre>
          </details>
        `
        : ''

      return `
        <section class="reference-card">
          <div class="reference-card-header">
            <strong>${escapeHtml(entry.object_ref)}</strong>
            <span>${escapeHtml(entry.role)}</span>
          </div>
          ${description}
          ${rawSource}
          ${decodedPreview}
        </section>
      `
    }),
  ].join('')
}

function setSelection(item: ReaderItem | null, pageData: PageData | null): void {
  activeSelectionId = item?.id ?? null
  selectionMeta.innerHTML = ''
  sourceComment.innerHTML = ''
  warningsEl.innerHTML = ''
  referenceChain.innerHTML = ''

  if (!item || !pageData) {
    selectionTitle.textContent = 'No object selected'
    sourceCode.classList.add('empty')
    sourceCode.textContent = 'Click a region in the PDF viewer to show the matching PDF source snippet here.'
    syncSelectionClasses()
    return
  }

  const titleByKind: Record<ReaderItem['kind'], string> = {
    vector_path: 'Vector object',
    text: 'Text object',
    image: 'Image object',
    link: 'Hyperlink',
  }
  selectionTitle.textContent = `${titleByKind[item.kind]} ${item.id}`
  sourceCode.classList.remove('empty')
  sourceCode.innerHTML = highlightSource(item.source)
  sourceComment.innerHTML = item.source_comment
    ? `<div class="source-note">${escapeHtml(item.source_comment)}</div>`
    : ''

  const metaRows: Array<[string, string]> = [
    ['Page', String(item.page_number)],
    ['Object ref', item.source.object_ref ?? 'None'],
    ['Content stream', pageData.content_streams.join(', ') || 'None'],
  ]

  if (item.kind === 'vector_path') {
    metaRows.push(['Type', 'Vector path'])
    metaRows.push(['Paint op', item.paint_operator])
    metaRows.push(['Command count', String(item.summary.command_count)])
    metaRows.push(['Point count', String(item.summary.point_count)])
  } else if (item.kind === 'link') {
    metaRows.push(['Type', 'Link annotation'])
    metaRows.push(['Link kind', item.link.kind ?? 'unknown'])
  } else if (item.kind === 'text') {
    metaRows.push(['Type', 'Text block'])
    metaRows.push(['Text', item.text.content])
    metaRows.push(['Text op', item.text.operator])
    metaRows.push(['Font', item.text.font ?? 'unknown'])
    metaRows.push(['Font size', String(item.text.font_size)])
    metaRows.push(['Chars', String(item.summary.char_count)])
    metaRows.push(['Decoded via ToUnicode', item.text.decoded_via_tounicode ? 'Yes' : 'No'])
    if (item.text.raw_glyph_text) {
      metaRows.push(['Raw glyph text', item.text.raw_glyph_text])
    }
  } else if (item.kind === 'image') {
    metaRows.push(['Type', 'Image XObject'])
    metaRows.push(['Image name', item.image.name])
    metaRows.push(['Image object', item.image.object_ref])
    metaRows.push(['Pixel size', `${item.image.pixel_width ?? '?'} × ${item.image.pixel_height ?? '?'}`])
    metaRows.push(['Filters', item.image.filters.join(', ') || 'None'])
    metaRows.push(['Draw size (pt)', `${item.summary.draw_width} × ${item.summary.draw_height}`])
  }

  metaRows.push(['BBox', `${item.bbox.x0}, ${item.bbox.y0}, ${item.bbox.x1}, ${item.bbox.y1}`])
  renderMeta(selectionMeta, metaRows)
  renderReferenceChain(item, pageData)

  if (item.kind === 'link') {
    warningsEl.innerHTML = `
      <div class="warning-card">
        <strong>Navigation target</strong>
        <pre>${escapeHtml(formatJson(item.link.target ?? item.link.action))}</pre>
      </div>
    `
  } else if (item.kind === 'text' && item.text.raw_glyph_text) {
    warningsEl.innerHTML = `
      <div class="warning-card">
        <strong>Raw glyph text</strong>
        <pre>${escapeHtml(item.text.raw_glyph_text)}</pre>
      </div>
    `
  } else if (pageData.warnings.length) {
    warningsEl.innerHTML = pageData.warnings
      .map((warning) => `<div class="warning-card">${escapeHtml(warning)}</div>`)
      .join('')
  }

  syncSelectionClasses()
}

function syncSelectionClasses(): void {
  document.querySelectorAll<HTMLElement>('.overlay-item').forEach((node) => {
    const isSelected = node.dataset.itemId === activeSelectionId
    node.classList.toggle('is-selected', isSelected)
    node.style.zIndex = isSelected ? '10' : node.dataset.baseZIndex ?? '1'
  })
}

function itemArea(item: ReaderItem): number {
  return Math.max(item.bbox.width * item.bbox.height, 1)
}

function compareHitPriority(a: ReaderItem, b: ReaderItem): number {
  const kindWeight: Record<ReaderItem['kind'], number> = {
    link: 4,
    text: 3,
    image: 2,
    vector_path: 1,
  }
  const kindDiff = kindWeight[b.kind] - kindWeight[a.kind]
  if (kindDiff !== 0) {
    return kindDiff
  }

  const areaDiff = itemArea(a) - itemArea(b)
  if (areaDiff !== 0) {
    return areaDiff
  }

  return a.id.localeCompare(b.id)
}

function getRenderedBounds(item: ReaderItem, scale: number, pageHeight: number) {
  const left = item.bbox.x0 * scale
  const top = (pageHeight - item.bbox.y1) * scale
  const minSize = item.kind === 'link' ? 8 : item.kind === 'text' || item.kind === 'image' ? 6 : 4
  const width = Math.max(item.bbox.width * scale, minSize)
  const height = Math.max(item.bbox.height * scale, minSize)

  return { left, top, width, height }
}

function getHitCandidates(pageData: PageData, hitX: number, hitY: number, scale: number): ReaderItem[] {
  return pageData.items
    .filter((item) => isLayerVisible(item.kind))
    .filter((item) => {
      const bounds = getRenderedBounds(item, scale, pageData.page_size.height_pt)
      return (
        hitX >= bounds.left &&
        hitX <= bounds.left + bounds.width &&
        hitY >= bounds.top &&
        hitY <= bounds.top + bounds.height
      )
    })
    .sort(compareHitPriority)
}

function selectItemAtPoint(pageData: PageData, hitX: number, hitY: number, scale: number): void {
  const candidates = getHitCandidates(pageData, hitX, hitY, scale)
  if (!candidates.length) {
    setSelection(null, null)
    return
  }

  let nextItem = candidates[0]
  const currentIndex = candidates.findIndex((item) => item.id === activeSelectionId)
  if (currentIndex >= 0) {
    nextItem = candidates[(currentIndex + 1) % candidates.length]
  }

  setSelection(nextItem, pageData)
}

function createOverlayItem(item: ReaderItem, scale: number, pageHeight: number): HTMLDivElement {
  const node = document.createElement('div')
  const overlayClass =
    item.kind === 'link'
      ? 'link'
      : item.kind === 'text'
        ? 'text'
        : item.kind === 'image'
          ? 'image'
          : 'vector'
  node.className = `overlay-item overlay-${overlayClass}`
  node.dataset.itemId = item.id
  node.dataset.baseZIndex =
    item.kind === 'link' ? '5' : item.kind === 'text' ? '4' : item.kind === 'image' ? '3' : '2'
  node.title =
    item.kind === 'link'
      ? `${item.link.kind ?? 'link'} | ${item.source.object_ref ?? 'no ref'}`
      : item.kind === 'text'
        ? `${item.text.content} | ${item.source.object_ref ?? 'no ref'}`
        : item.kind === 'image'
          ? `${item.image.name} | ${item.image.object_ref}`
          : `${item.paint_operator} | ${item.source.object_ref ?? 'no ref'}`

  const bounds = getRenderedBounds(item, scale, pageHeight)

  node.style.left = `${bounds.left}px`
  node.style.top = `${bounds.top}px`
  node.style.width = `${bounds.width}px`
  node.style.height = `${bounds.height}px`
  node.style.zIndex = node.dataset.baseZIndex

  return node
}

async function renderPage(pageNumber: number, options: RenderPageOptions = {}): Promise<void> {
  if (!activeDocument) {
    return
  }

  const pageManifest = getPageManifest(pageNumber)
  if (!pageManifest) {
    throw new Error(`Page ${pageNumber} not found.`)
  }

  const renderId = ++renderSequence
  setStatus(`Rendering page ${pageNumber}...`)
  viewerStage.innerHTML = ''
  currentPageState = null
  if (!options.preserveSelection) {
    setSelection(null, null)
  }

  const [pageData, pdf] = await Promise.all([
    loadPageData(activeDocument.id, pageManifest),
    getPdfDocument(activeDocument.pdf_url),
  ])
  if (renderId !== renderSequence) {
    return
  }
  if (activeSelectionId) {
    const selected = pageData.items.find((item) => item.id === activeSelectionId)
    if (selected && !isLayerVisible(selected.kind)) {
      activeSelectionId = null
    }
  }
  const pdfPage = await pdf.getPage(pageNumber)
  if (renderId !== renderSequence) {
    return
  }
  const desiredWidth = Math.min(getAvailableViewerWidth(), 1400)
  const baseViewport = pdfPage.getViewport({ scale: 1 })
  fitScale = desiredWidth / baseViewport.width
  const scale = fitScale * zoomFactor
  const viewport = pdfPage.getViewport({ scale })

  const pageShell = document.createElement('section')
  pageShell.className = 'page-shell'
  pageShell.dataset.pageNumber = String(pageNumber)

  const pageHeader = document.createElement('div')
  pageHeader.className = 'page-header'
  pageHeader.innerHTML = `
    <div>
      <strong>Page ${pageNumber}</strong>
      <span>${countOrZero(pageManifest.item_counts.vector_path)} vector, ${countOrZero(pageManifest.item_counts.text)} text, ${countOrZero(pageManifest.item_counts.image)} image, ${countOrZero(pageManifest.item_counts.link)} link</span>
    </div>
  `
  pageShell.appendChild(pageHeader)

  const surface = document.createElement('div')
  surface.className = 'page-surface'
  surface.style.width = `${viewport.width}px`
  surface.style.height = `${viewport.height}px`

  const canvas = document.createElement('canvas')
  canvas.width = Math.floor(viewport.width)
  canvas.height = Math.floor(viewport.height)
  canvas.style.width = `${viewport.width}px`
  canvas.style.height = `${viewport.height}px`

  const overlay = document.createElement('div')
  overlay.className = 'page-overlay'
  overlay.style.width = `${viewport.width}px`
  overlay.style.height = `${viewport.height}px`
  overlay.addEventListener('click', (event) => {
    const rect = overlay.getBoundingClientRect()
    const hitX = event.clientX - rect.left
    const hitY = event.clientY - rect.top
    selectItemAtPoint(pageData, hitX, hitY, scale)
  })

  surface.append(canvas, overlay)
  pageShell.appendChild(surface)
  viewerStage.appendChild(pageShell)

  const ctx = canvas.getContext('2d')
  if (!ctx) {
    throw new Error('Canvas 2D context not available.')
  }

  await pdfPage.render({ canvas: null, canvasContext: ctx, viewport }).promise
  if (renderId !== renderSequence) {
    return
  }

  const items = [...pageData.items].sort((a, b) => compareHitPriority(b, a))
  items.forEach((item) => {
    if (!isLayerVisible(item.kind)) {
      return
    }
    overlay.appendChild(createOverlayItem(item, scale, pageData.page_size.height_pt))
  })

  currentPageState = {
    pageNumber,
    pageData,
    canvas,
    overlay,
    scale,
  }

  renderMeta(pageMeta, [
    ['Page object', pageData.page_object_ref ?? 'None'],
    ['Content streams', pageData.content_streams.join(', ') || 'None'],
    ['Vector objects', String(countOrZero(pageData.item_counts.vector_path))],
    ['Text objects', String(countOrZero(pageData.item_counts.text))],
    ['Image objects', String(countOrZero(pageData.item_counts.image))],
    ['Link objects', String(countOrZero(pageData.item_counts.link))],
    ['Page size (pt)', `${pageData.page_size.width_pt} × ${pageData.page_size.height_pt}`],
  ])

  applyAnchorScroll(options.anchor ?? null, scale)
  updateZoomLabel()

  const selectedItem = activeSelectionId
    ? pageData.items.find((item) => item.id === activeSelectionId) ?? null
    : null
  if (selectedItem) {
    setSelection(selectedItem, pageData)
  } else if (pageData.warnings.length) {
    warningsEl.innerHTML = pageData.warnings
      .map((warning) => `<div class="warning-card">${escapeHtml(warning)}</div>`)
      .join('')
  } else if (!options.preserveSelection) {
    warningsEl.innerHTML = ''
  }

  setStatus(`Page ${pageNumber} rendered. Click a highlighted region to inspect its PDF source.`)
  syncSelectionClasses()
}

function populateDocumentSelect(documents: ReaderDocument[]): void {
  docSelect.innerHTML = documents
    .map((doc) => `<option value="${escapeHtml(doc.id)}">${escapeHtml(doc.title)}</option>`)
    .join('')
}

function populatePageSelect(documentData: ReaderDocument): void {
  pageSelect.innerHTML = documentData.pages
    .map(
      (page) =>
        `<option value="${page.page_number}">Page ${page.page_number} | Vector ${countOrZero(page.item_counts.vector_path)} | Text ${countOrZero(page.item_counts.text)} | Image ${countOrZero(page.item_counts.image)} | Links ${countOrZero(page.item_counts.link)}</option>`,
    )
    .join('')
}

async function selectDocument(documentId: string): Promise<void> {
  if (!manifest) {
    return
  }
  const documentData = manifest.documents.find((doc) => doc.id === documentId)
  if (!documentData) {
    throw new Error(`Document ${documentId} not found.`)
  }

  activeDocument = documentData
  activePageNumber = 1
  zoomFactor = 1
  populatePageSelect(documentData)
  pageSelect.value = '1'
  renderMeta(docMeta, [
    ['PDF header', documentData.header],
    ['Pages', String(documentData.page_count)],
    ['Resolved objects', String(documentData.resolved_object_count)],
    ['PDF URL', documentData.pdf_url],
  ])
  updateZoomLabel()
  await renderPage(activePageNumber)
}

async function selectPage(pageNumber: number): Promise<void> {
  if (!activeDocument) {
    return
  }
  activePageNumber = pageNumber
  pageSelect.value = String(pageNumber)
  await renderPage(pageNumber)
}

function updatePagerButtons(): void {
  if (!activeDocument) {
    prevPageButton.disabled = true
    nextPageButton.disabled = true
    return
  }
  prevPageButton.disabled = activePageNumber <= 1
  nextPageButton.disabled = activePageNumber >= activeDocument.page_count
}

function changeZoom(nextZoomFactor: number, anchor: ZoomAnchor | null): void {
  const clampedZoom = clamp(nextZoomFactor, MIN_ZOOM_FACTOR, MAX_ZOOM_FACTOR)
  if (Math.abs(clampedZoom - zoomFactor) < 0.001) {
    return
  }
  zoomFactor = clampedZoom
  updateZoomLabel()
  scheduleRender(activePageNumber, { preserveSelection: true, anchor })
}

docSelect.addEventListener('change', async () => {
  await selectDocument(docSelect.value)
  updatePagerButtons()
})

pageSelect.addEventListener('change', async () => {
  await selectPage(Number(pageSelect.value))
  updatePagerButtons()
})

prevPageButton.addEventListener('click', async () => {
  if (activePageNumber > 1) {
    await selectPage(activePageNumber - 1)
    updatePagerButtons()
  }
})

nextPageButton.addEventListener('click', async () => {
  if (activeDocument && activePageNumber < activeDocument.page_count) {
    await selectPage(activePageNumber + 1)
    updatePagerButtons()
  }
})

zoomInput.addEventListener('change', () => {
  const rawValue = Number.parseFloat(zoomInput.value)
  if (!Number.isFinite(rawValue)) {
    updateZoomLabel()
    return
  }
  const nextZoomFactor = clamp(rawValue / 100, MIN_ZOOM_FACTOR, MAX_ZOOM_FACTOR)
  changeZoom(nextZoomFactor, getViewportCenterAnchor())
})

zoomInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') {
    zoomInput.blur()
  }
})

viewerScroll.addEventListener(
  'wheel',
  (event) => {
    if (!activeDocument) {
      return
    }
    event.preventDefault()
    const anchor = getViewportAnchorFromClientPoint(event.clientX, event.clientY) ?? getViewportCenterAnchor()
    const zoomDelta = Math.exp(-event.deltaY * 0.0015)
    changeZoom(zoomFactor * zoomDelta, anchor)
  },
  { passive: false },
)

viewerScroll.addEventListener('pointerdown', (event) => {
  if (event.button !== 0) {
    return
  }
  const target = event.target as HTMLElement | null
  if (target?.closest('.overlay-item, .button, .select')) {
    return
  }
  isPanning = true
  panStartX = event.clientX
  panStartY = event.clientY
  panScrollLeft = viewerScroll.scrollLeft
  panScrollTop = viewerScroll.scrollTop
  viewerScroll.classList.add('is-panning')
})

window.addEventListener('pointermove', (event) => {
  if (!isPanning) {
    return
  }
  viewerScroll.scrollLeft = panScrollLeft - (event.clientX - panStartX)
  viewerScroll.scrollTop = panScrollTop - (event.clientY - panStartY)
})

window.addEventListener('pointerup', () => {
  isPanning = false
  viewerScroll.classList.remove('is-panning')
})

window.addEventListener('pointercancel', () => {
  isPanning = false
  viewerScroll.classList.remove('is-panning')
})

window.addEventListener('resize', async () => {
  if (activeDocument) {
    scheduleRender(activePageNumber, { preserveSelection: true, anchor: getViewportCenterAnchor() })
  }
})

function initLayerToggles(): void {
  document.querySelectorAll<HTMLInputElement>('.layer-toggle').forEach((input) => {
    const kind = input.dataset.layerKind as OverlayKind | undefined
    if (!kind || !(kind in layerVisibility)) {
      return
    }
    input.checked = layerVisibility[kind]
    input.addEventListener('change', () => {
      layerVisibility = { ...layerVisibility, [kind]: input.checked }
      saveLayerVisibility(layerVisibility)
      if (activeDocument) {
        if (activeSelectionId && currentPageState?.pageData) {
          const sel = currentPageState.pageData.items.find((item) => item.id === activeSelectionId)
          if (sel && !isLayerVisible(sel.kind)) {
            activeSelectionId = null
          }
        }
        scheduleRender(activePageNumber, { preserveSelection: true })
      }
    })
  })
}

async function bootstrap(): Promise<void> {
  try {
    initLayerToggles()
    manifest = await loadManifest()
    if (!manifest.documents.length) {
      setStatus('No PDF data available. Run the data generation script first.')
      return
    }
    populateDocumentSelect(manifest.documents)
    await selectDocument(manifest.documents[0].id)
    updatePagerButtons()
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    setStatus(`Load failed: ${message}`)
  }
}

void bootstrap()
