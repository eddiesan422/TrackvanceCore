import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { renderApp } from '../../test/render'
import { LineageGraph } from './LineageGraph'

vi.mock('../../api/client', async original => ({ ...await original<typeof import('../../api/client')>(), api: vi.fn() }))
beforeEach(() => vi.resetAllMocks())

it('uses selected version, continues a real branch cursor, zooms and retains uncertain delivery evidence', async () => {
  const nodes = [
    { id: 'DATASET_VERSION:v1', resource_id: 'v1', type: 'DATASET_VERSION', label: 'Datos · v1', href: '/datasets/d?version=v1', technical: false, has_more: true, status: 'READY', expand: { upstream: 'inputs', downstream: 'outputs' } },
    { id: 'RUN:delivery', resource_id: 'delivery', type: 'RUN', label: 'Entrega pendiente', href: '/runs/delivery', technical: false, has_more: false, status: 'UNKNOWN', decision: null },
  ]
  vi.mocked(api).mockImplementation(async path => path.includes('section=versions') ? { items: [{ id: 'v1', version: 1 }, { id: 'v2', version: 2 }], total: 2 } : { root_id: 'DATASET_VERSION:v1', version_id: 'v1', nodes, edges: [{ id: 'e', source: nodes[0].id, target: nodes[1].id, relation: 'DESTINO_PREVISTO', stored_relation: 'DELIVERED_TO', state: 'UNCONFIRMED' }], complete: path.includes('cursor='), pending_branches: path.includes('cursor=') ? [] : ['DATASET_VERSION:v1'], branches: path.includes('cursor=') ? [] : [{ node_id: 'DATASET_VERSION:v1', direction: 'both', cursor: 'real-page-2' }] })
  const user = userEvent.setup()
  renderApp(<LineageGraph datasetId="d" initialVersion="v1"/>)
  await screen.findByRole('button', { name: /Versión de datos Datos · v1/ })
  expect(api).toHaveBeenCalledWith('/catalog/datasets/d/lineage-graph?projection=origin&max_depth=4&max_nodes=100&version_id=v1')
  await user.click(screen.getByRole('button', { name: 'Datos · v1 · continuar' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/catalog/datasets/d/lineage-graph?projection=origin&max_depth=4&max_nodes=100&version_id=v1&cursor=real-page-2'))
  await waitFor(() => expect(screen.queryByLabelText('Ramas pendientes')).not.toBeInTheDocument())
  await user.click(screen.getByRole('button', { name: 'Acercar linaje' }))
  expect(screen.getByText('120%')).toBeVisible()
  await user.click(screen.getByRole('button', { name: 'Restablecer zoom del linaje al 100 %' }))
  expect(screen.getByText('100%')).toBeVisible()
  await user.click(screen.getByRole('button', { name: /Entrega pendiente/ }))
  expect(screen.getByRole('link', { name: 'Ver detalle autorizado' })).toHaveAttribute('href', '/runs/delivery')
  await user.click(screen.getByText(/Detalle tabular y evidencia/))
  expect(screen.getByText('Sin confirmación de entrega completa')).toBeVisible()
  await user.selectOptions(screen.getByLabelText('Versión del linaje'), 'v2')
  await waitFor(() => expect(api).toHaveBeenCalledWith('/catalog/datasets/d/lineage-graph?projection=origin&max_depth=4&max_nodes=100&version_id=v2'))
  expect(vi.mocked(api).mock.calls.some(([path]) => path.includes('/profile'))).toBe(false)
})

it('keeps thousand batch payloads grouped and loads authorized technical metadata only on demand', async () => {
  const node = { id: 'RUN:r', resource_id: 'r', type: 'RUN', label: 'Entrega completa', module: 'delivery', href: '/runs/r', technical: false, has_more: false, status: 'SUCCESS', decision: 'COMMITTED', evidence_cursor: 'evidence-1', technical_groups: [{ relation: 'REST_BATCH_PAYLOAD', label: 'Payloads de lotes REST', relationship_count: 1000 }] }
  vi.mocked(api).mockImplementation(async path => path.includes('section=versions') ? { items: [], total: 0 } : path.includes('evidence=true') ? { owner_id: node.id, items: [{ relationship_id: 'payload-link', relation: 'REST_BATCH_PAYLOAD', label: 'Payload de lote REST', artifact: { id: 'ARTIFACT:a', resource_id: 'a', label: 'Lote 1', sha256: 'f'.repeat(64), size_bytes: 120 } }], next_cursor: path.includes('evidence-2') ? null : 'evidence-2', complete: path.includes('evidence-2'), scanned_relationships: 80 } : { root_id: node.id, version_id: 'v1', nodes: [node], edges: [], complete: true, pending_branches: [], branches: [] })
  const user = userEvent.setup()
  renderApp(<LineageGraph datasetId="d" initialVersion="v1"/>)
  await user.click(await screen.findByRole('button', { name: /Entrega completa/ }))
  expect(screen.getByText('1.000 referencias técnicas agrupadas')).toBeVisible()
  expect(vi.mocked(api).mock.calls.some(([path]) => path.includes('evidence=true'))).toBe(false)
  await user.click(screen.getByRole('button', { name: 'Consultar archivos técnicos' }))
  expect(await screen.findByText('Lote 1')).toBeVisible()
  expect(screen.getByText('120 bytes')).toBeVisible()
  await user.click(screen.getByRole('button', { name: 'Siguiente' }))
  await waitFor(() => expect(vi.mocked(api).mock.calls.some(([path]) => path.includes('cursor=evidence-2'))).toBe(true))
  expect(screen.getByText('Página 2')).toBeVisible()
  expect(vi.mocked(api).mock.calls.every(([path]) => !path.includes('/profile') && !path.includes('/download'))).toBe(true)
})

it('shows expansion failures and keeps the current authorized graph', async () => {
  vi.mocked(api).mockImplementation(async path => {
    if (path.includes('section=versions')) return { items: [], total: 0 }
    if (path.includes('cursor=')) throw new Error('La continuación venció; actualiza el linaje.')
    return { root_id: 'v', version_id: 'v', nodes: [{ id: 'v', resource_id: 'v', type: 'DATASET_VERSION', label: 'Versión disponible', technical: false, status: 'READY', has_more: true }], edges: [], complete: false, pending_branches: ['v'], branches: [{ node_id: 'v', direction: 'both', cursor: 'expired' }] }
  })
  const user = userEvent.setup()
  renderApp(<LineageGraph datasetId="d"/>)
  await user.click(await screen.findByRole('button', { name: 'Versión disponible · continuar' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('La continuación venció')
  expect(screen.getByRole('button', { name: /Versión de datos Versión disponible/ })).toBeVisible()
})

it('signals a finite path limit without presenting the branch as complete', async () => {
  vi.mocked(api).mockImplementation(async path => path.includes('section=versions') ? { items: [], total: 0 } : { root_id: 'v', version_id: 'v', nodes: [{ id: 'v', resource_id: 'v', type: 'DATASET_VERSION', label: 'Versión intermedia', href: '/datasets/d?version=v', technical: false, has_more: true, expansion_limit: true, expand: null }], edges: [], complete: false, pending_branches: ['v'], branches: [] })
  renderApp(<LineageGraph datasetId="d"/>)
  expect(await screen.findByText(/El camino alcanzó su límite de 64 identidades/)).toBeVisible()
})

it('shows the exact version resolved by the latest graph and keeps an explicitly selected historical version', async () => {
  vi.mocked(api).mockImplementation(async path => {
    if (path.includes('section=versions')) return { items: [{ id: 'v2', version: 2 }, { id: 'v1', version: 1 }], total: 2 }
    const id = path.includes('version_id=v1') ? 'v1' : 'v2'
    return { root_id: `DATASET_VERSION:${id}`, version_id: id, nodes: [{ id: `DATASET_VERSION:${id}`, resource_id: id, type: 'DATASET_VERSION', label: `Fuente · ${id}`, href: null, technical: false, has_more: false }], edges: [], complete: true, pending_branches: [], branches: [] }
  })
  const user = userEvent.setup()
  renderApp(<LineageGraph datasetId="d"/>)
  await screen.findByRole('button', { name: /Fuente · v2/ })
  expect(screen.getByLabelText('Versión del linaje')).toHaveValue('v2')
  expect(api).toHaveBeenCalledWith('/catalog/datasets/d/lineage-graph?projection=origin&max_depth=4&max_nodes=100')
  await user.selectOptions(screen.getByLabelText('Versión del linaje'), 'v1')
  await screen.findByRole('button', { name: /Fuente · v1/ })
  expect(screen.getByLabelText('Versión del linaje')).toHaveValue('v1')
  expect(api).toHaveBeenCalledWith('/catalog/datasets/d/lineage-graph?projection=origin&max_depth=4&max_nodes=100&version_id=v1')
})

it('fits the complete long business chain inside the measured viewport and restores readable real size', async () => {
  const nodes = Array.from({ length: 12 }, (_, index) => ({ id: `DATASET_VERSION:v${index}`, resource_id: `v${index}`, type: 'DATASET_VERSION', label: `Versión ${index}`, technical: false, has_more: false }))
  const edges = nodes.slice(1).map((node, index) => ({ id: `e${index}`, source: nodes[index].id, target: node.id, relation: 'DERIVED_FROM', stored_relation: 'DERIVED_FROM', state: 'RECORDED' }))
  vi.mocked(api).mockImplementation(async path => path.includes('section=versions') ? { items: [], total: 0 } : { root_id: nodes[0].id, version_id: 'v0', nodes, edges, complete: true, pending_branches: [], branches: [] })
  const user = userEvent.setup()
  const { container } = renderApp(<LineageGraph datasetId="d"/>)
  await screen.findByRole('button', { name: /Versión de datos Versión 11/ })
  const viewport = screen.getByLabelText('Diagrama de linaje; usa las flechas para desplazar')
  const scrollTo = vi.fn()
  Object.defineProperties(viewport, { clientWidth: { value: 344 }, clientHeight: { value: 590 }, scrollTo: { value: scrollTo } })
  await user.click(screen.getByRole('button', { name: 'Ajustar a pantalla' }))
  const fullCanvas = container.querySelector('.lineage-canvas') as HTMLElement
  const fittedExtent = fullCanvas.parentElement as HTMLElement
  expect(Number.parseFloat(fittedExtent.style.width)).toBeLessThanOrEqual(320)
  expect(Number.parseFloat(fittedExtent.style.height)).toBeLessThanOrEqual(566)
  expect(Number.parseFloat(fittedExtent.style.width)).toBeGreaterThan(0)
  expect(container.querySelectorAll('.lineage-node')).toHaveLength(12)
  expect(scrollTo).toHaveBeenCalledWith({ left: 0, top: 0 })
  expect(screen.getByRole('button', { name: 'Alejar linaje' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Acercar linaje' })).toBeEnabled()
  await user.click(screen.getByRole('button', { name: 'Restablecer zoom del linaje al 100 %' }))
  expect(screen.getByText('100%')).toBeVisible()
  expect(fullCanvas.style.transform).toBe('scale(1)')
  expect(screen.getByRole('button', { name: 'Alejar linaje' })).toBeEnabled()
})

it('reveals a real-size card with finite keyboard panning in a narrow desktop viewport without oscillating', async () => {
  const node = { id: 'DATASET_VERSION:v', resource_id: 'v', type: 'DATASET_VERSION', label: 'Versión visible', technical: false, has_more: false }
  vi.mocked(api).mockImplementation(async path => path.includes('section=versions') ? { items: [], total: 0 } : { root_id: node.id, version_id: 'v', nodes: [node], edges: [], complete: true, pending_branches: [], branches: [] })
  const user = userEvent.setup()
  renderApp(<LineageGraph datasetId="d"/>)
  const card = await screen.findByRole('button', { name: /Versión de datos Versión visible/ })
  const viewport = screen.getByLabelText('Diagrama de linaje; usa las flechas para desplazar')
  const cardLeft = Number.parseFloat(card.style.left)
  let scrollLeft = cardLeft + 10
  Object.defineProperties(viewport, { clientWidth: { value: 344 }, scrollLeft: { get: () => scrollLeft } })
  const scrollBy = vi.fn((movement: ScrollToOptions) => { scrollLeft += movement.left || 0 })
  Object.defineProperty(viewport, 'scrollBy', { value: scrollBy })
  viewport.focus()
  let visible = false
  for (let attempt = 0; attempt < 8; attempt++) {
    const left = cardLeft - scrollLeft, right = left + 256
    if (left >= 4 && right <= 340) { visible = true; break }
    await user.keyboard(left < 4 ? '{ArrowLeft}' : '{ArrowRight}')
  }
  expect(visible).toBe(true)
  expect(scrollBy).toHaveBeenCalled()
  expect(Math.abs(scrollBy.mock.calls[0][0].left || 0)).toBeLessThanOrEqual((344 - 256 - 8) / 2)
  expect(screen.getByText('100%')).toBeVisible()
})
