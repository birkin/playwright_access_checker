"""
Saves local events immediately and builds reports without further requests.
"""

import json
import os
import platform
import subprocess
import time
import uuid
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

from lib.config import EASTERN, ROOT, Settings, public_settings
from lib.measurement import analyze


def timestamp() -> str:
    """
    Records Eastern dates with the numeric offset and EST or EDT label.
    Called by: Recorder
    """
    return datetime.now(EASTERN).strftime('%Y-%m-%dT%H:%M:%S.%f%z %Z')


def code_version() -> dict:
    """
    Records the checkout revision and whether tracked or new files differ.
    Called by: Recorder.__init__()
    """
    result = {'commit': 'unavailable', 'uncommitted_changes': None}
    try:
        commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True, check=True)
        status = subprocess.run(['git', 'status', '--porcelain'], cwd=ROOT, capture_output=True, text=True, check=True)
        result = {'commit': commit.stdout.strip(), 'uncommitted_changes': bool(status.stdout.strip())}
    except (OSError, subprocess.CalledProcessError):
        pass
    return result


class Recorder:
    def __init__(self, settings: Settings) -> None:
        """
        Creates a unique private results directory before launching the browser.
        Called by: browser_flow.run_trial()
        """
        self.trial_id = datetime.now(EASTERN).strftime('%Y%m%dT%H%M%S%z') + '-' + uuid.uuid4().hex[:8]
        self.directory = (ROOT / settings.output_dir).resolve() / self.trial_id
        self.directory.mkdir(parents=True, mode=0o700)
        self.stream = (self.directory / 'events.jsonl').open('x', encoding='utf-8', buffering=1)
        os.chmod(self.directory / 'events.jsonl', 0o600)
        self.events: list[dict] = []
        self.started: float | None = None
        self.stopped: float | None = None
        self.stage = 'setup'
        self.action_id: str | None = None
        self.metadata = {
            'schema_version': 1,
            'trial_id': self.trial_id,
            'created_at': timestamp(),
            'settings': public_settings(settings),
            'code': code_version(),
            'python': platform.python_version(),
            'playwright': version('playwright'),
            'selected': [],
            'tabs': [],
            'stop_reason': None,
            'known_unknowns': [
                'Cloudflare settings and connection details are user supplied; rule behavior and IP history are unverified.',
                'Browser request counts may differ from Cloudflare counters, caches, service workers, or shared-IP traffic.',
                'Playwright tabs may behave differently from manually selected browser tabs.',
                'Readiness means the Studio title and main content are present; it does not prove every viewer file is loaded.',
                'Only configured BDR_HOSTS determine stopping on supporting requests; review other-host activity separately.',
            ],
        }
        self.save()

    def emit(self, kind: str, **details: object) -> dict:
        """
        Appends and flushes an event before the next browser action.
        Called by: browser_flow, Recorder.stop()
        """
        now = time.monotonic()
        event = {
            'event_id': len(self.events) + 1,
            'kind': kind,
            'elapsed': None if self.started is None else now - self.started,
            'local_time': timestamp(),
            'timezone': 'America/New_York',
            'stage': self.stage,
            'action_id': self.action_id,
            'after_stop': self.stopped is not None,
            **details,
        }
        self.events.append(event)
        self.stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n')
        self.stream.flush()
        return event

    def stop(self, reason: str, **details: object) -> None:
        """
        Freezes the first stopping time and its evidence.
        Called by: browser_flow.Observer, browser_flow.run_trial()
        """
        if self.stopped is None:
            event = self.emit('stop', reason=reason, **details)
            self.stopped = time.monotonic()
            self.metadata.update(
                stop_reason=reason, stop=event, observed_seconds=event['elapsed'], stopped_at=event['local_time']
            )

    def save(self) -> None:
        """
        Writes an atomic metadata checkpoint and its readable summary.
        Called by: __init__(), browser_flow.run_trial(), browser_flow.run_workflow()
        """
        data = dict(self.metadata)
        data['analysis'] = analyze(self.events, data.get('observed_seconds'))
        write_json(self.directory / 'run.json', data)
        write_summary(self.directory, data, self.events)


