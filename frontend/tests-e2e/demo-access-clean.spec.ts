import { expect, test } from '@playwright/test'

test.skip(
  process.env.TV_EXPECT_CLEAN_DEMO !== 'true',
  'Este escenario requiere una instalación aislada con el seed demo deshabilitado.',
)

test('el acceso demo funciona sin sembrar datos sintéticos', async ({ page }) => {
  const healthResponse = await page.request.get('/api/v1/health')
  expect(healthResponse.status()).toBe(200)
  const health = await healthResponse.json()
  expect(health.demo_access_enabled).toBe(true)
  expect(health.demo_seed_enabled).toBe(false)

  await page.goto('/')
  const loginResponsePromise = page.waitForResponse(response =>
    new URL(response.url()).pathname === '/api/v1/auth/demo'
      && response.request().method() === 'POST',
  )
  await page.getByRole('button', { name: 'Entrar al entorno demo', exact: true }).click()
  expect((await loginResponsePromise).status()).toBe(200)
  await expect(page.getByRole('heading', { name: 'Centro de control', exact: true })).toBeVisible()

  const meResponse = await page.request.get('/api/v1/me')
  expect(meResponse.status()).toBe(200)
  expect((await meResponse.json()).demo_mode).toBe(true)

  const emptyCollections = [
    '/api/v1/datasets',
    '/api/v1/intake/contracts',
    '/api/v1/recon/controls',
    '/api/v1/monitors',
    '/api/v1/runs',
    '/api/v1/findings',
    '/api/v1/exceptions',
  ]
  for (const endpoint of emptyCollections) {
    const response = await page.request.get(endpoint)
    expect(response.status(), endpoint).toBe(200)
    expect(await response.json(), endpoint).toEqual({ items: [], total: 0 })
  }
})
