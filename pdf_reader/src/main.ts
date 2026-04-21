import './style.css'
import { GlobalWorkerOptions, getDocument } from 'pdfjs-dist/legacy/build/pdf.mjs'
import workerUrl from 'pdfjs-dist/legacy/build/pdf.worker.mjs?url'
import {
  commandsToPolylines,
  parseSnippetToPathCommands,
  pathCommandsToQueryRecords,
  searchSimilarVectorPaths,
  type ShapeSearchHit,
} from './shapeSearch'

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

type ComponentItem = {
  id: string
  kind: 'component'
  page_number: number
  bbox: BBox
  component: {
    label?: string
    nearby_text?: string[]
    detection_source?: string
    link_kind?: string | null
    target?: unknown
    annotation_refs?: string[]
    action_refs?: string[]
    duplicate_count?: number
  }
  source: ReaderSource
  source_comment: string
  reference_chain: RelatedObjectReference[]
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

function isComponentItem(item: { kind: string }): item is ComponentItem {
  return item.kind === 'component'
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
    component?: number
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
    component?: number
    vector_path: number
    text: number
    image: number
    link: number
  }
  warnings: string[]
  object_details: Record<string, ObjectDetail>
  items: ReaderItem[]
  components: ComponentItem[]
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

type PlaygroundBounds = {
  minX: number
  minY: number
  maxX: number
  maxY: number
  width: number
  height: number
}

type PlaygroundData = {
  bounds: PlaygroundBounds
  polylines: Array<{
    points: [number, number][]
    closed: boolean
  }>
}

type PlaygroundView = {
  scale: number
  offsetX: number
  offsetY: number
}

type PlaygroundHover = {
  x: number
  y: number
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

      <div class="panel shape-search-panel">
        <p class="field-label">Vector Shape Search</p>
        <p class="muted small">
          Match by geometric shape while ignoring translation, uniform scaling, and rotation (with optional mirror).
          Paste a path snippet using <code>m</code>/<code>l</code>/<code>c</code>/<code>re</code>, or select a vector on the page and search from selection.
        </p>
        <textarea
          id="shape-search-snippet"
          class="shape-search-textarea"
          rows="4"
          spellcheck="false"
          placeholder="Example: 19.724 841.89 m&#10;319.724 830.551 l&#10;S"
        ></textarea>
        <div class="shape-search-row">
          <label class="shape-search-tolerance" for="shape-search-tolerance">
            Tolerance (higher = looser)
            <input id="shape-search-tolerance" type="range" min="5" max="35" value="15" />
            <span id="shape-search-tolerance-label">15%</span>
          </label>
        </div>
        <label class="legend-item shape-search-mirror">
          <input type="checkbox" id="shape-search-mirror" checked />
          <span>Allow mirror (flip)</span>
        </label>
        <div class="shape-search-actions">
          <button id="shape-search-from-snippet" class="button" type="button">Search From Snippet</button>
          <button id="shape-search-from-selection" class="button" type="button" disabled>Search From Selection</button>
        </div>
        <div id="shape-search-status" class="shape-search-status muted small"></div>
        <div id="shape-search-results" class="shape-search-results"></div>
      </div>
    </aside>

    <main class="workspace">
      <div class="workspace-main">
        <section class="viewer-shell">
          <div class="viewer-topbar">
            <div id="viewer-status" class="status">Loading manifest...</div>
            <div class="viewer-toolbar">
              <button
                id="area-select-toggle"
                class="button icon-button"
                type="button"
                title="Select multiple components by dragging a rectangle"
                aria-pressed="false"
              >
                <span class="icon-button-glyph" aria-hidden="true">[]</span>
                <span>Area Select</span>
              </button>
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

        <section class="playground-shell">
          <div class="playground-topbar">
            <div>
              <p class="eyebrow">PDF Play Ground</p>
              <h2 class="section-title">Vector Whiteboard</h2>
            </div>
            <div class="playground-toolbar">
              <button id="playground-render" class="button" type="button">Render</button>
              <button id="playground-fit" class="button" type="button">Fit View</button>
              <button id="playground-reset" class="button" type="button">Reset</button>
            </div>
          </div>
          <div class="playground-content">
            <div class="playground-editor-pane">
              <label class="field-label" for="playground-code">PDF vector code block</label>
              <textarea
                id="playground-code"
                class="playground-code"
                spellcheck="false"
                placeholder="Paste PDF vector commands here, for example:&#10;19.724 841.89 m&#10;319.724 830.551 l&#10;S"
              ></textarea>
              <p id="playground-status" class="muted small">Paste vector path code to draw it on the whiteboard below.</p>
            </div>
            <div id="playground-board" class="playground-board">
              <canvas id="playground-canvas" class="playground-canvas"></canvas>
              <div id="playground-empty" class="playground-empty">Vector preview will appear here.</div>
              <div id="playground-coords" class="playground-coords">x: -, y: -</div>
            </div>
          </div>
        </section>
      </div>

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
const playgroundCode = mustQuery<HTMLTextAreaElement>('#playground-code')
const playgroundStatus = mustQuery<HTMLParagraphElement>('#playground-status')
const playgroundBoard = mustQuery<HTMLDivElement>('#playground-board')
const playgroundCanvas = mustQuery<HTMLCanvasElement>('#playground-canvas')
const playgroundEmpty = mustQuery<HTMLDivElement>('#playground-empty')
const playgroundCoords = mustQuery<HTMLDivElement>('#playground-coords')
const playgroundRenderBtn = mustQuery<HTMLButtonElement>('#playground-render')
const playgroundFitBtn = mustQuery<HTMLButtonElement>('#playground-fit')
const playgroundResetBtn = mustQuery<HTMLButtonElement>('#playground-reset')
const areaSelectToggleBtn = mustQuery<HTMLButtonElement>('#area-select-toggle')
const zoomInput = mustQuery<HTMLInputElement>('#zoom-input')
const zoomLabel = mustQuery<HTMLSpanElement>('#zoom-label')
const selectionTitle = mustQuery<HTMLHeadingElement>('#selection-title')
const selectionMeta = mustQuery<HTMLDivElement>('#selection-meta')
const sourceComment = mustQuery<HTMLDivElement>('#source-comment')
const sourceCode = mustQuery<HTMLPreElement>('#source-code')
const referenceChain = mustQuery<HTMLDivElement>('#reference-chain')
const warningsEl = mustQuery<HTMLDivElement>('#warnings')
const shapeSearchSnippet = mustQuery<HTMLTextAreaElement>('#shape-search-snippet')
const shapeSearchTolerance = mustQuery<HTMLInputElement>('#shape-search-tolerance')
const shapeSearchToleranceLabel = mustQuery<HTMLSpanElement>('#shape-search-tolerance-label')
const shapeSearchMirror = mustQuery<HTMLInputElement>('#shape-search-mirror')
const shapeSearchFromSnippetBtn = mustQuery<HTMLButtonElement>('#shape-search-from-snippet')
const shapeSearchFromSelectionBtn = mustQuery<HTMLButtonElement>('#shape-search-from-selection')
const shapeSearchStatus = mustQuery<HTMLDivElement>('#shape-search-status')
const shapeSearchResults = mustQuery<HTMLDivElement>('#shape-search-results')

type ShapeSearchCandidate = {
  id: string
  page_number: number
  commands: Array<Record<string, unknown>>
  bbox: BBox
  item_ids: string[]
  kind: 'component' | 'vector'
  label?: string
}

type EnrichedShapeSearchHit = ShapeSearchHit & {
  candidate: ShapeSearchCandidate
}

type ComponentGroupSelection = {
  pageNumber: number
  candidates: ShapeSearchCandidate[]
  itemIds: string[]
  commands: Array<Record<string, unknown>>
  bbox: BBox
}

const pageCache = new Map<string, PageData>()
const pdfDocumentCache = new Map<string, Promise<Awaited<ReturnType<typeof loadPdfDocument>>>>()
let manifest: Manifest | null = null
let activeDocument: ReaderDocument | null = null
let activePageNumber = 1
let activeSelectionId: string | null = null
let activeComponentGroupSelection: ComponentGroupSelection | null = null
let activeShapeSearchResultId: string | null = null
let currentPageState: PageState | null = null
let shapeSearchHitMap = new Map<string, EnrichedShapeSearchHit>()
let searchHighlightItemIds: Set<string> | null = null
let searchHighlightTimer: number | null = null
let componentGroupHighlightItemIds: Set<string> | null = null
let isAreaSelectMode = false
let areaSelectionBox: HTMLDivElement | null = null
let areaDragStart: { x: number; y: number } | null = null
let areaDragPointerId: number | null = null
let zoomFactor = 1
let fitScale = 1
let renderSequence = 0
let scheduledRenderTimer: number | null = null
let isPanning = false
let panStartX = 0
let panStartY = 0
let panScrollLeft = 0
let panScrollTop = 0
let playgroundData: PlaygroundData | null = null
let playgroundView: PlaygroundView = { scale: 1, offsetX: 0, offsetY: 0 }
let playgroundHover: PlaygroundHover | null = null
let playgroundPanPointerId: number | null = null
let playgroundPanStartX = 0
let playgroundPanStartY = 0
let playgroundPanOffsetX = 0
let playgroundPanOffsetY = 0

const MIN_ZOOM_FACTOR = 0.5
const MAX_ZOOM_FACTOR = 4
const PLAYGROUND_MIN_SCALE = 0.02
const PLAYGROUND_MAX_SCALE = 200

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

function normalizePageItems(
  data: Omit<PageData, 'items' | 'components'> & { items: Array<ReaderItem | ComponentItem> },
): PageData {
  return {
    ...data,
    items: data.items.filter((item): item is ReaderItem => isReaderItem(item)),
    components: data.items.filter((item): item is ComponentItem => isComponentItem(item)),
  }
}

async function loadPageData(documentId: string, page: PageManifest): Promise<PageData> {
  const key = pageKey(documentId, page.page_number)
  if (pageCache.has(key)) {
    return pageCache.get(key) as PageData
  }
  const response = await fetch(page.data_url)
  if (!response.ok) {
    throw new Error(`Failed to load page data: ${response.status}`)
  }
  const raw = (await response.json()) as Omit<PageData, 'items' | 'components'> & {
    items: Array<ReaderItem | ComponentItem>
  }
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

function setPlaygroundStatus(message: string): void {
  playgroundStatus.textContent = message
}

function getPlaygroundCanvasSize() {
  return {
    width: Math.max(playgroundBoard.clientWidth, 320),
    height: Math.max(playgroundBoard.clientHeight, 240),
  }
}

function computePlaygroundBounds(polylines: PlaygroundData['polylines']): PlaygroundBounds | null {
  const points = polylines.flatMap((polyline) => polyline.points)
  if (!points.length) {
    return null
  }

  let minX = Number.POSITIVE_INFINITY
  let minY = Number.POSITIVE_INFINITY
  let maxX = Number.NEGATIVE_INFINITY
  let maxY = Number.NEGATIVE_INFINITY

  for (const [x, y] of points) {
    minX = Math.min(minX, x)
    minY = Math.min(minY, y)
    maxX = Math.max(maxX, x)
    maxY = Math.max(maxY, y)
  }

  return {
    minX,
    minY,
    maxX,
    maxY,
    width: Math.max(maxX - minX, 1),
    height: Math.max(maxY - minY, 1),
  }
}

function parsePlaygroundSnippet(snippet: string): PlaygroundData | null {
  const commands = parseSnippetToPathCommands(snippet)
  const polylines = commandsToPolylines(commands).filter((polyline) => polyline.points.length >= 2)
  if (!polylines.length) {
    return null
  }

  const bounds = computePlaygroundBounds(polylines)
  if (!bounds) {
    return null
  }

  return { polylines, bounds }
}

function fitPlaygroundView(): void {
  if (!playgroundData) {
    playgroundView = { scale: 1, offsetX: 0, offsetY: 0 }
    renderPlayground()
    return
  }

  const { width, height } = getPlaygroundCanvasSize()
  const padding = 32
  const fitScale = Math.min(
    Math.max((width - padding * 2) / playgroundData.bounds.width, PLAYGROUND_MIN_SCALE),
    Math.max((height - padding * 2) / playgroundData.bounds.height, PLAYGROUND_MIN_SCALE),
  )
  const scale = clamp(fitScale, PLAYGROUND_MIN_SCALE, PLAYGROUND_MAX_SCALE)
  playgroundView = {
    scale,
    offsetX: (width - playgroundData.bounds.width * scale) / 2 - playgroundData.bounds.minX * scale,
    offsetY: (height - playgroundData.bounds.height * scale) / 2 - playgroundData.bounds.minY * scale,
  }
  renderPlayground()
}

function screenToPlaygroundWorld(screenX: number, screenY: number): { x: number; y: number } {
  const { height } = getPlaygroundCanvasSize()
  return {
    x: (screenX - playgroundView.offsetX) / playgroundView.scale,
    y: ((height - screenY) - playgroundView.offsetY) / playgroundView.scale,
  }
}

function worldToPlaygroundScreen(x: number, y: number): { x: number; y: number } {
  const { height } = getPlaygroundCanvasSize()
  return {
    x: x * playgroundView.scale + playgroundView.offsetX,
    y: height - (y * playgroundView.scale + playgroundView.offsetY),
  }
}

function getPlaygroundStep(scale: number): number {
  const targetPx = 88
  const rawStep = targetPx / scale
  const exponent = Math.floor(Math.log10(Math.max(rawStep, 1e-6)))
  const base = 10 ** exponent
  const multiples = [1, 2, 5, 10]
  for (const multiple of multiples) {
    const step = base * multiple
    if (step >= rawStep) {
      return step
    }
  }
  return base * 10
}

function renderPlayground(): void {
  const context = playgroundCanvas.getContext('2d')
  if (!context) {
    return
  }

  const { width, height } = getPlaygroundCanvasSize()
  const dpr = window.devicePixelRatio || 1
  const pixelWidth = Math.max(1, Math.round(width * dpr))
  const pixelHeight = Math.max(1, Math.round(height * dpr))
  if (playgroundCanvas.width !== pixelWidth || playgroundCanvas.height !== pixelHeight) {
    playgroundCanvas.width = pixelWidth
    playgroundCanvas.height = pixelHeight
  }
  playgroundCanvas.style.width = `${width}px`
  playgroundCanvas.style.height = `${height}px`

  context.setTransform(dpr, 0, 0, dpr, 0, 0)
  context.clearRect(0, 0, width, height)

  const background = context.createLinearGradient(0, 0, 0, height)
  background.addColorStop(0, '#08101f')
  background.addColorStop(1, '#0b172a')
  context.fillStyle = background
  context.fillRect(0, 0, width, height)

  const scale = Math.max(playgroundView.scale, PLAYGROUND_MIN_SCALE)
  const step = getPlaygroundStep(scale)
  const majorStep = step * 5
  const worldTopLeft = screenToPlaygroundWorld(0, 0)
  const worldBottomRight = screenToPlaygroundWorld(width, height)
  const minX = Math.min(worldTopLeft.x, worldBottomRight.x)
  const maxX = Math.max(worldTopLeft.x, worldBottomRight.x)
  const minY = Math.min(worldTopLeft.y, worldBottomRight.y)
  const maxY = Math.max(worldTopLeft.y, worldBottomRight.y)

  const drawGrid = (gridStep: number, strokeStyle: string, lineWidth: number) => {
    context.beginPath()
    for (let x = Math.floor(minX / gridStep) * gridStep; x <= maxX + gridStep; x += gridStep) {
      const screen = worldToPlaygroundScreen(x, 0)
      context.moveTo(screen.x, 0)
      context.lineTo(screen.x, height)
    }
    for (let y = Math.floor(minY / gridStep) * gridStep; y <= maxY + gridStep; y += gridStep) {
      const screen = worldToPlaygroundScreen(0, y)
      context.moveTo(0, screen.y)
      context.lineTo(width, screen.y)
    }
    context.strokeStyle = strokeStyle
    context.lineWidth = lineWidth
    context.stroke()
  }

  drawGrid(step, 'rgba(96, 165, 250, 0.12)', 1)
  drawGrid(majorStep, 'rgba(148, 163, 184, 0.24)', 1)

  const axisX = worldToPlaygroundScreen(0, 0).x
  const axisY = worldToPlaygroundScreen(0, 0).y
  context.beginPath()
  if (axisX >= 0 && axisX <= width) {
    context.moveTo(axisX, 0)
    context.lineTo(axisX, height)
  }
  if (axisY >= 0 && axisY <= height) {
    context.moveTo(0, axisY)
    context.lineTo(width, axisY)
  }
  context.strokeStyle = 'rgba(250, 204, 21, 0.85)'
  context.lineWidth = 1.5
  context.stroke()

  if (playgroundData) {
    context.save()
    context.strokeStyle = '#34d399'
    context.lineWidth = Math.max(1.5, Math.min(3, 2.2 / Math.sqrt(scale / 2)))
    context.lineJoin = 'round'
    context.lineCap = 'round'

    for (const polyline of playgroundData.polylines) {
      const first = polyline.points[0]
      if (!first) {
        continue
      }
      const start = worldToPlaygroundScreen(first[0], first[1])
      context.beginPath()
      context.moveTo(start.x, start.y)
      for (let index = 1; index < polyline.points.length; index += 1) {
        const point = polyline.points[index]
        const screen = worldToPlaygroundScreen(point[0], point[1])
        context.lineTo(screen.x, screen.y)
      }
      if (polyline.closed) {
        context.closePath()
      }
      context.stroke()
    }
    context.restore()
  }

  if (playgroundHover) {
    const hoverScreen = worldToPlaygroundScreen(playgroundHover.x, playgroundHover.y)
    context.save()
    context.setLineDash([6, 6])
    context.beginPath()
    context.moveTo(hoverScreen.x, 0)
    context.lineTo(hoverScreen.x, height)
    context.moveTo(0, hoverScreen.y)
    context.lineTo(width, hoverScreen.y)
    context.strokeStyle = 'rgba(248, 250, 252, 0.35)'
    context.lineWidth = 1
    context.stroke()
    context.restore()
  }
}

function updatePlaygroundHover(event: PointerEvent | MouseEvent): void {
  const rect = playgroundBoard.getBoundingClientRect()
  const x = clamp(event.clientX - rect.left, 0, rect.width)
  const y = clamp(event.clientY - rect.top, 0, rect.height)
  const world = screenToPlaygroundWorld(x, y)
  playgroundHover = world
  playgroundCoords.textContent = `x: ${world.x.toFixed(2)}, y: ${world.y.toFixed(2)}`
  renderPlayground()
}

function clearPlaygroundHover(): void {
  playgroundHover = null
  playgroundCoords.textContent = 'x: -, y: -'
  renderPlayground()
}

function refreshPlayground(autoFit = true): void {
  const nextData = parsePlaygroundSnippet(playgroundCode.value)
  playgroundData = nextData
  playgroundEmpty.hidden = Boolean(nextData)

  if (!playgroundCode.value.trim()) {
    playgroundData = null
    playgroundEmpty.hidden = false
    setPlaygroundStatus('Paste vector path code to draw it on the whiteboard below.')
    renderPlayground()
    return
  }

  if (!nextData) {
    setPlaygroundStatus('Unable to parse vector commands. Use PDF path operators such as m, l, c, re, h.')
    renderPlayground()
    return
  }

  setPlaygroundStatus(
    `Rendered ${nextData.polylines.length} path${nextData.polylines.length === 1 ? '' : 's'} with auto-scaled coordinates.`,
  )
  if (autoFit) {
    fitPlaygroundView()
  } else {
    renderPlayground()
  }
}

function zoomPlaygroundAt(clientX: number, clientY: number, factor: number): void {
  const rect = playgroundBoard.getBoundingClientRect()
  const screenX = clamp(clientX - rect.left, 0, rect.width)
  const screenY = clamp(clientY - rect.top, 0, rect.height)
  const anchor = screenToPlaygroundWorld(screenX, screenY)
  const nextScale = clamp(playgroundView.scale * factor, PLAYGROUND_MIN_SCALE, PLAYGROUND_MAX_SCALE)
  playgroundView.scale = nextScale
  playgroundView.offsetX = screenX - anchor.x * nextScale
  playgroundView.offsetY = (rect.height - screenY) - anchor.y * nextScale
  renderPlayground()
}

function initPlayground(): void {
  const resizeObserver = new ResizeObserver(() => {
    if (playgroundData) {
      fitPlaygroundView()
      return
    }
    renderPlayground()
  })
  resizeObserver.observe(playgroundBoard)

  let inputTimer: number | null = null
  playgroundCode.addEventListener('input', () => {
    if (inputTimer !== null) {
      window.clearTimeout(inputTimer)
    }
    inputTimer = window.setTimeout(() => {
      inputTimer = null
      refreshPlayground(true)
    }, 220)
  })

  playgroundRenderBtn.addEventListener('click', () => {
    refreshPlayground(true)
  })

  playgroundFitBtn.addEventListener('click', () => {
    fitPlaygroundView()
  })

  playgroundResetBtn.addEventListener('click', () => {
    playgroundView = { scale: 1, offsetX: 0, offsetY: 0 }
    if (playgroundData) {
      fitPlaygroundView()
      return
    }
    renderPlayground()
  })

  playgroundBoard.addEventListener(
    'wheel',
    (event) => {
      event.preventDefault()
      const factor = Math.exp(-event.deltaY * 0.0015)
      zoomPlaygroundAt(event.clientX, event.clientY, factor)
      updatePlaygroundHover(event)
    },
    { passive: false },
  )

  playgroundBoard.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) {
      return
    }
    playgroundPanPointerId = event.pointerId
    playgroundPanStartX = event.clientX
    playgroundPanStartY = event.clientY
    playgroundPanOffsetX = playgroundView.offsetX
    playgroundPanOffsetY = playgroundView.offsetY
    playgroundBoard.classList.add('is-panning')
    playgroundBoard.setPointerCapture(event.pointerId)
  })

  playgroundBoard.addEventListener('pointermove', (event) => {
    updatePlaygroundHover(event)
    if (playgroundPanPointerId !== event.pointerId) {
      return
    }
    const deltaX = event.clientX - playgroundPanStartX
    const deltaY = event.clientY - playgroundPanStartY
    playgroundView.offsetX = playgroundPanOffsetX + deltaX
    playgroundView.offsetY = playgroundPanOffsetY - deltaY
    renderPlayground()
  })

  const stopPlaygroundPan = (event: PointerEvent) => {
    if (playgroundPanPointerId !== event.pointerId) {
      return
    }
    playgroundPanPointerId = null
    playgroundBoard.classList.remove('is-panning')
  }

  playgroundBoard.addEventListener('pointerup', stopPlaygroundPan)
  playgroundBoard.addEventListener('pointercancel', stopPlaygroundPan)
  playgroundBoard.addEventListener('pointerleave', () => {
    clearPlaygroundHover()
  })
  playgroundBoard.addEventListener('mouseenter', () => {
    renderPlayground()
  })

  renderPlayground()
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

function formatCommandNumber(value: unknown): string {
  const n = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(n)) {
    return '0'
  }
  return n.toFixed(3).replace(/\.?0+$/, '')
}

