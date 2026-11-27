"""Browser tests (Playwright, Chromium) for the service record form at phone size: drafts, the outbox,
offline use and conflicts. They drive the real page against the real test server (temp DB per test).
Install the browser once with `python -m playwright install chromium`."""
import pytest

PHONE = {'width': 390, 'height': 844}


@pytest.fixture
def browser_context_args(browser_context_args):
    # Maine time on any machine (CI runs in UTC), so the page's "today" matches the server's.
    return {**browser_context_args, 'viewport': PHONE, 'timezone_id': 'America/New_York',
            'has_touch': True, 'service_workers': 'allow'}


@pytest.fixture
def base(app):
    return f'http://127.0.0.1:{app}'


@pytest.fixture
def signed_in(context, base, tech):
    """The browser context, signed in as the `tech` user (same session as the `tech` API client)."""
    name, value = tech.cookie.split('=', 1)
    context.add_cookies([{'name': name, 'value': value, 'url': base}])
    return context


@pytest.fixture
def native_dialogs(page):
    """Every browser alert/confirm/prompt the page opens. The app must never use them."""
    seen = []
    page.on('dialog', lambda d: (seen.append(d.message), d.dismiss()))
    return seen


class SwitchableServer:
    """A test server that can be stopped and started again on the same port: "no signal" for the
    service worker too (Playwright's set_offline doesn't reliably reach a worker's own fetches)."""

    def __init__(self):
        self.port = None
        self.start()

    def start(self):
        import threading
        import server
        self.httpd = server.ThreadingHTTPServer(('127.0.0.1', self.port or 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    @property
    def base(self):
        return f'http://127.0.0.1:{self.port}'


@pytest.fixture
def switchable(db, context, tech):
    """A stoppable server on the same database, with the browser signed in to it as `tech`."""
    srv = SwitchableServer()
    name, value = tech.cookie.split('=', 1)
    context.add_cookies([{'name': name, 'value': value, 'url': srv.base}])
    yield srv
    try:
        srv.stop()
    except Exception:
        pass
