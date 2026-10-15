export type LineageNode = {
  id: string; resource_id: string; type: string; label: string; href: string | null
  status: string | null; decision: string | null; technical: boolean; has_more: boolean
  sha256?: string; row_count?: number; module?: string; role?: string
  technical_groups?: { relation: string; label: string; relationship_count: number }[]
  evidence_cursor?: string; expand?: { upstream: string; downstream: string }
  expansion_limit?: boolean
}
export type LineageEdge = {
  id: string; source: string; target: string; relation: string; stored_relation: string; state: string
  label?: string; evidence_ids?: string[]; stored_relations?: string[]; evidence_count?: number
}
export type LineageBranch = { node_id: string; direction: string; cursor: string }
export type LineageData = {
  root_id: string | null; version_id: string | null; nodes: LineageNode[]; edges: LineageEdge[]
  complete: boolean; pending_branches: string[]; branches?: LineageBranch[]; anchor_id?: string
}
export type LineageEvidence = {
  owner_id: string; items: { relationship_id: string; relation: string; label: string; artifact: LineageNode & { size_bytes?: number } }[]
  next_cursor: string | null; complete: boolean; scanned_relationships: number
}
export const CARD_WIDTH = 256
export const MIN_CARD_HEIGHT = 156
export const VIEW_NODE_LIMIT = 500

/** Identity, not display text, controls deduplication. Each response stays finite. */
export function mergeLineage(current: LineageData, page: LineageData, consumedCursor?: string) {
  const nodes = new Map(current.nodes.map(node => [node.id, node]))
  for (const node of page.nodes) nodes.set(node.id, node)
  if (nodes.size > VIEW_NODE_LIMIT) return { graph: page, focused: true }
  const edges = new Map(current.edges.map(edge => [edge.id, edge]))
  for (const edge of page.edges) {
    // Backend IDs identify a grouped relation's first evidence record. Preserve
    // all independently recorded relations if another page encounters the group.
    const existing = [...edges.values()].find(item => item.source === edge.source && item.target === edge.target && item.state === edge.state && (item.relation === 'MONITOREA') === (edge.relation === 'MONITOREA'))
    if (existing) {
      const evidence = [...new Set([...(existing.evidence_ids || [existing.id]), ...(edge.evidence_ids || [edge.id])])]
      edges.set(existing.id, { ...existing, evidence_ids: evidence, evidence_count: evidence.length, stored_relations: [...new Set([...(existing.stored_relations || [existing.stored_relation]), ...(edge.stored_relations || [edge.stored_relation])])] })
    } else edges.set(edge.id, edge)
  }
  const branches = new Map((current.branches || []).filter(branch => branch.cursor !== consumedCursor).map(branch => [`${branch.node_id}:${branch.direction}`, branch]))
  for (const branch of page.branches || []) branches.set(`${branch.node_id}:${branch.direction}`, branch)
  const pending = [...new Set([...branches.values()].map(branch => branch.node_id))]
  return { graph: { ...current, nodes: [...nodes.values()], edges: [...edges.values()], branches: [...branches.values()], pending_branches: pending, complete: branches.size === 0 }, focused: false }
}

/** Directed SCC ranks avoid backward transforms and remain finite for real cycles.
 * Monitoring is placed beside its input and never determines transformation rank.
 * Heights come from ResizeObserver; a conservative minimum protects first paint.
 */
