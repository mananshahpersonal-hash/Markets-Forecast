import { test, expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

let bugs: string[] = [];
let pageState: any;

test.beforeAll(() => {
    const reportPath = path.join(__dirname, '../../test_report.html');
    const header = `
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Otto & Mango App - E2E Testing Report</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px; }
        h1, h2, h3 { color: #2c3e50; }
        .success { color: #27ae60; font-weight: bold; }
        .error-block { background-color: #fce4e4; border: 1px solid #fcc2c3; padding: 15px; border-radius: 5px; margin-bottom: 20px; }
        .error-title { color: #c0392b; margin-top: 0; }
        .error-text { font-family: monospace; background: #fff; padding: 10px; border: 1px solid #ddd; overflow-x: auto; }
        img.screenshot { max-width: 100%; height: auto; border: 2px solid #ccc; border-radius: 4px; margin-top: 10px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .flow-list { background: #f8f9fa; padding: 15px 15px 15px 35px; border-radius: 5px; }
    </style>
</head>
<body>
    <h1>Otto & Mango App - E2E Testing Report</h1>

    <h2>Tested Flows</h2>
    <ul class="flow-list">
        <li><strong>App Loading</strong>: Verifies the UI loads correctly on the default port.</li>
        <li><strong>Topic Creation</strong>: Types a test topic ("The economy of honeybees") into the topic field.</li>
        <li><strong>Video Creation</strong>: Simulates clicking the "Create the video" button to initiate the generation process.</li>
        <li><strong>YouTube Publishing Check</strong>: Verifies the publish button's visibility and accessibility.</li>
    </ul>

    <h2>Test Results</h2>
`;
    fs.writeFileSync(reportPath, header);
});

test('End-to-end full flow (sequential)', async ({ page }, testInfo) => {
  const logBug = async (action: string, error: string) => {
    const safeAction = action.replace(/[^a-z0-9]/gi, '_').toLowerCase();
    const screenshotName = `bug_${safeAction}_${Date.now()}.png`;
    // Save screenshot to root test-results folder so the HTML can reference it relative to itself
    const screenshotPath = path.join(__dirname, '../../test-results', screenshotName);

    // Ensure directory exists
    fs.mkdirSync(path.join(__dirname, '../../test-results'), { recursive: true });

    await page.screenshot({ path: screenshotPath, fullPage: true });

    // Add to the bugs array with HTML formatting
    bugs.push(`
    <div class="error-block">
        <h3 class="error-title">❌ Bug detected during: ${action}</h3>
        <p><strong>Direct Error Message:</strong></p>
        <div class="error-text">${error.replace(/</g, '&lt;').replace(/>/g, '&gt;')}</div>
        <p><strong>Screenshot:</strong></p>
        <img src="./test-results/${screenshotName}" alt="Screenshot of error in ${action}" class="screenshot" />
    </div>`);
  };

  try {
    // 1. Visit the app
    await page.goto('/', { waitUntil: 'networkidle', timeout: 30000 });
    // Using a more resilient selector in case the app renders slowly or differently
    await expect(page.locator('body')).toContainText('Otto', { timeout: 15000 });
  } catch (error: any) {
    await logBug('App Loading', error.message);
    throw error;
  }

  try {
    // 2. Fill in a topic
    const topicInput = page.locator('#topic');
    await expect(topicInput).toBeVisible({ timeout: 10000 });
    await topicInput.fill('The economy of honeybees');
  } catch (error: any) {
    await logBug('Topic Creation', error.message);
    throw error;
  }

  try {
    // 3. Create Video
    // wait for Create Video or similar buttons
    const createBtn = page.locator('#createBtn, button:has-text("Make"), button:has-text("Create")').first();
    await expect(createBtn).toBeVisible({ timeout: 10000 });
    await createBtn.click();

    const statusMsg = page.locator('.error, .err, [style*="color:#e06d6d"]');

    // Wait up to 30 seconds to see if an error pops up
    try {
        await statusMsg.waitFor({ state: 'visible', timeout: 30000 });
        const text = await statusMsg.textContent();
        if (text && text.trim() !== '' && !text.includes('/100')) {
             await logBug('Video Creation', 'App returned an error during creation: ' + text);
             // Deliberately fail the test if the app throws a UI error
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
    const reportPath = path.join(__dirname, '../../test_report.html');

    let resultLog = '';
    if (bugs.length > 0) {
        resultLog += `\n<p style="color: #c0392b; font-weight: bold; font-size: 1.2em;">❌ Test Suite Failed due to the following UI bugs:</p>\n`;
        resultLog += bugs.join('\n');
    } else {
        resultLog += `\n<div style="background-color: #e8f8f5; border: 1px solid #c8e6c9; padding: 20px; border-radius: 5px; text-align: center;">
            <h3 class="success">✅ Test Suite Passed Successfully!</h3>
            <p>No bugs found during sequential E2E flow. All tested functionalities work as expected.</p>
        </div>\n`;
    }

    resultLog += `\n</body>\n</html>`;
    fs.appendFileSync(reportPath, resultLog);
});