function commandKey(command: Record<string, unknown>): string {
  const op = String(command.op ?? '').toUpperCase()
  const pts = Array.isArray(command.points) ? (command.points as unknown[]) : []
  const nums = pts
    .flatMap((pair) => (Array.isArray(pair) ? pair : []))
    .map((v) => formatCommandNumber(v))
    .join(',')
  return `${op}|${nums}`
}

function extractCommonCommands(groups: Array<Array<Record<string, unknown>>>): Array<Record<string, unknown>> {
  if (!groups.length) {
    return []
  }
  const first = groups[0]
  const common = new Set(first.map((cmd) => commandKey(cmd)))
  for (let i = 1; i < groups.length; i++) {
    const set = new Set(groups[i].map((cmd) => commandKey(cmd)))
    for (const key of [...common]) {
      if (!set.has(key)) {
        common.delete(key)
      }
    }
    if (!common.size) {
      break
    }
  }
  return first.filter((cmd) => common.has(commandKey(cmd)))
}

function formatCommandsAsSnippet(commands: Array<Record<string, unknown>>, maxLines = 320): string {
  const lines: string[] = []
  for (const command of commands) {
    const op = String(command.op ?? '').toUpperCase()
    const points = Array.isArray(command.points) ? (command.points as unknown[]) : []
    if ((op === 'M' || op === 'L') && Array.isArray(points[0])) {
      const [x, y] = points[0] as [unknown, unknown]
      lines.push(`${formatCommandNumber(x)} ${formatCommandNumber(y)} ${op.toLowerCase()}`)
      continue
    }
    if (op === 'C' && Array.isArray(points[0]) && Array.isArray(points[1]) && Array.isArray(points[2])) {
      const [x1, y1] = points[0] as [unknown, unknown]
      const [x2, y2] = points[1] as [unknown, unknown]
      const [x3, y3] = points[2] as [unknown, unknown]
      lines.push(
        `${formatCommandNumber(x1)} ${formatCommandNumber(y1)} ${formatCommandNumber(x2)} ${formatCommandNumber(y2)} ${formatCommandNumber(x3)} ${formatCommandNumber(y3)} c`,
      )
      continue
    }
    if (op === 'Z') {
      lines.push('h')
      continue
    }
  }
  if (!lines.length) {
    return '// No path commands were extracted from the current selection.'
  }
  const clipped = lines.slice(0, maxLines)
  if (lines.length > maxLines) {
    clipped.push(`... (${lines.length - maxLines} more lines)`)
  }
  return clipped.join('\n')
}

function clearComponentGroupSelectionVisuals(): void {
  activeComponentGroupSelection = null
  componentGroupHighlightItemIds = null
}

function setComponentGroupSelection(group: ComponentGroupSelection | null, pageData: PageData | null): void {
  if (!group || !pageData) {
    clearComponentGroupSelectionVisuals()
    syncSelectionClasses()
    updateShapeSearchSelectionButton()
    return
  }

  activeSelectionId = null
  activeComponentGroupSelection = group
  componentGroupHighlightItemIds = new Set(group.itemIds)

  const labels = group.candidates
    .map((candidate) => {
      if (candidate.kind === 'component') {
        if (candidate.label) {
          return candidate.label
        }
        const componentId = candidate.id.replace(/^component:/, '')
        const component = pageData.components.find((item) => item.id === componentId)
        return component?.component?.label?.trim() || componentId
      }
      return candidate.id.replace(/^vector:/, '')
    })
    .filter((label): label is string => Boolean(label))

  const commonCommands = extractCommonCommands(group.candidates.map((c) => c.commands))
  const codeToShow = commonCommands.length ? commonCommands : group.commands
  const allVectors = group.candidates.every((candidate) => candidate.kind === 'vector')
  const subjectLabel = allVectors ? 'vectors' : 'objects'
  const codeTitle = commonCommands.length
    ? `Common code across ${group.candidates.length} selected ${subjectLabel}`
    : `Merged code from ${group.candidates.length} selected ${subjectLabel}`

  selectionTitle.textContent = `${group.candidates.length} ${allVectors ? 'Vectors' : 'Objects'} Selected`
  sourceCode.classList.remove('empty')
  sourceCode.textContent = `${codeTitle}\n\n${formatCommandsAsSnippet(codeToShow)}`
  sourceComment.innerHTML =
    '<div class="source-note">This code block is generated from the current area selection and can be used directly for shape search.</div>'
  warningsEl.innerHTML = ''

  renderMeta(selectionMeta, [
    ['Page', String(group.pageNumber)],
    [allVectors ? 'Vectors' : 'Objects', String(group.candidates.length)],
    ['Vector paths', String(group.itemIds.length)],
    ['Command count', String(group.commands.length)],
    ['BBox', `${group.bbox.x0}, ${group.bbox.y0}, ${group.bbox.x1}, ${group.bbox.y1}`],
  ])

  referenceChain.innerHTML = [
    `<div class="reference-section-title">Selected ${allVectors ? 'Vectors' : 'Objects'}</div>`,
    labels.length
      ? labels
          .map(
            (label) =>
              `<section class="reference-card"><div class="reference-card-header"><strong>${escapeHtml(label)}</strong><span>${allVectors ? 'Vector path' : 'Object'}</span></div></section>`,
          )
          .join('')
      : '<div class="reference-empty">No vector labels are available for the current area selection.</div>',
  ].join('')

  syncSelectionClasses()
  updateShapeSearchSelectionButton()
}

