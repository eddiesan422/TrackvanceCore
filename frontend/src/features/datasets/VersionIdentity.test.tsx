import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { renderApp } from '../../test/render'
import { ProfilingPolicy, SampleValue, VersionIdentity } from './VersionIdentity'

vi.mock('../../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../../api/client')>(), download: vi.fn() }))

describe('Observed values and DatasetVersion identity', () => {
  it('preserves snapshot identity without linking to a deleted connection', () => {
    renderApp(<VersionIdentity connectionState="DELETED" version={{ source_type: 'POSTGRESQL', filename: 'snapshot.parquet', ingestion_metadata: { source: { connection_id: 'deleted-connection', connection_version_id: 'historical-config' } } }}/>)
    expect(screen.getByDisplayValue('historical-config')).toBeInTheDocument()
    expect(screen.getByDisplayValue('snapshot.parquet')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Ver conexión de origen' })).not.toBeInTheDocument()
  })

  it('distinguishes null, empty string, whitespace, Unicode and identifier zeros', () => {
    render(<><SampleValue value={null}/><SampleValue value=""/><SampleValue value="  Cliente 12  "/><SampleValue value="Bogotá"/><SampleValue value="001234567"/></>)
    expect(screen.getByText('null')).toHaveClass('null-value')
    expect(screen.getByText('"" (texto vacío)')).toHaveClass('empty-value')
    expect(screen.getByText('Cliente 12').textContent).toBe('  Cliente 12  ')
    expect(screen.getByText('Bogotá')).toBeInTheDocument()
    expect(screen.getByText('001234567')).toBeInTheDocument()
  })

  it('labels Intake output as a derived artifact with canonical Parquet and lineage', () => {
    renderApp(<VersionIdentity version={{ source_type: 'INTAKE_OUTPUT', filename: 'accepted.parquet', sha256: 'derived-hash', schema_hash: 'schema-hash', parent_version_id: 'input-version', source_run_id: 'intake-run', artifacts: [{ artifact_id: 'parquet-id', name: 'accepted.parquet', kind: 'INTAKE_ACCEPTED', sha256: 'derived-hash', size_bytes: 200 }] }}/>)
    expect(screen.queryByLabelText('Archivo original')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Artefacto derivado')).toHaveValue('accepted.parquet')
    expect(screen.getByLabelText('DatasetVersion de entrada')).toHaveValue('input-version')
    expect(screen.getByRole('link', { name: 'Ver ejecución de Intake de origen' })).toHaveAttribute('href', '/runs/intake-run')
    expect(screen.getByRole('button', { name: 'Descargar Parquet' })).toBeEnabled()
  })

  it('retains original upload identity and enforces download permission', () => {
    renderApp(<VersionIdentity version={{ source_type: 'ORIGINAL_UPLOAD', has_original_upload: true, filename: 'input.csv', ingestion_metadata: { source_format: 'CSV', format_label: 'CSV', reader_options: { delimiter: ';' } }, artifacts: [{ artifact_id: 'upload-id', kind: 'ORIGINAL_UPLOAD', name: 'input.csv', size_bytes: 20 }] }}/>, { permissions: [] })
    expect(screen.getByLabelText('Archivo original')).toHaveValue('input.csv')
    expect(screen.getByLabelText('Formato de origen')).toHaveValue('CSV')
    expect(screen.getByLabelText('Delimitador')).toHaveValue('Punto y coma (;)')
    expect(screen.queryByLabelText('Artefacto derivado')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Descargar original' })).toBeDisabled()
  })

  it('shows the selected Excel sheet as immutable ingestion evidence', () => {
    renderApp(<VersionIdentity version={{ source_type: 'ORIGINAL_UPLOAD', filename: 'ventas.xlsx', ingestion_metadata: { source_format: 'XLSX', format_label: 'Excel XLSX', reader_options: { sheet_name: 'Ventas 2026' } } }}/>)
    expect(screen.getByLabelText('Formato de origen')).toHaveValue('Excel XLSX')
    expect(screen.getByLabelText('Hoja de Excel')).toHaveValue('Ventas 2026')
  })

  it('does not label a non-Parquet derived artifact as an original upload', () => {
    renderApp(<VersionIdentity version={{ source_type: 'INTAKE_OUTPUT', filename: 'accepted.parquet', artifacts: [{ artifact_id: 'report-id', kind: 'INTAKE_ACCEPTED', name: 'accepted.csv', size_bytes: 20 }] }}/>)
    expect(screen.queryByRole('button', { name: 'Descargar original' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Descargar artefacto' })).toBeEnabled()
  })

  it('does not claim exact observed profiling for preserved historical profiles', () => {
    const { rerender } = render(<ProfilingPolicy profile={{ row_count: 120 }}/>)
    expect(screen.getByText(/Perfil histórico/)).toHaveTextContent('podía normalizar espacios y texto vacío')
    expect(screen.queryByText(/Perfil de valores observados:/)).not.toBeInTheDocument()
    rerender(<ProfilingPolicy profile={{ profiling_policy: 'OBSERVED_EXACT_V2' }}/>)
    expect(screen.getByText(/Perfil de valores observados:/)).toHaveTextContent('Null y texto vacío se contabilizan por separado')
    expect(screen.queryByText(/Perfil histórico/)).not.toBeInTheDocument()
  })

  it.each([['POSTGRESQL', 'PostgreSQL'], ['SQLSERVER', 'SQL Server']])('shows immutable %s source configuration and snapshot identity', (sourceType, sourceName) => {
    renderApp(<VersionIdentity version={{ source_type: sourceType, filename: 'sales.orders.parquet', sha256: 'snapshot-hash', ingestion_metadata: { source: { schema_name: 'sales', object_name: 'orders', connection_id: 'external-connection', connection_version: 4, connection_version_id: 'config-version-4', config_hash: 'config-hash' } }, artifacts: [{ artifact_id: 'canonical-id', kind: 'CANONICAL_PARQUET', name: 'sales.orders.parquet', size_bytes: 200, sha256: 'snapshot-hash' }] }}/>)
    expect(screen.getByLabelText('Fuente de la versión')).toHaveValue(`Snapshot de ${sourceName}`)
    expect(screen.queryByLabelText('Archivo original')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Snapshot canónico')).toHaveValue('sales.orders.parquet')
    expect(screen.getByLabelText('Versión de la conexión')).toHaveValue('4')
    expect(screen.getByLabelText('Identificador de configuración de conexión')).toHaveValue('config-version-4')
    expect(screen.getByLabelText('Hash de configuración de conexión')).toHaveValue('config-hash')
    expect(screen.getByLabelText('SHA-256 del snapshot')).toHaveValue('snapshot-hash')
    expect(screen.getByRole('link', { name: 'Ver conexión de origen' })).toHaveAttribute('href', '/connections/external-connection')
    expect(screen.getByRole('button', { name: 'Descargar Parquet' })).toBeEnabled()
  })
})