export function layoutLineage(nodes: LineageNode[], edges: LineageEdge[], heights: Record<string, number> = {}) {
  const nodeMap = new Map(nodes.map(node => [node.id, node]))
  const business = nodes.filter(node => node.role !== 'MONITORING' && node.module !== 'sentinel')
  const ids = new Set(business.map(node => node.id))
  const transforms = edges.filter(edge => ids.has(edge.source) && ids.has(edge.target) && edge.relation !== 'MONITOREA')
  const adjacency = new Map(business.map(node => [node.id, [] as string[]]))
  for (const edge of transforms) adjacency.get(edge.source)!.push(edge.target)
  let index = 0
  const indices = new Map<string, number>(), low = new Map<string, number>(), stack: string[] = [], active = new Set<string>(), components: string[][] = []
  function visit(id: string) {
    indices.set(id, index); low.set(id, index++); stack.push(id); active.add(id)
    for (const next of adjacency.get(id) || []) {
      if (!indices.has(next)) { visit(next); low.set(id, Math.min(low.get(id)!, low.get(next)!)) }
      else if (active.has(next)) low.set(id, Math.min(low.get(id)!, indices.get(next)!))
    }
    if (low.get(id) === indices.get(id)) {
      const component: string[] = []; let next: string
      do { next = stack.pop()!; active.delete(next); component.push(next) } while (next !== id)
      components.push(component.sort())
    }
  }
  for (const node of business) if (!indices.has(node.id)) visit(node.id)
  const owner = new Map<string, number>(); components.forEach((group, i) => group.forEach(id => owner.set(id, i)))
  const ranks = components.map(() => 0), successors = components.map(() => new Set<number>()), incoming = components.map(() => 0)
  for (const edge of transforms) { const source = owner.get(edge.source)!, target = owner.get(edge.target)!; if (source !== target && !successors[source].has(target)) { successors[source].add(target); incoming[target]++ } }
  const ready = incoming.flatMap((count, i) => count === 0 ? [i] : [])
  for (let i = 0; i < ready.length; i++) for (const next of successors[ready[i]]) { ranks[next] = Math.max(ranks[next], ranks[ready[i]] + 1); if (--incoming[next] === 0) ready.push(next) }
  const rankOf = new Map(business.map(node => [node.id, ranks[owner.get(node.id)!]]))
  const monitors = nodes.filter(node => !ids.has(node.id))
  for (const node of monitors) {
    const parent = edges.find(edge => edge.target === node.id && edge.relation === 'MONITOREA')?.source
    rankOf.set(node.id, parent ? rankOf.get(parent) || 0 : 0)
  }
  const columns = new Map<number, LineageNode[]>()
  for (const node of business) { const rank = rankOf.get(node.id)!; columns.set(rank, [...(columns.get(rank) || []), node]) }
  const placed: (LineageNode & { x: number; y: number; width: number; height: number; rank: number })[] = []
  const heightOf = (id: string) => Math.max(MIN_CARD_HEIGHT, heights[id] || 0)
  let businessHeight = 0
  for (const [rank, column] of [...columns].sort(([a], [b]) => a - b)) {
    // Barycenter ordering keeps multiple inputs together without losing identity.
    column.sort((a, b) => {
      const parentY = (id: string) => { const parents = transforms.filter(edge => edge.target === id).map(edge => placed.find(node => node.id === edge.source)?.y).filter((y): y is number => y != null); return parents.length ? parents.reduce((sum, y) => sum + y, 0) / parents.length : 0 }
      return parentY(a.id) - parentY(b.id) || a.id.localeCompare(b.id)
    })
    let y = 66
    for (const node of column) { const height = heightOf(node.id); placed.push({ ...node, x: 44 + rank * (CARD_WIDTH + 116), y, width: CARD_WIDTH, height, rank }); y += height + 42 }
    businessHeight = Math.max(businessHeight, y)
  }
  const monitorY = new Map<number, number>()
  for (const node of monitors.sort((a, b) => a.id.localeCompare(b.id))) {
    const rank = rankOf.get(node.id)!, y = monitorY.get(rank) ?? businessHeight + 64, height = heightOf(node.id)
    placed.push({ ...node, x: 44 + rank * (CARD_WIDTH + 116), y, width: CARD_WIDTH, height, rank }); monitorY.set(rank, y + height + 42)
  }
  return { nodes: placed, edges: edges.filter(edge => nodeMap.has(edge.source) && nodeMap.has(edge.target)), width: Math.max(760, ...placed.map(node => node.x + node.width + 44)), height: Math.max(440, ...placed.map(node => node.y + node.height + 54)), monitoringY: monitors.length ? businessHeight + 22 : null }
}

/** Highlight only ancestors and descendants, never unrelated sibling consumers. */
export function lineagePath(selected: string | null, edges: LineageEdge[]) {
  const nodes = new Set<string>(), relations = new Set<string>()
  if (!selected) return { nodes, edges: relations }
  nodes.add(selected)
  for (const direction of ['upstream', 'downstream']) {
    const visited = new Set([selected]), queue = [selected]
    for (let i = 0; i < queue.length; i++) for (const edge of edges) {
      const next = direction === 'upstream' ? edge.target === queue[i] ? edge.source : null : edge.source === queue[i] ? edge.target : null
      if (next) { relations.add(edge.id); nodes.add(next); if (!visited.has(next)) { visited.add(next); queue.push(next) } }
    }
  }
  return { nodes, edges: relations }
}
