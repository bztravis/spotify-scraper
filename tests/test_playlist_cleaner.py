import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
import playlist_cleaner as app


def song(identifier='a', isrc=None, **extra):
    track = {'id': identifier, 'uri': 'spotify:track:' + identifier,
             'type': 'track', 'name': identifier, 'artists': [{'name': 'Artist'}], **extra}
    if isrc:
        track['external_ids'] = {'isrc': isrc}
    return {'item': track}


class FakeSpotify:
    def __init__(self):
        self.contents = []
        self.playlists = []
        self.creates = 0
        self.batches = []
        self.timeout_after_add = False
        self.timeout_after_create = False

    def request(self, method, path, body=None):
        if method == 'GET' and path == '/me':
            return {'id': 'owner'}
        if method == 'POST' and path == '/me/playlists':
            self.creates += 1
            result = {'id': 'dest', 'description': body['description'], 'owner': {'id': 'owner'}}
            self.playlists.append(result)
            if self.timeout_after_create:
                self.timeout_after_create = False
                raise TimeoutError('response lost')
            return result
        if method == 'POST' and path == '/playlists/dest/items':
            self.batches.append(body['uris'])
            self.contents.extend(body['uris'])
            if self.timeout_after_add:
                self.timeout_after_add = False
                raise TimeoutError('response lost')
            return {}
        raise AssertionError((method, path))

    def pages(self, path):
        return iter(self.playlists)

    def items(self, destination):
        return [{'item': {'uri': uri}} for uri in self.contents]


def run(count=205):
    return {'unique': [{'uri': f'spotify:track:{i}'} for i in range(count)],
            'owner_id': 'owner', 'run_id': 'a' * 16, 'destination_name': 'Clean copy'}


class DedupeTests(unittest.TestCase):
    def test_order_exact_ids_and_legacy_response(self):
        result = app.dedupe([song('b'), song('a'), song('b'), {'track': song('c')['item']}])
        self.assertEqual([s['name'] for s in result['unique']], ['b', 'a', 'c'])
        self.assertEqual(result['duplicates'][0]['position'], 3)

    def test_recording_mode_is_opt_in_and_missing_isrc_stays_distinct(self):
        entries = [song('a', 'XX123'), song('b', 'xx123'), song('c'), song('d')]
        self.assertEqual(len(app.dedupe(entries)['unique']), 4)
        self.assertEqual(len(app.dedupe(entries, 'recording')['unique']), 3)

    def test_skip_untransferable_items(self):
        entries = [{'item': None}, song('a', is_local=True), song('b', type='episode'),
                   song('c', is_playable=False), song('d')]
        result = app.dedupe(entries)
        self.assertEqual(len(result['skipped']), 4)
        self.assertEqual(len(result['unique']), 1)

    def test_relinked_track_is_same_identity(self):
        result = app.dedupe([song('a'), song('b', linked_from={'id': 'a'})])
        self.assertEqual(len(result['unique']), 1)

    def test_nullable_artist_metadata_keeps_tracks_and_dedupes(self):
        for artists in (None, [], [{'name': None}], [{}], [None],
                        [{'name': ''}], [{'name': 123}]):
            with self.subTest(artists=artists):
                entry = song('a', name=None, artists=artists)
                result = app.dedupe([entry, entry])
                self.assertEqual(len(result['unique']), 1)
                self.assertEqual(len(result['duplicates']), 1)
                self.assertEqual(result['skipped'], [])
                self.assertEqual(result['unique'][0]['artists'], 'Unknown artist')
                self.assertEqual(result['unique'][0]['name'], 'Unknown')

    def test_mixed_artist_metadata_preserves_valid_names(self):
        entry = song('a', artists=[{'name': None}, {'name': 'Artist A'},
                                   None, {}, {'name': 'Artist B'}])
        result = app.dedupe([entry])
        self.assertEqual(result['unique'][0]['artists'], 'Artist A, Artist B')

    def test_playlist_input_validation(self):
        identifier = 'a' * 22
        self.assertEqual(app.playlist_id('https://open.spotify.com/playlist/' + identifier + '?si=abc'), identifier)
        self.assertEqual(app.playlist_id('spotify:playlist:' + identifier), identifier)
        for value in ['../../config', 'https://example.com/playlist/' + identifier, 'invalid']:
            with self.assertRaises(app.AppError):
                app.playlist_id(value)


