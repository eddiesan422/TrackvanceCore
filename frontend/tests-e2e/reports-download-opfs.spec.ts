import { test, expect } from '@playwright/test'
import fs from 'node:fs/promises'
import path from 'node:path'
import { randomUUID } from 'node:crypto'

test.setTimeout(1_800_000)
test.skip(process.env.TV_REPORT_DOWNLOAD_OPFS !== 'true', 'Dedicated host oracle supplies the owned download fixture')

type Fixture = { definition_id: string; rows: number; project: string; source_sha: string; client_directory: string; filename: string }
type OPFSWindow = Window & { reportOPFSHandle?: FileSystemFileHandle; reportPickerCalls?: number }

test('Reports UI streams its full result into a real OPFS file and commits after terminal success', async ({ page }) => {
  const fixturePath = process.env.TV_DOWNLOAD_085_FIXTURE
  expect(fixturePath, 'The explicit runner must provide its private owned fixture').toBeTruthy()
  const fixture = JSON.parse(await fs.readFile(fixturePath!, 'utf8')) as Fixture
  expect(fixture.project).toMatch(/^trackvance-v080-test-/)
  expect(fixture.source_sha).toMatch(/^[a-f0-9]{40}$/)
  expect([120, 400000, 1000000]).toContain(fixture.rows)
  const directory = path.resolve(fixture.client_directory)
  expect(directory.split(path.sep)).toContain('.codex-local')
  expect(path.basename(directory)).toMatch(/^client-download-085-[a-f0-9]{12}$/)
  expect(fixture.filename).toBe('browser-result.xlsx.incomplete')
  const ownedName = `trackvance-opfs-${randomUUID()}.xlsx`
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.addInitScript(({ ownedName }) => {
    // Only the picker is redirected. The handle, createWritable, write, close,
    // browser Fetch stream and all API calls remain actual platform operations.
    Object.defineProperty(window, 'showSaveFilePicker', { configurable: true, value: async () => {
      const directory = await navigator.storage.getDirectory()
      const handle = await directory.getFileHandle(ownedName, { create: true })
      if (!(handle instanceof FileSystemFileHandle)) throw new Error('OPFS did not return a native file handle')
      const current = window as OPFSWindow
      current.reportOPFSHandle = handle
      current.reportPickerCalls = (current.reportPickerCalls || 0) + 1
      return handle
    } })
  }, { ownedName })
  try {
    await page.goto('/')
    await page.getByRole('button', { name: 'Entrar al entorno demo', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()
    await page.goto(`/reports/definitions/${fixture.definition_id}`)
    const resolve = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/reports/resolve' && response.request().method() === 'POST')
    await page.getByRole('button', { name: 'Resolver fuentes y validar', exact: true }).click()
    const resolvedResponse = await resolve
    expect(resolvedResponse.ok(), await resolvedResponse.text()).toBeTruthy()
    const resolved = await resolvedResponse.json()
    const preview = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/reports/preview' && response.request().method() === 'POST')
    await page.getByRole('button', { name: 'Ejecutar vista previa', exact: true }).click()
    const previewResponse = await preview
    expect(previewResponse.ok(), await previewResponse.text()).toBeTruthy()
    const previewValue = await previewResponse.json()
    expect(previewValue.status).toBe('SUCCESS')
    expect(previewValue.rows).toHaveLength(10)
    await expect(page.getByLabel('Formato de descarga', { exact: true })).toHaveValue('XLSX')
    const responsePromise = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/reports/download' && response.request().method() === 'POST')
    const began = Date.now()
    await page.getByRole('button', { name: 'Descargar reporte', exact: true }).click()
    const response = await responsePromise
    expect(response.status()).toBe(200)
    const identity = response.headers()['x-report-execution-id']
    expect(identity).toMatch(/^[a-f0-9-]{36}$/i)
    await expect(page.getByText('Generación y transferencia completas verificadas en el historial. El navegador gestiona el guardado local.', { exact: true })).toBeVisible({ timeout: 1_200_000 })
    const file = await page.evaluate(async () => {
      const current = window as OPFSWindow
      if (!current.reportOPFSHandle || current.reportPickerCalls !== 1) throw new Error('Direct OPFS path was not used exactly once')
      const saved = await current.reportOPFSHandle.getFile()
      return { bytes: saved.size, calls: current.reportPickerCalls }
    })
    expect(file.bytes).toBeGreaterThan(0)
    const state = await (await page.request.get(`/api/v1/reports/executions/${identity}`)).json()
    expect(state).toEqual(expect.objectContaining({ status: 'SUCCESS', generation_status: 'COMPLETE', transmission_status: 'COMPLETE' }))
    expect(state.metrics.rows).toBe(fixture.rows)
    expect(state.metrics.serialized_bytes).toBe(file.bytes)
    // A Blob backed by the committed OPFS file is handed to Chromium's download
    // service. No whole-file arrayBuffer or response body enters Node memory.
    const downloaded = page.waitForEvent('download')
    await page.evaluate(async filename => {
      const handle = (window as OPFSWindow).reportOPFSHandle!
      const url = URL.createObjectURL(await handle.getFile())
      const anchor = document.createElement('a')
      anchor.href = url; anchor.download = filename
      document.body.appendChild(anchor); anchor.click(); anchor.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000)
    }, fixture.filename)
    const download = await downloaded
    expect(await download.failure()).toBeNull()
    await download.saveAs(path.join(directory, fixture.filename))
    expect((await fs.stat(path.join(directory, fixture.filename))).size).toBe(file.bytes)
    expect(errors).toEqual([])
    await fs.writeFile(path.join(directory, 'browser-receipt.json'), JSON.stringify({
      status: 'PASS', method: 'REAL_OPFS_FILE_HANDLE', source_sha: fixture.source_sha, rows: fixture.rows,
      execution_id: identity, context_id: resolved.context_id, bytes_received: file.bytes,
      transfer_seconds: (Date.now() - began) / 1000, picker_calls: file.calls,
      bounded_memory_fallback_used: false, output_data_published: false,
    }, null, 2), 'utf8')
  } finally {
    if (!page.isClosed()) await page.evaluate(async name => {
      const directory = await navigator.storage.getDirectory()
      try { await directory.removeEntry(name) } catch (error) {
        if (!(error instanceof DOMException && error.name === 'NotFoundError')) throw error
      }
    }, ownedName)
  }
})
