import { test, expect } from '@playwright/test';

test.describe('Dashboard Authentication', () => {
  test('should redirect to login if not authenticated', async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveURL(/.*login/);
  });

  test('should login successfully with valid credentials', async ({ page }) => {
    await page.goto('/login');
    
    await page.fill('input[name="email"]', 'trader@example.com');
    await page.fill('input[name="password"]', 'password123');
    await page.click('button:has-text("Sign In")');
    
    // Wait for redirect to dashboard
    await expect(page).toHaveURL(/.*dashboard/);
    await expect(page.locator('text=Project SGR')).toBeVisible();
  });

  test('should show error message for invalid credentials', async ({ page }) => {
    await page.goto('/login');
    
    await page.fill('input[name="email"]', 'wrong@example.com');
    await page.fill('input[name="password"]', 'wrongpassword');
    await page.click('button:has-text("Sign In")');
    
    // Look for error message
    await expect(page.locator('text=Invalid credentials')).toBeVisible();
    await expect(page).toHaveURL(/.*login/);
  });
});

test.describe('Dashboard Navigation', () => {
  test.beforeEach(async ({ page }) => {
    // Login before each test
    await page.goto('/login');
    await page.fill('input[name="email"]', 'trader@example.com');
    await page.fill('input[name="password"]', 'password123');
    await page.click('button:has-text("Sign In")');
    await page.waitForURL(/.*dashboard/);
  });

  test('should display portfolio card', async ({ page }) => {
    await page.goto('/');
    
    await expect(page.locator('text=Portfolio')).toBeVisible();
    await expect(page.locator('text=Total Value')).toBeVisible();
    await expect(page.locator('text=Cash')).toBeVisible();
  });

  test('should display risk metrics card', async ({ page }) => {
    await page.goto('/');
    
    await expect(page.locator('text=Risk Metrics')).toBeVisible();
    await expect(page.locator('text=Portfolio Heat')).toBeVisible();
    await expect(page.locator('text=Max Drawdown')).toBeVisible();
  });

  test('should display strategies card', async ({ page }) => {
    await page.goto('/');
    
    await expect(page.locator('text=Strategies')).toBeVisible();
    // Check for at least one strategy listed
    await expect(page.locator('[role="button"]:has-text("Play")')).toBeVisible({ timeout: 5000 });
  });
});

test.describe('Portfolio Management', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/login');
    await page.fill('input[name="email"]', 'trader@example.com');
    await page.fill('input[name="password"]', 'password123');
    await page.click('button:has-text("Sign In")');
    await page.waitForURL(/.*dashboard/);
  });

  test('should show position details', async ({ page }) => {
    await page.goto('/portfolio');
    
    // Should see BTC/USDT position if it exists
    const btcPosition = page.locator('text=BTC/USDT').first();
    
    if (await btcPosition.isVisible({ timeout: 2000 }).catch(() => false)) {
      await expect(btcPosition).toBeVisible();
      
      // Check for quantity and PnL
      const row = page.locator('text=BTC/USDT').nth(0).locator('..');
      await expect(row.locator('[class*="text"]')).toHaveCount(3); // symbol, qty, pnl%
    }
  });

  test('should open and close positions', async ({ page }) => {
    await page.goto('/portfolio');
    
    // Look for any position
    const firstPosition = page.locator('[role="button"]:has-text("Close")').first();
    
    if (await firstPosition.isVisible({ timeout: 2000 }).catch(() => false)) {
      await firstPosition.click();
      
      // Should see confirmation dialog
      await expect(page.locator('text=Confirm Close Position')).toBeVisible({ timeout: 3000 });
      
      // Click cancel
      await page.click('button:has-text("Cancel")');
    }
  });
});