class HistogramTests(unittest.TestCase):
    def entry(self, timestamp, identifier='a'):
        return {**song(identifier), 'added_at': timestamp}

    def test_date_bounds_are_inclusive_and_filter_before_binning(self):
        entries = [self.entry('2026-01-14T23:59:59Z'), self.entry('2026-01-15T00:00:00Z'),
                   self.entry('2026-01-20T23:59:59Z'), self.entry('2026-01-21T00:00:00Z')]
        report = app.addition_histogram(entries, min_date=app.date(2026, 1, 15), max_date=app.date(2026, 1, 20))
        self.assertEqual(report['dated_songs'], 2)
        self.assertEqual(report['outside_range'], 2)
        self.assertEqual(report['bins'], [{'start': '2026-01-01', 'count': 2}])

    def test_single_bound_and_utc_conversion(self):
        entries = [self.entry('2026-01-15T00:30:00+02:00'), self.entry('2026-01-15T23:59:59Z')]
        self.assertEqual(app.addition_histogram(entries, min_date=app.date(2026, 1, 15))['dated_songs'], 1)
        self.assertEqual(app.addition_histogram(entries, max_date=app.date(2026, 1, 14))['dated_songs'], 1)

    def test_explicit_range_includes_empty_edge_bins(self):
        report = app.addition_histogram([self.entry('2026-02-15T00:00:00Z')],
            min_date=app.date(2026, 1, 1), max_date=app.date(2026, 3, 31))
        self.assertEqual([b['count'] for b in report['bins']], [0, 1, 0])
        empty = app.addition_histogram([], 'day', app.date(2026, 1, 1), app.date(2026, 1, 2))
        self.assertEqual([b['count'] for b in empty['bins']], [0, 0])
        ET.fromstring(app.histogram_svg('Empty', empty))

    def test_invalid_bounds_fail_before_any_api_call(self):
        for flags in (['--min-date', '2026-02-30'], ['--max-date', '20260101'],
                      ['--min-date', '2026-02-01', '--max-date', '2026-01-01']):
            with self.subTest(flags=flags), patch('sys.stderr'), patch.object(app, 'Auth') as auth:
                with self.assertRaises(SystemExit) as error:
                    app.main(['histogram', 'liked', *flags])
                self.assertEqual(error.exception.code, 2)
                auth.assert_not_called()

    def test_months_fill_gaps_and_count_duplicate_entries(self):
        report = app.addition_histogram([self.entry('2025-12-31T23:00:00Z'),
                                        self.entry('2026-02-02T10:00:00Z'),
                                        self.entry('2026-02-02T10:00:00Z')])
        self.assertEqual(report['bins'], [
            {'start': '2025-12-01', 'count': 1},
            {'start': '2026-01-01', 'count': 0},
            {'start': '2026-02-01', 'count': 2}])

    def test_day_uses_utc_and_week_starts_monday(self):
        entry = self.entry('2026-01-05T00:30:00+02:00')
        self.assertEqual(app.addition_histogram([entry], 'day')['bins'][0]['start'], '2026-01-04')
        self.assertEqual(app.addition_histogram([entry], 'week')['bins'][0]['start'], '2025-12-29')

    def test_year_bins_include_empty_years(self):
        report = app.addition_histogram([self.entry('2024-02-29T00:00:00Z'),
                                        self.entry('2026-01-01T00:00:00Z')], 'year')
        self.assertEqual([b['count'] for b in report['bins']], [1, 0, 1])

    def test_missing_dates_and_non_songs_are_reported(self):
        report = app.addition_histogram([self.entry(None), self.entry('bad'),
            self.entry('2026-01-01'), {'item': None}, song(type='episode')])
        self.assertEqual(report['unknown_dates'], 3)
        self.assertEqual(report['excluded_items'], 2)
        self.assertEqual(report['bins'], [])
        ET.fromstring(app.histogram_svg('Empty <playlist>', report))

    def test_chart_and_json_outputs_without_playlist_writes(self):
        api = unittest.mock.Mock()
        api.request.return_value = {'id': 'owner', 'name': 'Rock & <Roll>', 'snapshot_id': 'same'}
        api.items.return_value = [self.entry('2026-01-01T00:00:00Z')]
        with tempfile.TemporaryDirectory() as temp, patch('builtins.print'), patch.object(app, 'DATA', Path(temp)):
            output = Path(temp) / 'chart.svg'
            app.plot_histogram(api, 'source', 'month', output)
            root = ET.fromstring(output.read_text())
            self.assertIn('Rock & <Roll>', ''.join(root.itertext()))
            self.assertEqual(app.read(output.with_suffix('.json'))['dated_songs'], 1)
            self.assertTrue(all(call.args[0] == 'GET' for call in api.request.call_args_list))


