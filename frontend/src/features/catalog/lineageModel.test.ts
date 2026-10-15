import { expect, it } from 'vitest'
import { layoutLineage, lineagePath, mergeLineage, VIEW_NODE_LIMIT, type LineageData, type LineageEdge, type LineageNode } from './lineageModel'

function node(id: string, extra: Partial<LineageNode> = {}): LineageNode { return { id, resource_id: id, type: 'DATASET_VERSION', label: 'Nombre repetido', href: null, technical: false, has_more: false, status: 'READY', decision: null, ...extra } }
function edge(source: string, target: string, relation = 'RUN_INPUT'): LineageEdge { return { id: `${source}-${target}-${relation}`, source, target, relation, stored_relation: relation, state: 'RECORDED' } }
function graph(nodes: LineageNode[], edges: LineageEdge[] = []): LineageData { return { root_id: nodes[0]?.id || null, version_id: 'v', nodes, edges, complete: true, pending_branches: [], branches: [] } }

it('lays out directed multi-input transformations left to right using real measured heights without overlaps', () => {
  const nodes = [node('output'), node('input-a'), node('input-b'), node('report', { type: 'REPORT_EXECUTION' }), node('destination'), node('sentinel', { module: 'sentinel', role: 'MONITORING' })]
  const edges = [edge('input-a', 'report'), edge('input-b', 'report'), edge('report', 'output'), edge('output', 'destination'), edge('input-a', 'sentinel', 'MONITOREA')]
  const layout = layoutLineage(nodes, edges, { 'input-a': 340, 'input-b': 240, report: 302, sentinel: 270 })
  const positions = new Map(layout.nodes.map(value => [value.id, value]))
  for (const relation of edges.filter(value => value.relation !== 'MONITOREA')) expect(positions.get(relation.source)!.x + positions.get(relation.source)!.width).toBeLessThan(positions.get(relation.target)!.x)
  expect(positions.get('input-a')!.height).toBe(340)
  expect(positions.get('sentinel')!.y).toBeGreaterThan(Math.max(...layout.nodes.filter(value => value.id !== 'sentinel').map(value => value.y + value.height)))
  for (let i = 0; i < layout.nodes.length; i++) for (let j = i + 1; j < layout.nodes.length; j++) {
    const a = layout.nodes[i], b = layout.nodes[j]
    expect(a.x + a.width <= b.x || b.x + b.width <= a.x || a.y + a.height <= b.y || b.y + b.height <= a.y).toBe(true)
  }
  expect(layout.nodes).toHaveLength(6)
})

it('keeps cycles finite without rank inflation or discarding any recorded direction', () => {
  const edges = [edge('a', 'b'), edge('b', 'a'), edge('b', 'c')]
  const layout = layoutLineage([node('a'), node('b'), node('c')], edges)
  expect(layout.edges).toEqual(edges)
  expect(layout.nodes.every(value => Number.isFinite(value.x) && Number.isFinite(value.y))).toBe(true)
  expect(layout.nodes.find(value => value.id === 'c')!.rank).toBeGreaterThan(layout.nodes.find(value => value.id === 'a')!.rank)
})

it('merges cursor pages by identity, retains grouped evidence and consumes only the continued branch', () => {
  const first = graph([node('a'), node('b')], [edge('a', 'b')])
  first.complete = false; first.branches = [{ node_id: 'a', direction: 'downstream', cursor: 'one' }, { node_id: 'b', direction: 'upstream', cursor: 'other' }]
  const second = graph([node('b'), node('c')], [{ ...edge('a', 'b'), id: 'second-evidence', stored_relation: 'INTAKE_ACCEPTED_FROM' }, edge('b', 'c')])
  const result = mergeLineage(first, second, 'one')
  expect(result.graph.nodes.map(value => value.id)).toEqual(['a', 'b', 'c'])
  expect(result.graph.branches).toEqual([{ node_id: 'b', direction: 'upstream', cursor: 'other' }])
  expect(result.graph.complete).toBe(false)
  expect(result.graph.edges[0].evidence_count).toBe(2)
  expect(result.graph.edges[0].stored_relations).toEqual(['RUN_INPUT', 'INTAKE_ACCEPTED_FROM'])
})

it('focuses a finite new branch after 500 resources and keeps the returned continuation available', () => {
  const current = graph(Array.from({ length: VIEW_NODE_LIMIT }, (_, index) => node(`old-${index}`)))
  const page = graph([node('new')]); page.branches = [{ node_id: 'new', direction: 'downstream', cursor: 'next' }]
  const result = mergeLineage(current, page)
  expect(result.focused).toBe(true)
  expect(result.graph).toBe(page)
  expect(result.graph.branches![0].cursor).toBe('next')
})

it('highlights the selected ancestry and descendants without selecting sibling consumers', () => {
  const edges = [edge('source', 'selected'), edge('source', 'sibling'), edge('selected', 'destination')]
  const path = lineagePath('selected', edges)
  expect([...path.nodes].sort()).toEqual(['destination', 'selected', 'source'])
  expect(path.edges.has('source-sibling-RUN_INPUT')).toBe(false)
})