function setSelection(item: ReaderItem | null, pageData: PageData | null): void {
  clearComponentGroupSelectionVisuals()
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
    updateShapeSearchSelectionButton()
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
    metaRows.push(['Pixel size', `${item.image.pixel_width ?? '?'} x ${item.image.pixel_height ?? '?'}`])
    metaRows.push(['Filters', item.image.filters.join(', ') || 'None'])
    metaRows.push(['Draw size (pt)', `${item.summary.draw_width} x ${item.summary.draw_height}`])
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
  updateShapeSearchSelectionButton()
}

function syncSelectionClasses(): void {
  document.querySelectorAll<HTMLElement>('.overlay-item').forEach((node) => {
    const itemId = node.dataset.itemId ?? ''
    const isSelected = itemId === activeSelectionId
    const isSearchHit = Boolean(searchHighlightItemIds?.has(itemId))
    const isGroupSelected = Boolean(componentGroupHighlightItemIds?.has(itemId))
    node.classList.toggle('is-selected', isSelected)
    node.classList.toggle('is-search-hit', isSearchHit)
    node.classList.toggle('is-group-selected', isGroupSelected)
    node.style.zIndex = isSelected ? '10' : isSearchHit ? '9' : isGroupSelected ? '8' : node.dataset.baseZIndex ?? '1'
  })
}

function syncShapeSearchResultClasses(): void {
  document.querySelectorAll<HTMLButtonElement>('button.shape-search-hit').forEach((btn) => {
    btn.classList.toggle('is-active', btn.dataset.candidateId === activeShapeSearchResultId)
  })
}

function setTemporarySearchHighlight(itemIds: string[]): void {
  const cleaned = itemIds.filter((id) => Boolean(id))
  if (!cleaned.length) {
    return
  }
  searchHighlightItemIds = new Set(cleaned)
  syncSelectionClasses()
  if (searchHighlightTimer !== null) {
    window.clearTimeout(searchHighlightTimer)
  }
  searchHighlightTimer = window.setTimeout(() => {
    searchHighlightTimer = null
    searchHighlightItemIds = null
    syncSelectionClasses()
  }, 2000)
}

function scrollToBBoxCenter(bbox: BBox): void {
  const st = currentPageState
  if (!st) {
    return
  }
  const pageHeight = st.pageData.page_size.height_pt
  const scale = st.scale
  const cx = ((bbox.x0 + bbox.x1) / 2) * scale
  const cy = (pageHeight - (bbox.y0 + bbox.y1) / 2) * scale
  const viewportW = viewerScroll.clientWidth
  const viewportH = viewerScroll.clientHeight
  viewerScroll.scrollTo({
    left: Math.max(cx - viewportW / 2, 0),
    top: Math.max(cy - viewportH / 2, 0),
    behavior: 'smooth',
  })
}

