import { expect, test, type Page } from '@playwright/test'
import type { RecordData } from '../src/api/client'

test.setTimeout(120_000)

type JsonRecord = RecordData

async function signIn(page: Page) {
  await page.goto('/')
  await page.getByRole('button', { name: 'Entrar al entorno demo' }).click()
  await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
  const me = await (await page.request.get('/api/v1/me')).json()
  return { 'X-CSRF-Token': me.csrf_token as string }
}

async function post(page: Page, endpoint: string, data: unknown, headers: Record<string, string>) {
  const response = await page.request.post(`/api/v1${endpoint}`, { data, headers })
  expect(response.ok(), await response.text()).toBeTruthy()
  return response.json() as Promise<JsonRecord>
}

async function createDatasetVersion(
  page: Page,
  name: string,
  csv: string,
  headers: Record<string, string>,
) {
  const dataset = await post(page, '/datasets', { name, domain: 'E2E excepciones' }, headers)
  const upload = await page.request.post(`/api/v1/datasets/${dataset.id}/versions/upload`, {
    headers,
    multipart: {
      file: { name: `${name}.csv`, mimeType: 'text/csv', buffer: Buffer.from(csv) },
    },
  })
  expect(upload.status(), await upload.text()).toBe(201)
  return { dataset, version: await upload.json() as JsonRecord }
}

async function uploadVersion(
  page: Page,
  datasetId: string,
  filename: string,
  csv: string,
  headers: Record<string, string>,
) {
  const response = await page.request.post(`/api/v1/datasets/${datasetId}/versions/upload`, {
    headers,
    multipart: {
      file: { name: filename, mimeType: 'text/csv', buffer: Buffer.from(csv) },
    },
  })
  expect(response.status(), await response.text()).toBe(201)
  return response.json() as Promise<JsonRecord>
}

async function executeRecon(
  page: Page,
  controlId: string,
  sourceVersionId: string,
  targetVersionId: string,
  headers: Record<string, string>,
) {
  const run = await post(page, '/recon/runs', {
    control_id: controlId,
    source_version_id: sourceVersionId,
    target_version_id: targetVersionId,
  }, headers)
  await expect.poll(async () => {
    const response = await page.request.get(`/api/v1/runs/${run.id}`)
    return (await response.json()).status
  }, { timeout: 30_000, intervals: [250, 500, 1_000] }).toBe('SUCCESS')
  return (await (await page.request.get(`/api/v1/runs/${run.id}`)).json()) as JsonRecord
}

async function saveManagementState(page: Page, state: 'INVESTIGATING' | 'PENDING_VALIDATION') {
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('Estado de gestión', { exact: true }).selectOption(state)
  const response = page.waitForResponse(candidate =>
    new URL(candidate.url()).pathname.startsWith('/api/v1/exceptions/')
      && candidate.request().method() === 'PATCH',
  )
  await dialog.getByRole('button', { name: 'Guardar gestión', exact: true }).click()
  expect((await response).status()).toBe(200)
  await expect(dialog.getByLabel('Estado de gestión', { exact: true })).toHaveValue(state)
}

