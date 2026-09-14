"""
Generates timing plans, removes private URL data, and measures saved events.
"""

import random
import re
from collections import Counter
from itertools import pairwise
from typing import TypedDict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

WINDOWS = (30, 60, 120, 300)
PUBLIC_QUERY_KEYS = {'page', 'per_page', 'sort', 'view', 'embed', 'selected_facets'}


class SelectedItem(TypedDict):
    position: int
    url: str


def safe_url(url: str) -> str:
    """
    Removes login details, verification tokens, fragments, and unapproved query values.
    Called by: observation.Observer, browser_flow
    """
    result = '[invalid URL]'
    try:
        parts = urlsplit(url)
        if parts.scheme in {'http', 'https'} and parts.hostname:
            host = parts.hostname
            if ':' in host:
                host = '[' + host + ']'
            if parts.port:
                host += f':{parts.port}'
            query = [(key, value if key in PUBLIC_QUERY_KEYS else '[redacted]') for key, value in parse_qsl(parts.query)]
            path = parts.path
            if '/cdn-cgi/challenge-platform/' in path:
                path = path.split('/cdn-cgi/challenge-platform/')[0] + '/cdn-cgi/challenge-platform/[redacted]'
            elif parts.hostname == 'challenges.cloudflare.com' and path != '/turnstile/v0/api.js':
                path = '/turnstile/[redacted]'
            result = urlunsplit((parts.scheme, host, path, urlencode(query), ''))
        elif url == 'about:blank':
            result = url
    except ValueError:
        pass
    return result


def safe_error(error: Exception) -> str:
    """
    Keeps a useful failure category without storing browser logs or credentials.
    Called by: browser_flow.run_trial()
    """
    match = re.search(r'net::[A-Z0-9_]+', str(error))
    result = match.group() if match else type(error).__name__
    return result


def select_items(urls: list[str], start: int, limit: int) -> list[SelectedItem]:
    """
    Selects every other distinct item while preserving displayed order.
    Called by: browser_flow.gather_items()
    """
    unique = list(dict.fromkeys(urls))
    selected: list[SelectedItem] = [
        SelectedItem(position=index + 1, url=unique[index]) for index in range(start - 1, len(unique), 2)
    ][:limit]
    return selected


def planned_times(seed: int, count: int, opening: float, opening_jitter: float, viewing: float, view_jitter: float) -> dict:
    """
    Generates both sequences before browsing in a fixed, reproducible order.
    Called by: browser_flow.run_workflow()
    """
    generator = random.Random(seed)
    intervals = [generator.uniform(opening - opening_jitter, opening + opening_jitter) for _ in range(max(0, count - 1))]
    views = [generator.uniform(viewing - view_jitter, viewing + view_jitter) for _ in range(count)]
    return {'opening_intervals': intervals, 'viewing_durations': views}


def classify_response(status: int, headers: dict[str, str]) -> dict[str, str] | None:
    """
    Separates observed interference from assumptions about its cause.
    Called by: observation.Observer.on_response()
    """
    result = None
    if headers.get('cf-mitigated', '').lower() == 'challenge':
        result = {'reason': 'challenge', 'source': 'Cloudflare confirmed by cf-mitigated'}
    elif status in {403, 429}:
        result = {'reason': 'denied' if status == 403 else 'too_many_requests', 'source': 'unknown'}
    elif status >= 400:
        result = {'reason': 'http_error', 'source': 'unknown'}
    return result


def statistics(values: list[float]) -> dict | None:
    """
    Describes measured durations without inventing missing intervals.
    Called by: analyze()
    """
    result = None
    if values:
        result = {'average': sum(values) / len(values), 'minimum': min(values), 'maximum': max(values)}
    return result


def counts(events: list[dict], lower: float, end: float, include_lower: bool) -> dict:
    """
    Counts starts once and successes only when observed by the measurement end.
    Called by: analyze()
    """
    included = [
        event
        for event in events
        if event.get('elapsed') is not None
        and not event.get('after_stop')
        and (event['elapsed'] >= lower if include_lower else event['elapsed'] > lower)
        and event['elapsed'] <= end
    ]
    attempts = [event for event in included if event['kind'] == 'item_open']
    ready = {
        event['attempt_id']
        for event in events
        if event['kind'] == 'item_ready'
        and event['elapsed'] is not None
        and event['elapsed'] <= end
        and not event.get('after_stop')
    }
    viewed = {
        event['attempt_id']
        for event in events
        if event['kind'] == 'view_end' and event.get('completed') and event['elapsed'] <= end and not event.get('after_stop')
    }
    requests = [event for event in included if event['kind'] == 'request' and event.get('http', True)]
    bdr = [event for event in requests if event['included_host']]
    result = {
        'item_attempts': len(attempts),
        'item_successes': sum(event['attempt_id'] in ready for event in attempts),
        'completed_views': sum(event['attempt_id'] in viewed for event in attempts),
        'unfinished_attempts': sum(event['attempt_id'] not in ready for event in attempts),
        'page_requests': sum(event['resource_type'] == 'document' for event in bdr),
        'all_bdr_requests': len(bdr),
        'other_requests': len(requests) - len(bdr),
    }
    return result


def analyze(events: list[dict], end: float | None) -> dict:
    """
    Reconstructs all reporting periods and timing measurements from the log.
    Called by: results.Recorder.save(), results.rebuild_report()
    """
    bindings = {event['request_id']: event['tab_id'] for event in events if event['kind'] == 'request_tab'}
    for event in events:
        if event['kind'] == 'request' and event['request_id'] in bindings:
            event['tab_id'] = bindings[event['request_id']]
    result = {'preceding_periods': [], 'totals_at_marks': {}, 'totals': None, 'breakdowns': {}}
    if end is not None:
        result['totals'] = counts(events, 0, end, True)
        for window in WINDOWS:
            result['preceding_periods'].append(
                {
                    'seconds': window,
                    'observed_seconds': min(end, window),
                    'partial': end < window,
                    **counts(events, max(0, end - window), end, end < window),
                }
            )
            result['totals_at_marks'][str(window)] = counts(events, 0, window, True) if end >= window else None
        requests = [
            event
            for event in events
            if event['kind'] == 'request'
            and event['included_host']
            and event['elapsed'] is not None
            and 0 <= event['elapsed'] <= end
            and not event.get('after_stop')
        ]
        for key in ('hostname', 'resource_type', 'tab_id', 'page_role', 'stage'):
            result['breakdowns'][key] = dict(Counter(str(event.get(key, 'unknown')) for event in requests))
    openings = [
        event['elapsed']
        for event in events
        if event['kind'] == 'item_open' and event['elapsed'] is not None and end is not None and event['elapsed'] <= end
    ]
    intervals = [second - first for first, second in pairwise(openings)]
    views = [event['actual_seconds'] for event in events if event['kind'] == 'view_end' and event.get('completed')]
    result.update(
        actual_opening_intervals=intervals, opening_statistics=statistics(intervals), viewing_statistics=statistics(views)
    )
    return result
