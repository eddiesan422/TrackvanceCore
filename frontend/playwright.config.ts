import { defineConfig, devices } from '@playwright/test'

// Private identity tests must not generate automatic DOM snapshots on failure.
if (process.env.TV_E2E_PRIVATE_ARTIFACTS) process.env.PLAYWRIGHT_NO_COPY_PROMPT = '1'

export default defineConfig({
  testDir: './tests-e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  retries: 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: process.env.TV_E2E_URL || process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:3000',
    trace: process.env.TV_E2E_PRIVATE_ARTIFACTS ? 'off' : 'retain-on-failure',
    screenshot: process.env.TV_E2E_PRIVATE_ARTIFACTS ? 'off' : 'only-on-failure',
    acceptDownloads: true,
  },
  projects: [{
    name: 'local-chrome',
    use: { ...devices['Desktop Chrome'], channel: process.env.PLAYWRIGHT_CHANNEL || 'chrome' },
  }],
})
