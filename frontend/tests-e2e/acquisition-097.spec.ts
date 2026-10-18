import { randomBytes } from 'node:crypto'
import { chromium, expect, request, test, type APIRequestContext, type Browser } from '@playwright/test'

test.use({ trace: 'off', screenshot: 'off', video: 'off' })

test('AUT-097: guardar una vez, cerrar navegador y observar tres cortes reales de 30 segundos', async ({ baseURL }, info) => {
  test.setTimeout(180_000)
  const started = Date.now(), nonce = randomBytes(6).toString('hex')
  const project = process.env.TV_CORE095_TEST_PROJECT || ''
  expect(project === 'trackvance-codex' || /^trackvance-v070-test-e2e-[a-z0-9-]*[a-f0-9]{12}$/.test(project)).toBe(true)
  const internal = process.env.TV_CORE095_DOCUMENT_RECEIVER_URL || '', control = process.env.TV_CORE095_DOCUMENT_RECEIVER_CONTROL_URL || ''
  expect(internal.startsWith('http://document-fixture:18095') || internal.startsWith('http://rest-fixture:18090')).toBe(true)
  expect(['127.0.0.1', 'localhost']).toContain(new URL(control).hostname)
  let browser: Browser | undefined, reopenedBrowser: Browser | undefined
  let independentApi: APIRequestContext | undefined, cleanupApi: APIRequestContext | undefined
  let cleanupDatasetId: string | undefined
  let cleanupHeaders: Record<string, string> = {}
  try {
    browser = await chromium.launch({ channel: info.project.use.channel, headless: true })
    // The browser process belongs solely to this test. Preserve the configured
    // context location and viewport without passing unrelated project options.
    const contextOptions = {
      baseURL, locale: info.project.use.locale,
      timezoneId: info.project.use.timezoneId, viewport: info.project.use.viewport,
    }
    const context = await browser.newContext(contextOptions)
    const page = await context.newPage()
    await page.goto('/')
    if (project === 'trackvance-codex') {
      const username = process.env.TRACKVANCE_ADMIN_EMAIL, password = process.env.TRACKVANCE_ADMIN_PASSWORD
      if (!username || !password) throw new Error('Synthetic credentials must be provided through the environment')
      await page.getByRole('button', { name: 'Tengo una cuenta local', exact: true }).click()
      await page.getByLabel('Usuario o correo', { exact: true }).fill(username)
      await page.getByLabel('Contraseña', { exact: true }).fill(password)
      await page.getByRole('button', { name: 'Iniciar sesión', exact: true }).click()
    } else await page.getByRole('button', { name: 'Entrar al entorno demo', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
    let api = context.request
    cleanupApi = api
    const fixturePath = `/acquisition/${nonce}`
    const csrf = await api.get('/api/v1/me')
    const me = await csrf.json()
    const csrfCookie = (await context.cookies()).find(cookie => cookie.name.includes('csrf'))
    const headers = { 'X-CSRF-Token': me.csrf_token || csrfCookie?.value || '' }
    cleanupHeaders = headers
    const connectionHttp = await api.post('/api/v1/connections', { headers, data: { name: `AUT097 source ${nonce}`, source_type: 'REST', options: {
      url: internal + fixturePath + '/source', auth: 'NONE', collection: 'records', fields: [{ name: 'id', logical_type: 'STRING' }, { name: 'amount', logical_type: 'DECIMAL' }], pagination: { mode: 'PAGE' }, page_size: 2, max_pages: 10,
    } } })
    expect(connectionHttp.status()).toBe(201)
    const connection = await connectionHttp.json()
    const firstHttp = await api.post(`/api/v1/connections/${connection.id}/acquisitions`, { headers, data: { name: `AUT097 dataset ${nonce}` } })
    expect(firstHttp.status()).toBe(202)
    const initial = (await firstHttp.json()).acquisition
    await expect.poll(async () => (await (await api.get(`/api/v1/acquisitions/${initial.id}`)).json()).status, { timeout: 30_000, intervals: [250, 500, 1000] }).toBe('SUCCESS')
    const acquired = await (await api.get(`/api/v1/acquisitions/${initial.id}`)).json()
    cleanupDatasetId = acquired.dataset_id
    await page.goto(`/datasets/${acquired.dataset_id}`)
    await expect(page.getByText(`Fuente REST · AUT097 source ${nonce}`, { exact: true })).toBeVisible()
    await page.getByLabel('Frecuencia', { exact: true }).fill('30')
    await page.getByLabel('Publicación', { exact: true }).selectOption('EVERY_CUT')
    await page.getByRole('checkbox', { name: 'Activar actualizaciones programadas', exact: true }).check()
    const savedResponse = page.waitForResponse(response => response.request().method() === 'PUT' && new URL(response.url()).pathname === `/api/v1/datasets/${acquired.dataset_id}/source-settings`)
    await page.getByRole('button', { name: 'Guardar origen y actualización', exact: true }).click()
    const savedHttp = await savedResponse
    expect(savedHttp.status()).toBe(200)
    const saved = await savedHttp.json()
    expect(saved.settings.interval_seconds).toBe(30)
    expect(saved.settings.timezone).toBe('America/Bogota')
    await expect(page.getByText(/Guardado: intervalo efectivo de 30 segundos/)).toBeVisible()
    const sourceBefore = await (await api.get(control + fixturePath + '/receipt')).json()
    const storageState = await context.storageState()
    independentApi = await request.newContext({ baseURL, storageState })
    api = independentApi
    cleanupApi = independentApi
    await browser.close()
    expect(browser.isConnected()).toBe(false)
    const closedAt = new Date().toISOString()
    // Only reads follow. No tick function, worker call or refresh request can
    // manufacture a successful schedule in this independent observation window.
    let occurrences: { id: string; planned_at: string; dispatched_at: string; acquisition_id: string; acquisition_status: string; coalesced_intervals: number; output_version_id: string }[] = []
    await expect.poll(async () => {
      const response = await api.get(`/api/v1/datasets/${acquired.dataset_id}/acquisition-occurrences?limit=10`)
      expect(response.status()).toBe(200)
      occurrences = (await response.json()).items
      return occurrences.filter(item => item.acquisition_status === 'SUCCESS').length
    }, { timeout: 120_000, intervals: [500, 1000] }).toBe(3)
    expect(occurrences).toHaveLength(3)
    expect(new Set(occurrences.map(item => item.id)).size).toBe(3)
    expect(new Set(occurrences.map(item => item.acquisition_id)).size).toBe(3)
    expect(new Set(occurrences.map(item => item.output_version_id)).size).toBe(3)
    const cuts = [...occurrences].sort((a, b) => Date.parse(a.planned_at) - Date.parse(b.planned_at))
    expect(Date.parse(cuts[1].planned_at) - Date.parse(cuts[0].planned_at)).toBe(30_000)
    expect(Date.parse(cuts[2].planned_at) - Date.parse(cuts[1].planned_at)).toBe(30_000)
    expect(cuts.every(item => item.coalesced_intervals === 0 && Date.parse(item.dispatched_at) >= Date.parse(closedAt))).toBe(true)
    const state = await (await api.get(`/api/v1/datasets/${acquired.dataset_id}/source-settings`)).json()
    const pause = await api.put(`/api/v1/datasets/${acquired.dataset_id}/source-settings`, { headers, data: { ...state.settings, expected_version: state.version, enabled: false } })
    expect(pause.status()).toBe(200)
    const sourceAfter = await (await api.get(control + fixturePath + '/receipt')).json()
    expect(sourceAfter.calls.slice(sourceBefore.calls.length)).toEqual(Array.from({ length: 3 }, () => [{ page: 1, page_size: 2, status: 200 }, { page: 2, page_size: 2, status: 200 }, { page: 3, page_size: 2, status: 200 }]).flat())
    const dataset = await (await api.get(`/api/v1/datasets/${acquired.dataset_id}`)).json()
    expect(dataset.versions).toHaveLength(4)
    expect(dataset.versions.every((version: { row_count: number }) => version.row_count === 3)).toBe(true)
    const graphHttp = await api.get(`/api/v1/catalog/datasets/${acquired.dataset_id}/lineage-graph?projection=origin&version_id=${cuts[2].output_version_id}&max_nodes=100&max_depth=4`)
    expect(graphHttp.status()).toBe(200)
    const graph = await graphHttp.json()
    expect(new Set(graph.nodes.map((node: { type: string }) => node.type))).toEqual(new Set(['CONNECTION_VERSION', 'ACQUISITION', 'DATASET_VERSION']))
    const operationsHttp = await api.get(`/api/v1/operations?grouped=true&process=occurrence:${cuts[2].id}`)
    expect(operationsHttp.status()).toBe(200)
    const operations = await operationsHttp.json()
    expect(operations.items).toHaveLength(1)
    expect(operations.items[0].process_id).toBe(`occurrence:${cuts[2].id}`)
    expect(operations.items[0].stages.some((stage: { id: string }) => stage.id === `acquisition:${cuts[2].acquisition_id}`)).toBe(true)
    expect(browser.isConnected()).toBe(false)
    const reopenedAt = new Date().toISOString()
    reopenedBrowser = await chromium.launch({ channel: info.project.use.channel, headless: true })
    const returnedContext = await reopenedBrowser.newContext({ ...contextOptions, storageState })
    const returned = await returnedContext.newPage()
    await returned.goto(`/datasets/${acquired.dataset_id}`)
    await expect(returned.getByText('Consulta exitosa', { exact: true })).toHaveCount(3)
    await returned.getByRole('link', { name: 'Ver en Operación', exact: true }).click()
    await expect(returned.getByRole('heading', { name: 'Operación', exact: true })).toBeVisible()
    await info.attach('aut-097-three-real-cuts.json', { body: JSON.stringify({ requirement: 'AUT-097-01/02', project, nonce, connection_id: connection.id,
      dataset_id: acquired.dataset_id, closed_at: closedAt, reopened_at: reopenedAt, interval_seconds: 30, timezone: saved.settings.timezone, occurrences: cuts,
      real_source_receipt: sourceAfter, version_ids: dataset.versions.map((version: { id: string }) => version.id), origin_graph: graph, operation: operations.items[0],
      elapsed_seconds: (Date.now() - started) / 1000, browser_process_closed_during_all_cuts: true, independent_http_observer: true, manual_dispatch_or_refresh: false }), contentType: 'application/json' })
  } finally {
    try {
      if (cleanupApi && cleanupDatasetId) {
        const stateHttp = await cleanupApi.get(`/api/v1/datasets/${cleanupDatasetId}/source-settings`)
        expect(stateHttp.status()).toBe(200)
        const state = await stateHttp.json()
        if (state.enabled) {
          const pause = await cleanupApi.put(`/api/v1/datasets/${cleanupDatasetId}/source-settings`, { headers: cleanupHeaders, data: { ...state.settings, expected_version: state.version, enabled: false } })
          expect(pause.status()).toBe(200)
        }
      }
    } finally {
      try {
        await reopenedBrowser?.close()
      } finally {
        try {
          await browser?.close()
        } finally {
          await independentApi?.dispose()
        }
      }
    }
  }
})

test('AUT-097: la página nativa sigue disponible después de cerrar el navegador propio', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Tu espacio de control', exact: true })).toBeVisible()
  expect((await page.request.get('/api/v1/me')).status()).toBe(401)
})
