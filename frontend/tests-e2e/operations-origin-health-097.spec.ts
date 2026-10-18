import { randomBytes } from 'node:crypto'
import { expect, test } from '@playwright/test'

test.use({ trace: 'off', screenshot: 'off', video: 'off' })
test.setTimeout(120_000)

test('OPS/DASH-097: procesos manuales separados, lectura acotada, ruta histórica y selector local con datos reales', async ({ page, context }, info) => {
  const nonce = randomBytes(6).toString('hex'), started = Date.now()
  await page.goto('/')
  if (process.env.TV_CORE095_TEST_PROJECT === 'trackvance-codex') {
    const username = process.env.TRACKVANCE_ADMIN_EMAIL, password = process.env.TRACKVANCE_ADMIN_PASSWORD
    if (!username || !password) throw new Error('Synthetic credentials must be provided through the environment')
    await page.getByRole('button', { name: 'Tengo una cuenta local', exact: true }).click()
    await page.getByLabel('Usuario o correo', { exact: true }).fill(username)
    await page.getByLabel('Contraseña', { exact: true }).fill(password)
    await page.getByRole('button', { name: 'Iniciar sesión', exact: true }).click()
  } else await page.getByRole('button', { name: 'Entrar al entorno demo', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
  const api = context.request, session = await (await api.get('/api/v1/me')).json()
  const headers = { 'X-CSRF-Token': session.csrf_token as string }
  const datasetHttp = await api.post('/api/v1/datasets', { headers, data: { name: `DASH097 zero ${nonce}` } })
  expect(datasetHttp.status()).toBe(201)
  const dataset = await datasetHttp.json()
  const uploadHttp = await api.post(`/api/v1/datasets/${dataset.id}/versions/upload`, { headers, multipart: {
    file: { name: 'negative.csv', mimeType: 'text/csv', buffer: Buffer.from('id,amount\n00001,-1.00\n00002,-2.00\n') },
  } })
  expect(uploadHttp.status()).toBe(201)
  const version = await uploadHttp.json()
  const configHttp = await api.post('/api/v1/intake/contracts', { headers, data: { name: `DASH097 reject ${nonce}`, dataset_id: dataset.id,
    config: { required_columns: ['id', 'amount'], positive_columns: ['amount'], max_error_rate: 0 } } })
  expect(configHttp.status()).toBe(201)
  const config = await configHttp.json()
  async function run() {
    const response = await api.post('/api/v1/intake/runs', { headers, data: { contract_id: config.id, dataset_version_id: version.id } })
    expect(response.status()).toBe(202)
    const registered = await response.json()
    await expect.poll(async () => (await (await api.get(`/api/v1/runs/${registered.id}`)).json()).status,
      { timeout: 30_000, intervals: [250, 500, 1000] }).toBe('SUCCESS')
    const completed = await (await api.get(`/api/v1/runs/${registered.id}`)).json()
    expect(completed.decision).toBe('REJECTED')
    return completed
  }
  const first = await run()
  async function personal(runId: string) {
    const response = await api.get(`/api/v1/notifications/inbox?grouped=true&process=run:${runId}`)
    expect(response.status()).toBe(200)
    return response.json()
  }
  type NewsItem = { id: string; resource_id: string; resource_type: string; module: string; origin: string; status: string; decision: string }
  async function personalPair(completed: { id: string; output_version_id: string }, sentinelDecision: 'ALERT' | 'HEALTHY') {
    await expect.poll(async () => (await personal(completed.id)).groups[0]?.items.length || 0,
      { timeout: 20_000, intervals: [250, 500, 1000] }).toBe(2)
    const news = await personal(completed.id)
    expect(news.groups).toHaveLength(1)
    expect(news.groups[0].process_id).toBe(`run:${completed.id}`)
    expect(news.groups[0].unread_count).toBe(2)
    const items = news.groups[0].items as NewsItem[]
    expect(items.map(item => item.module).sort()).toEqual(['intake', 'sentinel'])
    expect(new Set(items.map(item => item.id)).size).toBe(2)
    expect(items.find(item => item.module === 'intake')).toMatchObject({ resource_type: 'RUN', resource_id: completed.id,
      origin: 'MANUAL', status: 'SUCCESS', decision: 'REJECTED' })
    const automatic = items.find(item => item.module === 'sentinel')!
    expect(automatic).toMatchObject({ resource_type: 'RUN', origin: 'AUTOMATIC', status: 'SUCCESS', decision: sentinelDecision })
    const response = await api.get(`/api/v1/runs/${automatic.resource_id}`)
    expect(response.status()).toBe(200)
    const sentinel = await response.json()
    expect(sentinel).toMatchObject({ id: automatic.resource_id, module: 'sentinel', dataset_version_id: completed.output_version_id,
      decision: sentinelDecision, metrics: { profile_dataset_version_id: completed.output_version_id, profile_snapshot: { row_count: 0 },
        failed_checks: sentinelDecision === 'ALERT' ? 1 : 0 } })
    expect(sentinel.metrics.checks.filter((check: { code: string }) => check.code.startsWith('VOLUME_'))).toEqual([
      expect.objectContaining(sentinelDecision === 'ALERT'
        ? { code: 'VOLUME_MIN', actual: 0, expected: { min: 1, method: 'fixed_threshold' }, status: 'FAIL',
            metric_method: 'EXACT_OBSERVED', metric_definition_version: 2 }
        : { code: 'VOLUME_CHANGE', actual: 0, expected: { max_pct: 50, baseline_rows: 0, method: 'previous_version' }, status: 'PASS',
            metric_method: 'EXACT_OBSERVED', metric_definition_version: 2 }),
    ])
    expect(sentinel.execution_plan.sentinel_automatic.dataset_version_id).toBe(completed.output_version_id)
    expect(sentinel.execution_plan.sentinel_automatic.event_id).toEqual(expect.any(String))
    return news
  }
  // A rejected Intake still publishes its empty accepted output. Its real
  // publication event triggers a separate automatic Sentinel stage and notice.
  // Its first accepted output is empty without a prior output baseline.
  const initialNews = await personalPair(first, 'ALERT')
  expect(initialNews.groups[0].process_id).toBe(`run:${first.id}`)
  const ids = initialNews.groups[0].items.map((item: { id: string }) => item.id)
  await page.goto(`/notifications?process=run:${first.id}`)
  await expect(page).toHaveURL(new RegExp(`/operations\\?.*view=news`))
  await expect(page.getByRole('heading', { name: 'Novedades personales', exact: true })).toBeVisible()
  const reading = page.getByRole('checkbox', { name: /^Leída:/ })
  await expect(reading).toHaveCount(2)
  for (const checkbox of await reading.all()) await expect(checkbox).not.toBeChecked()
  const second = await run()
  expect(second.id).not.toBe(first.id)
  // The next empty output has the first empty output as a real baseline:
  // zero volume change passes, although its own Intake remains rejected.
  await personalPair(second, 'HEALTHY')
  const markResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/notifications/inbox/read-all' && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Marcar esta página como leída', exact: true }).click()
  const mark = await markResponse
  expect(mark.status()).toBe(200)
  expect(mark.request().postDataJSON().ids).toEqual(ids)
  expect((await personal(first.id)).groups[0].unread_count).toBe(0)
  expect((await personal(second.id)).groups[0].unread_count).toBe(2)
  expect((await (await api.get(`/api/v1/runs/${first.id}`)).json()).decision).toBe('REJECTED')
  const groups = []
  for (const [runId, sentinelDecision] of [[first.id, 'ALERT'], [second.id, 'HEALTHY']]) {
    const response = await api.get(`/api/v1/operations?grouped=true&process=run:${runId}`)
    expect(response.status()).toBe(200)
    const operation = await response.json()
    expect(operation.items).toHaveLength(1)
    expect(operation.items[0].process_id).toBe(`run:${runId}`)
    expect(operation.items[0].technical_state).toBe('SUCCESS')
    expect(operation.items[0].functional_results).toEqual([sentinelDecision, 'REJECTED'])
    expect(operation.items[0].stages.find((stage: { id: string }) => stage.id === `run:${runId}`))
      .toMatchObject({ module: 'intake', state: 'SUCCESS', decision: 'REJECTED' })
    expect(operation.items[0].intervention_required).toBe(true)
    groups.push(operation.items[0])
  }
  await page.getByRole('link', { name: /^Ver notificaciones/ }).click()
  await expect(page).toHaveURL(/\/operations\?view=news&read_state=UNREAD/)
  await expect(page.getByLabel('Lectura', { exact: true })).toHaveValue('UNREAD')
  await page.goto(`/?period=7d&dataset_id=${dataset.id}`)
  const chart = page.getByRole('region', { name: 'Gráfica de salud', exact: true })
  await expect(chart.getByRole('radio', { name: 'General', exact: true })).toBeChecked()
  const metrics = page.getByRole('region', { name: 'Indicadores operativos', exact: true })
  const unchangedMetrics = await metrics.innerText()
  const dashboard = await (await api.get(`/api/v1/dashboard?period=7d&dataset_id=${dataset.id}`)).json()
  expect(dashboard.stats.health_score).toBe(0)
  expect(dashboard.health_history.some((point: { intake: number }) => point.intake === 0)).toBe(true)
  for (const label of ['General', 'Data Intake', 'ReconOps', 'Sentinel']) {
    await chart.getByRole('radio', { name: label, exact: true }).check()
    await expect(chart.getByRole('heading', { name: `Evolución temporal · ${label}`, exact: true })).toBeVisible()
    expect(await metrics.innerText()).toBe(unchangedMetrics)
    if (label === 'General' || label === 'Data Intake') {
      await expect(chart.getByLabel(`Salud de ${label}`, { exact: true })).toHaveText('0%')
      await expect(chart.locator('circle')).toHaveCount(1)
      // Actual zero is a measured point. No compatible historical day is
      // invented, and no line connects this observation to missing periods.
      await expect(chart.locator('polyline')).toHaveCount(1)
      expect((await chart.locator('polyline').getAttribute('points'))?.trim().split(' ')).toHaveLength(1)
    } else await expect(chart.getByRole('heading', { name: 'Sin mediciones comparables', exact: true })).toBeVisible()
  }
  await chart.getByRole('radio', { name: 'Data Intake', exact: true }).check()
  await page.getByLabel('Filtrar por módulo', { exact: true }).selectOption('recon')
  await expect(chart.getByRole('status')).toHaveText('Los filtros globales excluyen Data Intake. Cambia el filtro global de módulo para consultar esta serie.')
  await chart.getByRole('radio', { name: 'General', exact: true }).focus()
  await page.keyboard.press('ArrowRight')
  await expect(chart.getByRole('radio', { name: 'Data Intake', exact: true })).toBeChecked()
  await info.attach('ops-dash-097-real.json', { body: JSON.stringify({ nonce, dataset_id: dataset.id, source_version_id: version.id,
    independent_run_ids: [first.id, second.id], initial_snapshot_ids: ids, new_event_remains_unread: true,
    groups, dashboard, measured_zero: true, unavailable_series_do_not_become_100: true,
    historical_gap_fixture: 'Separate unit contract uses explicit nonconsecutive dates; this live API created one measured day only.',
    elapsed_seconds: (Date.now() - started) / 1000 }), contentType: 'application/json' })
})
