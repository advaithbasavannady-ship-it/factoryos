# Browser E2E tests

Install the pinned Playwright dependency from `tests/package.json` using the repository's standard workspace setup. Run the simultaneous refresh browser check with:

```bash
cd tests
npm run test:e2e -- --project=desktop
```

The config starts the Vite app on `127.0.0.1:3000`. The refresh test intercepts every `/api/*` request in the browser and returns synthetic responses; it does not connect to the backend, MongoDB, production or demo services. It validates browser-side cross-tab coordination. Server-side refresh security is tested separately in the isolated Mongo integration suite.

Playwright uses its installed Chromium by default. On a local machine with Chrome but without the Playwright Chromium build, set `PLAYWRIGHT_CHROMIUM_EXECUTABLE` to the local browser executable path for that command. Results are written to `tests/test-results/`; preserved first-run race evidence is in `tests/evidence/auth-refresh-before-fix/`.
