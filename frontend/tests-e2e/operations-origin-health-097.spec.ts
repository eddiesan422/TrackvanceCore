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
  await expect.poll(async () => (await personal(first.id)).groups[0]?.items.length || 0,
    { timeout: 20_000, intervals: [250, 500, 1000] }).toBe(1)
  const initialNews = await personal(first.id)
  expect(initialNews.groups[0].process_id).toBe(`run:${first.id}`)
  const ids = initialNews.groups[0].items.map((item: { id: string }) => item.id)
  await page.goto(`/notifications?process=run:${first.id}`)
  await expect(page).toHaveURL(new RegExp(`/operations\\?.*view=news`))
  await expect(page.getByRole('heading', { name: 'Novedades personales', exact: true })).toBeVisible()
  await expect(page.getByRole('checkbox', { name: /^Leída:/ })).not.toBeChecked()
  const second = await run()
  expect(second.id).not.toBe(first.id)
  await expect.poll(async () => (await personal(second.id)).groups[0]?.unread_count || 0,
    { timeout: 20_000, intervals: [250, 500, 1000] }).toBe(1)
  const markResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/notifications/inbox/read-all' && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Marcar esta página como leída', exact: true }).click()
  const mark = await markResponse
  expect(mark.status()).toBe(200)
  expect(mark.request().postDataJSON().ids).toEqual(ids)
  expect((await personal(first.id)).groups[0].unread_count).toBe(0)
  expect((await personal(second.id)).groups[0].unread_count).toBe(1)
  expect((await (await api.get(`/api/v1/runs/${first.id}`)).json()).decision).toBe('REJECTED')
  const groups = []
  for (const runId of [first.id, second.id]) {
    const response = await api.get(`/api/v1/operations?grouped=true&process=run:${runId}`)
    expect(response.status()).toBe(200)
    const operation = await response.json()
    expect(operation.items).toHaveLength(1)
    expect(operation.items[0].process_id).toBe(`run:${runId}`)
    expect(operation.items[0].technical_state).toBe('SUCCESS')
    expect(operation.items[0].functional_result).toBe('REJECTED')
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
