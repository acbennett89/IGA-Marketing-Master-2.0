"""epic_steps — modular Playwright automation steps for EPIC.

Each module exposes a single function:

    def run(page: Page) -> bool          # single-tab steps
    def run(context: BrowserContext) -> bool  # multi-tab steps

Return True when the step completed successfully (or was not needed).
Return False when the step detected an unrecoverable error.

Call order for a new submission:
    1. step_enterprise_id   — dismiss the Enterprise ID prompt on first launch
    2. step_login           — handle the Applied IDP credential form (new tab)
    3. step_database_select — pick DEMO (debug) or PROD (non-debug) database
    4. (more steps to follow)

Close-app sequence:
    step_logout  — click EPIC Logout and wait for the session to end
"""
