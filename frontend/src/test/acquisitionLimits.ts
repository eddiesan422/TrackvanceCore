import type { EffectiveLimits } from '../features/datasets/AcquisitionLimits'

export function acquisitionLimitsFixture(path: string): EffectiveLimits | undefined {
  if (!path.startsWith('/acquisitions/limits?')) return undefined
  const query = new URLSearchParams(path.split('?')[1])
  const route = query.get('route') === 'LEGACY_UPLOAD' ? 'LEGACY_UPLOAD' : 'ASYNC_ACQUISITION'
  const format = query.get('format') || 'CSV'
  return { route, format, limits: {
    compressed_bytes: { max: route === 'LEGACY_UPLOAD' ? 10 * 1024 ** 2 : 1024 ** 3, unit: 'bytes' },
    data_rows: { max: route === 'LEGACY_UPLOAD' || format === 'JSON' ? 100_000 : 1_000_000, unit: 'records' },
    ...(format === 'XLSX' ? { expanded_bytes: { max: 4 * 1024 ** 3, unit: 'bytes' }, metadata_bytes: { max: 8 * 1024 ** 2, unit: 'bytes' } } : {}),
  }, ...(format === 'XLSX' ? { physical_sheet_rows: 1_048_576 } : {}), header_row_number: null, inspection_limited: false }
}
