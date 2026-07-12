import { test, expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

let bugs: string[] = [];
let pageState: any; // Keep state across sequential tests if necessary. For playwright we should just combine them into one long test.

test.beforeAll(() => {
    const reportPath = path.join(__dirname, '../../test_report.md');
    const header = `# Otto & Mango App - E2E Testing Report\n\n## Tested Flows\n- **App Loading**: Verifies the UI loads correctly on the default port.\n- **Topic Creation**: Types a test topic ("The economy of honeybees") into the topic field.\n- **Video Creation**: Simulates clicking the "Create the video" button to initiate the generation process.\n- **YouTube Publishing Check**: Verifies the publish button's visibility and accessibility.\n\n## Bug Log\n`;
    fs.writeFileSync(reportPath, header);
});

test('End-to-end full flow (sequential)', async ({ page }, testInfo) => {
  const logBug = async (action: string, error: string) => {
    const safeAction = action.replace(/[^a-z0-9]/gi, '_').toLowerCase();
    const screenshotName = `bug_${safeAction}_${Date.now()}.png`;
    const screenshotPath = path.join(__dirname, '../../test-results', screenshotName);

    // Ensure directory exists
    fs.mkdirSync(path.join(__dirname, '../../test-results'), { recursive: true });

    await page.screenshot({ path: screenshotPath, fullPage: true });

    // Add to the bugs array with relative image link for markdown
    bugs.push(`### Bug in: ${action}\n**Error:** ${error}\n\n![Screenshot of error](./test-results/${screenshotName})`);
  };

  try {
    // 1. Visit the app
    await page.goto('/');
    await expect(page.locator('h1')).toContainText('Otto & Mango', { timeout: 10000 });
  } catch (error: any) {
    await logBug('App Loading', error.message);
    throw error;
  }

  try {
    // 2. Fill in a topic
    const topicInput = page.locator('#topic');
    await expect(topicInput).toBeVisible({ timeout: 5000 });
    await topicInput.fill('The economy of honeybees');
  } catch (error: any) {
    await logBug('Topic Creation', error.message);
    throw error;
  }

  try {
    // 3. Create Video
    const createBtn = page.locator('#createBtn');
    await expect(createBtn).toBeVisible({ timeout: 5000 });
    await createBtn.click();

    const statusMsg = page.locator('.error, .err, [style*="color:#e06d6d"]');

    // Wait up to 30 seconds to see if an error pops up
    try {
        await statusMsg.waitFor({ state: 'visible', timeout: 30000 });
        const text = await statusMsg.textContent();
        if (text && text.trim() !== '' && !text.includes('/100')) {
             await logBug('Video Creation', 'App returned an error during creation: ' + text);
             expect(text).not.toContain('error');
             expect(text).not.toContain('No real script');
        }
    } catch (e: any) {
        // If it times out, wait for success state
    }

  } catch (error: any) {
    await logBug('Starting Video Creation', error.message);
    throw error;
  }

  try {
    // 4. Test YouTube publish button
    const ytBtn = page.locator('#ytBtn');
    await expect(ytBtn).toBeVisible({ timeout: 10000 });
    await expect(ytBtn).toBeEnabled({ timeout: 5000 });
  } catch (error: any) {
    await logBug('YouTube Publish Button', error.message);
    throw error;
  }
});

test.afterAll(async () => {
    const reportPath = path.join(__dirname, '../../test_report.md');

    let resultLog = '';
    if (bugs.length > 0) {
        resultLog += bugs.join('\n\n') + '\n';
        resultLog += `\n❌ **Test Suite Failed** due to UI bugs caught above.\n`;
    } else {
        resultLog += `\n✅ **Test Suite Passed!**\n\nNo bugs found during sequential E2E flow.\n`;
    }

    fs.appendFileSync(reportPath, resultLog);
});