class LikedSongsTests(unittest.TestCase):
    def test_liked_source_alias_and_permission(self):
        self.assertEqual(app.playlist_id('liked'), 'liked')
        self.assertIn('user-library-read', app.SCOPES.split())

    def api(self):
        api = app.Spotify(None)
        first = {'track': song('a')['item'], 'added_at': '2024-01-01T12:00:00Z'}
        second = {'track': song('b')['item'], 'added_at': '2024-03-01T12:00:00Z'}
        api.request = unittest.mock.Mock(side_effect=[
            {'items': [first], 'next': app.API + '/me/tracks?offset=1&limit=50'},
            {'items': [second], 'next': None}])
        return api

    def test_liked_histogram_paginates_saved_dates(self):
        api = self.api()
        api.request.side_effect = [{'id': 'owner'}, *api.request.side_effect]
        with tempfile.TemporaryDirectory() as temp, patch('builtins.print'), patch.object(app, 'DATA', Path(temp)):
            output = Path(temp) / 'liked.svg'
            app.plot_histogram(api, 'liked', 'month', output)
            report = app.read(output.with_suffix('.json'))
            self.assertEqual(report['playlist_name'], 'Liked Songs')
            self.assertEqual([b['count'] for b in report['bins']], [1, 0, 1])
            self.assertIn('Liked Songs', output.read_text())
        api.request.assert_any_call('GET', '/me/tracks?limit=50')
        self.assertFalse(any('/playlists/' in str(c) for c in api.request.call_args_list))

    def test_liked_deduplication_report_supports_creation(self):
        api = self.api()
        saved_pages = api.request.side_effect
        api.request.side_effect = [{'id': 'owner'}, *saved_pages]
        report = app.snapshot(api, 'liked', 'exact')
        self.assertEqual(report['source_name'], 'Liked Songs')
        self.assertEqual(report['owner_id'], 'owner')
        self.assertEqual([t['uri'] for t in report['unique']], ['spotify:track:a', 'spotify:track:b'])
        self.assertIsNone(report['source_snapshot'])


class HistogramCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(app, 'DATA', Path(self.temp.name)).start()
        patch('builtins.print').start()
        self.clock = patch.object(app.time, 'time', return_value=1000).start()
        self.api = unittest.mock.Mock()
        self.api.request.return_value = {'id': 'owner'}
        self.source = ({'name': 'Liked Songs'}, [{'track': song()['item'], 'added_at': '2024-01-01T00:00:00Z'}])
        self.fetch = patch.object(app, 'fetch_source', return_value=self.source).start()

    def test_cache_survives_until_exact_five_minute_boundary(self):
        app.histogram_source(self.api, 'liked')
        self.clock.return_value = 1299
        self.assertEqual(app.histogram_source(self.api, 'liked'), self.source)
        self.assertEqual(self.fetch.call_count, 1)
        self.clock.return_value = 1300
        app.histogram_source(self.api, 'liked')
        self.assertEqual(self.fetch.call_count, 2)

    def test_ignore_cache_fetches_and_replaces_cached_data(self):
        app.histogram_source(self.api, 'liked')
        updated = ({'name': 'Liked Songs'}, [])
        self.fetch.return_value = updated
        self.assertEqual(app.histogram_source(self.api, 'liked', ignore_cache=True), updated)
        self.assertEqual(app.histogram_source(self.api, 'liked'), updated)
        self.assertEqual(self.fetch.call_count, 2)

    def test_cache_is_isolated_by_account_and_source(self):
        app.histogram_source(self.api, 'liked')
        app.histogram_source(self.api, 'playlist-a')
        self.api.request.return_value = {'id': 'different-owner'}
        app.histogram_source(self.api, 'liked')
        self.assertEqual(self.fetch.call_count, 3)

    def test_corrupt_cache_is_refetched(self):
        app.histogram_source(self.api, 'liked')
        path = next((app.DATA / 'histogram-cache').glob('*.json'))
        path.write_text('broken JSON')
        app.histogram_source(self.api, 'liked')
        self.assertEqual(self.fetch.call_count, 2)

    def test_chart_options_reuse_source_cache(self):
        output = Path(self.temp.name) / 'chart.svg'
        app.plot_histogram(self.api, 'liked', 'month', output)
        app.plot_histogram(self.api, 'liked', 'year', output, label_every=1)
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(app.read(output.with_suffix('.json'))['interval'], 'year')
        app.plot_histogram(self.api, 'liked', 'month', output, min_date=app.date(2025, 1, 1))
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(app.read(output.with_suffix('.json'))['dated_songs'], 0)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'run.json'
        self.api = FakeSpotify()
        self.run = run()
        self.output = patch('builtins.print').start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.temp.cleanup)

    def test_creation_batches_and_verification(self):
        app.populate(self.api, self.run, self.path)
        self.assertEqual([len(b) for b in self.api.batches], [100, 100, 5])
        self.assertEqual(app.read(self.path)['status'], 'complete')
        app.populate(self.api, app.read(self.path), self.path)
        self.assertEqual(self.api.creates, 1)
        self.assertEqual(len(self.api.batches), 3)

    def test_timeout_after_accepted_add_does_not_duplicate(self):
        self.api.timeout_after_add = True
        with self.assertRaises(TimeoutError):
            app.populate(self.api, self.run, self.path)
        self.assertEqual(len(self.api.contents), 100)
        app.populate(self.api, app.read(self.path), self.path)
        self.assertEqual(len(self.api.contents), 205)
        self.assertEqual(len(set(self.api.contents)), 205)
        self.assertEqual(self.api.creates, 1)

    def test_timeout_after_creation_finds_existing_playlist(self):
        self.api.timeout_after_create = True
        with self.assertRaises(TimeoutError):
            app.populate(self.api, self.run, self.path)
        app.populate(self.api, app.read(self.path), self.path)
        self.assertEqual(self.api.creates, 1)
        self.assertEqual(len(self.api.contents), 205)

    def test_uncertain_creation_never_blindly_retries(self):
        self.run['creation_attempted'] = True
        with self.assertRaises(app.AppError):
            app.populate(self.api, self.run, self.path)
        self.assertEqual(self.api.creates, 0)

    def test_manually_modified_destination_is_rejected(self):
        self.run['destination_id'] = 'dest'
        self.api.contents = ['spotify:track:unexpected']
        with self.assertRaises(app.AppError):
            app.populate(self.api, self.run, self.path)
        self.assertEqual(self.api.batches, [])

    def test_source_change_during_pagination_is_rejected(self):
        api = FakeSpotify()
        api.request = unittest.mock.Mock(side_effect=[{'id': 'owner'},
            {'name': 'Source', 'owner': {'id': 'owner'}, 'snapshot_id': 'old'},
            {'snapshot_id': 'new'}])
        with self.assertRaises(app.AppError):
            app.snapshot(api, 'source', 'exact')


