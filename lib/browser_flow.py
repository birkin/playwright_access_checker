"""
Runs both browsing workflows with actual Studio links and one Chromium session.
"""

import json
import re
import time
from urllib.parse import parse_qsl, urlsplit

from playwright.sync_api import Error, Locator, Page, ProxySettings, sync_playwright

from lib.config import Settings
from lib.measurement import SelectedItem, planned_times, safe_error, safe_url, select_items
from lib.observation import Attempt, Observer, TrialStopped, content_ready
from lib.results import Recorder, timestamp

THUMBNAILS = '.item-thumbnail a[href*="/studio/item/"]:has(img)'
POSITION_SCRIPT = """() => ({y: window.scrollY, height: window.innerHeight,
    total: document.documentElement.scrollHeight,
    bottom: window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2})"""


def pause_until(observer: Observer, page: Page, deadline: float) -> None:
    """
    Processes browser notifications during pacing and viewing pauses.
    Called by: gather_items(), view_item(), run_workflow()
    """
    while time.monotonic() < deadline:
        observer.poll()
        observer.guard()
        remaining = deadline - time.monotonic()
        if remaining > 0:
            page.wait_for_timeout(min(50, remaining * 1000))
    observer.poll()


def wait_ready(observer: Observer, page: Page, role: str, visit_id: str) -> None:
    """
    Waits separately for selected-tab content while observing other tabs.
    Called by: open_collection(), view_item(), return_to_collection(), restore_listing()
    """
    deadline = time.monotonic() + observer.settings.navigation_timeout_seconds
    observer.recorder.emit('content_wait_start', tab_id=observer.pages[page], role=role, visit_id=visit_id)
    while not content_ready(page, role):
        observer.poll()
        if time.monotonic() >= deadline:
            observer.recorder.stop(
                'content_timeout',
                tab_id=observer.pages[page],
                role=role,
                visit_id=visit_id,
                url=safe_url(page.url),
                error='The tab opened, but expected page content did not become ready.',
            )
            observer.guard()
        page.wait_for_timeout(50)
    observer.poll()
    observer.guard()
    observer.recorder.emit('content_wait_end', tab_id=observer.pages[page], role=role, visit_id=visit_id)
    if role == 'overview':
        observer.recorder.emit(
            'visit_ready', visit_id=visit_id, tab_id=observer.pages[page], url=safe_url(page.url), title=page.title()
        )


def listing_state(page: Page) -> dict:
    """
    Reads the actual page-size control, current page, sort choice, and filters.
    Called by: check_listing()
    """
    state = page.evaluate("""() => ({
        per_page: document.querySelector('#per_page-dropdown button')?.textContent.trim() || '',
        page: document.querySelector('[aria-label="Current Page"]')?.textContent.trim() || '',
        sort: document.querySelector('#sort-dropdown button')?.textContent.trim() || ''})""")
    size = re.search(r'^(\d+)\s+per page', state['per_page'])
    state['per_page'] = int(size.group(1)) if size else None
    state['page'] = int(state['page']) if state['page'].isdigit() else None
    state['filters'] = [
        (key, value)
        for key, value in parse_qsl(urlsplit(safe_url(page.url)).query)
        if key not in {'page', 'per_page', 'sort'}
    ]
    state['url'] = safe_url(page.url)
    return state


def check_listing(observer: Observer, page: Page, initial: bool = False) -> dict:
    """
    Verifies the collection and preserves its sort and filter choices.
    Called by: open_collection(), return_to_collection(), restore_listing()
    """
    observer.guard()
    state = listing_state(page)
    observer.guard()
    observer.recorder.emit('listing', tab_id=observer.pages[page], **state)
    expected, actual = urlsplit(observer.settings.collection_url), urlsplit(page.url)
    valid = (actual.hostname, actual.path.rstrip('/')) == (expected.hostname, expected.path.rstrip('/'))
    valid = valid and state['page'] == 1 and bool(state['sort'])
    original = observer.recorder.metadata.get('initial_listing')
    if original and (state['sort'] != original['sort'] or state['filters'] != original['filters']):
        valid = False
    if not valid or (initial and state['per_page'] != 50):
        observer.recorder.stop('unexpected_listing', observed=state)
        observer.guard()
    if initial:
        observer.recorder.metadata['initial_listing'] = state
    return state


def open_collection(observer: Observer, page: Page) -> None:
    """
    Makes the first collection request directly with page 1 and 50 requested.
    Called by: run_workflow()
    """
    recorder = observer.recorder
    recorder.stage, recorder.action_id = 'initial_collection', 'collection-1'
    recorder.emit(
        'collection_open', visit_id='collection-1', tab_id=observer.pages[page], url=observer.settings.collection_url
    )
    page.goto(observer.settings.collection_url, wait_until='commit', timeout=observer.timeout_ms())
    observer.guard()
    wait_ready(observer, page, 'overview', 'collection-1')
    check_listing(observer, page, initial=True)


def scroll_page(observer: Observer, page: Page, purpose: str, direction: int = 1) -> dict:
    """
    Scrolls one section and records requested and observed movement.
    Called by: gather_items(), find_link(), view_item()
    """
    observer.guard()
    before = page.evaluate(POSITION_SCRIPT)
    observer.guard()
    distance = before['height'] * 0.8 * direction
    observer.recorder.emit(
        'scroll_start',
        tab_id=observer.pages[page],
        purpose=purpose,
        requested_distance=distance,
        before=before,
        method='window.scrollBy',
    )
    page.evaluate('(distance) => window.scrollBy({top: distance, behavior: "instant"})', distance)
    page.wait_for_timeout(20)
    after = page.evaluate(POSITION_SCRIPT)
    observer.recorder.emit(
        'scroll', tab_id=observer.pages[page], purpose=purpose, requested_distance=distance, before=before, after=after
    )
    observer.guard()
    return after


def thumbnail_urls(page: Page) -> list[str]:
    """
    Reads distinct rendered thumbnails in displayed order without fetching URLs.
    Called by: gather_items(), return_to_collection()
    """
    urls = page.locator(THUMBNAILS).evaluate_all("""elements => elements.filter(e => e.getClientRects().length)
        .map(e => ({url:e.href, y:e.getBoundingClientRect().top, x:e.getBoundingClientRect().left}))
        .sort((a,b) => a.y-b.y || a.x-b.x).map(e => e.url)""")
    return list(dict.fromkeys(urls))


def gather_items(observer: Observer, page: Page) -> list[SelectedItem]:
    """
    Gathers page-one thumbnails within shared time and scroll limits.
    Called by: run_workflow()
    """
    recorder, settings = observer.recorder, observer.settings
    recorder.stage, recorder.action_id = 'gather_links', 'selection'
    started = time.monotonic()
    urls = thumbnail_urls(page)
    scrolls = 0
    required = settings.selection_start + 2 * (settings.max_items - 1)
    reason = 'enough_candidates'
    while len(urls) < required:
        observer.poll()
        if scrolls >= settings.max_scroll_actions:
            reason = 'scroll_limit'
            break
        if page.evaluate(POSITION_SCRIPT)['bottom']:
            reason = 'end_of_listing'
            break
        scroll_page(observer, page, 'overview_gathering')
        scrolls += 1
        pause_until(observer, page, time.monotonic() + 0.1)
        urls = list(dict.fromkeys([*urls, *thumbnail_urls(page)]))
    selected = select_items(urls, settings.selection_start, settings.max_items)
    saved_selection = [{**item, 'url': safe_url(item['url'])} for item in selected]
    recorder.metadata.update(displayed_urls=[safe_url(url) for url in urls], selected=saved_selection)
    recorder.emit(
        'selection',
        available_count=len(urls),
        selected=saved_selection,
        scrolls=scrolls,
        duration=time.monotonic() - started,
        reason=reason,
        examined_all=reason == 'end_of_listing',
    )
    if len(selected) < settings.max_items:
        recorder.metadata['known_unknowns'].append(
            f'Only {len(selected)} eligible items selected; gathering ended at {reason}.'
        )
    if not selected:
        recorder.stop('no_eligible_items' if reason == 'end_of_listing' else 'selection_limit')
        observer.guard()
    return selected