function focusSearchTarget(itemIds: string[], bbox: BBox | null): void {
  const firstNode = itemIds
    .map((id) => currentPageState?.overlay.querySelector<HTMLElement>(`.overlay-item[data-item-id="${id}"]`))
    .find((node): node is HTMLElement => Boolean(node))
  if (firstNode) {
    firstNode.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'center' })
  } else if (bbox) {
    scrollToBBoxCenter(bbox)
  }
  setTemporarySearchHighlight(itemIds)
}

function updateShapeSearchSelectionButton(): void {
  const st = currentPageState
  if (!st?.pageData) {
    shapeSearchFromSelectionBtn.disabled = true
    return
  }
  if (
    activeComponentGroupSelection &&
    activeComponentGroupSelection.pageNumber === st.pageData.page_number &&
    activeComponentGroupSelection.commands.length > 0
  ) {
    shapeSearchFromSelectionBtn.disabled = false
    return
  }
  if (!activeSelectionId) {
    shapeSearchFromSelectionBtn.disabled = true
    return
  }
  const item = st.pageData.items.find((i) => i.id === activeSelectionId)
  shapeSearchFromSelectionBtn.disabled = !item || item.kind !== 'vector_path'
}

function toleranceSliderToMaxDistance(percent: number): number {
  return 0.02 + ((percent - 5) / 30) * 0.2
}

function bboxIntersectsWithPadding(a: BBox, b: BBox, padding = 1): boolean {
  return !(a.x1 < b.x0 - padding || a.x0 > b.x1 + padding || a.y1 < b.y0 - padding || a.y0 > b.y1 + padding)
}

function mergeBBox(boxes: BBox[]): BBox {
  if (!boxes.length) {
    return { x0: 0, y0: 0, x1: 0, y1: 0, width: 0, height: 0 }
  }
  const x0 = Math.min(...boxes.map((b) => b.x0))
  const y0 = Math.min(...boxes.map((b) => b.y0))
  const x1 = Math.max(...boxes.map((b) => b.x1))
  const y1 = Math.max(...boxes.map((b) => b.y1))
  return { x0, y0, x1, y1, width: x1 - x0, height: y1 - y0 }
}

function countSubpaths(commands: Array<Record<string, unknown>>): number {
  return commands.reduce((n, c) => (String(c.op ?? '').toUpperCase() === 'M' ? n + 1 : n), 0)
}

function buildShapeSearchCandidatesForPage(pageData: PageData): ShapeSearchCandidate[] {
  const vectors = pageData.items.filter((item): item is VectorItem => item.kind === 'vector_path')
  const candidates: ShapeSearchCandidate[] = []
  const signatures = new Set<string>()

  for (const component of pageData.components) {
    const members = vectors
      .filter((v) => bboxIntersectsWithPadding(v.bbox, component.bbox, Math.max(v.effective_line_width, 1.1)))
      .sort((a, b) => a.id.localeCompare(b.id))
    if (members.length < 1) {
      continue
    }

    const memberIds = members.map((m) => m.id)
    const signature = memberIds.join('|')
    if (!signature || signatures.has(signature)) {
      continue
    }
    signatures.add(signature)

    const label = typeof component.component?.label === 'string' ? component.component.label.trim() : undefined
    candidates.push({
      id: `component:${component.id}`,
      page_number: component.page_number,
      commands: members.flatMap((m) => m.commands),
      // Use the original component rectangle for area-selection and focus.
      // The merged vector bbox can drift outward when nearby strokes are grouped in.
      bbox: component.bbox,
      item_ids: memberIds,
      kind: 'component',
      label: label || undefined,
    })
  }

  for (const vector of vectors) {
    candidates.push({
      id: `vector:${vector.id}`,
      page_number: vector.page_number,
      commands: vector.commands,
      bbox: vector.bbox,
      item_ids: [vector.id],
      kind: 'vector',
    })
  }

  return candidates
}

function bboxContains(outer: BBox, inner: BBox, epsilon = 0): boolean {
  return (
    inner.x0 >= outer.x0 - epsilon &&
    inner.y0 >= outer.y0 - epsilon &&
    inner.x1 <= outer.x1 + epsilon &&
    inner.y1 <= outer.y1 + epsilon
  )
}

function mergeCommands(candidates: ShapeSearchCandidate[]): Array<Record<string, unknown>> {
  return candidates.flatMap((candidate) => candidate.commands)
}

function toPageBBoxFromOverlayRect(
  left: number,
  top: number,
  right: number,
  bottom: number,
  pageHeight: number,
  scale: number,
): BBox {
  const x0 = left / scale
  const x1 = right / scale
  const y1 = pageHeight - top / scale
  const y0 = pageHeight - bottom / scale
  return { x0, y0, x1, y1, width: x1 - x0, height: y1 - y0 }
}

function setAreaSelectMode(enabled: boolean): void {
  isAreaSelectMode = enabled
  areaSelectToggleBtn.setAttribute('aria-pressed', String(enabled))
  areaSelectToggleBtn.classList.toggle('is-active', enabled)
  if (!enabled && areaSelectionBox) {
    areaSelectionBox.remove()
    areaSelectionBox = null
  }
  const overlay = currentPageState?.overlay
  if (overlay) {
    overlay.classList.toggle('area-select-mode', enabled)
  }
}

function selectVectorsInsideArea(pageData: PageData, area: BBox): void {
  const vectorCandidates = buildShapeSearchCandidatesForPage(pageData).filter((candidate) => candidate.kind === 'vector')
  if (!vectorCandidates.length) {
    setStatus('No vector objects were detected on this page.')
    setSelection(null, null)
    return
  }

  const selected = vectorCandidates.filter((candidate) => bboxContains(area, candidate.bbox))
  if (!selected.length) {
    setStatus(`No vector object was fully enclosed by the selected area (${vectorCandidates.length} vector candidates on this page).`)
    setSelection(null, null)
    return
  }

  const uniqueItemIds = [...new Set(selected.flatMap((candidate) => candidate.item_ids))]
  const mergedBox = mergeBBox(selected.map((candidate) => candidate.bbox))
  const group: ComponentGroupSelection = {
    pageNumber: pageData.page_number,
    candidates: selected,
    itemIds: uniqueItemIds,
    commands: mergeCommands(selected),
    bbox: mergedBox,
  }
  setStatus(`Selected ${selected.length} vector objects.`)
  setComponentGroupSelection(group, pageData)
}

