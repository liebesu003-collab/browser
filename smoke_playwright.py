import asyncio

from playwright.async_api import async_playwright


async def main() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(
            """
            <html>
              <head><title>Smoke OK</title></head>
              <body>
                <main>
                  <h1>Hello from GitHub Actions</h1>
                  <p>Playwright started Chromium successfully.</p>
                </main>
              </body>
            </html>
            """
        )

        title = await page.title()
        heading = await page.locator("h1").text_content()

        assert title == "Smoke OK", f"Unexpected page title: {title!r}"
        assert heading == "Hello from GitHub Actions", f"Unexpected heading: {heading!r}"

        await browser.close()

    print("Playwright smoke test passed.")


if __name__ == "__main__":
    asyncio.run(main())