def write_json(path: Path, data: dict) -> None:
    """
    Replaces a JSON checkpoint only after a complete file is saved.
    Called by: Recorder.save(), rebuild_report()
    """
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8')
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def cell(value: object) -> str:
    """
    Escapes report table values, including user notes and page titles.
    Called by: write_summary(), timing_text()
    """
    displayed = f'{value:.3f}' if isinstance(value, float) else str(value if value is not None else 'unavailable')
    return (
        displayed.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('|', '&#124;').replace('\n', ' ')
    )


def timing_text(values: dict | None) -> str:
    """
    Describes average and range or explicitly marks unavailable values.
    Called by: write_summary()
    """
    result = 'unavailable'
    if values:
        result = f'{values["average"]:.3f}s ({values["minimum"]:.3f}–{values["maximum"]:.3f}s)'
    return result


def write_summary(directory: Path, data: dict, events: list[dict]) -> None:
    """
    Writes checked URLs, views, counts, conditions, and a comparison row.
    Called by: Recorder.save(), rebuild_report()
    """
    settings = data['settings']
    analysis = data['analysis']
    stop = data.get('stop', {})
    totals = analysis['totals'] or {}
    reason = data.get('stop_reason') or 'incomplete'
    outcome = (
        'No interference observed within these limits.'
        if reason == 'workflow_complete'
        else f'Trial stopped: {reason.replace("_", " ")}.'
    )
    if reason not in {
        'workflow_complete',
        'challenge',
        'denied',
        'too_many_requests',
        'denial_page',
        'verification_required',
    }:
        outcome += ' Inconclusive; this does not establish a browsing-rate threshold.'
    missing_evidence = [event for event in events if event['kind'] == 'request_evidence_missing']
    if missing_evidence:
        outcome += (
            f' Item-document response evidence is missing for {len(missing_evidence)} item(s);'
            ' request counts are lower bounds and cannot establish a browsing-rate threshold.'
        )
    if any(event['kind'] == 'collection_open' for event in events) and not any(
        event['kind'] == 'visit_ready' and event['visit_id'] == 'collection-1' for event in events
    ):
        outcome += ' Initial collection access did not complete; this run does not show that its browsing caused a block.'
    headings = [
        'Conditions and outcome',
        'Request counts',
        'Checked URLs',
        'Viewing and tab switches',
        'Listing observations',
        'Comparison row',
        'Known unknowns and evidence limits',
        'Central IT findings',
    ]
    lines = [
        f'# Trial {data["trial_id"]}',
        '',
        *[f'- [{heading}](#{heading.lower().replace(" ", "-")})' for heading in headings],
        '',
        '## Conditions and outcome',
        '',
        outcome,
        '',
        f'- Started: {cell(data.get("measurement_started_at", data["created_at"]))}.',
        f'- Observed duration: {cell(data.get("observed_seconds"))} seconds. Workflow: {settings["workflow"]}.',
        f'- Cloudflare settings (user supplied): {cell(settings["cf_settings_label"])}. {cell(settings["cf_settings_notes"])}',
        f'- Settings took effect: {cell(settings["cf_settings_since"])}.',
        f'- Connection: {cell(settings["network_label"])}; public IP: {cell(settings["public_ip"])}. {cell(settings["ip_notes"])}',
        f'- Included BDR hosts: {cell(settings["bdr_hosts"])}.',
        f'- Item-tab opening method: {cell(data.get("browser", {}).get("tab_opening_method"))}.',
        f'- Stopped during {cell(stop.get("stage"))}, tab {cell(stop.get("tab_id"))}, request {cell(stop.get("request_id"))}.',
        f'- Affected item attempt: {cell(stop.get("attempt_id"))}.',
        f'- Evidence: HTTP {cell(stop.get("status"))}; source: {cell(stop.get("source"))}; Ray ID: {cell(stop.get("headers", {}).get("cf-ray"))}.',
        f'- Affected URL: {cell(stop.get("url"))}. Error: {cell(stop.get("error", stop.get("failure")))}.',
        f'- Selected links: {len(data["selected"])}; item attempts: {totals.get("item_attempts", 0)}; ready: {totals.get("item_successes", 0)}; completed views: {totals.get("completed_views", 0)}.',
        f'- Total BDR page requests: {totals.get("page_requests", 0)}; all BDR HTTP requests: {totals.get("all_bdr_requests", 0)}; other HTTP requests: {totals.get("other_requests", 0)}.',
        f'- Actual opening interval average (range): {timing_text(analysis["opening_statistics"])}.',
        f'- Completed viewing duration average (range): {timing_text(analysis["viewing_statistics"])}.',
        '',
        '## Request counts',
        '',
        'Each row counts starts across all tabs. Successes were observed ready by the row’s end.',
        '',
        '| Preceding seconds | Observed seconds | Coverage | Attempts | Ready | Completed views | Page requests | All BDR | Other hosts |',
        '| --- | --- | --- | --- | --- | --- | --- | --- | --- |',
    ]
    for row in analysis['preceding_periods']:
        values = [
            row['seconds'],
            f'{row["observed_seconds"]:.3f}',
            'partial observation' if row['partial'] else 'full',
            row['item_attempts'],
            row['item_successes'],
            row['completed_views'],
            row['page_requests'],
            row['all_bdr_requests'],
            row['other_requests'],
        ]
        lines.append('| ' + ' | '.join(cell(value) for value in values) + ' |')
    lines.extend(
        [
            '',
            '| Seconds from start | Attempts | Ready | Completed views | Page requests | All BDR | Other hosts |',
            '| --- | --- | --- | --- | --- | --- | --- |',
        ]
    )
    for mark, totals in analysis['totals_at_marks'].items():
        keys = ('item_attempts', 'item_successes', 'completed_views', 'page_requests', 'all_bdr_requests', 'other_requests')
        lines.append(
            '| ' + mark + ' | ' + ' | '.join(cell(totals[key] if totals is not None else None) for key in keys) + ' |'
        )
    lines.extend(
        [
            '',
            'Request breakdowns by hostname, content type, tab, page role, and stage are in `run.json`.',
            '',
            '## Checked URLs',
            '',
            'Only attempted visits appear here. Selected but unopened items remain in `run.json`.',
            '',
            '| Local Eastern start | Step | Tab | Requested URL | Final URL | Result |',
            '| --- | --- | --- | --- | --- | --- |',
        ]
    )
    bindings = {event['attempt_id']: event['tab_id'] for event in events if event['kind'] == 'attempt_tab'}
    outcomes = {event['visit_id']: event for event in events if event['kind'] == 'visit_ready'}
    for event in events:
        if event['kind'] in {'collection_open', 'item_open', 'return_open', 'listing_restore'}:
            final = outcomes.get(event['visit_id'], {})
            tab_id = event.get('tab_id') or bindings.get(event.get('attempt_id'))
            following = [
                candidate
                for candidate in events
                if candidate['kind'] in {'item_open', 'return_open', 'listing_restore'}
                and candidate.get('tab_id') == tab_id
                and candidate['event_id'] > event['event_id']
            ]
            end_id = following[0]['event_id'] if following else float('inf')
            changes = [
                change
                for change in events
                if change['kind'] == 'page_change'
                and change['tab_id'] == tab_id
                and event['event_id'] < change['event_id'] < end_id
                and not change['after_stop']
            ]
            final_url = final.get('url') or (changes[-1]['url'] if changes else None)
            lines.append(
                '| '
                + ' | '.join(
                    cell(value)
                    for value in [
                        event['local_time'],
                        event['kind'].replace('_', ' '),
                        tab_id,
                        event['url'],
                        final_url,
                        'ready' if final else 'unfinished at stop',
                    ]
                )
                + ' |'
            )
    lines.extend(
        [
            '',
            '## Viewing and tab switches',
            '',
            'A completed viewing duration does not mean the entire page was seen.',
            '',
            '| Attempt | Planned seconds | Actual seconds | Scrolls | Bottom reached | Completed |',
            '| --- | --- | --- | --- | --- | --- |',
        ]
    )
    for event in events:
        if event['kind'] == 'view_end':
            lines.append(
                '| '
                + ' | '.join(
                    cell(event.get(key))
                    for key in ('attempt_id', 'planned_seconds', 'actual_seconds', 'scrolls', 'bottom_reached', 'completed')
                )
                + ' |'
            )
    lines.extend(
        [
            '',
            '| Switch time | Attempt | Tab |',
            '| --- | --- | --- |',
            *[
                '| ' + ' | '.join(cell(event.get(key)) for key in ('local_time', 'attempt_id', 'tab_id')) + ' |'
                for event in events
                if event['kind'] == 'tab_switch'
            ],
            '',
            '## Listing observations',
            '',
            'Requested: page 1, 50 per page. Original item selection is retained after returns.',
            '',
            '| Time | Step | Page | Per page | Sort | Filters |',
            '| --- | --- | --- | --- | --- | --- |',
        ]
    )
    for event in events:
        if event['kind'] == 'listing':
            lines.append(
                '| '
                + ' | '.join(cell(event.get(key)) for key in ('local_time', 'stage', 'page', 'per_page', 'sort', 'filters'))
                + ' |'
            )
    for event in events:
        if event['kind'] == 'selection':
            lines.extend(
                [
                    '',
                    f'Gathered {event["available_count"]} distinct thumbnails in {event["duration"]:.3f}s with {event["scrolls"]} scrolls; ended because of {event["reason"].replace("_", " ")}.',
                ]
            )
    totals = analysis['totals'] or {}
    opening = (
        'unused'
        if settings['workflow'] == 'return'
        else f'{settings["open_interval_seconds"]} ± {settings["open_jitter_seconds"]}s'
    )
    comparison = [
        data['trial_id'],
        data.get('measurement_started_at', data['created_at']),
        settings['cf_settings_label'],
        settings['workflow'],
        f'{settings["selection_start"]}/{settings["max_items"]}',
        opening,
        f'{settings["view_seconds"]} ± {settings["view_jitter_seconds"]}s',
        timing_text(analysis['opening_statistics']),
        data.get('observed_seconds'),
        f'{totals.get("item_attempts", 0)}/{totals.get("item_successes", 0)}/{totals.get("completed_views", 0)}',
        reason,
    ]
    lines.extend(
        [
            '',
            '## Comparison row',
            '',
            '| Trial | Eastern start | Settings | Workflow | Start/limit | Opening target | Viewing target | Actual opening avg (range) | Duration | Attempts/ready/views | Stop | Report |',
            '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |',
            '| ' + ' | '.join(cell(value) for value in comparison) + f' | [report]({data["trial_id"]}/summary.md) |',
            '',
            'The report link is relative to a comparison table in the parent runs directory.',
            '',
            '## Known unknowns and evidence limits',
            '',
            *['- ' + cell(note) for note in data['known_unknowns']],
            '',
            '## Central IT findings',
            '',
            'Pending review. Record confirmed rule/action, public IP, Ray IDs, and user notes here.',
            '',
            'These files are local diagnostics. Review their URLs, notes, and connection details before copying material for others.',
            '',
        ]
    )
    path = directory / 'summary.md'
    path.write_text('\n'.join(lines), encoding='utf-8')
    os.chmod(path, 0o600)