def find_link(observer: Observer, page: Page, locator: Locator, purpose: str) -> Locator:
    """
    Finds an actual link and records scrolls needed to click it.
    Called by: run_workflow(), return_to_collection(), restore_listing()
    """
    scrolls, found = 0, False
    while not found:
        observer.poll()
        box = locator.bounding_box() if locator.count() else None
        observer.guard()
        height = page.evaluate(POSITION_SCRIPT)['height']
        if box and box['height'] > 0 and box['y'] >= 0 and box['y'] + min(box['height'], 40) <= height:
            found = True
        else:
            if scrolls >= observer.settings.max_scroll_actions:
                observer.recorder.stop('link_scroll_limit', purpose=purpose)
                observer.guard()
            direction = -1 if box and box['y'] < 0 else 1
            after = scroll_page(observer, page, purpose, direction)
            scrolls += 1
            if not box and after['bottom']:
                observer.recorder.stop('missing_selected_link' if purpose == 'overview_thumbnail' else 'missing_return_link')
                observer.guard()
    return locator


def click_link(observer: Observer, link: Locator, new_tab: bool = False) -> None:
    """
    Clicks the actual link, temporarily targeting a new tab when requested.
    Called by: run_workflow(), return_to_collection(), restore_listing()
    """
    observer.guard()
    if new_tab:
        ## A target=_blank click records initial requests that visible Chromium misses on middle-click.
        original_target = link.get_attribute('target')
        link.evaluate('(element) => element.setAttribute("target", "_blank")')
        try:
            observer.guard()
            link.click(no_wait_after=True, timeout=observer.timeout_ms())
        finally:
            link.evaluate(
                """(element, original) => {
                if (original === null) element.removeAttribute('target');
                else element.setAttribute('target', original);
            }""",
                original_target,
            )
    else:
        link.click(no_wait_after=True, timeout=observer.timeout_ms())
    observer.guard()


def view_item(observer: Observer, attempt: Attempt, duration: float) -> None:
    """
    Views from the top with section scrolls and pauses inside one total duration.
    Called by: run_workflow()
    """
    recorder = observer.recorder
    recorder.stage, recorder.action_id = 'wait_for_item_tab', attempt.attempt_id
    while attempt.page is None:
        observer.poll()
        next(iter(observer.pages)).wait_for_timeout(50)
    page = attempt.page
    recorder.stage, recorder.action_id = 'view_item', attempt.attempt_id
    observer.guard()
    if observer.settings.workflow == 'tabs':
        page.bring_to_front()
        observer.guard()
        recorder.emit('tab_switch', tab_id=observer.pages[page], attempt_id=attempt.attempt_id)
    wait_ready(observer, page, 'item', attempt.attempt_id)
    observer.guard()
    position = page.evaluate(POSITION_SCRIPT)
    if position['y'] != 0:
        observer.guard()
        page.evaluate('window.scrollTo(0, 0)')
        observer.guard()
        recorder.emit('view_reset_top', tab_id=observer.pages[page], before=position)
    started = time.monotonic()
    deadline, scrolls, bottom, completed = started + duration, 0, page.evaluate(POSITION_SCRIPT)['bottom'], False
    recorder.emit('view_start', attempt_id=attempt.attempt_id, tab_id=observer.pages[page], planned_seconds=duration)
    try:
        while time.monotonic() < deadline:
            recorder.emit('pause_start', attempt_id=attempt.attempt_id, tab_id=observer.pages[page])
            try:
                pause_until(observer, page, min(deadline, time.monotonic() + 1.0))
            finally:
                recorder.emit(
                    'pause_end',
                    attempt_id=attempt.attempt_id,
                    tab_id=observer.pages[page],
                    interrupted=recorder.stopped is not None,
                )
            if time.monotonic() + 0.03 < deadline:
                position = page.evaluate(POSITION_SCRIPT)
                observer.guard()
                if not position['bottom']:
                    scrolls += 1
                    position = scroll_page(observer, page, 'item_viewing')
                bottom = position['bottom']
        observer.poll()
        completed = True
    finally:
        ending = min(time.monotonic(), recorder.stopped) if recorder.stopped is not None else time.monotonic()
        recorder.emit(
            'view_end',
            attempt_id=attempt.attempt_id,
            tab_id=observer.pages[page],
            planned_seconds=duration,
            actual_seconds=max(0, ending - started),
            scrolls=scrolls,
            bottom_reached=bottom,
            completed=completed,
        )


