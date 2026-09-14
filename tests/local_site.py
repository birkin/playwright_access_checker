"""
Provides a small local test website with Studio's observed controls and link structure.
"""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class LocalSite(ThreadingHTTPServer):
    def __init__(self) -> None:
        """
        Starts a local-only server with configurable delays and denial responses.
        Called by: tests.test_browser.TestBrowser.setUpClass()
        """
        super().__init__(('127.0.0.1', 0), Handler)
        self.mode = 'normal'
        self.hits: list[dict] = []
        self.collection_visits = 0
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def origin(self) -> str:
        """
        Returns the local server's assigned address.
        Called by: tests.test_browser.TestBrowser.run_case()
        """
        return f'http://127.0.0.1:{self.server_port}'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        """
        Keeps routine test HTTP logs out of test output.
        Called by: BaseHTTPRequestHandler
        """

    def do_GET(self) -> None:
        """
        Serves overview pages, item pages, and observed supporting requests.
        Called by: BaseHTTPRequestHandler
        """
        site = self.server
        assert isinstance(site, LocalSite)
        path = urlsplit(self.path).path
        site.hits.append({'path': self.path, 'started': time.monotonic(), 'referer': self.headers.get('Referer')})
        mode = site.mode
        status, headers, body = 200, {}, ''
        if mode == 'slow' and path == '/studio/item/bdr:1/':
            time.sleep(0.65)
        if mode == 'timeout' and path == '/studio/item/bdr:1/':
            time.sleep(1.0)
        if path.startswith('/studio/collections/'):
            site.collection_visits += 1
            size = parse_qs(urlsplit(self.path).query).get('per_page', ['20'])[0]
            if mode == 'ignored':
                size = '20'
            numbers = list(range(1, 7)) if mode != 'empty' else []
            if mode == 'changed' and site.collection_visits > 1:
                numbers.reverse()
            links = ''.join(
                f'<div class="item-thumbnail"><a href="/studio/item/bdr:{number}/"><img alt="Thumbnail for {number}" src="/thumb/{number}"></a></div>'
                for number in numbers
            )
            body = f"""<h1>Example collection</h1><h2 id="item-results">Items</h2>
                <span aria-label="Current Page">1</span><div id="sort-dropdown"><button>Sort by title (A-Z)</button></div>
                <div id="per_page-dropdown"><button onclick="this.nextElementSibling.hidden=false">{size} per page</button>
                <ul hidden><li><a href="?page=1&amp;per_page=50">50 per page</a></li></ul></div>
                {links}<a href="?page=2&amp;per_page=50">Next Page</a>"""
            if mode == 'final_return' and site.collection_visits > 1:
                status, headers, body = 403, {'cf-mitigated': 'challenge', 'cf-ray': 'local-final-return'}, 'Challenge'
            if mode == 'turnstile':
                body = '<h1>Verify you are human</h1><div class="cf-turnstile"></div>'
            if mode == 'turnstile_with_content':
                body += '<div class="cf-turnstile"></div>'
            if mode == 'unsupported_restore' and size == '20':
                body = body.replace('id="per_page-dropdown"', 'id="unavailable-control"')
        elif path.startswith('/studio/item/'):
            number = path.split('bdr:')[1].strip('/')
            back = '/studio/collections/bdr:nz9qn2kb/?page=1&per_page=' + (
                '20' if mode in {'reset', 'unsupported_restore'} else '50'
            )
            back_link = f'<a href="{back}">Back to Results</a>' if mode != 'missing_return' else ''
            height = 2500 if mode in {'long', 'scroll_challenge', 'interrupt', 'time_limit'} else 120
            lazy = """<script>let sent=false; addEventListener('scroll', () => {
                if (!sent && scrollY > 0) {sent=true; fetch('/scroll-data');}
            }); addEventListener('focus', () => fetch('/focus-data'));</script>"""
            background = (
                '<script>setTimeout(()=>fetch("/challenge"),180)</script>'
                if mode == 'background_challenge' and number == '1'
                else ''
            )
            supporting = '<img src="/challenge">' if mode == 'supporting_challenge' else ''
            body = f'{back_link}<h1>Item {number}</h1><main id="content-main"><div id="description">Item description</div><div style="height:{height}px">Page content {supporting}</div></main>{lazy}{background}'
            if mode == 'redirect' and number == '1' and not urlsplit(self.path).query:
                status, headers = 302, {'Location': self.path + '?view=full'}
            if mode == 'denial_page':
                body = '<h1>Access denied</h1><div id="cf-error-details">Denied</div>'
            if mode == 'no_content':
                body = '<h1>Loading item</h1>'
            if mode == 'hidden_title':
                body = body.replace(f'<h1>Item {number}</h1>', f'<h1 hidden>Item {number}</h1>')
            body += '<div id="feedbackModal" hidden><h1 class="modal-title">Feedback</h1></div>'
        elif path == '/challenge' or (path == '/scroll-data' and mode == 'scroll_challenge'):
            status, headers, body = 403, {'cf-mitigated': 'challenge', 'cf-ray': 'local-test-ray'}, 'Challenge'
        elif path.startswith('/thumb/'):
            headers['Content-Type'] = 'image/svg+xml'
            body = '<svg xmlns="http://www.w3.org/2000/svg" width="70" height="50"><rect width="70" height="50"/></svg>'
        else:
            body = 'data'
        if path.startswith('/studio/') and status == 200:
            body = (
                '<!doctype html><html><head><title>Local Studio test</title><link rel="icon" href="data:,"></head><body>'
                + body
                + '</body></html>'
            )
        encoded = body.encode()
        self.send_response(status)
        self.send_header('Content-Type', headers.pop('Content-Type', 'text/html; charset=utf-8'))
        self.send_header('Content-Length', str(len(encoded)))
        self.send_header('Cache-Control', 'no-store')
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass
