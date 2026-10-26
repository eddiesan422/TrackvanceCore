import { createHash } from 'node:crypto'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, isAbsolute, join } from 'node:path'
import { expect, test, type Page } from '@playwright/test'
import type { RecordData as Json } from '../src/api/client'
import { fillSecret } from './identity-helpers'

// This driver never starts services, installs a model, mocks inference, or creates
// templates. The campaign owner freezes those inputs and the call budget first.
type Scenario = 'NATIVE_DIGITAL' | 'QWEN_VISUAL' | 'PADDLE_STRUCTURED' | 'SELECTIVE' | 'PARTIAL_FAULT' | 'CRITICAL_CONFLICT' | 'AUTO_ELIGIBLE' | 'CORRECTION'
interface ConsumerFixture {
  structure: string
  required_columns: string[]
  key_columns: string[]
  comparison_column: string
  delivery_configuration_id?: string
  delivery_draft?: Json
  receiver_destination_url: string
  receiver_receipt_url: string
  receiver_expected: Json
}
interface LosslessCase {
  id: string
  scenarios: Scenario[]
  provenance: 'REAL_MODEL' | 'SYNTHETIC_NATIVE' | 'COORDINATION_FAULT'
  fixture_receipt_sha256?: string
  family_id: string
  template_id: string
  extraction_strategy: string
  source_sha256: string
  expected_pages: number[]
  original_id?: string
  upload_path?: string
  configure_strategy?: boolean
  expected_issue_prefixes?: string[]
  expect_complete: boolean
  expect_auto?: boolean
  query_region_cell?: string
  normalization?: { path: string; observed: string; normalized: string; rule: string }[]
  replay?: boolean
  review?: { changes: Record<string, string>; confirmed: string[]; oracle_sha256: string }
  publish?: boolean
  profiles?: Record<string, { row_count: number; sample_sha256?: string }>
  consumers?: ConsumerFixture | ConsumerFixture[]
}
interface Manifest {
  version: 1
  run_id: string
  organization_id: string
  product_sha: string
  gateway_receipt_sha256: string
  max_uploads: number
  required_scenarios: Scenario[]
  cases: LosslessCase[]
}

