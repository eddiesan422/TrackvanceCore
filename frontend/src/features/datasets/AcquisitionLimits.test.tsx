import { screen } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { renderApp } from '../../test/render'
import { acquisitionLimitsFixture } from '../../test/acquisitionLimits'
import { AcquisitionLimits, getAcquisitionLimits, limitFormat } from './AcquisitionLimits'

vi.mock('../../api/client', async original => ({ ...await original<typeof import('../../api/client')>(), api: vi.fn() }))
beforeEach(() => vi.clearAllMocks())

it('renders exact selected-route caps, physical rows and header from the backend descriptor', () => {
  const limits = acquisitionLimitsFixture('/acquisitions/limits?format=XLSX&route=ASYNC_ACQUISITION')!
  limits.limits.data_rows.max = 456789
  limits.limits.compressed_bytes.max = 450 * 1024 ** 2
  limits.header_row_number = 4
  renderApp(<AcquisitionLimits limits={limits}/>)
  expect(screen.getByText('456.789 registros')).toBeInTheDocument()
  expect(screen.getByText('450 MiB')).toBeInTheDocument()
  expect(screen.getByText(/1.048.576 filas, incluidos el encabezado/)).toBeInTheDocument()
  expect(screen.getByText(/Encabezado observado en la fila 4/)).toBeInTheDocument()
  expect(screen.queryByText('1.000.000 registros')).not.toBeInTheDocument()
})

it('requests the real legacy route without raising its JSON cap', async () => {
  vi.mocked(api).mockImplementation(async path => acquisitionLimitsFixture(path)!)
  const limits = await getAcquisitionLimits('JSON', 'LEGACY_UPLOAD')
  expect(api).toHaveBeenCalledWith('/acquisitions/limits?format=JSON&route=LEGACY_UPLOAD')
  expect(limits.limits.data_rows.max).toBe(100000)
  expect(limits.limits.compressed_bytes.max).toBe(10 * 1024 ** 2)
  expect(limitFormat('data.ndjson')).toBe('JSON_LINES')
})

it.each([
  { route: 'LEGACY_UPLOAD', format: 'XLSX', limits: { data_rows: { max: 100000, unit: 'records' } } },
  { route: 'ASYNC_ACQUISITION', format: 'XLSX', limits: {} },
  { route: 'ASYNC_ACQUISITION', format: 'XLSX', limits: { data_rows: { max: -1, unit: 'records' } } },
])('fails safely when the service does not provide a valid descriptor for the selected route', async value => {
  vi.mocked(api).mockResolvedValue(value)
  await expect(getAcquisitionLimits('XLSX', 'ASYNC_ACQUISITION')).rejects.toThrow('No están disponibles los límites efectivos')
})