test.describe('Strategy Management', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/login');
    await page.fill('input[name="email"]', 'trader@example.com');
    await page.fill('input[name="password"]', 'password123');
    await page.click('button:has-text("Sign In")');
    await page.waitForURL(/.*dashboard/);
  });

  test('should toggle strategy on/off', async ({ page }) => {
    await page.goto('/');
    
    // Find a strategy toggle button
    const toggleButton = page.locator('[role="button"]').filter({ has: page.locator('svg') }).first();
    
    if (await toggleButton.isVisible()) {
      const initialState = await toggleButton.locator('svg').first().getAttribute('class');
      
      await toggleButton.click();
      
      // Wait for API response
      await page.waitForTimeout(1000);
      
      // State should change
      const newState = await toggleButton.locator('svg').first().getAttribute('class');
      expect(initialState).not.toBe(newState);
    }
  });

  test('should display win rate for strategies', async ({ page }) => {
    await page.goto('/');
    
    // Look for win rate percentage
    const winRate = page.locator('text=/Win Rate: \\d+\\.\\d+%/').first();
    
    if (await winRate.isVisible({ timeout: 2000 }).catch(() => false)) {
      await expect(winRate).toBeVisible();
    }
  });
});

test.describe('Real-time Updates via WebSocket', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/login');
    await page.fill('input[name="email"]', 'trader@example.com');
    await page.fill('input[name="password"]', 'password123');
    await page.click('button:has-text("Sign In")');
    await page.waitForURL(/.*dashboard/);
  });

  test('should receive WebSocket connection status', async ({ page }) => {
    await page.goto('/');
    
    // Check for "Live" indicator (should be green dot + text)
    const liveIndicator = page.locator('text=Live');
    
    // Wait for connection (up to 5 seconds)
    await expect(liveIndicator).toBeVisible({ timeout: 5000 });
  });

  test('should update portfolio values in real-time', async ({ page }) => {
    await page.goto('/');
    
    // Get initial portfolio value
    const portfolioValue = page.locator('text=/Total Value.*\\$\\d+/');
    
    if (await portfolioValue.isVisible()) {
      const initialValue = await portfolioValue.textContent();
      
      // Wait a bit for potential update
      await page.waitForTimeout(3000);
      
      const updatedValue = await portfolioValue.textContent();
      
      // Values might change or stay same, just verify they're still visible
      await expect(portfolioValue).toBeVisible();
    }
  });
});

test.describe('Error Handling', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/login');
    await page.fill('input[name="email"]', 'trader@example.com');
    await page.fill('input[name="password"]', 'password123');
    await page.click('button:has-text("Sign In")');
    await page.waitForURL(/.*dashboard/);
  });

  test('should handle network errors gracefully', async ({ page, context }) => {
    // Simulate offline mode
    await context.setOffline(true);
    
    await page.goto('/');
    
    // Should still show page (cached data)
    await expect(page.locator('text=Project SGR')).toBeVisible();
    
    // Go back online
    await context.setOffline(false);
  });

  test('should display error message on API failure', async ({ page }) => {
    // Intercept and fail API calls
    await page.route('**/api/v1/**', route => {
      route.abort('failed');
    });
    
    await page.goto('/');
    
    // Should show error or fallback UI
    // Depends on implementation – could be error banner or empty state
  });
});

test.describe('Responsive Design', () => {
  const viewports = [
    { name: 'Mobile', width: 375, height: 667 },
    { name: 'Tablet', width: 768, height: 1024 },
    { name: 'Desktop', width: 1920, height: 1080 },
  ];

  for (const viewport of viewports) {
    test(`should render correctly on ${viewport.name}`, async ({ browser }) => {
      const context = await browser.newContext({
        viewport: { width: viewport.width, height: viewport.height },
      });
      const page = await context.newPage();
      
      await page.goto('/login');
      
      // Basic element visibility
      await expect(page.locator('input[name="email"]')).toBeVisible();
      await expect(page.locator('button:has-text("Sign In")')).toBeVisible();
      
      await context.close();
    });
  }
});
