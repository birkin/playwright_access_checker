"""
Observes requests and all open tabs before, during, and after deliberate actions.
"""

import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Error, Frame, Page, Request, Response

from lib.config import Settings
from lib.measurement import classify_response, safe_url
from lib.results import Recorder, timestamp


class TrialStopped(Exception):
    """
    Ends orchestration after a recorded stopping condition.
    """


@dataclass
class Attempt:
    attempt_id: str
    url: str
    started: float
    page: Page | None = None
    ready: bool = False
    document_received: bool = False


def content_ready(page: Page, role: str) -> bool:
    """
    Checks expected Studio content without assuming all viewer resources are loaded.
    Called by: Observer.poll(), browser_flow.wait_ready()
    """
    ready = False
    try:
        if role == 'overview':
            ready = '/studio/collections/' in page.url and page.locator('#item-results').is_visible()
        else:
            ready = (
                '/studio/item/' in page.url
                and page.locator('h1:visible').count() > 0
                and page.locator('#content-main').is_visible()
            )
            ready = ready and page.locator('#display-content, #description, #access_conditions').count() > 0
    except Error:
        pass
    return ready


class Observer:
    def __init__(self, context: BrowserContext, settings: Settings, recorder: Recorder) -> None:
        """
        Subscribes before the collection visit, including requests without a Page yet.
        Called by: browser_flow.run_trial()
        """
        self.context = context
        self.settings = settings
        self.recorder = recorder
        self.pages: dict[Page, str] = {}
        self.requests: dict[Request, dict] = {}
        self.finished: set[str] = set()
        self.responses: dict[str, dict] = {}
        self.page_observations: dict[Page, dict] = {}
        self.attempts: list[Attempt] = []
        context.on('page', self.on_page)
        context.on('request', self.on_request)
        context.on('response', self.on_response)
        context.on('requestfailed', self.on_failure)
        context.on('requestfinished', self.on_finished)

    def on_page(self, page: Page) -> None:
        """
        Assigns stable tab IDs and subscribes to navigation notifications.
        Called by: Playwright page notification, on_request(), bind_requests()
        """
        if page not in self.pages:
            tab_id = f'tab-{len(self.pages) + 1}'
            self.pages[page] = tab_id
            self.recorder.metadata['tabs'].append({'tab_id': tab_id, 'url': safe_url(page.url)})
            self.recorder.emit('tab_created', tab_id=tab_id, url=safe_url(page.url))
            page.on('framenavigated', self.on_navigation)
            page.on('crash', self.on_crash)
            if len(self.pages) > self.settings.max_items + 1 or (self.settings.workflow == 'return' and len(self.pages) > 1):
                self.recorder.stop('unexpected_extra_tab', tab_id=tab_id)

    def on_crash(self, page: Page) -> None:
        """
        Stops if Chromium reports that a page crashed.
        Called by: Playwright Page crash notification
        """
        self.recorder.stop('page_crashed', tab_id=self.pages.get(page))

    def on_navigation(self, frame: Frame) -> None:
        """
        Records final page locations without inventing requests.
        Called by: Playwright Page framenavigated notification
        """
        if frame == frame.page.main_frame:
            self.recorder.emit('page_change', tab_id=self.pages.get(frame.page), url=safe_url(frame.url))

    def request_page(self, request: Request) -> Page | None:
        """
        Handles first popup requests and service-worker requests without a frame.
        Called by: on_request(), bind_requests()
        """
        page = None
        try:
            page = request.frame.page
        except Error:
            pass
        return page

    def on_request(self, request: Request) -> None:
        """
        Counts each HTTP request once at its start and records its originating tab.
        Called by: Playwright request notification, on_response(), on_failure()
        """
        if request not in self.requests:
            parts = urlsplit(request.url)
            included = parts.hostname in self.settings.hosts
            first = self.recorder.started is None and included and request.resource_type == 'document'
            if first:
                self.recorder.started = time.monotonic()
                self.recorder.metadata['measurement_started_at'] = timestamp()
            page = self.request_page(request)
            if page is not None:
                self.on_page(page)
            role_url = request.url if request.resource_type == 'document' or page is None else page.url
            role = 'overview' if '/studio/collections/' in role_url else 'item' if '/studio/item/' in role_url else 'unknown'
            redirected_from = request.redirected_from
            redirected = self.requests.get(redirected_from, {}) if redirected_from is not None else {}
            details = {
                'request_id': f'request-{len(self.requests) + 1}',
                'tab_id': self.pages[page] if page is not None else None,
                'url': safe_url(request.url),
                'hostname': parts.hostname,
                'included_host': included,
                'resource_type': request.resource_type,
                'http': parts.scheme in {'http', 'https'},
                'method': request.method,
                'page_role': role,
                'referer': safe_url(request.headers['referer']) if request.headers.get('referer') else None,
                'redirected_from': redirected.get('request_id'),
            }
            if first:
                details['elapsed'] = 0.0
            self.requests[request] = self.recorder.emit('request', **details)

    def on_response(self, response: Response) -> None:
        """
        Saves allowed response evidence and stops on included-host interference.
        Called by: Playwright BrowserContext response notification
        """
        request = response.request
        if request not in self.requests:
            self.on_request(request)
        record = self.requests[request]
        headers = {
            key: value
            for key, value in response.headers.items()
            if key in {'cf-mitigated', 'cf-ray', 'content-type', 'server', 'retry-after'}
        }
        self.recorder.emit(
            'response',
            request_id=record['request_id'],
            tab_id=record['tab_id'],
            url=safe_url(response.url),
            status=response.status,
            headers=headers,
        )
        signal = classify_response(response.status, headers)
        self.responses[record['request_id']] = {'status': response.status, 'headers': headers}
        if signal and record['included_host']:
            self.recorder.stop(
                signal['reason'],
                source=signal['source'],
                request_id=record['request_id'],
                tab_id=record['tab_id'],
                status=response.status,
                headers=headers,
                url=record['url'],
                resource_type=record['resource_type'],
                request_elapsed=record['elapsed'],
            )
        if request.resource_type == 'document':
            original = request
            while original.redirected_from is not None:
                original = original.redirected_from
            for attempt in self.attempts:
                if original.url == attempt.url:
                    attempt.document_received = True

    def on_failure(self, request: Request) -> None:
        """
        Records connection failure categories without browser logs or credentials.
        Called by: Playwright BrowserContext requestfailed notification
        """
        if request not in self.requests:
            self.on_request(request)
        record = self.requests[request]
        match = re.search(r'net::[A-Z0-9_]+', request.failure or '')
        failure = match.group() if match else 'network failure'
        self.recorder.emit('request_failed', request_id=record['request_id'], tab_id=record['tab_id'], failure=failure)
        self.finished.add(record['request_id'])
        if record['included_host']:
            self.recorder.stop('network_error', request_id=record['request_id'], tab_id=record['tab_id'], failure=failure)

    def on_finished(self, request: Request) -> None:
        """
        Marks transfer completion separately from response headers.
        Called by: Playwright BrowserContext requestfinished notification
        """
        record = self.requests.get(request)
        if record:
            self.recorder.emit('request_finished', request_id=record['request_id'], tab_id=record['tab_id'])
            self.finished.add(record['request_id'])

    def bind_requests(self) -> None:
        """
        Matches requests and tabs, using the page URL when a request was not observed.
        Called by: poll()
        """
        for request, record in list(self.requests.items()):
            if record['tab_id'] is None:
                page = self.request_page(request)
                if page is not None:
                    self.on_page(page)
                    record['tab_id'] = self.pages[page]
                    self.recorder.emit('request_tab', request_id=record['request_id'], tab_id=record['tab_id'])
        for attempt in self.attempts:
            if attempt.page is None:
                for request, record in list(self.requests.items()):
                    original = request
                    while original.redirected_from is not None:
                        original = original.redirected_from
                    if original.url == attempt.url and record['tab_id']:
                        attempt.page = self.request_page(request)
                        self.recorder.emit(
                            'attempt_tab', attempt_id=attempt.attempt_id, tab_id=record['tab_id'], source='request'
                        )
                        break
            if attempt.page is None:
                assigned = {item.page for item in self.attempts if item.page is not None}
                matches = [page for page in self.pages if page not in assigned and page.url == attempt.url]
                if len(matches) == 1:
                    attempt.page = matches[0]
                    self.recorder.emit(
                        'attempt_tab',
                        attempt_id=attempt.attempt_id,
                        tab_id=self.pages[attempt.page],
                        source='page_url',
                    )

    def guard(self) -> None:
        """
        Prevents the next action after a signal or the monotonic duration limit.
        Called by: browser_flow actions, poll(), timeout_ms()
        """
        recorder = self.recorder
        if (
            recorder.stopped is None
            and recorder.started is not None
            and time.monotonic() - recorder.started >= self.settings.max_duration_seconds
        ):
            recorder.stop('time_limit', elapsed=self.settings.max_duration_seconds)
        if recorder.stopped is not None:
            raise TrialStopped

    def timeout_ms(self) -> float:
        """
        Limits navigation waits to the remaining observation time.
        Called by: browser_flow.open_collection(), browser_flow.click_link()
        """
        self.guard()
        remaining = self.settings.navigation_timeout_seconds
        if self.recorder.started is not None:
            remaining = min(remaining, self.settings.max_duration_seconds - (time.monotonic() - self.recorder.started))
        return max(1, remaining * 1000)

    def poll(self) -> None:
        """
        Checks every tab for readiness, denial pages, and pending opening deadlines.
        Called by: browser_flow
        """
        self.guard()
        self.bind_requests()
        for page in list(self.pages):
            self.guard()
            if page.is_closed():
                self.recorder.stop('tab_closed', tab_id=self.pages[page])
                self.guard()
            try:
                evidence = page.evaluate("""() => ({title: document.title.slice(0, 300),
                    challenge: !!document.querySelector('#challenge-running, #challenge-form, #cf-challenge-running'),
                    denial: !!document.querySelector('#cf-error-details'),
                    turnstile: !!document.querySelector('.cf-turnstile, [name="cf-turnstile-response"], iframe[src*="challenges.cloudflare.com"]'),
                    turnstile_script: !!document.querySelector('script[src*="challenges.cloudflare.com/turnstile/"]'),
                    parsed: document.readyState !== 'loading',
                    expected: !!document.querySelector('#item-results, #content-main #description, #content-main #display-content'),
                    heading_count: document.querySelectorAll('h1').length,
                    heading: document.querySelector('h1')?.textContent?.trim().slice(0, 300) || ''})""")
            except Error:
                continue
            self.guard()
            evidence['url'] = safe_url(page.url)
            if self.page_observations.get(page) != evidence:
                self.recorder.emit('page_observed', tab_id=self.pages[page], **evidence)
                self.page_observations[page] = evidence
            verification = (
                (evidence['turnstile'] or evidence['turnstile_script']) and evidence['parsed'] and not evidence['expected']
            )
            denied = (
                evidence['challenge']
                or evidence['denial']
                or evidence['heading'].lower() in {'access denied', 'you have been blocked', 'sorry, you have been blocked'}
            )
            if urlsplit(page.url).hostname in self.settings.hosts and (denied or verification):
                documents = [
                    record
                    for record in self.requests.values()
                    if record['tab_id'] == self.pages[page] and record['resource_type'] == 'document'
                ]
                document = documents[-1] if documents else {}
                response = self.responses.get(document['request_id'], {}) if document else {}
                self.recorder.stop(
                    'verification_required' if verification else 'denial_page',
                    tab_id=self.pages[page],
                    url=safe_url(page.url),
                    title=evidence['title'],
                    request_id=document.get('request_id'),
                    request_elapsed=document.get('elapsed'),
                    **response,
                    source='Turnstile verification page; rule unconfirmed'
                    if verification
                    else 'suspected; requires confirmation',
                )
                self.guard()
        for attempt in self.attempts:
            self.guard()
            if not attempt.ready and attempt.page is not None and content_ready(attempt.page, 'item'):
                self.guard()
                attempt.ready = True
                if not attempt.document_received:
                    self.recorder.emit(
                        'request_evidence_missing',
                        attempt_id=attempt.attempt_id,
                        tab_id=self.pages[attempt.page],
                        url=safe_url(attempt.url),
                        detail='Item content is ready, but its document response was not recorded.',
                    )
                self.recorder.emit(
                    'item_ready',
                    attempt_id=attempt.attempt_id,
                    tab_id=self.pages[attempt.page],
                    url=safe_url(attempt.page.url),
                )
                self.recorder.emit(
                    'visit_ready',
                    visit_id=attempt.attempt_id,
                    tab_id=self.pages[attempt.page],
                    url=safe_url(attempt.page.url),
                    title=attempt.page.title(),
                )
            if time.monotonic() - attempt.started >= self.settings.navigation_timeout_seconds and (
                attempt.page is None or (not attempt.document_received and not attempt.ready)
            ):
                documents = [
                    record
                    for record in self.requests.values()
                    if record['resource_type'] == 'document' and record['url'] == safe_url(attempt.url)
                ]
                self.recorder.stop(
                    'page_opening_timeout',
                    attempt_id=attempt.attempt_id,
                    tab_id=self.pages[attempt.page] if attempt.page is not None else None,
                    request_id=documents[-1]['request_id'] if documents else None,
                    url=safe_url(attempt.url),
                    page_assigned=attempt.page is not None,
                    document_received=attempt.document_received,
                    error='No tab was matched to the selected item before the opening timeout.'
                    if attempt.page is None
                    else 'The matched tab has neither a recorded document response nor ready item content.',
                )
                self.guard()
