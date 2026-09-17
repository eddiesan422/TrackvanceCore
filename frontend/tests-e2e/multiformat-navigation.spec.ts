import { expect, test, type Page } from '@playwright/test'
import fs from 'node:fs/promises'
import type { RecordData } from '../src/api/client'

test.setTimeout(180_000)

const fixtureUrl = (name: string) => new URL(`../../scripts/tests/fixtures/${name}.base64`, import.meta.url)

async function fixture(name: string) {
  return Buffer.from((await fs.readFile(fixtureUrl(name), 'utf8')).trim(), 'base64')
}

async function signIn(page: Page) {
  await page.goto('/')
  await page.getByRole('button', { name: 'Entrar al entorno demo' }).click()
  await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
  const session = await (await page.request.get('/api/v1/me')).json()
  return { 'X-CSRF-Token': session.csrf_token as string }
}

async function post(
  page: Page,
  endpoint: string,
  data: unknown,
  headers: Record<string, string>,
) {
  const response = await page.request.post(`/api/v1${endpoint}`, { data, headers })
  expect(response.ok(), await response.text()).toBeTruthy()
  return response.json() as Promise<RecordData>
}

async function completedRun(page: Page, runId: string) {
  await expect.poll(async () => {
    const response = await page.request.get(`/api/v1/runs/${runId}`)
    return (await response.json()).status
  }, { timeout: 30_000, intervals: [250, 500, 1_000] }).toBe('SUCCESS')
  return (await (await page.request.get(`/api/v1/runs/${runId}`)).json()) as RecordData
}

