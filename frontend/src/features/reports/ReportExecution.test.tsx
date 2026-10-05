import { screen } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { renderApp } from '../../test/render'
import { ReportExecution } from './ReportExecution'

vi.mock('../../api/client', async original => ({ ...await original<typeof import('../../api/client')>(), api: vi.fn() }))
beforeEach(() => vi.clearAllMocks())

it('shows measured publication counts and their recorded quality state', async () => {
  vi.mocked(api).mockResolvedValue({ id: 'job', profile: 'DATASET', status: 'SUCCESS', output_dataset_id: 'result', progress_percent: 100, progress_stage: 'Publicado', metrics: { rows: 3, canonical_size_bytes: 1250, parts: 1, elapsed_seconds: 2.5 } })
  renderApp(<ReportExecution executionId="job"/>, { permissions: ['reports:read'] })
  expect(await screen.findByText('3 filas de resultado')).toBeVisible()
  expect(screen.getByText('1.250 bytes canónicos preparados')).toBeVisible()
  expect(screen.getByText('1 partes')).toBeVisible()
  expect(screen.getByText('2,5 s')).toBeVisible()
  expect(screen.getByText(/Estado al publicar: Pendiente de validación de calidad/)).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Cancelar generación' })).not.toBeInTheDocument()
})

it('does not present a download as a background generation or invent unmeasured zeroes', async () => {
  vi.mocked(api).mockResolvedValue({ id: 'download', profile: 'DOWNLOAD', status: 'RUNNING', generation_status: 'RUNNING', transmission_status: 'STARTED', metrics: {} })
  renderApp(<ReportExecution executionId="download"/>, { permissions: ['reports:read', 'reports:generate'] })
  expect(await screen.findByText('— filas de resultado')).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Cancelar generación' })).not.toBeInTheDocument()
  expect(screen.queryByText(/El trabajo de generación continúa/)).not.toBeInTheDocument()
})