class ClientTests(unittest.TestCase):
    def test_default_workers_match_remaining_page_count(self):
        api = app.Spotify(None)
        def response(method, path):
            query = dict(app.urllib.parse.parse_qsl(app.urllib.parse.urlsplit(path).query))
            offset = int(query.get('offset', 0))
            return {'items': list(range(offset, min(offset + 50, 175))), 'total': 175,
                    'offset': offset, 'next': app.API + '/me/tracks?limit=50&offset=50' if offset == 0 else None}
        api.request = response
        with patch('builtins.print'), patch.object(app, 'ThreadPoolExecutor', wraps=app.ThreadPoolExecutor) as executor:
            self.assertEqual(list(api.pages('/me/tracks?limit=50', parallel=True)), list(range(175)))
        executor.assert_called_once_with(max_workers=3)

    def test_excess_workers_are_limited_to_remaining_pages(self):
        api = app.Spotify(None, workers=1000000)
        offsets = []
        def response(method, path):
            query = dict(app.urllib.parse.parse_qsl(app.urllib.parse.urlsplit(path).query))
            offset = int(query.get('offset', 0))
            offsets.append(offset)
            return {'items': list(range(offset, min(offset + 50, 125))), 'total': 125,
                    'offset': offset, 'next': app.API + '/me/tracks?limit=50&offset=50' if offset == 0 else None}
        api.request = response
        with patch('builtins.print'), patch.object(app, 'ThreadPoolExecutor', wraps=app.ThreadPoolExecutor) as executor:
            self.assertEqual(list(api.pages('/me/tracks?limit=50', parallel=True)), list(range(125)))
            executor.assert_called_once_with(max_workers=2)
        self.assertEqual(sorted(offsets), [0, 50, 100])

    def test_single_page_needs_no_workers(self):
        api = app.Spotify(None, workers=1000000)
        api.request = unittest.mock.Mock(return_value={'items': list(range(30)), 'total': 30, 'next': None})
        with patch('builtins.print'), patch.object(app, 'ThreadPoolExecutor') as executor:
            self.assertEqual(list(api.pages('/me/tracks?limit=50', parallel=True)), list(range(30)))
            executor.assert_not_called()
        self.assertEqual(api.request.call_count, 1)

    def test_worker_argument_has_no_upper_limit_but_must_be_positive(self):
        self.assertEqual(app.positive_int('1000000'), 1000000)
        for value in ('0', '-1', 'abc', '1.5'):
            with self.subTest(value=value), self.assertRaises(app.argparse.ArgumentTypeError):
                app.positive_int(value)

    def test_parallel_pages_are_concurrent_but_return_original_order(self):
        api = app.Spotify(None, workers=2)
        rendezvous = threading.Barrier(2)
        guard = threading.Lock()
        active = 0
        peak = 0

        def response(method, path):
            nonlocal active, peak
            query = dict(app.urllib.parse.parse_qsl(app.urllib.parse.urlsplit(path).query))
            offset = int(query.get('offset', 0))
            if offset in (50, 100):
                with guard:
                    active += 1
                    peak = max(peak, active)
                rendezvous.wait(timeout=3)
                with guard:
                    active -= 1
            return {'items': list(range(offset, min(offset + 50, 160))), 'total': 160,
                    'offset': offset, 'next': app.API + '/me/tracks?limit=50&offset=50' if offset == 0 else None}

        api.request = response
        with patch('builtins.print'):
            result = list(api.pages('/me/tracks?limit=50', parallel=True))
        self.assertEqual(result, list(range(160)))
        self.assertEqual(peak, 2)

    def test_incomplete_parallel_page_fails(self):
        api = app.Spotify(None, workers=2)
        api.request = unittest.mock.Mock(side_effect=[
            {'items': list(range(50)), 'total': 60, 'next': app.API + '/me/tracks?limit=50&offset=50'},
            {'items': [], 'total': 60, 'offset': 50}])
        with patch('builtins.print'), self.assertRaisesRegex(app.AppError, 'incomplete'):
            list(api.pages('/me/tracks?limit=50', parallel=True))

    def test_single_worker_follows_next_links_sequentially(self):
        api = app.Spotify(None, workers=1)
        api.request = unittest.mock.Mock(side_effect=[
            {'items': [1], 'total': 2, 'next': app.API + '/next'},
            {'items': [2], 'total': 2, 'next': None}])
        with patch('builtins.print'), patch.object(api, 'remaining_pages') as parallel:
            self.assertEqual(list(api.pages('/first', parallel=True)), [1, 2])
        parallel.assert_not_called()

    def test_parallel_token_requests_refresh_once(self):
        auth = app.Auth('client')
        auth.store = unittest.mock.Mock()
        auth.store.get.return_value = 'refresh'
        with patch.object(app, 'http', return_value={'access_token': 'access', 'expires_in': 3600}) as http:
            with app.ThreadPoolExecutor(max_workers=4) as executor:
                self.assertEqual(list(executor.map(lambda _: auth.token(), range(8))), ['access'] * 8)
        self.assertEqual(http.call_count, 1)

    def test_shared_rate_cooldown_delays_other_requests(self):
        auth = unittest.mock.Mock()
        auth.token.return_value = 'token'
        api = app.Spotify(auth)
        api._retry_at = 102
        clock = [100.0]
        def advance(seconds):
            clock[0] += seconds
        with patch.object(app, 'http', return_value={}), patch.object(app.time, 'sleep', side_effect=advance) as sleep, patch.object(app.time, 'monotonic', side_effect=lambda: clock[0]):
            api.request('GET', '/me')
        sleep.assert_called_once_with(2)

    def test_keychain_timeout_is_actionable_and_does_not_expose_token(self):
        store = app.TokenStore('client')
        secret = 'private-refresh-token'
        with patch.object(app.subprocess, 'run', side_effect=app.subprocess.TimeoutExpired(
                ['security', '-w', secret], 30)), patch('builtins.print'):
            with self.assertRaises(app.AppError) as caught:
                store.keychain(['add-generic-password', '-w', secret], 'Saving login')
        self.assertIn('Keychain', str(caught.exception))
        self.assertNotIn(secret, str(caught.exception))

    def test_repeated_pagination_stops_instead_of_looping(self):
        api = app.Spotify(None)
        api.request = unittest.mock.Mock(return_value={'items': [1], 'next': app.API + '/first'})
        with patch('builtins.print'), self.assertRaisesRegex(app.AppError, 'repeated pagination'):
            list(api.pages('/first'))
        self.assertEqual(api.request.call_count, 1)

    def test_fetch_progress_includes_total(self):
        api = app.Spotify(None)
        api.request = unittest.mock.Mock(return_value={'items': [1, 2], 'total': 2, 'next': None})
        with patch('builtins.print') as output:
            self.assertEqual(list(api.pages('/first')), [1, 2])
        output.assert_any_call('  Loaded 2 / 2 items', flush=True)

    def test_refresh_token_rotation_and_access_token_reuse(self):
        auth = app.Auth('client')
        auth.store = unittest.mock.Mock()
        auth.store.get.return_value = 'old-refresh'
        with patch.object(app, 'http', return_value={
                'access_token': 'access', 'refresh_token': 'new-refresh', 'expires_in': 3600}) as http:
            self.assertEqual(auth.token(), 'access')
            self.assertEqual(auth.token(), 'access')
            self.assertEqual(http.call_count, 1)
            auth.store.put.assert_called_once_with('new-refresh')
            self.assertIn(b'grant_type=refresh_token', http.call_args.kwargs['data'])

    def test_missing_login_has_actionable_error(self):
        auth = app.Auth('client')
        auth.store = unittest.mock.Mock()
        auth.store.get.return_value = None
        with self.assertRaisesRegex(app.AppError, 'login'):
            auth.token()

    def test_concurrent_commands_are_blocked(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(app, 'DATA', Path(temp)):
            with app.command_lock():
                with self.assertRaisesRegex(app.AppError, 'Another'):
                    with app.command_lock():
                        self.fail('Second command must not acquire lock')
            with app.command_lock():
                pass

    def test_pagination_follows_all_pages(self):
        api = app.Spotify(None)
        api.request = unittest.mock.Mock(side_effect=[{'items': [1, 2], 'next': app.API + '/next'},
                                                      {'items': [3], 'next': None}])
        self.assertEqual(list(api.pages('/first')), [1, 2, 3])

    def test_write_server_failure_is_not_retried(self):
        auth = unittest.mock.Mock()
        auth.token.return_value = 'token'
        error = app.urllib.error.HTTPError(app.API, 503, 'Unavailable', {}, None)
        with patch.object(app, 'http', side_effect=error) as http:
            with self.assertRaises(app.AppError):
                app.Spotify(auth).request('POST', '/me/playlists', {})
            self.assertEqual(http.call_count, 1)

    def test_rate_limit_uses_retry_after(self):
        auth = unittest.mock.Mock()
        auth.token.return_value = 'token'
        error = app.urllib.error.HTTPError(app.API, 429, 'Slow down', {'Retry-After': '2'}, None)
        clock = [100.0]
        def advance(seconds):
            clock[0] += seconds
        with patch.object(app, 'http', side_effect=[error, {}]), patch.object(app.time, 'sleep', side_effect=advance) as sleep, patch.object(app.time, 'monotonic', side_effect=lambda: clock[0]):
            app.Spotify(auth).request('GET', '/me')
            sleep.assert_called_once_with(2)


if __name__ == '__main__':
    unittest.main()
