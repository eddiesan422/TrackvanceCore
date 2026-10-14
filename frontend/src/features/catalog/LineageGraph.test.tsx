import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { renderApp } from '../../test/render'
import { LineageGraph } from './LineageGraph'

vi.mock('../../api/client', async original => ({ ...await original<typeof import('../../api/client')>(), api: vi.fn() }))
beforeEach(() => vi.resetAllMocks())

it('uses selected version, expands partial metadata, zooms and retains uncertain delivery evidence', async () => {
  const nodes = [
    { id: 'DATASET_VERSION:v1', resource_id: 'v1', type: 'DATASET_VERSION', label: 'Datos · v1', href: '/datasets/d?version=v1', technical: false, has_more: true, status: 'READY' },
    { id: 'RUN:delivery', resource_id: 'delivery', type: 'RUN', label: 'Entrega pendiente', href: '/runs/delivery', technical: false, has_more: false, status: 'UNKNOWN', decision: null },
  ]
  vi.mocked(api).mockImplementation(async path => path.includes('section=versions') ? { items: [{ id: 'v1', version: 1 }, { id: 'v2', version: 2 }], total: 2 } : { root_id: 'DATASET_VERSION:v1', version_id: 'v1', nodes, edges: [{ id: 'e', source: nodes[0].id, target: nodes[1].id, relation: 'DESTINO_PREVISTO', stored_relation: 'DELIVERED_TO', state: 'UNCONFIRMED' }], complete: path.includes('max_depth=3'), pending_branches: ['DATASET_VERSION:v1'] })
  const user = userEvent.setup()
  renderApp(<LineageGraph datasetId="d" initialVersion="v1"/>)
  await screen.findByRole('button', { name: /Datos · v1/ })
  expect(api).toHaveBeenCalledWith('/catalog/datasets/d/lineage-graph?max_depth=2&max_nodes=100&version_id=v1')
  await user.click(screen.getByRole('button', { name: 'Expandir recorrido' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/catalog/datasets/d/lineage-graph?max_depth=3&max_nodes=100&version_id=v1'))
  await user.click(screen.getByRole('button', { name: 'Acercar linaje' }))
  expect(screen.getByText('120%')).toBeVisible()
  await user.click(screen.getByRole('button', { name: /Entrega pendiente/ }))
  expect(screen.getByRole('link', { name: 'Ver detalle autorizado' })).toHaveAttribute('href', '/runs/delivery')
  await user.click(screen.getByText(/Detalle tabular y evidencia/))
  expect(screen.getByText('Sin confirmación de entrega completa')).toBeVisible()
  await user.selectOptions(screen.getByLabelText('Versión del linaje'), 'v2')
  await waitFor(() => expect(api).toHaveBeenCalledWith('/catalog/datasets/d/lineage-graph?max_depth=2&max_nodes=100&version_id=v2'))
  expect(vi.mocked(api).mock.calls.some(([path]) => path.includes('/profile'))).toBe(false)
})
