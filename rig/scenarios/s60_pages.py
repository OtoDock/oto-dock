"""The one place a browser is used: where the page itself is the thing tested."""
from __future__ import annotations

import json

from lib.scenario import check, scenario

# Each route: where the browser must land and what that page shows. The dashboard
# draws its sign-in form at any address, so a page counts only with no password
# field, at its own address, with its own landmark.
ROUTES = {
    "/": {"url": "/chat/", "selector": "textarea", "what": "the default agent's chat composer"},
    "/admin": {"url": "/admin", "selector": ":is(h1, h2):has-text('Overview')", "what": "the admin overview"},
    "/chat/personal-assistant": {"url": "/chat/personal-assistant", "selector": "textarea", "what": "the chat composer"},
}

# Runs in the file-tools service, which carries a headless browser.
PAGE_WALK = r"""
import json, sys
from playwright.sync_api import sync_playwright
base, email, password, routes = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
seen = {"failed": [], "errors": [], "pages": {}, "login": None}
watching = [False]

def answered(response):
    path = response.url.split(base, 1)[-1]
    if watching[0] and response.status >= 400 and path.startswith(("/v1/", "/auth/")):
        seen["failed"].append(f"{response.status} {path[:120]}")

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.on("response", answered)
    page.on("pageerror", lambda e: seen["errors"].append(str(e)[:200]))
    page.goto(base + "/", wait_until="networkidle")
    page.fill('input[type="email"]', email)
    page.fill('input[type="password"]', password)
    with page.expect_response(lambda r: r.url.endswith("/auth/login/local")) as login:
        page.keyboard.press("Enter")
    seen["login"] = login.value.status
    if seen["login"] == 200:
        page.locator('input[type="password"]').wait_for(state="detached", timeout=30000)
        watching[0] = True
        for route, want in routes.items():
            # An app's frame mints one viewer token per two seconds and person:
            # a person changes pages slower than that, and so does the walk.
            page.wait_for_timeout(2500)
            page.goto(base + route, wait_until="networkidle")
            seen["pages"][route] = {
                "url": page.url.split(base, 1)[-1],
                "password_fields": page.locator('input[type="password"]').count(),
                "landmark": page.locator(want["selector"]).count(),
            }
    browser.close()
print(json.dumps(seen))
"""


@scenario("pages", needs=("sign-in",), needs_rig=True)
def pages(ctx, broken: bool = False):
    """The sign-in form signs in, and the home page, the admin pages and a chat page
    render in a real browser. Broken: the browser signs in with a wrong password, and
    every page draws the sign-in form."""
    # That service has no route to the public address; the proxy's name serves the same pages.
    done = ctx.compose("exec", "-T", "file-tools", "python3", "-c", PAGE_WALK, "http://otodock-proxy:8400",
                       ctx.email("owner"), ("wrong-" if broken else "") + ctx.password("owner"), json.dumps(ROUTES), timeout=180)
    check(done.returncode == 0, f"the browser walk exited {done.returncode}: {done.stderr[-900:]}")
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    check(seen["login"] == 200, f"the sign-in form's request answered {seen['login']}")
    for route, want in ROUTES.items():
        page = seen["pages"].get(route, {})
        check(page.get("password_fields") == 0, f"{route} shows the sign-in form: {page}")
        check(page.get("url", "").startswith(want["url"]), f"{route} landed at {page.get('url')}, not {want['url']}")
        check(page.get("landmark", 0) > 0, f"{route} shows no {want['what']}: {page}")
    check(not seen["failed"], f"the pages had answers of 400 and above: {seen['failed'][:6]}")
    check(not seen["errors"], f"the pages threw: {seen['errors'][:5]}")
    return f"{len(seen['pages'])} pages"
