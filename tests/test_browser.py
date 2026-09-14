"""
Checks complete browser trials against a local website; never contacts BDR.
"""

import json
import tempfile
import unittest
from unittest.mock import PropertyMock, patch

from playwright.sync_api import Request, Response, sync_playwright

from lib.browser_flow import click_link, run_trial
from lib.config import Settings
from lib.observation import Observer
from lib.results import Recorder, rebuild_report
from tests.local_site import LocalSite


class MissingItemDocuments(Observer):
    def on_request(self, request: Request) -> None:
        """
        Omits item-document notifications to exercise independent tab matching.
        Called by: Playwright request notification
        """
        if not (request.resource_type == 'document' and '/studio/item/' in request.url):
            super().on_request(request)

    def on_response(self, response: Response) -> None:
        """
        Omits the corresponding response without inventing a successful request.
        Called by: Playwright response notification
        """
        if not (response.request.resource_type == 'document' and '/studio/item/' in response.url):
            super().on_response(response)


class TestBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        """
        Checks browser workflows against one local HTTP server.
        """
        cls.site = LocalSite()

    @classmethod
    def tearDownClass(cls) -> None:
        """
        Checks cleanup by closing the local server and its thread.
        """
        cls.site.shutdown()
        cls.site.server_close()
        cls.site.thread.join()

    def setUp(self) -> None:
        """
        Checks each trial using its own results directory and browser context.
        """
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.site.mode = 'normal'
        self.site.hits = []
        self.site.collection_visits = 0

    def run_case(self, workflow: str = 'tabs', mode: str = 'normal', headless: bool = True, **changes: object) -> tuple:
        """
        Checks a complete local trial with shortened deterministic timing.
        """
        self.site.mode = mode
        self.site.hits = []
        self.site.collection_visits = 0
        values = {
            'collection_id': 'bdr:nz9qn2kb',
            'workflow': workflow,
            'cf_settings_label': 'local test',
            'cf_settings_notes': 'Made-up responses.',
            'output_dir': self.temp.name,
            'bdr_hosts': '127.0.0.1',
            'seed': 42,
            'max_items': 3,
            'view_seconds': 0.06,
            'view_jitter_seconds': 0,
            'open_interval_seconds': 0.12,
            'open_jitter_seconds': 0,
            'navigation_timeout_seconds': 2,
            'max_duration_seconds': 12,
        }
        values.update(changes)
        url = self.site.origin + '/studio/collections/bdr:nz9qn2kb/?page=1&per_page=50'
        with patch.object(Settings, 'collection_url', new_callable=PropertyMock, return_value=url):
            recorder = run_trial(Settings(**values), headless=headless)
        data = json.loads((recorder.directory / 'run.json').read_text())
        return recorder, data

    def check_tabs_overlap_and_first_requests(self, headless: bool) -> None:
        """
        Checks later tabs open during a slow first load, first requests, referrers, and review order.
        Called by: test_tabs_overlap_and_first_requests(), test_visible_tabs_overlap_and_first_requests()
        """
        recorder, data = self.run_case(mode='slow', headless=headless)
        self.assertEqual(data['stop_reason'], 'workflow_complete', data.get('stop'))
        opens = [event for event in recorder.events if event['kind'] == 'item_open']
        ready = [event for event in recorder.events if event['kind'] == 'item_ready']
        views = [event for event in recorder.events if event['kind'] == 'view_start']
        first_ready = next(event for event in ready if event['attempt_id'] == 'item-1')
        self.assertLess(opens[2]['elapsed'], first_ready['elapsed'])
        self.assertGreater(views[0]['elapsed'], opens[-1]['elapsed'])
        self.assertEqual([event['attempt_id'] for event in views], ['item-1', 'item-2', 'item-3'])
        self.assertEqual(len(data['tabs']), 4)
        self.assertEqual(data['analysis']['totals']['completed_views'], 3)
        documents = [
            event for event in recorder.events if event['kind'] == 'request' and event['resource_type'] == 'document'
        ]
        self.assertEqual(len(documents), 4)
        self.assertEqual(documents[0]['elapsed'], 0)
        self.assertTrue(all(event['tab_id'] for event in documents))
        self.assertTrue(all('/studio/collections/' in event['referer'] for event in documents[1:]))
        self.assertCountEqual(
            [event['url'] for event in documents],
            [self.site.origin + hit['path'] for hit in self.site.hits if hit['path'].startswith('/studio/')],
        )
        self.assertTrue(all(event['focused'] for event in recorder.events if event['kind'] == 'overview_focus'))
        self.assertFalse(any(event['kind'] == 'request_evidence_missing' for event in recorder.events))
        self.assertTrue(all('page=2' not in hit['path'] for hit in self.site.hits))
        self.assertEqual(
            len({event['request_id'] for event in recorder.events if event['kind'] == 'request'}),
            data['analysis']['totals']['all_bdr_requests'],
        )

    def test_tabs_overlap_and_first_requests(self) -> None:
        """
        Checks overlapping loads and complete item-request recording in a hidden browser.
        """
        self.check_tabs_overlap_and_first_requests(headless=True)

    def test_visible_tabs_overlap_and_first_requests(self) -> None:
        """
        Checks overlapping loads and complete item-request recording in a visible browser.
        """
        self.check_tabs_overlap_and_first_requests(headless=False)

    def test_visible_tabs_scroll_in_order(self) -> None:
        """
        Checks a visible browser switches to and scrolls every selected item in order.
        """
        recorder, data = self.run_case(mode='long', headless=False, max_items=2, view_seconds=1.2)
        self.assertEqual(data['stop_reason'], 'workflow_complete', data.get('stop'))
        switches = [event for event in recorder.events if event['kind'] == 'tab_switch']
        self.assertEqual([event['attempt_id'] for event in switches], ['item-1', 'item-2'])
        scrolls = [event for event in recorder.events if event['kind'] == 'scroll' and event['purpose'] == 'item_viewing']
        self.assertEqual([event['tab_id'] for event in scrolls], [event['tab_id'] for event in switches])
        self.assertTrue(all(event['after']['y'] > event['before']['y'] for event in scrolls))
        self.assertEqual(data['analysis']['totals']['completed_views'], 2)
        observed = [
            event for event in recorder.events if event['kind'] == 'page_observed' and '/studio/item/' in event['url']
        ]
        self.assertTrue(any(event['heading_count'] == 2 for event in observed))

    def test_new_tab_click_restores_thumbnail_target(self) -> None:
        """
        Checks actual thumbnail clicks preserve their URLs and restore absent or existing targets.
        """
        settings = Settings(
            output_dir=self.temp.name, bdr_hosts='127.0.0.1', collection_id='bdr:nz9qn2kb', workflow='tabs', max_items=2
        )
        recorder = Recorder(settings)
        self.addCleanup(recorder.stream.close)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                observer = Observer(context, settings, recorder)
                page = context.new_page()
                page.goto(self.site.origin + '/studio/collections/bdr:nz9qn2kb/?page=1&per_page=50')
                link = page.locator('.item-thumbnail a').first
                original_href = link.get_attribute('href')
                assert original_href is not None
                for original_target in (None, '_self'):
                    with self.subTest(original_target=original_target):
                        if original_target is not None:
                            link.evaluate('(element, target) => element.setAttribute("target", target)', original_target)
                        with context.expect_page(timeout=3000) as opened:
                            click_link(observer, link, new_tab=True)
                        self.assertEqual(opened.value.url, self.site.origin + original_href)
                        self.assertEqual(link.get_attribute('target'), original_target)
                        self.assertEqual(link.get_attribute('href'), original_href)
            finally:
                browser.close()

    def test_missing_requests_allow_viewing_with_explicit_evidence_limit(self) -> None:
        """
        Checks ready tabs can be viewed beyond the opening deadline when their request events are missing.
        """
        with patch('lib.browser_flow.Observer', MissingItemDocuments):
            recorder, data = self.run_case(max_items=2, view_seconds=1.0, navigation_timeout_seconds=0.8)
        self.assertEqual(data['stop_reason'], 'workflow_complete', data.get('stop'))
        self.assertEqual(data['analysis']['totals']['completed_views'], 2)
        bindings = [event for event in recorder.events if event['kind'] == 'attempt_tab']
        self.assertEqual([event['source'] for event in bindings], ['page_url', 'page_url'])
        self.assertEqual(sum(event['kind'] == 'request_evidence_missing' for event in recorder.events), 2)
        self.assertFalse(any(event['kind'] == 'request' and '/studio/item/' in event['url'] for event in recorder.events))
        report = (recorder.directory / 'summary.md').read_text()
        self.assertIn('request counts are lower bounds', report)
        rebuilt = rebuild_report(recorder.directory)
        self.assertEqual(rebuilt['analysis'], data['analysis'])
        self.assertIn('request counts are lower bounds', (recorder.directory / 'summary.md').read_text())

    def test_unmatched_tab_timeout_identifies_item(self) -> None:
        """
        Checks a click that opens no tab reports the item and the tab-assignment wait.
        """
        with patch('lib.browser_flow.click_link'):
            recorder, data = self.run_case(max_items=1, navigation_timeout_seconds=0.3)
        self.assertEqual(data['stop_reason'], 'page_opening_timeout')
        self.assertEqual(data['stop']['stage'], 'wait_for_item_tab')
        self.assertEqual(data['stop']['attempt_id'], 'item-1')
        self.assertFalse(data['stop']['page_assigned'])
        self.assertEqual(data['stop']['url'], self.site.origin + '/studio/item/bdr:1/')
        self.assertIn('No tab was matched', (recorder.directory / 'summary.md').read_text())

    def test_open_tab_without_content_reports_content_timeout(self) -> None:
        """
        Checks a loaded document without item content reports its known tab and URL.
        """
        _, data = self.run_case(mode='no_content', headless=False, max_items=1, navigation_timeout_seconds=0.5)
        self.assertEqual(data['stop_reason'], 'content_timeout', data.get('stop'))
        self.assertEqual(data['stop']['tab_id'], 'tab-2')
        self.assertEqual(data['stop']['url'], self.site.origin + '/studio/item/bdr:1/')

    def test_hidden_item_title_does_not_count_as_ready(self) -> None:
        """
        Checks main content and a hidden item title do not satisfy readiness.
        """
        _, data = self.run_case(mode='hidden_title', max_items=1, navigation_timeout_seconds=0.5)
        self.assertEqual(data['stop_reason'], 'content_timeout', data.get('stop'))
        self.assertEqual(data['analysis']['totals']['item_successes'], 0)

    def test_visible_item_response_timeout_records_request(self) -> None:
        """
        Checks a genuinely delayed visible-tab response times out with its recorded request and URL.
        """
        recorder, data = self.run_case(mode='timeout', headless=False, max_items=1, navigation_timeout_seconds=0.5)
        self.assertEqual(data['stop_reason'], 'page_opening_timeout', data.get('stop'))
        self.assertEqual(data['stop']['url'], self.site.origin + '/studio/item/bdr:1/')
        self.assertIsNotNone(data['stop']['request_id'])
        self.assertFalse(data['stop']['document_received'])
        self.assertFalse(any(event['kind'] == 'view_start' for event in recorder.events))

    def test_return_preserves_order_and_one_tab(self) -> None:
        """
        Checks the one-tab open/view/actual-link-return sequence through the final return.
        """
        recorder, data = self.run_case('return')
        self.assertEqual(data['stop_reason'], 'workflow_complete', data.get('stop'))
        self.assertEqual(len(data['tabs']), 1)
        self.assertEqual(self.site.collection_visits, 4)
        actions = [event['kind'] for event in recorder.events if event['kind'] in {'item_open', 'view_start', 'return_open'}]
        self.assertEqual(actions, ['item_open', 'view_start', 'return_open'] * 3)
        self.assertEqual([item['position'] for item in data['selected']], [1, 3, 5])

    def test_return_restores_actual_page_size_control(self) -> None:
        """
        Checks one restoration per return and records the intermediate 20-item listing.
        """
        recorder, data = self.run_case('return', 'reset', max_items=2)
        self.assertEqual(data['stop_reason'], 'workflow_complete', data.get('stop'))
        self.assertEqual(len([event for event in recorder.events if event['kind'] == 'listing_restore']), 2)
        sizes = [event['per_page'] for event in recorder.events if event['kind'] == 'listing']
        self.assertEqual(sizes, [50, 20, 50, 20, 50])
        self.assertEqual(self.site.collection_visits, 5)

    def test_long_page_scrolling_in_both_workflows(self) -> None:
        """
        Checks total viewing time includes pauses and requests triggered by scrolling.
        """
        for workflow in ('tabs', 'return'):
            with self.subTest(workflow=workflow):
                recorder, data = self.run_case(workflow, 'long', max_items=1, view_seconds=1.2)
                self.assertEqual(data['stop_reason'], 'workflow_complete', data.get('stop'))
                end = next(event for event in recorder.events if event['kind'] == 'view_end')
                self.assertTrue(end['completed'])
                self.assertFalse(end['bottom_reached'])
                self.assertGreaterEqual(end['scrolls'], 1)
                self.assertLess(end['actual_seconds'], 1.6)
                self.assertTrue(
                    any(event['kind'] == 'request' and '/scroll-data' in event['url'] for event in recorder.events)
                )

    def test_short_view_has_no_scroll(self) -> None:
        """
        Checks a viewing duration shorter than one pause stays on the initial area.
        """
        recorder, data = self.run_case(max_items=1)
        self.assertEqual(data['stop_reason'], 'workflow_complete')
        end = next(event for event in recorder.events if event['kind'] == 'view_end')
        self.assertEqual(end['scrolls'], 0)
        self.assertTrue(end['bottom_reached'])

    def test_interference_stops_all_further_actions(self) -> None:
        """
        Checks challenges in supporting files, background tabs, scrolling, and final returns.
        """
        for mode in ('supporting_challenge', 'background_challenge', 'scroll_challenge', 'final_return', 'denial_page'):
            with self.subTest(mode=mode):
                workflow = 'return' if mode == 'final_return' else 'tabs'
                recorder, data = self.run_case(
                    workflow, mode, view_seconds=1.3, max_items=1 if mode != 'background_challenge' else 3
                )
                self.assertIn(data['stop_reason'], {'challenge', 'denial_page'}, data.get('stop'))
                stop = next(event for event in recorder.events if event['kind'] == 'stop')
                actions = {'item_open', 'return_open', 'tab_switch', 'scroll_start', 'listing_restore'}
                self.assertFalse(
                    any(event['kind'] in actions and event['event_id'] > stop['event_id'] for event in recorder.events)
                )

    def test_errors_save_incomplete_reports(self) -> None:
        """
        Checks empty/changed listings, missing return links, ignored settings, and timeouts.
        """
        cases = [
            ('empty', 'no_eligible_items'),
            ('ignored', 'unexpected_listing'),
            ('changed', 'listing_order_changed'),
            ('missing_return', 'missing_return_link'),
            ('timeout', 'page_opening_timeout'),
        ]
        for mode, expected in cases:
            with self.subTest(mode=mode):
                _, data = self.run_case(
                    'return' if mode in {'changed', 'missing_return'} else 'tabs',
                    mode,
                    max_items=1,
                    navigation_timeout_seconds=0.3 if mode == 'timeout' else 2,
                )
                self.assertEqual(data['stop_reason'], expected, data.get('stop'))

    def test_verification_page_without_challenge_header(self) -> None:
        """
        Checks a Turnstile gate at HTTP 200 stops before selecting or opening items.
        """
        recorder, data = self.run_case(mode='turnstile')
        self.assertEqual(data['stop_reason'], 'verification_required', data.get('stop'))
        self.assertLess(data['observed_seconds'], 1)
        self.assertFalse(any(event['kind'] == 'item_open' for event in recorder.events))
        self.assertEqual(data['stop']['source'], 'Turnstile verification page; rule unconfirmed')

    def test_widget_on_accessible_content_is_not_a_denial(self) -> None:
        """
        Checks a Turnstile widget beside expected collection content does not establish denial.
        """
        _, data = self.run_case(mode='turnstile_with_content', max_items=1)
        self.assertEqual(data['stop_reason'], 'workflow_complete', data.get('stop'))

    def test_unavailable_restoration_control_stops(self) -> None:
        """
        Checks a return without a usable page-size control saves an incomplete trial.
        """
        _, data = self.run_case('return', 'unsupported_restore', max_items=1)
        self.assertEqual(data['stop_reason'], 'missing_page_size_control', data.get('stop'))

    def test_time_limit_and_rebuild(self) -> None:
        """
        Checks interrupted viewing and identical counts rebuilt from saved events.
        """
        recorder, data = self.run_case(mode='time_limit', max_items=1, view_seconds=5, max_duration_seconds=0.7)
        self.assertEqual(data['stop_reason'], 'time_limit')
        self.assertEqual(data['analysis']['totals']['completed_views'], 0)
        rebuilt = rebuild_report(recorder.directory)
        self.assertEqual(data['analysis'], rebuilt['analysis'])
        self.assertIn('partial observation', (recorder.directory / 'summary.md').read_text())

    def test_user_interrupt_saves_results(self) -> None:
        """
        Checks KeyboardInterrupt preserves evidence and a readable report.
        """
        with patch('lib.browser_flow.view_item', side_effect=KeyboardInterrupt):
            recorder, data = self.run_case(max_items=1)
        self.assertEqual(data['stop_reason'], 'user_interrupted')
        self.assertTrue((recorder.directory / 'summary.md').exists())

    def test_redirect_requests_counted_separately(self) -> None:
        """
        Checks redirects add requests while retaining one item attempt.
        """
        for headless in (True, False):
            with self.subTest(headless=headless):
                recorder, data = self.run_case(mode='redirect', max_items=1, headless=headless)
                self.assertEqual(data['stop_reason'], 'workflow_complete', data.get('stop'))
                self.assertEqual(data['analysis']['totals']['item_attempts'], 1)
                self.assertEqual(data['analysis']['totals']['page_requests'], 3)
                self.assertTrue(any(event['kind'] == 'request' and event['redirected_from'] for event in recorder.events))
                documents = [
                    event for event in recorder.events if event['kind'] == 'request' and event['resource_type'] == 'document'
                ]
                self.assertCountEqual(
                    [event['url'] for event in documents],
                    [self.site.origin + hit['path'] for hit in self.site.hits if hit['path'].startswith('/studio/')],
                )
