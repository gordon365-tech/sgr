import { test, expect } from '@playwright/test';

// This file exercises the SGR dashboard as it actually exists today: a
// single unauthenticated page ("/") that renders PortfolioCard, RiskCard
// and StrategiesCard inside a WebSocketProvider (see frontend/app/page.tsx).
// There is currently no login flow and no client-side routing (/login,
// /dashboard, /portfolio do not exist), so tests only cover that surface.
// Each card shows a loading/empty placeholder until data arrives over the
// WebSocket connection to the API, so assertions accept either state
// rather than assuming live backend data is present.

test.describe('Dashboard shell', () => {
  test('should load the dashboard with header and branding', async ({ page }) => {
    await page.goto('/');

    await expect(page.locator('h1:has-text("Project SGR")')).toBeVisible();
    await expect(page.locator('text=Institutional AI-powered Trading System')).toBeVisible();
  });

  test('should display the live connection indicator', async ({ page }) => {
    await page.goto('/');

    await expect(page.locator('text=Live')).toBeVisible();
  });
});

test.describe('Portfolio card', () => {
  test('should render the portfolio section (loaded or loading state)', async ({ page }) => {
    await page.goto('/');

    // PortfolioCard shows "Loading portfolio..." until the store has data,
    // then a "Portfolio" heading with Total Value / Cash / Daily PnL.
    await expect(
      page.locator('text=Portfolio').or(page.locator('text=Loading portfolio...'))
    ).toBeVisible();
  });
});

test.describe('Risk metrics card', () => {
  test('should render the risk metrics section (loaded or loading state)', async ({ page }) => {
    await page.goto('/');

    // RiskCard shows "Loading risk metrics..." until the store has data,
    // then a "Risk Metrics" heading with Portfolio Heat / Max Drawdown / VaR.
    await expect(
      page.locator('text=Risk Metrics').or(page.locator('text=Loading risk metrics...'))
    ).toBeVisible();
  });
});

test.describe('Strategies card', () => {
  test('should render the strategies section (loaded or empty state)', async ({ page }) => {
    await page.goto('/');

    // StrategiesCard shows "No strategies loaded" until the store has
    // strategies, then a "Strategies" heading with one row per strategy.
    await expect(
      page.locator('text=Strategies').or(page.locator('text=No strategies loaded'))
    ).toBeVisible();
  });
});

test.describe('Error handling', () => {
  test('should still render the dashboard shell when offline', async ({ page, context }) => {
    await page.goto('/');
    await expect(page.locator('h1:has-text("Project SGR")')).toBeVisible();

    // Going offline breaks any *new* navigation/fetch (the browser has no
    // network), so this checks that the already-rendered shell survives
    // losing connectivity - not that a fresh page.goto() still works.
    await context.setOffline(true);
    await expect(page.locator('h1:has-text("Project SGR")')).toBeVisible();

    await context.setOffline(false);
  });
});

test.describe('Responsive design', () => {
  const viewports = [
    { name: 'Mobile', width: 375, height: 667 },
    { name: 'Tablet', width: 768, height: 1024 },
    { name: 'Desktop', width: 1920, height: 1080 },
  ];

  for (const viewport of viewports) {
    test(`should render the dashboard shell on ${viewport.name}`, async ({ browser }) => {
      const context = await browser.newContext({
        viewport: { width: viewport.width, height: viewport.height },
      });
      const page = await context.newPage();

      await page.goto('/');
      await expect(page.locator('h1:has-text("Project SGR")')).toBeVisible();

      await context.close();
    });
  }
});