const enabled = process.env.TV_CORE098_LOSSLESS_E2E === 'true'
const secrets = new Set<string>()
const digest = (value: string | Buffer) => createHash('sha256').update(value).digest('hex')
const jsonDigest = (value: unknown) => digest(JSON.stringify(value))
const profileDigest = (profile: Json) => jsonDigest({ id: profile.id, sha256: profile.sha256, schema_hash: profile.schema_hash, canonical_artifact_id: profile.canonical_artifact_id, row_count: profile.row_count, schema: profile.schema, sample: profile.sample })
const sha = (value: unknown) => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const terminal = (item: Json) => !['QUEUED', 'RUNNING'].includes(item.status)
const artifacts = (item: Json) => (item.recognition_artifacts || []).map((entry: Json) => ({ id: entry.artifact_id, sha256: entry.sha256 })).sort((a: Json, b: Json) => a.id.localeCompare(b.id))
function requireCondition(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message)
}
function localURL(value: string) {
  const url = new URL(value)
  requireCondition(['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname) && ['http:', 'https:'].includes(url.protocol), 'Only an explicitly local Codex/receiver URL is allowed')
  requireCondition(!url.username && !url.password, 'Credentials must not appear in a fixture URL')
  return url
}
function payloadCell(item: Json, path: string): Json {
  const [structure, row, field] = path.split('.')
  requireCondition(!!structure && !!row && !!field && path.split('.').length === 3, 'Invalid cell path in frozen manifest')
  return structure === 'headers' ? item.extraction.payload.fields[field] : item.extraction.payload.tables[structure]?.[Number(row)]?.[field]
}

test.use({ trace: 'off', screenshot: 'off', video: 'off', actionTimeout: 30_000, navigationTimeout: 30_000 })
test.setTimeout(7_200_000)
test.skip(!enabled, 'Requires a frozen private E3 manifest and the managed Codex stack; no default inference')
test.afterEach(async ({ browser }, info) => {
  for (const context of browser.contexts()) for (const cookie of await context.cookies()) if (cookie.value) secrets.add(cookie.value)
  for (const error of info.errors) for (const key of ['message', 'stack', 'snippet', 'value', 'errorContext'] as const) {
    if (typeof error[key] === 'string') for (const secret of secrets) error[key] = error[key]!.split(secret).join('[REDACTED]')
  }
  for (const context of browser.contexts()) for (const page of context.pages()) await page.evaluate(() => document.body.replaceChildren()).catch(() => undefined)
  secrets.clear()
})

async function login(page: Page, manifest: Manifest) {
  requireCondition(process.env.TV_CORE098_TEST_PROJECT === 'trackvance-codex', 'The E3 driver requires the explicitly managed trackvance-codex project')
  localURL(process.env.TV_E2E_URL || '')
  const username = process.env.TRACKVANCE_ADMIN_EMAIL, password = process.env.TRACKVANCE_ADMIN_PASSWORD
  requireCondition(username && password, 'Codex credentials must be supplied through the child process environment')
  secrets.add(username); secrets.add(password)
  await page.goto('/')
  await page.getByRole('button', { name: 'Tengo una cuenta local', exact: true }).click()
  await page.getByLabel('Usuario o correo', { exact: true }).fill(username)
  await fillSecret(page.getByLabel('Contraseña', { exact: true }), password)
  await page.getByRole('button', { name: 'Iniciar sesión', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
  const response = await page.request.get('/api/v1/me')
  requireCondition(response.status() === 200, 'The browser session did not authenticate')
  const me = await response.json()
  expect(me.organization.id).toBe(manifest.organization_id)
  secrets.add(me.csrf_token)
  return String(me.csrf_token)
}
async function get(page: Page, path: string): Promise<Json> {
  const response = await page.request.get(`/api/v1${path}`)
  requireCondition(response.status() === 200, `Unexpected GET status at ${path.split('?')[0]}`)
  return response.json()
}
async function post(page: Page, csrf: string, path: string, data: Json, status: number): Promise<Json> {
  const response = await page.request.post(`/api/v1${path}`, { data, headers: { 'X-CSRF-Token': csrf } })
  requireCondition(response.status() === status, `Unexpected POST status at ${path.split('?')[0]}`)
  return response.json()
}

async function openDocument(page: Page, item: Json) {
  await page.goto(`/documents/families/${item.family_id}?document=${item.id}`)
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('region', { name: 'Reconocimiento original conservado', exact: true })).toBeVisible()
  await expect(dialog.locator('.document-progress-bar')).toHaveCount(0)
  return dialog
}

async function obtainOriginal(page: Page, item: LosslessCase, output: string) {
  if (item.original_id) return item.original_id
  requireCondition(process.env.TV_CORE098_LOSSLESS_ALLOW_UPLOADS === 'true' && item.upload_path && isAbsolute(item.upload_path), 'New inference uploads require a second explicit flag and an absolute frozen source path')
  const source = await readFile(item.upload_path)
  requireCondition(digest(source) === item.source_sha256, 'Frozen source bytes changed before upload')
  const lock = join(output, `${item.id}.upload-intent.private.json`)
  let existing: Json | undefined
  try { existing = JSON.parse(await readFile(lock, 'utf8')) } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
  }
  if (existing) {
    requireCondition(existing.source_sha256 === item.source_sha256 && existing.family_id === item.family_id, 'An earlier upload intent is bound to different inputs')
    requireCondition(existing.original_id, 'An earlier upload intent has no receipt: reconcile the family manually; automatic re-upload is forbidden')
    return String(existing.original_id)
  }
  const engine = await get(page, `/documents/engines?family_id=${item.family_id}`)
  requireCondition(engine.configuration.max_retries === 0 && engine.configuration.max_parallel_jobs === 1, 'Fresh browser model cases require zero automatic retries and one admitted document')
  await writeFile(lock, JSON.stringify({ state: 'INTENT', family_id: item.family_id, source_sha256: item.source_sha256, at: new Date().toISOString() }), { flag: 'wx' })
  await page.goto(`/documents/families/${item.family_id}`)
  await page.getByRole('button', { name: 'Cargar documentos', exact: true }).click()
  const received = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === `/api/v1/documents/families/${item.family_id}/originals`)
  await page.getByRole('dialog').getByLabel('Seleccionar documentos', { exact: true }).setInputFiles(item.upload_path)
  const response = await received
  requireCondition(response.status() === 200, 'The original upload did not return its native receipt')
  const result = await response.json()
  requireCondition(result.items?.length === 1 && result.items[0].id, 'Upload receipt does not identify exactly one original')
  await writeFile(lock, JSON.stringify({ state: 'RECEIVED', family_id: item.family_id, source_sha256: item.source_sha256, original_id: result.items[0].id, at: new Date().toISOString() }))
  return String(result.items[0].id)
}

async function executeConsumer(page: Page, module: 'intake' | 'recon' | 'sentinel', configuration: Json, source: string, target?: string) {
  requireCondition(typeof configuration.dataset_id === 'string', 'The consumer configuration must identify its native dataset')
  await page.goto(`/${module}?dataset_id=${encodeURIComponent(configuration.dataset_id)}`)
  const start = page.getByRole('button', { name: 'Nueva ejecución', exact: true })
  await expect(start).toBeEnabled({ timeout: 60_000 })
  await start.click()
  const dialog = page.getByRole('dialog')
  await dialog.locator('select').first().selectOption(configuration.id)
  await expect(dialog.locator('select').nth(1).locator(`option[value="${source}"]`)).toBeAttached()
  await dialog.locator('select').nth(1).selectOption(source)
  if (target) {
    await expect(dialog.locator('select').nth(2).locator(`option[value="${target}"]`)).toBeAttached()
    await dialog.locator('select').nth(2).selectOption(target)
  }
  await dialog.getByRole('button', { name: { intake: 'Validar datos', recon: 'Conciliar fuentes', sentinel: 'Evaluar monitor' }[module], exact: true }).click()
  await page.waitForURL(/\/runs\/[^/?]+$/)
  const id = new URL(page.url()).pathname.split('/runs/')[1]
  await expect.poll(async () => (await get(page, `/runs/${id}`)).status, { timeout: 180_000 }).toBe('SUCCESS')
  const run = await get(page, `/runs/${id}`)
  expect(run.dataset_version_id).toBe(source)
  if (target) expect(run.target_version_id).toBe(target)
  return run
}

async function verifyConsumers(page: Page, csrf: string, item: LosslessCase, original: Json, spec: ConsumerFixture, prefix: string) {
  localURL(spec.receiver_receipt_url)
  requireCondition(Object.keys(spec.receiver_expected).length > 0, 'The local receiver must verify actual fields, counts or payload hashes')
  const publication = original.publications.find((entry: Json) => entry.extraction_id === original.extraction.id)
  requireCondition(publication?.versions?.[spec.structure], 'Consumers require the current revision native DatasetVersion')
  const version = publication.versions[spec.structure]
  const family = await get(page, `/documents/families/${item.family_id}`)
  const dataset = family.output_datasets[spec.structure]
  const contract = await post(page, csrf, '/intake/contracts', { name: `${prefix} Intake`, dataset_id: dataset, description: 'Automated E3 integration fixture; no human corpus acceptance.', config: { required_columns: spec.required_columns, max_error_rate: 0 } }, 201)
  const intake = await executeConsumer(page, 'intake', contract, version)
  expect(intake.decision).toBe('APPROVED')
  requireCondition(intake.output_version_id, 'Intake did not create its native approved output')
  const approved = await get(page, `/dataset-versions/${intake.output_version_id}/profile`)
  expect(approved.source_type).toBe('INTAKE_OUTPUT')
  const source = await get(page, `/dataset-versions/${version}/profile`)
  expect(approved.row_count).toBe(source.row_count)
  const sourceColumns = (source.schema || []).map((column: Json) => column.name)
  const project = (rows: Json[]) => rows.map(row => Object.fromEntries(sourceColumns.map((column: string) => [column, row[column]])))
  requireCondition(sourceColumns.length && jsonDigest(project(approved.sample)) === jsonDigest(project(source.sample)), 'Intake changed the native published sample despite having no transforms')
  const control = await post(page, csrf, '/recon/controls', { name: `${prefix} Recon`, dataset_id: dataset, target_dataset_id: approved.dataset_id, config: { key_columns: spec.key_columns, key_normalization: { trim: false, case: 'NONE', unicode_normalization: 'NONE' }, comparison_rules: [{ type: 'numeric_tolerance', source_column: spec.comparison_column, target_column: spec.comparison_column, parameters: { abs: '0' } }] } }, 201)
  const recon = await executeConsumer(page, 'recon', control, version, intake.output_version_id)
  expect(recon.decision).toBe('CONFORME')
  const monitor = await post(page, csrf, '/monitors', { name: `${prefix} Sentinel`, dataset_id: dataset, config: { required_columns: spec.required_columns, null_columns: spec.key_columns, max_null_rate: 0 } }, 201)
  const sentinel = await executeConsumer(page, 'sentinel', monitor, version)
  expect(sentinel.decision).toBe('HEALTHY')
  let delivery: Json
  if (spec.delivery_draft) {
    const destination = await get(page, `/delivery/destinations/${spec.delivery_draft.destination_id}`)
    requireCondition(destination.sink_type === 'REST', 'This browser fixture delivers only to an explicitly local REST receiver')
    // The destination may use a Codex container-network hostname. Its exact URL
    // is frozen by the owner; the browser receipt/control endpoint is loopback.
    expect(destination.options.url).toBe(spec.receiver_destination_url)
    const draft = { ...spec.delivery_draft, dataset_version_id: intake.output_version_id }
    expect((await post(page, csrf, '/delivery/preflight', draft, 200)).status).toBe('PASS')
    delivery = await post(page, csrf, '/delivery/configurations', { ...draft, name: `${prefix} Delivery` }, 201)
  } else {
    const configurations = await get(page, '/delivery/configurations')
    delivery = configurations.items.find((entry: Json) => entry.id === spec.delivery_configuration_id)
    requireCondition(delivery, 'The reserved Delivery configuration is missing')
    const destination = await get(page, `/delivery/destinations/${delivery.destination_id || delivery.config.destination_id}`)
    requireCondition(destination.sink_type === 'REST', 'This browser fixture delivers only to an explicitly local REST receiver')
    expect(destination.options.url).toBe(spec.receiver_destination_url)
  }
  requireCondition((delivery.dataset_version_id || delivery.config?.dataset_version_id) === intake.output_version_id || (delivery.dataset_version_id || delivery.config?.dataset_version_id) === version, 'Delivery must be pinned to this publication or its actual approved Intake version')
  await page.goto('/delivery')
  const card = page.locator('.delivery-config-list .config-card').filter({ has: page.getByRole('heading', { name: delivery.name, exact: true }) })
  const started = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/v1/delivery/runs')
  await card.getByRole('button', { name: 'Ejecutar', exact: true }).click()
  const response = await started
  requireCondition(response.status() === 202, 'Delivery did not register its native run')
  const queued = await response.json()
  await expect.poll(async () => (await get(page, `/runs/${queued.id}`)).status, { timeout: 180_000 }).toBe('SUCCESS')
  const receipt = await get(page, `/delivery/runs/${queued.id}/receipt`)
  expect(receipt.result).toBe('COMMITTED')
  await expect.poll(async () => {
    const result = await page.request.get(spec.receiver_receipt_url)
    if (result.status() !== 200) return false
    const body = await result.json()
    return Object.entries(spec.receiver_expected).every(([key, value]) => jsonDigest(body[key]) === jsonDigest(value))
  }, { timeout: 180_000 }).toBe(true)
  return { source_version_id: version, intake_run: intake.id, approved_version: intake.output_version_id, recon_run: recon.id, sentinel_run: sentinel.id, delivery_run: queued.id, delivery_receipt_sha256: jsonDigest(receipt), approved_sample_sha256: jsonDigest(approved.sample) }
}

test('E3 lossless browser flow uses native revisions, publication and four consumers', async ({ page }, info) => {
  const path = process.env.TV_CORE098_LOSSLESS_MANIFEST || ''
  requireCondition(isAbsolute(path) && path.replaceAll('\\', '/').includes('/.codex-local/'), 'A private absolute manifest path is mandatory')
  const bytes = await readFile(path)
  const manifest: Manifest = JSON.parse(bytes.toString('utf8'))
  requireCondition(manifest.version === 1 && /^[a-z0-9-]{6,80}$/.test(manifest.run_id), 'Invalid frozen E3 manifest identity')
  requireCondition(/^[a-f0-9]{40}$/.test(manifest.product_sha) && sha(manifest.gateway_receipt_sha256), 'The campaign must bind final product and gateway receipts before browser execution')
  requireCondition(manifest.cases.length > 0 && manifest.cases.length <= 10 && new Set(manifest.cases.map(item => item.id)).size === manifest.cases.length, 'Use one to ten distinct frozen cases')
  requireCondition(manifest.cases.filter(item => !item.original_id).length <= manifest.max_uploads, 'The frozen new-upload budget is exceeded')
  const selected = process.env.TV_CORE098_LOSSLESS_CASE_IDS?.split(',').filter(Boolean)
  if (!selected) for (const scenario of manifest.required_scenarios) requireCondition(manifest.cases.some(item => item.scenarios.includes(scenario)), `Missing required scenario ${scenario}`)
  const cases = manifest.cases.filter(item => !selected || selected.includes(item.id))
  requireCondition(cases.length > 0, 'The case filter selected no frozen inputs')
  const output = join(dirname(path), `browser-lossless-${manifest.run_id}`)
  await mkdir(output, { recursive: true })
  await writeFile(join(output, 'manifest.sha256'), digest(bytes) + '\n')
  const csrf = await login(page, manifest)
  const organizationBefore = await get(page, '/documents/engines')
  const results: Json[] = []
  for (const item of cases) {
    requireCondition(/^[a-z0-9-]+$/.test(item.id) && sha(item.source_sha256), 'Invalid frozen case/source identity')
    requireCondition(item.provenance !== 'COORDINATION_FAULT' || sha(item.fixture_receipt_sha256), 'Fault fixtures require a separately identified coordination receipt')
    const family = await get(page, `/documents/families/${item.family_id}`)
    requireCondition(family.name.startsWith('D098 E3 ') && family.active_template_id === item.template_id, 'Only explicitly reserved E3 families with a pinned active template are allowed')
    if (item.configure_strategy) {
      await page.goto(`/documents/families/${item.family_id}?view=references`)
      await page.getByText('Motor específico de esta familia', { exact: true }).click()
      const settings = page.getByRole('region', { name: 'Motor y disponibilidad', exact: true })
      await settings.getByLabel('Estrategia de extracción', { exact: true }).selectOption(item.extraction_strategy)
      if (['NATIVE_FIRST', 'QWEN_VISUAL', 'SELECTIVE'].includes(item.extraction_strategy)) await expect(settings.getByLabel('Contexto máximo (tokens)', { exact: true })).toHaveValue('32768')
      await settings.getByLabel('Motivo del cambio de motor', { exact: true }).fill(`Automated E3 reserved fixture ${manifest.run_id}/${item.id}; opt-in new jobs; no human corpus acceptance.`)
      const saved = page.waitForResponse(response => response.request().method() === 'PUT' && new URL(response.url()).pathname === '/api/v1/documents/engines')
      await settings.getByRole('button', { name: 'Guardar configuración auditada', exact: true }).click()
      requireCondition((await saved).status() === 200, 'The explicit opt-in strategy did not save')
    }
    const id = await obtainOriginal(page, item, output)
    await expect.poll(async () => terminal(await get(page, `/documents/originals/${id}`)), { timeout: 1_200_000, intervals: [1000, 3000, 5000] }).toBe(true)
    let original = await get(page, `/documents/originals/${id}`)
    expect(original.sha256).toBe(item.source_sha256)
    expect(original.template_id).toBe(item.template_id)
    expect(original.engine_snapshot.extraction_strategy).toBe(item.extraction_strategy)
    expect(original.execution.expected_pages).toEqual(item.expected_pages)
    expect(original.execution.recognition_complete).toBe(item.expect_complete)
    requireCondition(original.recognition_artifacts?.length, 'No durable recognition artifact reached the existing document review')
    if (item.expect_complete) expect(original.execution.pending_pages).toEqual([])
    else {
      requireCondition(original.execution.pending_pages.length || !original.execution.recognition_complete, 'The partial fixture must remain explicitly incomplete')
      requireCondition(original.quality?.status !== 'AUTO_VERIFIED' && !original.publications.length, 'An incomplete attempt must not automatically approve or publish')
    }
    if (item.expect_auto) expect(original.quality.status).toBe('AUTO_VERIFIED')
    for (const prefix of item.expected_issue_prefixes || []) requireCondition(original.issues.some((issue: string) => issue.startsWith(prefix)), `Missing required validator block ${prefix}`)
    for (const expected of item.normalization || []) {
      const cell = payloadCell(original, expected.path)
      requireCondition(cell?.normalization, 'Missing declared normalization provenance')
      requireCondition(jsonDigest(cell.normalization.observed) === jsonDigest(expected.observed) && jsonDigest(cell.normalization.normalized) === jsonDigest(expected.normalized), 'Observed/normalized values do not match the frozen post-recognition oracle')
      expect(cell.normalization.rule).toBe(expected.rule)
    }
    let dialog = await openDocument(page, original)
    const source = await page.request.get(`/api/v1/documents/originals/${id}/file`)
    requireCondition(source.status() === 200 && digest(await source.body()) === item.source_sha256, 'The served immutable source does not match the frozen original')
    const actualPageValues = await dialog.getByLabel('Página del original', { exact: true }).locator('option').evaluateAll(nodes => nodes.map(node => (node as HTMLOptionElement).value))
    for (const expected of item.expected_pages) expect(actualPageValues).toContain(String(expected))
    // Normal navigation waits for the initial preview; rapid-switch contention is tested separately.
    await expect(dialog.locator('.document-source img')).toHaveCount(1)
    await expect.poll(async () => dialog.locator('.document-source img').evaluateAll(images => images.every(image => (image as HTMLImageElement).complete && (image as HTMLImageElement).naturalWidth > 0))).toBe(true)
    await dialog.getByLabel('Página del original', { exact: true }).selectOption(String(item.expected_pages.at(-1)))
    await expect(dialog.locator('.document-source img')).toHaveCount(1)
    await expect.poll(async () => dialog.locator('.document-source img').evaluateAll(images => images.every(image => (image as HTMLImageElement).complete && (image as HTMLImageElement).naturalWidth > 0))).toBe(true)
    if (!item.expect_complete || item.expected_issue_prefixes?.length) await expect(dialog.getByRole('button', { name: original.publications.length ? 'Publicar revisión como corrección' : 'Publicar esta revisión en datasets', exact: true })).toBeDisabled()
    if (item.query_region_cell) {
      const cell = payloadCell(original, item.query_region_cell)
      expect(String(cell.geometry_kind).toUpperCase()).toBe('QUERY_REGION')
      await dialog.getByRole('button', { name: item.query_region_cell.split('.')[2], exact: true }).first().click()
      await expect(dialog.locator('.document-origin-region')).toHaveCount(0)
      await expect(dialog.getByText(/Región consultada; la ubicación exacta/)).toBeVisible()
    }
    const rawBefore = artifacts(original)
    for (const artifact of original.recognition_artifacts) {
      const link = dialog.locator(`a[href$="/recognition-artifacts/${artifact.artifact_id}/content"]`)
      await expect(link).toHaveAttribute('download', '')
      const response = await page.request.get((await link.getAttribute('href'))!)
      requireCondition(response.status() === 200, 'A linked raw recognition artifact was not downloadable')
      expect(response.headers()['content-disposition']).toMatch(/^attachment/i)
      const contentTypeOptions = (response.headers()['x-content-type-options'] || '').split(',').map(value => value.trim().toLowerCase())
      expect(contentTypeOptions.length).toBeGreaterThan(0)
      expect(contentTypeOptions.every(value => value === 'nosniff')).toBe(true)
      const raw = await response.body()
      expect(digest(raw)).toBe(artifact.sha256)
      expect(raw.length).toBe(artifact.bytes)
    }
    const oldPublications = [...original.publications]
    const oldProfiles = new Map<string, string>()
    for (const publication of oldPublications) for (const version of Object.values(publication.versions || {})) oldProfiles.set(String(version), profileDigest(await get(page, `/dataset-versions/${version}/profile`)))
    if (item.replay) {
      const previous = original.extraction?.id || null
      await dialog.getByLabel('Motivo de la nueva adaptación', { exact: true }).fill(`Automated E3 saved-recognition replay ${manifest.run_id}/${item.id}; no new inference and no human acceptance.`)
      const replayed = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === `/api/v1/documents/originals/${id}/replay`)
      await dialog.getByRole('button', { name: 'Adaptar resultados guardados sin nueva inferencia', exact: true }).click()
      const response = await replayed
      requireCondition(response.status() === 200, 'Saved recognition replay did not create its revision')
      original = await response.json()
      requireCondition(original.extraction?.id && original.extraction.id !== previous, 'Replay must create a new extraction revision')
      expect(artifacts(original)).toEqual(rawBefore)
      expect(original.publications).toEqual(oldPublications)
      dialog = await openDocument(page, original)
    }
    if (item.review) {
      requireCondition(sha(item.review.oracle_sha256) && item.provenance === 'SYNTHETIC_NATIVE', 'Automatic review confirmation is restricted to a separately frozen synthetic source oracle')
      for (const [path, value] of Object.entries(item.review.changes)) await dialog.getByLabel(`Valor ${path}`, { exact: true }).fill(value)
      for (const path of item.review.confirmed) await dialog.locator(`[id="document-cell-${path}"]`).getByRole('checkbox', { name: 'Confirmar valor', exact: true }).check()
      await dialog.getByLabel('Motivo de la revisión', { exact: true }).fill(`Automated E3 synthetic-oracle check ${item.review.oracle_sha256}; actor is an automated test agent, not a human corpus reviewer.`)
      const saved = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === `/api/v1/documents/originals/${id}/review`)
      await dialog.getByRole('button', { name: /Guardar revisión y comprobar publicación|Guardar valores de la revisión parcial/ }).click()
      const response = await saved
      requireCondition(response.status() === 200, 'The explicit synthetic review did not save')
      original = await response.json()
      dialog = await openDocument(page, original)
    }
    if (item.publish && !original.publications.some((entry: Json) => entry.extraction_id === original.extraction.id)) {
      const published = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === `/api/v1/documents/families/${item.family_id}/batches`)
      await dialog.getByRole('button', { name: original.publications.length ? 'Publicar revisión como corrección' : 'Publicar esta revisión en datasets', exact: true }).click()
      const response = await published
      requireCondition(response.status() === 200, 'Explicit publication did not register its native batch')
      const batch = await response.json()
      expect(batch.status).toBe('PUBLISHED')
      original = await get(page, `/documents/originals/${id}`)
      expect(original.execution.publication_complete).toBe(true)
    }
    for (const [version, hash] of oldProfiles) expect(profileDigest(await get(page, `/dataset-versions/${version}/profile`))).toBe(hash)
    for (const [structure, expected] of Object.entries(item.profiles || {})) {
      const publication = original.publications.find((entry: Json) => entry.extraction_id === original.extraction.id)
      requireCondition(publication?.versions?.[structure], 'Expected published structure does not have a native version')
      const profile = await get(page, `/dataset-versions/${publication.versions[structure]}/profile`)
      expect(profile.row_count).toBe(expected.row_count)
      if (expected.sample_sha256) expect(jsonDigest(profile.sample)).toBe(expected.sample_sha256)
      await page.goto(`/datasets/${profile.dataset_id}?version=${publication.versions[structure]}`)
      await expect(page.getByRole('heading').first()).toBeVisible()
    }
    const consumers = []
    for (const fixture of item.consumers ? Array.isArray(item.consumers) ? item.consumers : [item.consumers] : []) consumers.push(await verifyConsumers(page, csrf, item, original, fixture, `D098 E3 ${manifest.run_id} ${item.id} ${fixture.structure}`))
    results.push({ id: item.id, scenarios: item.scenarios, provenance: item.provenance, original_id: id, source_sha256: original.sha256, extraction_id: original.extraction?.id, execution: original.execution, raw_artifacts: artifacts(original), publication_ids: original.publications.map((entry: Json) => entry.batch_id), consumers, actor: 'AUTOMATED_TEST_AGENT', human_corpus_acceptance: false })
    await writeFile(join(output, `${item.id}.receipt.private.json`), JSON.stringify(results.at(-1), null, 2))
  }
  const organizationAfter = await get(page, '/documents/engines')
  expect(organizationAfter.configuration).toEqual(organizationBefore.configuration)
  expect(organizationAfter.version).toBe(organizationBefore.version)
  const result = { run_id: manifest.run_id, manifest_sha256: digest(bytes), product_sha: manifest.product_sha, gateway_receipt_sha256: manifest.gateway_receipt_sha256, cases: results, organization_settings_unchanged: true, human_corpus_acceptance: false, gateway_call_ledger_requires_separate_verification: true }
  await writeFile(join(output, 'browser.receipt.private.json'), JSON.stringify(result, null, 2))
  await info.attach('lossless-browser-metadata.json', { body: JSON.stringify(result), contentType: 'application/json' })
})
