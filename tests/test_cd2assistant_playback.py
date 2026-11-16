import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

PATH = Path(__file__).resolve().parents[1] / 'plugins.v3/cd2assistant/playback.py'
spec = importlib.util.spec_from_file_location('playback', PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def task(status='Transfer', path='/test.mkv', size=20):
    return {'status': status, 'destPath': path, 'size': size}


def session(ip='8.8.8.8', paused=False):
    return {'NowPlayingItem': {'MediaType': 'Video'},
            'PlayState': {'IsPaused': paused}, 'RemoteEndPoint': ip}


def service(sessions=None, status=200, kind='emby'):
    response = Mock(status_code=status)
    response.json.return_value = sessions if sessions is not None else []
    return NS(type=kind, instance=NS(get_data=Mock(return_value=response)))


class API:
    def __init__(self):
        self.tasks = {'auto': task(), 'manual': task('Pause', '/manual.mkv')}
        self.actions = []
        self.fail_resume = False

    def list_uploads(self):
        return copy.deepcopy(self.tasks)

    def pause(self, key):
        self.actions.append(('pause', key))
        self.tasks[key]['status'] = 'Pause'

    def resume(self, key):
        if self.fail_resume:
            raise ConnectionError()
        self.actions.append(('resume', key))
        self.tasks[key]['status'] = 'Transfer'


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.log = Mock()
        self.saved = {}
        self.now = 0
        self.api = API()
        self.targets = {'server': ('CD2', self.api)}
        self.guard = m.UploadGuard({}, self.save, self.log, lambda: self.now)

    def save(self, value):
        self.saved = copy.deepcopy(value)

    def tick(self, playing, **kw):
        self.guard.tick(playing, self.targets, delay=45, **kw)

    def test_private_ipv4_ipv6_mapped_and_ports_ignored(self):
        for ip in ['192.168.1.2:1234', '10.0.0.1', '172.16.0.1', '127.0.0.1',
                   '[fd00::1]:123', '::1', 'fe80::1%eth0', '::ffff:192.168.1.2', '100.64.0.1']:
            with self.subTest(ip=ip):
                self.assertIs(m.external_address(ip), False)

    def test_public_and_unknown_ips(self):
        for ip in ['8.8.8.8:123', '[2606:4700:4700::1111]:456', '::ffff:8.8.8.8']:
            self.assertIs(m.external_address(ip), True)
        for ip in ['', None, 'invalid', '[broken']:
            self.assertIsNone(m.external_address(ip))

    def test_private_only_does_not_pause(self):
        result = m.detect_external_playback({'emby': service([session('192.168.1.1')])}, self.log)
        self.assertIs(result, False)
        self.tick(result)
        self.assertFalse(self.api.actions)

    def test_multiuser_any_remote_playback_wins(self):
        result = m.detect_external_playback({'emby': service([session(paused=True), session()]),
                                             'broken': service(status=500)}, self.log)
        self.assertIs(result, True)

    def test_failure_unknown_instead_of_idle(self):
        self.assertIsNone(m.detect_external_playback({'emby': service(status=500)}, self.log))
        self.assertIsNone(m.detect_external_playback({}, self.log))
        self.assertIsNone(m.detect_external_playback({'emby': service([session(None)])}, self.log))

    def test_malformed_response_unknown_and_closed(self):
        s = service({'unexpected': []})
        self.assertIsNone(m.detect_external_playback({'a': s}, self.log))
        s.instance.get_data.return_value.close.assert_called_once()

    def test_jellyfin_camelcase_and_stale(self):
        s = {'nowPlayingItem': {'mediaType': 'Video'}, 'playState': {'isPaused': 'false'},
             'remoteEndPoint': '8.8.8.8'}
        self.assertIs(m.video_session(s, True), True)
        s['lastPlaybackCheckIn'] = '2000-01-01T00:00:00Z'
        self.assertIs(m.video_session(s, True), False)

    def test_paused_video_and_audio_do_not_trigger(self):
        self.assertIs(m.video_session(session(paused=True)), False)
        s = session()
        s['NowPlayingItem']['MediaType'] = 'Audio'
        self.assertIs(m.video_session(s), False)

    def test_plex_player_state_and_local_flag(self):
        player = NS(state='playing', address='8.8.8.8', local=False)
        plex = NS(sessions=lambda: [NS(TAG='Video', players=[player])])
        s = NS(type='plex', instance=NS(get_plex=lambda: plex))
        self.assertIs(m.detect_external_playback({'p': s}, self.log), True)
        player.state = 'paused'
        self.assertIs(m.detect_external_playback({'p': s}, self.log), False)
        player.state, player.local = 'playing', True
        self.assertIs(m.detect_external_playback({'p': s}, self.log), False)

    def test_pause_and_delayed_resume_preserves_manual_pause(self):
        self.tick(True)
        self.assertEqual(self.api.actions, [('pause', 'auto')])
        self.tick(False)
        self.now = 44
        self.tick(False)
        self.assertEqual(len(self.api.actions), 1)
        self.now = 45
        self.tick(False)
        self.assertEqual(self.api.actions[-1], ('resume', 'auto'))
        self.assertEqual(self.api.tasks['manual']['status'], 'Pause')

    def test_new_tasks_paused_on_next_poll_without_duplicate_calls(self):
        self.tick(True)
        self.tick(True)
        self.api.tasks['new'] = task('Inqueue', '/new.mkv')
        self.tick(True)
        self.assertEqual(self.api.actions, [('pause', 'auto'), ('pause', 'new')])

    def test_unknown_resets_recovery_delay(self):
        self.tick(True)
        self.tick(False)
        self.now = 50
        self.tick(None)
        self.tick(False)
        self.assertEqual(len(self.api.actions), 1)
        self.now = 95
        self.tick(False)
        self.assertEqual(len(self.api.actions), 2)

    def test_new_playback_resets_delay(self):
        self.tick(True)
        self.tick(False)
        self.now = 44
        self.tick(True)
        self.now = 45
        self.tick(False)
        self.assertEqual(len(self.api.actions), 1)

    def test_restart_restores_owned_keys(self):
        self.tick(True)
        self.guard = m.UploadGuard(copy.deepcopy(self.saved), self.save, self.log, lambda: self.now)
        self.tick(False)
        self.now = 45
        self.tick(False)
        self.assertEqual(self.api.actions[-1], ('resume', 'auto'))

    def test_failed_resume_keeps_ownership_for_retry(self):
        self.tick(True)
        self.api.fail_resume = True
        self.tick(False, enabled=False)
        self.assertIn('auto', self.saved['server'])
        self.api.fail_resume = False
        self.tick(False, enabled=False)
        self.assertFalse(self.saved)

    def test_pause_response_lost_can_recover(self):
        pause = self.api.pause
        def lost(key):
            pause(key)
            raise TimeoutError()
        self.api.pause = lost
        self.tick(True)
        self.assertIn('auto', self.saved['server'])
        self.tick(False, enabled=False)
        self.assertEqual(self.api.actions[-1], ('resume', 'auto'))

    def test_key_reused_for_other_file_not_resumed(self):
        self.tick(True)
        self.api.tasks['auto'] = task('Pause', '/different.mkv')
        self.tick(False, enabled=False)
        self.assertEqual(len(self.api.actions), 1)

    def test_disabled_feature_releases_its_pauses(self):
        self.tick(True)
        self.tick(None, enabled=False)
        self.assertEqual(self.api.actions[-1], ('resume', 'auto'))

    def test_failed_listing_never_resumes(self):
        self.tick(True)
        self.api.list_uploads = Mock(side_effect=ConnectionError())
        self.tick(False, enabled=False)
        self.assertEqual(len(self.api.actions), 1)
        self.assertTrue(self.saved)

    def test_numeric_status_support(self):
        self.assertEqual(m.upload_status({'statusEnum': 4}), 'Pause')

    def test_rpc_pagination_auth_metadata_and_deadline(self):
        rows = [dict(key=str(i), **task()) for i in range(201)]
        def listing(request, *, metadata, timeout):
            self.assertEqual(metadata, [('authorization', 'test')])
            self.assertEqual(timeout, 15)
            self.assertFalse(request.getAll)
            return NS(totalCount=201, uploadFiles=rows[request.pageNumber*200:(request.pageNumber+1)*200])
        stub = NS(GetUploadFileList=listing, PauseUploadFiles=Mock(), ResumeUploadFiles=Mock())
        client = NS(stub=stub, metadata=[('authorization', 'test')])
        proto = NS(GetUploadFileListRequest=NS, MultpleUploadFileKeyRequest=NS)
        api = m.CloudUploadAPI(client, proto)
        self.assertEqual(len(api.list_uploads()), 201)
        api.pause('0')
        api.resume('0')
        self.assertEqual(stub.PauseUploadFiles.call_args.args[0].keys, ['0'])

    def test_incomplete_pagination_raises(self):
        client = NS(stub=NS(GetUploadFileList=lambda *a, **kw: NS(totalCount=2, uploadFiles=[])), metadata=[])
        with self.assertRaises(ValueError):
            m.CloudUploadAPI(client, NS(GetUploadFileListRequest=NS)).list_uploads()

    def test_expired_token_refreshes_once_and_retries(self):
        class Expired(Exception):
            def code(self):
                return NS(name='UNAUTHENTICATED')
        stub = NS(PauseUploadFiles=Mock(side_effect=[Expired(), None]),
                  GetToken=Mock(return_value=NS(success=True, token='new')))
        client = NS(stub=stub, metadata=[], username='u', password='p')
        api = m.CloudUploadAPI(client, NS(GetTokenRequest=NS, MultpleUploadFileKeyRequest=NS))
        api.pause('key')
        self.assertEqual(stub.PauseUploadFiles.call_count, 2)
        self.assertEqual(client.metadata, [('authorization', 'Bearer new')])

    def test_shutdown_prevents_further_upload_mutations(self):
        stub = NS(PauseUploadFiles=Mock())
        api = m.CloudUploadAPI(NS(stub=stub), NS(MultpleUploadFileKeyRequest=NS), stopping=lambda: True)
        with self.assertRaises(RuntimeError):
            api.pause('key')
        stub.PauseUploadFiles.assert_not_called()


if __name__ == '__main__':
    unittest.main()