test('una excepción requiere validación posterior y el cierre administrativo permanece separado', async ({ page }) => {
  const headers = await signIn(page)
  const stamp = Date.now()
  const sourceCsv = 'record_id,amount\nA-01,10.00\n'
  const mismatchCsv = 'record_id,amount\nA-01,11.00\n'
  const correctedCsv = 'record_id,amount\nA-01,10.00\n'
  const secondMismatchCsv = 'record_id,amount\nA-01,15.00\n'

  const source = await createDatasetVersion(page, `E2E excepción ${stamp} origen`, sourceCsv, headers)
  const target = await createDatasetVersion(page, `E2E excepción ${stamp} destino`, mismatchCsv, headers)
  const control = await post(page, '/recon/controls', {
    name: `E2E excepción ${stamp} control`,
    dataset_id: source.dataset.id,
    target_dataset_id: target.dataset.id,
    config: {
      key_columns: ['record_id'],
      key_normalization: { trim: false, case: 'NONE', unicode_normalization: 'NONE' },
      comparison_rules: [{
        type: 'exact_compare',
        source_column: 'amount',
        target_column: 'amount',
        parameters: {},
      }],
    },
  }, headers)

  const originRun = await executeRecon(page, control.id, source.version.id, target.version.id, headers)
  expect(originRun.decision).toBe('WITH_FINDINGS')
  expect(originRun.metrics.counts.VALUE_MISMATCH).toBe(1)
  const originFinding = originRun.findings.find((finding: JsonRecord) => finding.code === 'VALUE_MISMATCH')
  expect(originFinding).toBeTruthy()
  const exception = await post(page, `/findings/${originFinding.id}/exceptions`, {}, headers)

  await page.goto(`/exceptions?id=${exception.id}`)
  let dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('link', { name: 'Ver ejecución de origen', exact: true }).first())
    .toHaveAttribute('href', `/runs/${originRun.id}`)
  await expect(dialog.getByText(control.name, { exact: true })).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'Resolver excepción', exact: true })).toBeDisabled()
  await expect(dialog.getByText('Resolución bloqueada', { exact: true })).toBeVisible()

  await saveManagementState(page, 'INVESTIGATING')
  await saveManagementState(page, 'PENDING_VALIDATION')
  dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('button', { name: 'Validar corrección', exact: true })).toBeDisabled()
  await expect(dialog.getByRole('button', { name: 'Resolver excepción', exact: true })).toBeDisabled()

  const pending = await (await page.request.get(`/api/v1/exceptions/${exception.id}`)).json() as JsonRecord
  const rejectedResolve = await page.request.patch(`/api/v1/exceptions/${exception.id}`, {
    headers,
    data: {
      version: pending.version,
      state: 'RESOLVED',
      root_cause: 'Importe distinto en el archivo destino.',
      resolution: 'Se corrigió el importe del destino.',
    },
  })
  expect(rejectedResolve.status()).toBe(422)

  const correctedVersion = await uploadVersion(
    page,
    target.dataset.id,
    `corregido-${stamp}.csv`,
    correctedCsv,
    headers,
  )
  const validationRun = await executeRecon(page, control.id, source.version.id, correctedVersion.id, headers)
  expect(validationRun.decision).toBe('CONFORME')
  expect(validationRun.findings).toHaveLength(0)

  await page.reload()
  dialog = page.getByRole('dialog')
  // Completing the later run refreshes pending exceptions automatically. The
  // final RESOLVED transition remains an explicit user decision.
  await expect(dialog.getByText('Validada técnicamente', { exact: true })).toBeVisible()
  await expect(dialog.getByRole('link', { name: 'Ver ejecución de validación', exact: true }))
    .toHaveAttribute('href', `/runs/${validationRun.id}`)

  await dialog.getByLabel('Causa raíz', { exact: true }).fill('El destino recibió un importe desactualizado.')
  await dialog.getByLabel('Corrección aplicada', { exact: true }).fill('El importe fue corregido y conciliado nuevamente.')
  await expect(dialog.getByRole('button', { name: 'Resolver excepción', exact: true })).toBeEnabled()
  const resolveResponse = page.waitForResponse(candidate =>
    new URL(candidate.url()).pathname === `/api/v1/exceptions/${exception.id}`
      && candidate.request().method() === 'PATCH',
  )
  await dialog.getByRole('button', { name: 'Resolver excepción', exact: true }).click()
  expect((await resolveResponse).status()).toBe(200)
  await expect(dialog.getByText('Esta excepción fue resuelta después de una validación técnica.')).toBeVisible()

  const resolved = await (await page.request.get(`/api/v1/exceptions/${exception.id}`)).json() as JsonRecord
  expect(resolved).toEqual(expect.objectContaining({
    state: 'RESOLVED',
    origin_run_id: originRun.id,
    run_id: originRun.id,
    configuration_id: control.id,
    validation_run_id: validationRun.id,
  }))
  expect(resolved.technical_validation).toEqual(expect.objectContaining({
    validated: true,
    can_resolve: false,
    validation_run_id: validationRun.id,
  }))
  const unchangedOrigin = await (await page.request.get(`/api/v1/runs/${originRun.id}`)).json() as JsonRecord
  expect(unchangedOrigin.decision).toBe('WITH_FINDINGS')
  expect(unchangedOrigin.metrics.counts.VALUE_MISMATCH).toBe(1)

  const laterMismatchVersion = await uploadVersion(
    page,
    target.dataset.id,
    `no-aplica-${stamp}.csv`,
    secondMismatchCsv,
    headers,
  )
  const administrativeRun = await executeRecon(page, control.id, source.version.id, laterMismatchVersion.id, headers)
  const administrativeFinding = administrativeRun.findings.find((finding: JsonRecord) => finding.code === 'VALUE_MISMATCH')
  expect(administrativeFinding).toBeTruthy()
  const administrativeCase = await post(page, `/findings/${administrativeFinding.id}/exceptions`, {}, headers)

  await page.goto(`/exceptions?id=${administrativeCase.id}`)
  dialog = page.getByRole('dialog')
  await dialog.getByText('Cierre administrativo', { exact: true }).click()
  await dialog.getByLabel('Decisión administrativa', { exact: true }).selectOption('NOT_APPLICABLE')
  await expect(dialog.getByRole('button', { name: 'Registrar cierre administrativo', exact: true })).toBeDisabled()
  await dialog.getByLabel('Motivo administrativo', { exact: true })
    .fill('El registro corresponde a una prueba fuera del alcance del control operativo.')
  await expect(dialog.getByRole('button', { name: 'Registrar cierre administrativo', exact: true })).toBeEnabled()
  const administrativeResponse = page.waitForResponse(candidate =>
    new URL(candidate.url()).pathname === `/api/v1/exceptions/${administrativeCase.id}`
      && candidate.request().method() === 'PATCH',
  )
  await dialog.getByRole('button', { name: 'Registrar cierre administrativo', exact: true }).click()
  expect((await administrativeResponse).status()).toBe(200)
  await expect(dialog.getByText(/decisión administrativa: No aplica/)).toBeVisible()

  const administrativelyClosed = await (await page.request.get(`/api/v1/exceptions/${administrativeCase.id}`)).json() as JsonRecord
  expect(administrativelyClosed).toEqual(expect.objectContaining({
    state: 'NOT_APPLICABLE',
    administrative_reason: 'El registro corresponde a una prueba fuera del alcance del control operativo.',
    validation_run_id: null,
  }))
  expect(administrativelyClosed.technical_validation.validated).toBe(false)
})
