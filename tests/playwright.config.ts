import { defineConfig, devices } from '@playwright/test';

// Pre-scaffolded canonical config — edit the marked lines only; do not re-create.
export default defineConfig({
  testDir: './e2e',
  outputDir: './test-results',
  timeout: 60_000,
  retries: 0,
  workers: 2,
  reporter: [
    ['list'],
    ['json', { outputFile: './test-results/results.json' }],
  ],
  use: {
    // farm-ts: localhost — Vite proxies /api to FastAPI; the external preview host is not in-pod routable.
    baseURL: 'http://localhost:3000',
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
      : undefined,
    screenshot: 'on',
    trace: 'on-first-retry',
    headless: true,
    ignoreHTTPSErrors: true,
  },
  projects: [
    // Keep the project matching the brief's form factor; DELETE the other (both = 2x test time).
    // Only chromium is installed — never switch to iPhone/webkit device descriptors.
    {
      name: 'desktop',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } },
    },
    {
      name: 'mobile',
      use: { browserName: 'chromium', viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true },
    },
  ],
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 3000 --strictPort',
    cwd: '../frontend',
    url: 'http://127.0.0.1:3000',
    reuseExistingServer: false,
    timeout: 60_000,
    env: {
      DISABLE_HOT_RELOAD: 'true',
      DISABLE_VISUAL_EDITS: 'true',
      DISABLE_EMERGENT_OVERLAY: 'true',
    },
  },
});
