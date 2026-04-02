# browser

Minimal GitHub Actions smoke test for Python + Playwright.

This repository intentionally contains only a safe browser-startup check:

- install Python dependencies
- install Playwright Chromium
- launch a headless browser
- verify a local test page renders correctly

The workflow lives in `.github/workflows/playwright-smoke.yml`.