def wait_navigation(observer: Observer, page: Page, destination: str) -> None:
    """
    Waits for a link navigation, including its redirects, without a direct visit.
    Called by: return_to_collection(), restore_listing(), run_workflow()
    """
    deadline = time.monotonic() + observer.settings.navigation_timeout_seconds
    expected_path = urlsplit(destination).path
    while urlsplit(page.url).path != expected_path:
        observer.poll()
        if time.monotonic() >= deadline:
            observer.recorder.stop('navigation_timeout', url=safe_url(destination), tab_id=observer.pages[page])
            observer.guard()
        page.wait_for_timeout(50)
    observer.poll()


def restore_listing(observer: Observer, page: Page, visit_id: str) -> None:
    """
    Makes one restoration attempt through Studio's actual page-size control.
    Called by: return_to_collection()
    """
    button = page.locator('#per_page-dropdown button')
    if button.count() != 1:
        observer.recorder.stop('missing_page_size_control')
        observer.guard()
    find_link(observer, page, button, 'return_restore')
    click_link(observer, button)
    link = page.locator('#per_page-dropdown a').filter(has_text=re.compile(r'^\s*50\s*per page\s*$')).first
    if link.count() != 1:
        observer.recorder.stop('missing_page_size_control')
        observer.guard()
    observer.guard()
    url = link.evaluate('(element) => element.href')
    observer.recorder.emit('listing_restore', visit_id=visit_id, tab_id=observer.pages[page], url=safe_url(url))
    click_link(observer, link)
    deadline = time.monotonic() + observer.settings.navigation_timeout_seconds
    restored = False
    while not restored:
        observer.poll()
        try:
            restored = listing_state(page)['per_page'] == 50
        except Error:
            restored = False
        if time.monotonic() >= deadline:
            observer.recorder.stop('listing_restoration_failed')
            observer.guard()
        if not restored:
            page.wait_for_timeout(50)
    wait_ready(observer, page, 'overview', visit_id)
    check_listing(observer, page)


def return_to_collection(observer: Observer, attempt: Attempt) -> None:
    """
    Follows the return link, restores 50 when needed, and checks original item order.
    Called by: run_workflow()
    """
    page, recorder = attempt.page, observer.recorder
    assert page is not None, 'Returning to the collection requires an opened item tab.'
    visit_id = 'return-' + attempt.attempt_id
    recorder.stage, recorder.action_id = 'return_to_collection', visit_id
    link = page.get_by_role('link', name=re.compile(r'^\s*Back to Results\s*$')).first
    if not link.count():
        path = urlsplit(observer.settings.collection_url).path
        link = page.locator(f'a[href={json.dumps(path)}]').first
    link = find_link(observer, page, link, 'return_link')
    observer.guard()
    destination = link.evaluate('(element) => element.href')
    recorder.emit('return_open', visit_id=visit_id, tab_id=observer.pages[page], url=safe_url(destination))
    click_link(observer, link)
    wait_navigation(observer, page, destination)
    wait_ready(observer, page, 'overview', visit_id)
    state = check_listing(observer, page)
    if state['per_page'] != 50:
        restore_listing(observer, page, visit_id + '-restore')
    observer.guard()
    urls, original = thumbnail_urls(page), recorder.metadata['displayed_urls']
    if [safe_url(url) for url in urls[: len(original)]] != original[: len(urls)]:
        recorder.metadata['known_unknowns'].append('The displayed overview order changed after returning.')
        recorder.stop('listing_order_changed')
        observer.guard()


