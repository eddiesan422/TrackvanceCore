import { randomBytes } from 'node:crypto'
import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'
import type { RecordData } from '../src/api/client'
import { fillSecret } from './identity-helpers'

const privateValues = new Set<string>()
test.afterEach(async ({ page }, info) => {
  for (const error of info.errors) for (const key of ['message', 'stack', 'snippet', 'value', 'errorContext'] as const) {
    if (typeof error[key] === 'string') for (const secret of privateValues) error[key] = error[key]!.split(secret).join('[REDACTED]')
  }
  await page.evaluate(() => document.body.replaceChildren()).catch(() => undefined)
  privateValues.clear()
})

// Only real HTTP/UI against an explicitly identified synthetic stack. The
// credential-bearing login is never included in traces, screenshots or video.
test.use({ trace: 'off', screenshot: 'off', video: 'off', actionTimeout: 30_000, navigationTimeout: 30_000 })
test.setTimeout(300_000)
test.skip(process.env.TV_CORE095_DOCUMENT_E2E !== 'true', 'Requires the guarded synthetic Core 0.9.5 environment')

async function signIn(page: Page) {
  const project = process.env.TV_CORE095_TEST_PROJECT || ''
  expect(project === 'trackvance-codex' || /^trackvance-v070-test-e2e-[a-z0-9-]*[a-f0-9]{12}$/.test(project), 'Explicit synthetic-stack ownership is mandatory').toBe(true)
  expect(['127.0.0.1', 'localhost']).toContain(new URL(process.env.TV_E2E_URL || '').hostname)
  await page.goto('/')
  if (project === 'trackvance-codex') {
    const username = process.env.TRACKVANCE_ADMIN_EMAIL, password = process.env.TRACKVANCE_ADMIN_PASSWORD
    if (!username || !password) throw new Error('Synthetic Codex credentials must be supplied through the environment')
    await page.getByRole('button', { name: 'Tengo una cuenta local', exact: true }).click()
    await page.getByLabel('Usuario o correo', { exact: true }).fill(username)
    privateValues.add(password)
    await fillSecret(page.getByLabel('Contraseña', { exact: true }), password)
    expect(await page.getByLabel('Contraseña', { exact: true }).inputValue() === password, 'Password input is ready').toBe(true)
    await page.getByRole('button', { name: 'Iniciar sesión', exact: true }).click()
  } else await page.getByRole('button', { name: 'Entrar al entorno demo', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
  const me = await (await page.request.get('/api/v1/me')).json()
  return { me, headers: { 'X-CSRF-Token': String(me.csrf_token) } }
}

async function post(page: Page, path: string, data: unknown, headers: Record<string, string>) {
  const response = await page.request.post(`/api/v1${path}`, { data, headers })
  expect(response.ok(), `Synthetic setup HTTP ${path}`).toBe(true)
  return response.json() as Promise<RecordData>
}

async function run(page: Page, contractId: string, versionId: string, headers: Record<string, string>) {
  const item = await post(page, '/intake/runs', { contract_id: contractId, dataset_version_id: versionId, requested_engine: 'POLARS' }, headers)
  await expect.poll(async () => (await (await page.request.get(`/api/v1/runs/${item.id}`)).json()).status,
    { timeout: 90_000, intervals: [250, 500, 1000] }).toBe('SUCCESS')
  return (await (await page.request.get(`/api/v1/runs/${item.id}`)).json()) as RecordData
}

async function setupProblem(page: Page, nonce: string, headers: Record<string, string>) {
  const dataset = await post(page, '/datasets', { name: `R095 synthetic ${nonce}`, description: 'Isolated known two-row resolution oracle. Synthetic test data only.' }, headers)
  const uploaded = await page.request.post(`/api/v1/datasets/${dataset.id}/versions/upload`, { headers,
    multipart: { file: { name: `R095-origin-${nonce}.csv`, mimeType: 'text/csv', buffer: Buffer.from('record_id,amount\nA,10\nB,-1\n') } } })
  expect(uploaded.status()).toBe(201)
  const version = await uploaded.json()
  const contract = await post(page, '/intake/contracts', { name: `R095 control ${nonce}`, dataset_id: dataset.id, config: { positive_columns: ['amount'] } }, headers)
  const origin = await run(page, contract.id, version.id, headers)
  expect(origin.decision).toBe('REJECTED')
  expect(origin.findings).toHaveLength(1)
  // The exception is actually created from the native finding in the browser.
  await page.goto(`/runs/${origin.id}`)
  await page.getByRole('button', { name: /^Hallazgos/ }).click()
  const created = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === `/api/v1/findings/${origin.findings[0].id}/exceptions`)
  await page.getByRole('button', { name: 'Crear excepción', exact: true }).click()
  const response = await created
  expect(response.status()).toBe(201)
  const exception = await response.json()
  await page.waitForURL(new RegExp(`/exceptions\\?id=${exception.id}$`))
  return { dataset, version, contract, origin, exception }
}

async function save(page: Page, id: string, state: string) {
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('Estado de gestión', { exact: true }).selectOption(state)
  const saved = page.waitForResponse(r => r.request().method() === 'PATCH' && new URL(r.url()).pathname === `/api/v1/exceptions/${id}`)
  await dialog.getByRole('button', { name: 'Guardar gestión', exact: true }).click()
  const response = await saved
  expect(response.status()).toBe(200)
  const value = await response.json()
  await expect(dialog.getByText(new RegExp(`^Versión ${value.version} ·`))).toBeVisible()
  await expect(dialog.getByLabel('Estado de gestión', { exact: true })).toHaveValue(state)
  await expect(dialog.getByLabel('Estado de gestión', { exact: true })).toBeEnabled()
  return value as RecordData
}

async function findOperation(page: Page, versionId: string, exceptionId: string, queue: string) {
  await page.goto(`/operations?queue=${queue}`)
  await expect(page.getByRole('heading', { name: 'Operación', exact: true })).toBeVisible()
  const loaded = page.waitForResponse(r => {
    const url = new URL(r.url())
    return r.request().method() === 'GET' && url.pathname === '/api/v1/operations' && url.searchParams.get('queue') === queue && url.searchParams.get('search') === versionId
  })
  await page.getByPlaceholder('Buscar por nombre, diagnóstico o identidad de seguimiento…', { exact: true }).fill(versionId)
  const response = await loaded
  expect(response.status()).toBe(200)
  expect((await response.json()).items.some((item: RecordData) => item.id === `exception:${exceptionId}`)).toBe(true)
  const card = page.locator('article.operation-card').filter({ has: page.locator(`a[href="/exceptions?id=${exceptionId}"]`) })
  await expect(card).toHaveCount(1)
  return card
}

test('native resolution: assignment → waiting → correction → verified comparison → technical close → reopen and lineage', async ({ page }, info) => {
  const started = Date.now(), nonce = randomBytes(6).toString('hex')
  const { me, headers } = await signIn(page)
  const { dataset, version, contract, origin, exception } = await setupProblem(page, nonce, headers)
  let dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('link', { name: 'Datos afectados', exact: true })).toHaveAttribute('href', `/datasets/${dataset.id}?version=${version.id}`)
  await expect(dialog.getByRole('button', { name: 'Resolver excepción', exact: true })).toBeDisabled()
  await dialog.getByLabel('Responsable', { exact: true }).selectOption(me.user.id)
  await dialog.getByLabel('Prioridad del caso', { exact: true }).selectOption('CRITICAL')
  await dialog.getByLabel('SLA (horas)', { exact: true }).fill('24')
  await save(page, exception.id, 'ASSIGNED')
  await dialog.getByLabel('Comentario para el historial', { exact: true }).fill('Waiting for the known synthetic source owner to correct amount B; no external write is initiated.')
  await save(page, exception.id, 'WAITING_EXTERNAL')
  const waiting = await findOperation(page, version.id, exception.id, 'waiting')
  await expect(waiting).toContainText(`version:${version.id}`)
  await expect(waiting).toContainText('El cierre de una excepción y la confirmación de una entrega son etapas independientes.')
  await waiting.getByRole('link', { name: 'Gestionar excepción', exact: true }).click()
  dialog = page.getByRole('dialog')
  await dialog.getByLabel('Comentario para el historial', { exact: true }).fill('Received corrected synthetic source; continue through the same control and exact population.')
  await save(page, exception.id, 'INVESTIGATING')
  await dialog.getByLabel('Causa raíz', { exact: true }).fill('Known source row B has a negative amount.')
  await dialog.getByLabel('Corrección aplicada', { exact: true }).fill('Replace only amount B with 1, preserving both declared record identities; validate original positive control.')
  await save(page, exception.id, 'PENDING_VALIDATION')
  await expect(dialog.getByRole('button', { name: 'Validar corrección', exact: true })).toBeDisabled()
  // Real upload acquisition is the correction boundary; no DB fixture mutation.
  await dialog.getByRole('link', { name: 'Datos afectados', exact: true }).click()
  await page.getByRole('button', { name: 'Nueva versión', exact: true }).click()
  const upload = page.getByRole('dialog', { name: 'Adquirir nueva versión', exact: true })
  await upload.getByLabel('Archivo', { exact: true }).setInputFiles({ name: `R095-corrected-${nonce}.csv`, mimeType: 'text/csv', buffer: Buffer.from('record_id,amount\nA,10\nB,1\n') })
  const registered = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === `/api/v1/datasets/${dataset.id}/acquisitions`)
  await upload.getByRole('button', { name: 'Registrar adquisición', exact: true }).click()
  const registration = await registered
  expect(registration.status()).toBe(202)
  const acquisition = await registration.json()
  await expect.poll(async () => (await (await page.request.get(`/api/v1/acquisitions/${acquisition.id}`)).json()).status,
    { timeout: 90_000 }).toBe('SUCCESS')
  const completed = await (await page.request.get(`/api/v1/acquisitions/${acquisition.id}`)).json()
  const correctedVersion = completed.output_version_id as string
  const correctedProfile = await (await page.request.get(`/api/v1/dataset-versions/${correctedVersion}/profile`)).json()
  expect(correctedProfile.row_count).toBe(2)
  expect(correctedProfile.sample.map((row: RecordData) => [row.record_id, row.amount])).toEqual([['A', '10'], ['B', '1']])
  // Execute the exact original configuration through its normal browser dialog.
  await page.goto(`/intake?contract_id=${contract.id}`)
  await page.getByRole('button', { name: 'Nueva ejecución', exact: true }).click()
  const execute = page.getByRole('dialog', { name: 'Nueva ejecución', exact: true })
  await execute.getByLabel('Contratos de datos', { exact: true }).selectOption(contract.id)
  await execute.getByLabel(/^Versión de /).selectOption(correctedVersion)
  await execute.getByLabel('Motor de procesamiento', { exact: true }).selectOption('POLARS')
  const submitted = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/v1/intake/runs')
  await execute.getByRole('button', { name: 'Validar datos', exact: true }).click()
  const submittedResponse = await submitted
  expect(submittedResponse.ok()).toBe(true)
  const candidate = await submittedResponse.json()
  await expect.poll(async () => (await (await page.request.get(`/api/v1/runs/${candidate.id}`)).json()).status, { timeout: 90_000 }).toBe('SUCCESS')
  expect((await (await page.request.get(`/api/v1/runs/${candidate.id}`)).json()).decision).toBe('APPROVED')
  await page.goto(`/exceptions?id=${exception.id}`)
  dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('button', { name: 'Resolver excepción', exact: true })).toBeDisabled()
  await expect(dialog).toContainText('otra población o lote')
  const failedValidation = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === `/api/v1/exceptions/${exception.id}/validate`)
  await dialog.getByRole('button', { name: 'Validar corrección', exact: true }).click()
  const failedResponse = await failedValidation
  expect(failedResponse.status()).toBe(200)
  expect((await failedResponse.json()).technical_validation.validated).toBe(false)
  await expect(dialog.getByRole('button', { name: 'Resolver excepción', exact: true })).toBeDisabled()
  await dialog.getByRole('link', { name: 'Revisar cambios entre versiones', exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`exception_id=${exception.id}`))
  await page.getByLabel('Versión anterior', { exact: true }).selectOption(version.id)
  await page.getByLabel('Versión posterior', { exact: true }).selectOption(correctedVersion)
  await page.getByRole('checkbox', { name: 'record_id (STRING)', exact: true }).check()
  const comparing = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === `/api/v1/datasets/${dataset.id}/comparisons`)
  await page.getByRole('button', { name: 'Comparar poblaciones completas', exact: true }).click()
  const comparisonHttp = await comparing
  expect(comparisonHttp.status()).toBe(202)
  const comparison = await comparisonHttp.json()
  await expect(page.getByRole('button', { name: 'Modificados: 1', exact: true })).toBeVisible({ timeout: 90_000 })
  await expect(page.getByRole('button', { name: 'Sin cambios: 1', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Ausentes: 0', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Incorporados: 0', exact: true })).toBeVisible()
  await expect(page).toHaveURL(new RegExp(`exception_id=${exception.id}`))
  await page.getByRole('link', { name: 'Volver a la incidencia y validar la corrección', exact: true }).click()
  dialog = page.getByRole('dialog')
  const verified = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === `/api/v1/exceptions/${exception.id}/validate`)
  await dialog.getByRole('button', { name: 'Validar corrección', exact: true }).click()
  const verifiedResponse = await verified
  expect(verifiedResponse.status()).toBe(200)
  expect((await verifiedResponse.json()).technical_validation.validated).toBe(true)
  await expect(dialog.getByText('Validada técnicamente', { exact: true })).toBeVisible()
  const resolvedResponse = page.waitForResponse(r => r.request().method() === 'PATCH' && new URL(r.url()).pathname === `/api/v1/exceptions/${exception.id}`)
  await dialog.getByRole('button', { name: 'Resolver excepción', exact: true }).click()
  const closedHttp = await resolvedResponse
  expect(closedHttp.status()).toBe(200)
  const closed = await closedHttp.json()
  expect(closed.state).toBe('RESOLVED')
  expect(closed.validation_run_id).toBe(candidate.id)
  expect(closed.validation_evidence.population_proofs).toEqual([expect.objectContaining({ method: 'VERIFIED_COMPLETE_COMPARISON_KEY_SET',
    comparison_id: comparison.id, report_metadata_verified: true, before_version_id: version.id, after_version_id: correctedVersion })])
  await expect(dialog.getByText('Esta excepción fue resuelta después de una validación técnica.', { exact: true })).toBeVisible()
  const finished = await findOperation(page, version.id, exception.id, 'finished')
  await finished.getByRole('link', { name: 'Gestionar excepción', exact: true }).click()
  dialog = page.getByRole('dialog')
  await dialog.getByLabel('Comentario para el historial', { exact: true }).fill('Reopen synthetic case to review recurrence; previous successful run must not validate this new investigation.')
  const reopening = page.waitForResponse(r => r.request().method() === 'PATCH' && new URL(r.url()).pathname === `/api/v1/exceptions/${exception.id}`)
  await dialog.getByRole('button', { name: 'Reabrir excepción', exact: true }).click()
  const reopenedHttp = await reopening
  expect(reopenedHttp.status()).toBe(200)
  const reopened = await reopenedHttp.json()
  expect(reopened.state).toBe('REOPENED')
  expect(reopened.technical_validation.status).toBe('NO_LATER_RUN')
  expect((await (await page.request.get(`/api/v1/runs/${origin.id}`)).json()).decision).toBe('REJECTED')
  const graphResponse = page.waitForResponse(r => r.request().method() === 'GET' && new URL(r.url()).pathname === `/api/v1/catalog/datasets/${dataset.id}/lineage-graph`)
  await page.goto(`/catalog/datasets/${dataset.id}?section=lineage&version_id=${correctedVersion}`)
  const graphHttp = await graphResponse
  expect(graphHttp.status()).toBe(200)
  const graph = await graphHttp.json()
  const comparisonNode = graph.nodes.find((node: RecordData) => node.type === 'VERSION_COMPARISON' && node.resource_id === comparison.id)
  expect(comparisonNode).toBeTruthy()
  await expect(page.getByLabel('Diagrama de linaje; usa las flechas para desplazar', { exact: true })).toBeVisible()
  await page.locator('button.lineage-node').filter({ hasText: comparisonNode.label }).click()
  await expect(page.getByLabel('Detalle del nodo', { exact: true }).getByRole('link', { name: 'Ver detalle autorizado', exact: true }))
    .toHaveAttribute('href', `/datasets/${dataset.id}/versions/compare?report=${comparison.id}`)
  await info.attach('resolution-095-native-evidence.json', { body: JSON.stringify({ nonce, dataset_id: dataset.id, origin_version_id: version.id,
    corrected_version_id: correctedVersion, origin_run_id: origin.id, candidate_run_id: candidate.id, acquisition_id: acquisition.id,
    exception_id: exception.id, resolved_state: closed.state, reopened_state: reopened.state, proof: closed.validation_evidence.population_proofs,
    lineage_comparison_node: comparisonNode.resource_id, elapsed_seconds: (Date.now() - started) / 1000 }), contentType: 'application/json' })
})