function attachAreaSelectionHandlers(overlay: HTMLDivElement, pageData: PageData, scale: number): void {
  overlay.classList.toggle('area-select-mode', isAreaSelectMode)

  const clearDragState = () => {
    areaDragStart = null
    areaDragPointerId = null
  }

  const finishAreaSelection = (endX: number, endY: number) => {
    if (!areaDragStart) {
      clearDragState()
      return
    }
    const left = Math.min(areaDragStart.x, endX)
    const right = Math.max(areaDragStart.x, endX)
    const top = Math.min(areaDragStart.y, endY)
    const bottom = Math.max(areaDragStart.y, endY)

    if (areaSelectionBox) {
      areaSelectionBox.remove()
      areaSelectionBox = null
    }

    clearDragState()
    if (right - left < 4 || bottom - top < 4) {
      return
    }

    const pageBBox = toPageBBoxFromOverlayRect(left, top, right, bottom, pageData.page_size.height_pt, scale)
    selectVectorsInsideArea(pageData, pageBBox)
  }

  overlay.addEventListener('pointerdown', (event) => {
    if (!isAreaSelectMode || event.button !== 0) {
      return
    }
    event.preventDefault()
    event.stopPropagation()
    const rect = overlay.getBoundingClientRect()
    const x = clamp(event.clientX - rect.left, 0, rect.width)
    const y = clamp(event.clientY - rect.top, 0, rect.height)
    areaDragStart = { x, y }
    areaDragPointerId = event.pointerId
    overlay.setPointerCapture(event.pointerId)
    areaSelectionBox = document.createElement('div')
    areaSelectionBox.className = 'area-selection-rect'
    areaSelectionBox.style.left = `${x}px`
    areaSelectionBox.style.top = `${y}px`
    areaSelectionBox.style.width = '0px'
    areaSelectionBox.style.height = '0px'
    overlay.appendChild(areaSelectionBox)
  })

  overlay.addEventListener('pointermove', (event) => {
    if (!isAreaSelectMode || areaDragStart === null || areaDragPointerId !== event.pointerId || !areaSelectionBox) {
      return
    }
    event.preventDefault()
    const rect = overlay.getBoundingClientRect()
    const x = clamp(event.clientX - rect.left, 0, rect.width)
    const y = clamp(event.clientY - rect.top, 0, rect.height)
    const left = Math.min(areaDragStart.x, x)
    const right = Math.max(areaDragStart.x, x)
    const top = Math.min(areaDragStart.y, y)
    const bottom = Math.max(areaDragStart.y, y)
    areaSelectionBox.style.left = `${left}px`
    areaSelectionBox.style.top = `${top}px`
    areaSelectionBox.style.width = `${right - left}px`
    areaSelectionBox.style.height = `${bottom - top}px`
  })

  overlay.addEventListener('pointerup', (event) => {
    if (!isAreaSelectMode || areaDragPointerId !== event.pointerId) {
      return
    }
    event.preventDefault()
    const rect = overlay.getBoundingClientRect()
    const x = clamp(event.clientX - rect.left, 0, rect.width)
    const y = clamp(event.clientY - rect.top, 0, rect.height)
    finishAreaSelection(x, y)
  })

  overlay.addEventListener('pointercancel', (event) => {
    if (areaDragPointerId !== event.pointerId) {
      return
    }
    if (areaSelectionBox) {
      areaSelectionBox.remove()
      areaSelectionBox = null
    }
    clearDragState()
  })
}

function renderShapeSearchResults(hits: EnrichedShapeSearchHit[]): void {
  shapeSearchResults.innerHTML = ''
  shapeSearchHitMap = new Map(hits.map((h) => [h.id, h]))
  if (!hits.length) {
    activeShapeSearchResultId = null
    syncShapeSearchResultClasses()
    return
  }

  for (let i = 0; i < hits.length; i++) {
    const h = hits[i]
    const btn = document.createElement('button')
    btn.type = 'button'
    btn.className = 'shape-search-hit'
    btn.dataset.candidateId = h.id

    const kindLabel = h.candidate.kind === 'component' ? 'Component' : 'Path'
    const sizeLabel =
      h.candidate.kind === 'component' ? `${h.candidate.item_ids.length} paths` : `${h.candidate.item_ids.length} path`
    const label = h.candidate.label ? ` | ${escapeHtml(h.candidate.label)}` : ''

    btn.innerHTML = `<span class="shape-hit-rank">#${i + 1}</span><span>P${h.pageNumber} | ${kindLabel} (${sizeLabel})${label}</span><span class="shape-hit-score">${h.score.toFixed(3)}</span>`
    shapeSearchResults.appendChild(btn)
  }

  syncShapeSearchResultClasses()
}