def run_workflow(observer: Observer, overview: Page) -> None:
    """
    Opens the common selection and completes the selected workflow within its limits.
    Called by: run_trial()
    """
    open_collection(observer, overview)
    selected = gather_items(observer, overview)
    settings, recorder = observer.settings, observer.recorder
    plan = planned_times(
        settings.seed,
        len(selected),
        settings.open_interval_seconds,
        settings.open_jitter_seconds,
        settings.view_seconds,
        settings.view_jitter_seconds,
    )
    recorder.metadata['planned_times'] = plan
    recorder.save()
    previous_start = None
    for index, item in enumerate(selected):
        recorder.stage, recorder.action_id = 'open_items', f'item-{index + 1}'
        deadline = (
            previous_start + plan['opening_intervals'][index - 1]
            if previous_start is not None and settings.workflow == 'tabs'
            else None
        )
        if deadline is not None:
            pause_until(observer, overview, deadline)
        path = urlsplit(item['url']).path
        locator = (
            overview.locator(THUMBNAILS)
            .and_(overview.locator(f'a[href={json.dumps(path)}], a[href={json.dumps(item["url"])}]'))
            .first
        )
        link = find_link(observer, overview, locator, 'overview_thumbnail')
        observer.guard()
        started = time.monotonic()
        attempt_id = f'item-{index + 1}'
        attempt = Attempt(attempt_id, item['url'], started, overview if settings.workflow == 'return' else None)
        observer.attempts.append(attempt)
        recorder.emit(
            'item_open',
            attempt_id=attempt_id,
            visit_id=attempt_id,
            url=safe_url(item['url']),
            tab_id=observer.pages[overview] if settings.workflow == 'return' else None,
            position=item['position'],
            opening_delay=None if deadline is None else max(0, started - deadline),
        )
        click_link(observer, link, new_tab=settings.workflow == 'tabs')
        previous_start = started
        if settings.workflow == 'tabs':
            focus = overview.evaluate('() => ({focused: document.hasFocus(), visibility: document.visibilityState})')
            observer.guard()
            recorder.emit('overview_focus', tab_id=observer.pages[overview], **focus)
        if settings.workflow == 'return':
            wait_navigation(observer, overview, item['url'])
            view_item(observer, attempt, plan['viewing_durations'][index])
            return_to_collection(observer, attempt)
        observer.poll()
    if settings.workflow == 'tabs':
        for index, attempt in enumerate(observer.attempts):
            view_item(observer, attempt, plan['viewing_durations'][index])
    observer.poll()
    recorder.stop('workflow_complete')


def run_trial(settings: Settings, headless: bool = False) -> Recorder:
    """
    Saves evidence on completion, error, or interruption and closes the trial browser.
    Called by: main.main(), tests.test_browser
    """
    recorder = Recorder(settings)
    browser, observer = None, None
    try:
        with sync_playwright() as playwright:
            try:
                proxy: ProxySettings | None = None
                if settings.proxy_server:
                    proxy = {'server': settings.proxy_server}
                    if settings.proxy_username:
                        proxy.update(username=settings.proxy_username, password=settings.proxy_password)
                browser = playwright.chromium.launch(headless=headless, proxy=proxy)
                context = browser.new_context(
                    viewport={'width': 1280, 'height': 900}, locale='en-US', timezone_id='America/New_York'
                )
                recorder.metadata['browser'] = {
                    'version': browser.version,
                    'visible': not headless,
                    'viewport': {'width': 1280, 'height': 900},
                    'locale': 'en-US',
                    'timezone': 'America/New_York',
                    'new_session': True,
                    'tab_opening_method': 'left click on actual thumbnail with temporary target=_blank; original target restored',
                    'supporting_requests': 'enabled',
                    'cache': 'normal browser behavior',
                    'extra_headers': {},
                }
                observer = Observer(context, settings, recorder)
                overview = context.new_page()
                run_workflow(observer, overview)
            except TrialStopped:
                pass
            except KeyboardInterrupt:
                recorder.stop('user_interrupted')
            except Error as exc:
                if observer is not None:
                    try:
                        observer.guard()
                    except TrialStopped:
                        pass
                recorder.stop('browser_error', error=safe_error(exc))
            except Exception as exc:  # noqa: BLE001 -- preserve evidence for unexpected application failures
                recorder.stop('application_error', error=safe_error(exc))
            finally:
                recorder.metadata['observation_saved_at'] = timestamp()
                if observer:
                    observer.bind_requests()
                    recorder.metadata['unfinished_requests'] = [
                        record['request_id']
                        for record in observer.requests.values()
                        if record['request_id'] not in observer.finished
                    ]
                recorder.save()
                if browser:
                    try:
                        browser.close()
                    except Error:
                        pass
    except KeyboardInterrupt:
        recorder.stop('user_interrupted')
    except Exception as exc:  # noqa: BLE001 -- preserve startup and cleanup failures without logging secrets
        recorder.stop('browser_startup_error', error=safe_error(exc))
    finally:
        recorder.metadata['cleanup_finished_at'] = timestamp()
        recorder.save()
        recorder.stream.close()
    return recorder
