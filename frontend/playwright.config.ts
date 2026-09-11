import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:3000",
    viewport: { width: 1440, height: 900 },
    headless: true,
    channel: "chrome",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: "npm run dev",
    url: "http://127.0.0.1:3000/jobs",
    reuseExistingServer: !process.env.CI,
    timeout: 120000,
  },
});
