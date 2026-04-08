# browser

GitHub Actions workflows for both a safe Playwright smoke check and a manual full-script run.

## Safe smoke check

The lightweight smoke workflow verifies only that Python, Playwright, and Chromium start correctly.

- workflow: `.github/workflows/playwright-smoke.yml`
- script: `smoke_playwright_ci.py`

## Manual real run

The full workflow runs `smoke_playwright.py` on GitHub Actions and is meant for manual execution only.

- workflow: `.github/workflows/playwright-real.yml`
- dependencies: `requirements-real.txt`
- config example: `config.github-actions.example.json`

Set these repository secrets before running it:

- `SMOKE_PLAYWRIGHT_CONFIG_JSON`: base JSON config copied from `config.github-actions.example.json` and adjusted for your account
- `SMOKE_PLAYWRIGHT_ACCOUNT_PASSWORD`: optional override for `account_password`
- `SMOKE_PLAYWRIGHT_TEMPMAIL_API_KEY`: optional override for `tempmail_api_key`
- `SMOKE_PLAYWRIGHT_API_PROXY`: optional override for `api_proxy`
- `SMOKE_PLAYWRIGHT_BROWSER_PROXY`: optional override for `browser_proxy`

Behavior notes:

- the workflow is `workflow_dispatch` only
- it writes a temporary `config.json` during the run
- it removes `virtualbrowser_executable_path` from the runtime config so GitHub-hosted Linux runners use Playwright Chromium
- it uploads `logs/`, `tokens/`, screenshots, and `proxy_rotation_state.json` as workflow artifacts