test('native resolution: administrative close stays unverified and source content restriction blocks case and operations', async ({ page }, info) => {
  const started = Date.now(), nonce = randomBytes(6).toString('hex')
  const { headers } = await signIn(page)
  const { dataset, version, exception } = await setupProblem(page, nonce, headers)
  let dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('button', { name: 'Resolver excepción', exact: true })).toBeDisabled()
  await dialog.getByText('Cierre administrativo', { exact: true }).click()
  await dialog.getByLabel('Decisión administrativa', { exact: true }).selectOption('NOT_APPLICABLE')
  await expect(dialog.getByRole('button', { name: 'Registrar cierre administrativo', exact: true })).toBeDisabled()
  await dialog.getByLabel('Motivo administrativo', { exact: true }).fill('Explicit synthetic fixture outside the operational population; not a technical correction.')
  const closing = page.waitForResponse(r => r.request().method() === 'PATCH' && new URL(r.url()).pathname === `/api/v1/exceptions/${exception.id}`)
  await dialog.getByRole('button', { name: 'Registrar cierre administrativo', exact: true }).click()
  const closedHttp = await closing
  expect(closedHttp.status()).toBe(200)
  const closed = await closedHttp.json()
  expect(closed.state).toBe('NOT_APPLICABLE')
  expect(closed.validation_run_id).toBeNull()
  expect(closed.technical_validation.validated).toBe(false)
  await expect(dialog).toContainText('No se considera una resolución técnica.')
  await dialog.getByLabel('Comentario para el historial', { exact: true }).fill('Reopen to exercise a current source restriction without editing any production resource.')
  const reopening = page.waitForResponse(r => r.request().method() === 'PATCH' && new URL(r.url()).pathname === `/api/v1/exceptions/${exception.id}`)
  await dialog.getByRole('button', { name: 'Reabrir excepción', exact: true }).click()
  expect((await reopening).status()).toBe(200)
  await expect(dialog.getByLabel('Estado de gestión', { exact: true })).toHaveValue('REOPENED')
  // A real native catalog restriction is applied through the browser UI.
  await page.goto(`/catalog/datasets/${dataset.id}`)
  await page.getByRole('button', { name: 'Bloquear uso', exact: true }).click()
  const restriction = page.getByRole('dialog', { name: 'Bloquear uso del dataset', exact: true })
  await restriction.getByLabel('Alcance', { exact: true }).selectOption('CONTENT')
  await restriction.getByLabel('Motivo', { exact: true }).fill(`R095 synthetic content restriction ${nonce}`)
  const blocking = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === `/api/v1/catalog/datasets/${dataset.id}/blocks`)
  await restriction.getByRole('button', { name: 'Confirmar bloqueo', exact: true }).click()
  const blockHttp = await blocking
  expect(blockHttp.status()).toBe(201)
  const block = await blockHttp.json()
  try {
    const denial = page.waitForResponse(r => r.request().method() === 'GET' && new URL(r.url()).pathname === `/api/v1/exceptions/${exception.id}`)
    await page.goto(`/exceptions?id=${exception.id}`)
    expect((await denial).status()).toBe(403)
    dialog = page.getByRole('dialog')
    await expect(dialog.getByLabel('Estado de gestión', { exact: true })).toHaveCount(0)
    await expect(dialog.getByRole('button', { name: 'Resolver excepción', exact: true })).toHaveCount(0)
    const denied = await page.request.get(`/api/v1/exceptions/${exception.id}`)
    expect(denied.status()).toBe(403)
    expect((await denied.json()).error.code).toBe('DATASET_BLOCKED')
    await page.goto('/operations?queue=intervention')
    const loaded = page.waitForResponse(r => {
      const url = new URL(r.url())
      return r.request().method() === 'GET' && url.pathname === '/api/v1/operations' && url.searchParams.get('queue') === 'intervention' && url.searchParams.get('search') === version.id
    })
    await page.getByPlaceholder('Buscar por nombre, diagnóstico o identidad de seguimiento…', { exact: true }).fill(version.id)
    const feedResponse = await loaded
    expect(feedResponse.status()).toBe(200)
    expect((await feedResponse.json()).items).toEqual([])
    await expect(page.getByRole('heading', { name: 'Sin operaciones en esta vista', exact: true })).toBeVisible()
    await expect(page.locator('article.operation-card')).toHaveCount(0)
    // Content restriction does not silently grant content through the feed.
    const feed = await (await page.request.get(`/api/v1/operations?queue=intervention&search=${version.id}`)).json()
    expect(feed.items).toEqual([])
    await info.attach('resolution-095-restriction-evidence.json', { body: JSON.stringify({ nonce, dataset_id: dataset.id, exception_id: exception.id,
      administrative_state: closed.state, technical_validated: closed.technical_validation.validated, block_id: block.id, source_denial: 'DATASET_BLOCKED',
      visible_operation_count: feed.items.length, elapsed_seconds: (Date.now() - started) / 1000 }), contentType: 'application/json' })
  } finally {
    const released = await page.request.patch(`/api/v1/catalog/blocks/${block.id}`, { headers,
      data: { expected_version: block.version, active: false, reason: 'Synthetic E2E finished; restore only this explicitly created fixture restriction.' } })
    expect(released.status()).toBe(200)
  }
})
