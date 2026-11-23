import { expect, test } from '@playwright/test';

const accessToken = 'e2e-access-token-for-two-tabs';
const user = {
  id: 'e2e-user',
  name: 'Browser Test User',
  email: 'browser-test@example.test',
  company_id: 'browser-test-company',
  membership_id: 'browser-test-membership',
  role: 'owner',
  department: null,
  department_ids: [],
};

test('simultaneous tabs share one refresh rotation and both restore the session', async ({ browser }) => {
  const context = await browser.newContext();
  await context.addInitScript(() => {
    localStorage.clear();
    sessionStorage.clear();
  });

  let refreshCalls = 0;
  const refreshRequestIds: string[] = [];
  const sessionAuthorization: Array<string | undefined> = [];

  await context.route('**/api/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;

    if (path === '/api/auth/refresh' && request.method() === 'POST') {
      refreshCalls += 1;
      refreshRequestIds.push(request.headers()['x-refresh-request-id']);
      // Make both page effects overlap so Web Locks and the shared result are exercised.
      await new Promise((resolve) => setTimeout(resolve, 250));
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers: { 'Set-Cookie': 'factoryos_refresh=e2e-opaque; HttpOnly; SameSite=Strict; Path=/api/auth' },
        body: JSON.stringify({ access_token: accessToken, token_type: 'bearer', expires_in: 600 }),
      });
      return;
    }

    if (path === '/api/auth/session') {
      sessionAuthorization.push(request.headers().authorization);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ user, permissions: ['reports.company.read'] }),
      });
      return;
    }

    if (path === '/api/auth/memberships') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([{ id: user.membership_id, company_id: user.company_id, role: user.role }]),
      });
      return;
    }

    if (path === '/api/dashboard/overview') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          date: '2026-11-12', unit: 'units', company_name: 'Browser Test Factory', owner_name: user.name,
          kpis: {
            orders_today: 0, dispatched_today: 0, pending_orders: 0, overdue_orders: 0,
            employees_total: 0, employees_present: 0, employees_absent: 0, employees_leave: 0,
            employees_late: 0, machines_total: 0, machines_running: 0, machines_idle: 0,
            machines_maintenance: 0, machines_offline: 0, production_today: 0, production_target: 0,
            achievement: 0, rejected_today: 0, efficiency_today: 0,
          },
          alerts: [], order_status: [], order_summary: {}, attendance_trend: [], today_events: [],
          departments: [], trend: [],
        }),
      });
      return;
    }

    await route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ detail: 'unmocked test API' }) });
  });

  const firstTab = await context.newPage();
  const secondTab = await context.newPage();
  await Promise.all([firstTab.goto('/'), secondTab.goto('/')]);

  await expect(firstTab.locator('.profile-name')).toHaveText(user.name);
  await expect(secondTab.locator('.profile-name')).toHaveText(user.name);
  await expect(firstTab.getByRole('heading', { name: 'Browser Test Factory' })).toBeVisible();
  await expect(secondTab.getByRole('heading', { name: 'Browser Test Factory' })).toBeVisible();

  expect(refreshCalls).toBe(1);
  expect(refreshRequestIds).toHaveLength(1);
  expect(refreshRequestIds[0]).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
  expect(sessionAuthorization.length).toBeGreaterThanOrEqual(2);
  expect(sessionAuthorization.every((header) => header === `Bearer ${accessToken}`)).toBe(true);

  await context.close();
});