test('CSV, XLSX, JSON, Parquet y TXT convergen al mismo contrato de Intake', async ({ page }) => {
  const headers = await signIn(page)
  const stamp = Date.now()
  const xlsx = await fixture('multiformat.xlsx')
  const parquet = await fixture('multiformat.parquet')
  const sources = [
    {
      format: 'CSV', filename: 'reader.csv', mimeType: 'text/csv',
      buffer: Buffer.from('record_key,amount\nA,10\nB,-2\n'),
    },
    {
      format: 'XLSX', filename: 'reader.xlsx',
      mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: xlsx,
    },
    {
      format: 'JSON', filename: 'reader.json', mimeType: 'application/json',
      buffer: Buffer.from('[{"record_key":"A","amount":10},{"record_key":"B","amount":-2}]'),
    },
    {
      format: 'PARQUET', filename: 'reader.parquet',
      mimeType: 'application/vnd.apache.parquet', buffer: parquet,
    },
    {
      format: 'TXT', filename: 'reader.txt', mimeType: 'text/plain',
      buffer: Buffer.from('record_key|amount\nA|10\nB|-2\n'),
    },
  ]

  const observed: RecordData[] = []
  for (const source of sources) {
    const name = `E2E lector ${source.format} ${stamp}`
    await page.goto('/datasets?upload=1')
    const dialog = page.getByRole('dialog')
    await dialog.getByLabel('Nombre del dataset', { exact: true }).fill(name)
    const inspected = page.waitForResponse(response =>
      new URL(response.url()).pathname === '/api/v1/datasets/uploads/inspect'
        && response.request().method() === 'POST',
    )
    await dialog.locator('input[type=file]').setInputFiles({
      name: source.filename,
      mimeType: source.mimeType,
      buffer: source.buffer,
    })
    expect((await inspected).status()).toBe(200)
    await expect(dialog.getByRole('region', { name: 'Inspección del archivo' })).toContainText(source.format)

    if (source.format === 'XLSX') {
      await expect(dialog.getByLabel('Hoja de Excel', { exact: true })).toBeVisible()
      const rescanned = page.waitForResponse(response =>
        new URL(response.url()).pathname === '/api/v1/datasets/uploads/inspect'
          && response.request().method() === 'POST',
      )
      await dialog.getByLabel('Hoja de Excel', { exact: true }).selectOption('Detalle')
      expect((await rescanned).status()).toBe(200)
      await expect(dialog.getByLabel('Tipo de record_key', { exact: true })).toBeVisible()
      await expect(dialog.getByLabel('Tipo de amount', { exact: true })).toBeVisible()
    }
    if (source.format === 'TXT') {
      await expect(dialog.getByLabel('Delimitador del TXT', { exact: true })).toContainText('Barra vertical')
      const rescanned = page.waitForResponse(response =>
        new URL(response.url()).pathname === '/api/v1/datasets/uploads/inspect'
          && response.request().method() === 'POST',
      )
      await dialog.getByLabel('Delimitador del TXT', { exact: true }).selectOption('|')
      expect((await rescanned).status()).toBe(200)
    }

    await expect(dialog.getByText('record_key', { exact: true })).toBeVisible()
    await expect(dialog.getByText('amount', { exact: true })).toBeVisible()
    await dialog.getByRole('button', { name: 'Cargar y analizar', exact: true }).click()
    await page.waitForURL(/\/datasets\/[^/?]+$/)
    const datasetId = page.url().split('/datasets/')[1]
    const dataset = await (await page.request.get(`/api/v1/datasets/${datasetId}`)).json() as RecordData
    const version = dataset.versions[0] as RecordData
    const profile = await (await page.request.get(`/api/v1/dataset-versions/${version.id}/profile`)).json() as RecordData
    expect(version.ingestion_metadata.source_format).toBe(source.format)
    expect(profile.row_count).toBe(2)
    expect(profile.schema.map((column: RecordData) => column.name)).toEqual(['record_key', 'amount'])
    if (source.format === 'XLSX') expect(version.ingestion_metadata.reader_options.sheet_name).toBe('Detalle')
    if (source.format === 'TXT') expect(version.ingestion_metadata.reader_options.delimiter).toBe('|')

    const contract = await post(page, '/intake/contracts', {
      name: `${name} contrato`,
      dataset_id: datasetId,
      config: { positive_columns: ['amount'], max_error_rate: 0 },
    }, headers)
    const queued = await post(page, '/intake/runs', {
      contract_id: contract.id,
      dataset_version_id: version.id,
    }, headers)
    const run = await completedRun(page, String(queued.id))
    observed.push({
      total_rows: run.metrics.total_rows,
      valid_rows: run.metrics.valid_rows,
      error_rows: run.metrics.error_rows,
      decision: run.decision,
    })
  }

  expect(observed).toEqual(Array.from({ length: sources.length }, () => ({
    total_rows: 2,
    valid_rows: 1,
    error_rows: 1,
    decision: 'REJECTED',
  })))
})

test('la navegación principal abre todos los módulos operativos sin errores de página', async ({ page }) => {
  await signIn(page)
  const pageErrors: string[] = []
  page.on('pageerror', error => pageErrors.push(error.message))
  const destinations = [
    ['Datasets', 'Datasets'],
    ['Intake', 'Data Intake'],
    ['ReconOps', 'ReconOps'],
    ['Sentinel', 'Sentinel'],
    ['Excepciones', 'Excepciones'],
    ['Biblioteca de reglas', 'Biblioteca de reglas'],
    ['Auditoría', 'Auditoría'],
  ]
  for (const [link, heading] of destinations) {
    await page.getByRole('navigation').getByRole('link', { name: new RegExp(`^${link}(?: IN| RE| SE)?$`) }).click()
    await expect(page.getByRole('heading', { name: heading, exact: true })).toBeVisible()
  }
  await page.getByRole('link', { name: 'Configuración', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Configuración', exact: true })).toBeVisible()
  await page.goto('/runs')
  await expect(page.getByRole('heading', { name: 'Historial de ejecuciones', exact: true })).toBeVisible()
  await page.getByRole('link', { name: 'Centro de control', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
  expect(pageErrors).toEqual([])
})
