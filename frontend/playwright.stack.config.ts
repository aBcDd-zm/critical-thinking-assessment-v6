import { defineConfig, devices } from "@playwright/test";

const frontendUrl = "http://127.0.0.1:5177";
const backendUrl = "http://127.0.0.1:8061";

export default defineConfig({
  testDir: "./e2e-stack",
  timeout: 90_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: "list",
  use: {
    baseURL: frontendUrl,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: [
    {
      command: "../scripts/start-playwright-stack-backend.sh",
      url: `${backendUrl}/api/v1/health`,
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: "pipe",
      stderr: "pipe",
      gracefulShutdown: { signal: "SIGTERM", timeout: 5_000 },
    },
    {
      command: "./node_modules/.bin/vite --host 127.0.0.1 --port 5177 --strictPort",
      url: `${frontendUrl}/assessment`,
      reuseExistingServer: false,
      timeout: 120_000,
      env: {
        ...process.env,
        VITE_API_BASE_URL: `${backendUrl}/api/v1`,
      },
      stdout: "pipe",
      stderr: "pipe",
      gracefulShutdown: { signal: "SIGTERM", timeout: 5_000 },
    },
  ],
  projects: [{ name: "chromium-real-stack", use: { ...devices["Desktop Chrome"] } }],
});
