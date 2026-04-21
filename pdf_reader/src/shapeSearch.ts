/**
 * Geometric similarity for PDF vector paths: invariant to translation,
 * uniform scaling, and rotation (sampled). Optional reflection.
 * Curves (c) are sampled; rectangles (re) expand to polylines.
 * Supports both single-path and multi-path (component-like) matching.
 */

export type PathCommand = {
  op: string
  points?: number[][]
}

export type ShapeSearchHit = {
  id: string
  pageNumber: number
  score: number
}

type PathPolyline = {
  points: [number, number][]
  closed: boolean
}

const DEFAULT_SAMPLES = 96
const ROTATION_STEPS = 36

function isNumericToken(t: string): boolean {
  return /^-?(?:\d+\.?\d*|\.\d+)$/.test(t)
}

/** Tokenize a PDF content fragment (numbers and operators). */
export function tokenizePdfContentStream(src: string): string[] {
  const out: string[] = []
  let i = 0
  while (i < src.length) {
    const ch = src[i]
    if (ch === '%') {
      while (i < src.length && src[i] !== '\n' && src[i] !== '\r') {
        i++
      }
      continue
    }
    if (/\s/.test(ch)) {
      i++
      continue
    }
    const rest = src.slice(i)
    const num = rest.match(/^-?(?:\d+\.?\d*|\.\d+)/)
    if (num) {
      out.push(num[0])
      i += num[0].length
      continue
    }
    const op = rest.match(/^[A-Za-z*']+/)
    if (op) {
      out.push(op[0])
      i += op[0].length
      continue
    }
    i++
  }
  return out
}

/** Parse path-related operators into commands (user-space, identity CTM). */
export function parsePathCommandsFromTokens(tokens: string[]): PathCommand[] {
  const cmds: PathCommand[] = []
  const stack: number[] = []
  let current: [number, number] | null = null

  const popN = (n: number): number[] | null => {
    if (stack.length < n) {
      return null
    }
    return stack.splice(stack.length - n, n)
  }

  for (const t of tokens) {
    if (isNumericToken(t)) {
      stack.push(Number.parseFloat(t))
      continue
    }

    switch (t) {
      case 'm': {
        const p = popN(2)
        if (p) {
          cmds.push({ op: 'M', points: [[p[0], p[1]]] })
          current = [p[0], p[1]]
        }
        break
      }
      case 'l': {
        const p = popN(2)
        if (p) {
          cmds.push({ op: 'L', points: [[p[0], p[1]]] })
          current = [p[0], p[1]]
        }
        break
      }
      case 'c': {
        const p = popN(6)
        if (p) {
          cmds.push({
            op: 'C',
            points: [
              [p[0], p[1]],
              [p[2], p[3]],
              [p[4], p[5]],
            ],
          })
          current = [p[4], p[5]]
        }
        break
      }
      case 'v': {
        const p = popN(4)
        if (p && current) {
          cmds.push({
            op: 'C',
            points: [current, [p[0], p[1]], [p[2], p[3]]],
          })
          current = [p[2], p[3]]
        }
        break
      }
      case 'y': {
        const p = popN(4)
        if (p && current) {
          const end: [number, number] = [p[2], p[3]]
          cmds.push({
            op: 'C',
            points: [[p[0], p[1]], end, end],
          })
          current = end
        }
        break
      }
      case 're': {
        const p = popN(4)
        if (p) {
          const [x, y, w, h] = p
          cmds.push({ op: 'M', points: [[x, y]] })
          cmds.push({ op: 'L', points: [[x + w, y]] })
          cmds.push({ op: 'L', points: [[x + w, y + h]] })
          cmds.push({ op: 'L', points: [[x, y + h]] })
          cmds.push({ op: 'Z' })
          current = [x, y]
        }
        break
      }
      case 'h':
        cmds.push({ op: 'Z' })
        break
      default:
        break
    }
  }

  return cmds
}

export function parseSnippetToPathCommands(snippet: string): PathCommand[] {
  return parsePathCommandsFromTokens(tokenizePdfContentStream(snippet))
}

function bezierPoint(
  p0: [number, number],
  p1: [number, number],
  p2: [number, number],
  p3: [number, number],
  t: number,
): [number, number] {
  const u = 1 - t
  const tt = t * t
  const uu = u * u
  const uuu = uu * u
  const ttt = tt * t
  const x = uuu * p0[0] + 3 * uu * t * p1[0] + 3 * u * tt * p2[0] + ttt * p3[0]
  const y = uuu * p0[1] + 3 * uu * t * p1[1] + 3 * u * tt * p2[1] + ttt * p3[1]
  return [x, y]
}

/** Flatten commands to a polyline (samples curves). */
export function commandsToPolyline(commands: PathCommand[]): [number, number][] {
  const polylines = commandsToPolylines(commands)
  return polylines.flatMap((p) => p.points)
}

/** Split commands into subpaths and flatten each subpath to a polyline. */
export function commandsToPolylines(commands: PathCommand[]): PathPolyline[] {
  const out: PathPolyline[] = []
  let points: [number, number][] = []
  let current: [number, number] | null = null
  let subStart: [number, number] | null = null
  let closed = false

  const flush = () => {
    if (points.length >= 2) {
      out.push({ points: points.slice(), closed })
    }
    points = []
    current = null
    subStart = null
    closed = false
  }

  for (const c of commands) {
    if (c.op === 'M' && c.points?.[0]) {
      flush()
      const p = c.points[0] as [number, number]
      points.push(p)
      current = p
      subStart = p
      continue
    }

    if (c.op === 'L' && c.points?.[0]) {
      const p = c.points[0] as [number, number]
      if (!current) {
        points.push(p)
      } else {
        points.push(p)
      }
      current = p
      if (!subStart) {
        subStart = points[0] ?? p
      }
      continue
    }

    if (c.op === 'C' && c.points?.length === 3 && current) {
      const [p1, p2, p3] = c.points as [[number, number], [number, number], [number, number]]
      const p0 = current
      for (let s = 1; s <= 6; s++) {
        const t = s / 6
        points.push(bezierPoint(p0, p1, p2, p3, t))
      }
      current = p3
      continue
    }

    if (c.op === 'Z' && subStart && current) {
      points.push(subStart)
      current = subStart
      closed = true
    }
  }

  flush()
  return out
}

function pathLength(points: [number, number][], closed: boolean): number {
  if (points.length < 2) {
    return 0
  }
  let len = 0
  const max = closed ? points.length : points.length - 1
  for (let i = 0; i < max; i++) {
    const j = (i + 1) % points.length
    len += Math.hypot(points[j][0] - points[i][0], points[j][1] - points[i][1])
  }
  return len
}

function polylineClosed(commands: PathCommand[]): boolean {
  return commands.some((c) => c.op === 'Z')
}

/** Evenly resample by arc length; closed paths include wrap segment. */
export function resamplePolyline(
  points: [number, number][],
  closed: boolean,
  sampleCount: number,
): [number, number][] {
  if (points.length < 2) {
    return points.slice()
  }

  const segs: Array<{ a: [number, number]; b: [number, number]; len: number }> = []
  const n = closed ? points.length : points.length - 1
  let total = 0
  for (let i = 0; i < n; i++) {
    const a = points[i]
    const b = points[(i + 1) % points.length]
    const len = Math.hypot(b[0] - a[0], b[1] - a[1])
    segs.push({ a, b, len })
    total += len
  }
  if (total < 1e-9) {
    return [points[0]]
  }

  const out: [number, number][] = []
  for (let k = 0; k < sampleCount; k++) {
    const target = (k / sampleCount) * total
    let acc = 0
    for (const s of segs) {
      if (acc + s.len >= target || s === segs[segs.length - 1]) {
        const local = s.len > 1e-12 ? (target - acc) / s.len : 0
        const t = Math.min(1, Math.max(0, local))
        const x = s.a[0] + t * (s.b[0] - s.a[0])
        const y = s.a[1] + t * (s.b[1] - s.a[1])
        out.push([x, y])
        break
      }
      acc += s.len
    }
  }
  return out
}

/** Translate centroid to origin; scale so RMS radius = 1. */
export function normalizeShape(points: [number, number][]): [number, number][] {
  if (!points.length) {
    return []
  }
  let cx = 0
  let cy = 0
  for (const p of points) {
    cx += p[0]
    cy += p[1]
  }
  cx /= points.length
  cy /= points.length
  let acc = 0
  for (const p of points) {
    acc += (p[0] - cx) ** 2 + (p[1] - cy) ** 2
  }
  const scale = Math.sqrt(acc / points.length) || 1
  return points.map((p) => [(p[0] - cx) / scale, (p[1] - cy) / scale])
}

function rotatePoint(p: [number, number], theta: number): [number, number] {
  const c = Math.cos(theta)
  const s = Math.sin(theta)
  return [p[0] * c - p[1] * s, p[0] * s + p[1] * c]
}

function reflectX(p: [number, number]): [number, number] {
  return [-p[0], p[1]]
}

function meanDistance(a: [number, number][], b: [number, number][]): number {
  const n = Math.min(a.length, b.length)
  if (!n) {
    return Number.POSITIVE_INFINITY
  }
  let s = 0
  for (let i = 0; i < n; i++) {
    s += Math.hypot(a[i][0] - b[i][0], a[i][1] - b[i][1])
  }
  return s / n
}

/** Minimum mean distance over rotations (and optional mirror). Lower is closer match. */
export function shapeDistance(
  pointsA: [number, number][],
  pointsB: [number, number][],
  options: { rotationSteps?: number; allowMirror?: boolean } = {},
): number {
  const steps = options.rotationSteps ?? ROTATION_STEPS
  const allowMirror = options.allowMirror ?? true

  if (pointsA.length < 2 || pointsB.length < 2) {
    return Number.POSITIVE_INFINITY
  }

  const na = normalizeShape(pointsA)
  const nb = normalizeShape(pointsB)

  let minD = Number.POSITIVE_INFINITY
  const variants: [number, number][][] = [nb]
  if (allowMirror) {
    variants.push(nb.map(reflectX))
  }

  for (const variant of variants) {
    for (let k = 0; k < steps; k++) {
      const theta = (k * 2 * Math.PI) / steps
      const rotated = variant.map((p) => rotatePoint(p, theta))
      minD = Math.min(minD, meanDistance(na, rotated))
    }
  }

  return minD
}

export function commandsRecordToPathCommands(
  commands: Array<Record<string, unknown>>,
): PathCommand[] {
  return commands.map((c) => {
    const op = String(c.op ?? '')
    const pts = c.points as number[][] | undefined
    return pts ? { op, points: pts } : { op }
  })
}

/** Prepare polyline from JSON commands (vector_path item). */
export function polylineFromItemCommands(commands: Array<Record<string, unknown>>): {
  polyline: [number, number][]
  closed: boolean
} {
  const cmds = commandsRecordToPathCommands(commands)
  const polylines = commandsToPolylines(cmds)
  const polyline = polylines.flatMap((p) => p.points)
  const closed = polylines.length === 1 ? polylines[0].closed : polylineClosed(cmds)
  return { polyline, closed }
}

function buildPointCloud(paths: PathPolyline[], sampleCount: number): [number, number][] {
  const usable = paths.filter((p) => p.points.length >= 2)
  if (!usable.length) {
    return []
  }

  const lengths = usable.map((p) => pathLength(p.points, p.closed))
  const totalLength = lengths.reduce((s, n) => s + n, 0)
  if (totalLength < 1e-9) {
    return usable.flatMap((p) => p.points.slice(0, 1))
  }

  const target = Math.max(sampleCount, usable.length)
  const exact = lengths.map((len) => (len / totalLength) * target)
  const counts = exact.map((v) => Math.max(1, Math.floor(v)))
  let assigned = counts.reduce((s, n) => s + n, 0)

  if (assigned < target) {
    const order = exact
      .map((v, i) => ({ i, frac: v - Math.floor(v) }))
      .sort((a, b) => b.frac - a.frac)
    let ptr = 0
    while (assigned < target) {
      counts[order[ptr % order.length].i] += 1
      assigned += 1
      ptr += 1
    }
  } else if (assigned > target) {
    let ptr = 0
    while (assigned > target) {
      const i = ptr % counts.length
      if (counts[i] > 1) {
        counts[i] -= 1
        assigned -= 1
      }
      ptr += 1
      if (ptr > counts.length * 16) {
        break
      }
    }
  }

  const cloud: [number, number][] = []
  for (let i = 0; i < usable.length; i++) {
    cloud.push(...resamplePolyline(usable[i].points, usable[i].closed, counts[i]))
  }
  return cloud
}

function comparePathCollections(
  queryPaths: PathPolyline[],
  candidatePaths: PathPolyline[],
  sampleCount = DEFAULT_SAMPLES,
  allowMirror = true,
): number {
  if (!queryPaths.length || !candidatePaths.length) {
    return Number.POSITIVE_INFINITY
  }

  const queryCloud = buildPointCloud(queryPaths, sampleCount)
  const candidateCloud = buildPointCloud(candidatePaths, sampleCount)
  if (queryCloud.length < 2 || candidateCloud.length < 2) {
    return Number.POSITIVE_INFINITY
  }

  const qCount = queryPaths.length
  const cCount = candidatePaths.length
  const countPenalty = (Math.abs(qCount - cCount) / Math.max(qCount, cCount, 1)) * 0.08

  const qClosedRatio = queryPaths.filter((p) => p.closed).length / qCount
  const cClosedRatio = candidatePaths.filter((p) => p.closed).length / cCount
  const closurePenalty = Math.abs(qClosedRatio - cClosedRatio) * 0.04

  return countPenalty + closurePenalty + shapeDistance(queryCloud, candidateCloud, { allowMirror })
}

/** Compare query polyline to candidate; uses resampling inside. */
export function comparePathShapes(
  queryPolyline: [number, number][],
  queryClosed: boolean,
  candidatePolyline: [number, number][],
  candidateClosed: boolean,
  sampleCount = DEFAULT_SAMPLES,
  allowMirror = true,
): number {
  return comparePathCollections(
    [{ points: queryPolyline, closed: queryClosed }],
    [{ points: candidatePolyline, closed: candidateClosed }],
    sampleCount,
    allowMirror,
  )
}

export function searchSimilarVectorPaths(options: {
  queryCommands: Array<Record<string, unknown>>
  vectors: Array<{
    id: string
    page_number: number
    commands: Array<Record<string, unknown>>
  }>
  maxDistance: number
  maxResults?: number
  sampleCount?: number
  allowMirror?: boolean
  excludeIds?: Set<string>
}): ShapeSearchHit[] {
  const {
    queryCommands,
    vectors,
    maxDistance,
    maxResults = 80,
    sampleCount = DEFAULT_SAMPLES,
    allowMirror = true,
    excludeIds,
  } = options
  const cmds = commandsRecordToPathCommands(queryCommands)
  const queryPaths = commandsToPolylines(cmds).filter((p) => p.points.length >= 2)
  if (!queryPaths.length) {
    return []
  }

  const hits: ShapeSearchHit[] = []
  for (const v of vectors) {
    if (excludeIds?.has(v.id)) {
      continue
    }
    const cPaths = commandsToPolylines(commandsRecordToPathCommands(v.commands)).filter(
      (p) => p.points.length >= 2,
    )
    if (!cPaths.length) {
      continue
    }
    const d = comparePathCollections(queryPaths, cPaths, sampleCount, allowMirror)
    if (d <= maxDistance) {
      hits.push({ id: v.id, pageNumber: v.page_number, score: d })
    }
  }

  hits.sort((a, b) => a.score - b.score)
  return hits.slice(0, maxResults)
}

export function pathCommandsToQueryRecords(cmds: PathCommand[]): Array<Record<string, unknown>> {
  return cmds.map((c) => {
    const o: Record<string, unknown> = { op: c.op }
    if (c.points) {
      o.points = c.points
    }
    return o
  })
}