def rebuild_report(directory: Path) -> dict:
    """
    Rebuilds measurements from flushed events, tolerating one interrupted final line.
    Called by: main.main()
    """
    data = json.loads((directory / 'run.json').read_text(encoding='utf-8'))
    events = []
    lines = (directory / 'events.jsonl').read_text(encoding='utf-8').splitlines()
    for index, line in enumerate(lines):
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            if index != len(lines) - 1:
                raise
    for event in events:
        if event['kind'] == 'selection':
            data['selected'] = event['selected']
    data['tabs'] = [{'tab_id': event['tab_id'], 'url': event['url']} for event in events if event['kind'] == 'tab_created']
    stops = [event for event in events if event['kind'] == 'stop']
    if stops:
        data.update(stop=stops[0], stop_reason=stops[0]['reason'], observed_seconds=stops[0]['elapsed'])
    else:
        data.update(stop_reason='incomplete_saved_events', observed_seconds=events[-1]['elapsed'] if events else None)
        note = 'Recording ended without a stopping event; the last saved event is only a lower bound on observation time.'
        if note not in data['known_unknowns']:
            data['known_unknowns'].append(note)
    data['analysis'] = analyze(events, data['observed_seconds'])
    write_json(directory / 'run.json', data)
    write_summary(directory, data, events)
    return data
