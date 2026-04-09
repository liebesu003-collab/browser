# browser

GitHub Actions workflows for both a safe Playwright smoke check and a manual full-script run.

## Safe smoke check

The lightweight smoke workflow verifies only that Python, Playwright, and Chromium start correctly.

- workflow: `.github/workflows/playwright-smoke.yml`
- script: `smoke_playwright_ci.py`

## Manual real run

The full workflow runs `smoke_playwright.py` on a self-hosted runner and is meant for manual execution only.

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
- it targets a self-hosted runner via the `runner_labels` input, which defaults to `["self-hosted","linux"]`
- it writes a temporary `config.json` during the run
- when `use_subscription_proxy=true` on a Linux runner, it fetches nodes from `proxy_subscription_url` and starts a local `sing-box` proxy on the runner
- Linux runners use `xvfb-run` for the Playwright session when `headless` is `false`
- Linux and macOS runners remove `virtualbrowser_executable_path` from the runtime config so they use Playwright Chromium
- Windows runners keep `virtualbrowser_executable_path` if your base config provides one
- it uploads `logs/`, `tokens/`, screenshots, `sing-box.log`, `sing-box-proxy.json`, and `proxy_rotation_state.json` as workflow artifacts
