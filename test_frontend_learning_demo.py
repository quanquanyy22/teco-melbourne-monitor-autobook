#!/usr/bin/env python3
"""Local Playwright exercise for frontend_learning_demo.html."""

from pathlib import Path

from playwright.sync_api import sync_playwright


DEMO = Path(__file__).with_name("frontend_learning_demo.html")


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(DEMO.as_uri())

        # Wait for a meaningful application state, never a guessed sleep.
        form = page.locator("#demo-form")
        form.wait_for(state="visible", timeout=5_000)

        # Label-based locators follow accessible HTML relationships.
        page.get_by_label("Full name").fill("Demo Student")
        page.get_by_label("Email").fill("demo@example.invalid")
        page.get_by_label("What is 12 + 7?").select_option(label="19")
        page.get_by_label("Choose an Australian city").select_option(label="Brisbane")
        page.get_by_label("I confirm these demo values are correct").check()

        # Verify the browser state before causing the submit event.
        assert page.get_by_label("Full name").input_value() == "Demo Student"
        assert page.get_by_label("Email").input_value() == "demo@example.invalid"
        assert page.get_by_label("What is 12 + 7?").input_value() == "19"
        assert page.get_by_label("Choose an Australian city").input_value() == "Brisbane"
        assert page.get_by_label("I confirm these demo values are correct").is_checked()

        page.get_by_role("button", name="Submit demo").click()
        result = page.locator("#status").inner_text()
        assert "Submitted locally" in result
        assert "Demo Student" in result
        print("PASS: delayed form loaded, filled, verified, and submitted locally")
        browser.close()


if __name__ == "__main__":
    main()