async function runShapeSearch(
  queryCommands: Array<Record<string, unknown>>,
  excludeCandidateIds?: Set<string>,
): Promise<void> {
  if (!activeDocument) {
    return
  }
  const maxDistance = toleranceSliderToMaxDistance(Number.parseFloat(shapeSearchTolerance.value))
  const allowMirror = shapeSearchMirror.checked
  const querySubpathCount = countSubpaths(queryCommands)

  shapeSearchStatus.textContent = 'Scanning vectors...'
  shapeSearchResults.innerHTML = ''
  shapeSearchHitMap = new Map()
  activeShapeSearchResultId = null
  shapeSearchFromSnippetBtn.disabled = true
  shapeSearchFromSelectionBtn.disabled = true

  const candidates: ShapeSearchCandidate[] = []
  const doc = activeDocument
  try {
    for (let i = 0; i < doc.pages.length; i++) {
      const pm = doc.pages[i]
      shapeSearchStatus.textContent = `Scanning page ${i + 1} / ${doc.pages.length}...`
      const pd = await loadPageData(doc.id, pm)
      candidates.push(...buildShapeSearchCandidatesForPage(pd))
    }

    const componentCandidates = candidates.filter((c) => c.kind === 'component')
    const searchPool =
      querySubpathCount > 1 && componentCandidates.length > 0 ? componentCandidates : candidates

    const hits = searchSimilarVectorPaths({
      queryCommands,
      vectors: searchPool.map((c) => ({
        id: c.id,
        page_number: c.page_number,
        commands: c.commands,
      })),
      maxDistance,
      maxResults: 100,
      allowMirror,
      excludeIds: excludeCandidateIds,
    })

    const candidateById = new Map(searchPool.map((c) => [c.id, c]))
    const enriched: EnrichedShapeSearchHit[] = hits
      .map((h) => {
        const candidate = candidateById.get(h.id)
        if (!candidate) {
          return null
        }
        return { ...h, candidate }
      })
      .filter((h): h is EnrichedShapeSearchHit => Boolean(h))

    if (enriched.length) {
      const mode = querySubpathCount > 1 && componentCandidates.length > 0 ? 'component' : 'mixed'
      shapeSearchStatus.textContent = `Found ${enriched.length} ${mode} matches (lower score = better).`
    } else {
      shapeSearchStatus.textContent =
        'No close match found. Try a higher tolerance, enable mirror, or paste a more complete snippet.'
    }
    renderShapeSearchResults(enriched)
  } finally {
    shapeSearchFromSnippetBtn.disabled = false
    updateShapeSearchSelectionButton()
  }
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
    if (isAreaSelectMode) {
      return
    }
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
  attachAreaSelectionHandlers(overlay, pageData, scale)

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
    ['Page size (pt)', `${pageData.page_size.width_pt} x ${pageData.page_size.height_pt}`],
  ])

  applyAnchorScroll(options.anchor ?? null, scale)
  updateZoomLabel()

  const selectedItem = activeSelectionId ? pageData.items.find((item) => item.id === activeSelectionId) ?? null : null
  if (
    activeComponentGroupSelection &&
    activeComponentGroupSelection.pageNumber === pageData.page_number &&
    activeComponentGroupSelection.candidates.length
  ) {
    setComponentGroupSelection(activeComponentGroupSelection, pageData)
  } else if (selectedItem) {
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
  updateShapeSearchSelectionButton()
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
  clearComponentGroupSelectionVisuals()
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
  if (activeComponentGroupSelection && activeComponentGroupSelection.pageNumber !== pageNumber) {
    clearComponentGroupSelectionVisuals()
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

areaSelectToggleBtn.addEventListener('click', () => {
  setAreaSelectMode(!isAreaSelectMode)
  if (isAreaSelectMode) {
    setStatus('Area select mode enabled. Drag a rectangle on the page to select vector objects.')
  } else {
    setStatus(`Page ${activePageNumber} ready.`)
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

window.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && isAreaSelectMode) {
    setAreaSelectMode(false)
    setStatus(`Page ${activePageNumber} ready.`)
  }
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

function syncShapeSearchToleranceLabel(): void {
  shapeSearchToleranceLabel.textContent = `${shapeSearchTolerance.value}%`
}

shapeSearchTolerance.addEventListener('input', syncShapeSearchToleranceLabel)
syncShapeSearchToleranceLabel()

function findComponentCandidateForVector(pageData: PageData, vectorId: string): ShapeSearchCandidate | null {
  const candidates = buildShapeSearchCandidatesForPage(pageData)
    .filter((c) => c.kind === 'component' && c.item_ids.includes(vectorId))
    .sort((a, b) => a.bbox.width * a.bbox.height - (b.bbox.width * b.bbox.height))
  return candidates[0] ?? null
}

shapeSearchFromSnippetBtn.addEventListener('click', async () => {
  const cmds = parseSnippetToPathCommands(shapeSearchSnippet.value)
  const records = pathCommandsToQueryRecords(cmds)
  if (!records.length) {
    shapeSearchStatus.textContent = 'Cannot parse path commands from snippet. Include m/l/c/re operators and numbers.'
    shapeSearchResults.innerHTML = ''
    shapeSearchHitMap = new Map()
    activeShapeSearchResultId = null
    syncShapeSearchResultClasses()
    return
  }
  await runShapeSearch(records)
})

shapeSearchFromSelectionBtn.addEventListener('click', async () => {
  const st = currentPageState
  if (!st?.pageData) {
    return
  }

  if (
    activeComponentGroupSelection &&
    activeComponentGroupSelection.pageNumber === st.pageData.page_number &&
    activeComponentGroupSelection.commands.length > 0
  ) {
    shapeSearchStatus.textContent = `Using area-selected vector group (${activeComponentGroupSelection.candidates.length} vectors).`
    await runShapeSearch(activeComponentGroupSelection.commands)
    return
  }

  if (!activeSelectionId) {
    return
  }
  const item = st.pageData.items.find((i) => i.id === activeSelectionId)
  if (!item || item.kind !== 'vector_path') {
    return
  }

  const componentCandidate = findComponentCandidateForVector(st.pageData, item.id)
  if (componentCandidate && countSubpaths(componentCandidate.commands) > 1) {
    shapeSearchStatus.textContent = `Using component query (${componentCandidate.item_ids.length} paths).`
    await runShapeSearch(componentCandidate.commands)
    return
  }

  await runShapeSearch(item.commands)
})

shapeSearchResults.addEventListener('click', async (e) => {
  const btn = (e.target as HTMLElement).closest('button.shape-search-hit')
  if (!btn || !activeDocument) {
    return
  }
  const candidateId = (btn as HTMLButtonElement).dataset.candidateId
  if (!candidateId) {
    return
  }
  const hit = shapeSearchHitMap.get(candidateId)
  if (!hit) {
    return
  }

  const primaryItemId = hit.candidate.item_ids[0] ?? null
  if (!primaryItemId) {
    return
  }

  activeShapeSearchResultId = candidateId
  syncShapeSearchResultClasses()
  activeSelectionId = primaryItemId
  layerVisibility = { ...layerVisibility, vector_path: true }
  saveLayerVisibility(layerVisibility)
  document.querySelectorAll<HTMLInputElement>('.layer-toggle[data-layer-kind="vector_path"]').forEach((el) => {
    el.checked = true
  })
  await selectPage(hit.pageNumber)
  updatePagerButtons()

  const st = currentPageState
  if (st?.pageData) {
    const selected = st.pageData.items.find((i) => i.id === primaryItemId)
    if (selected) {
      setSelection(selected, st.pageData)
    }
  }
  focusSearchTarget(hit.candidate.item_ids, hit.candidate.bbox)
})

async function bootstrap(): Promise<void> {
  try {
    initLayerToggles()
    initPlayground()
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
