"""Load both plugin builds with MoviePilot host boundary doubles."""
import asyncio
import copy
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch
from enum import Enum
from abc import ABC, abstractmethod

import grpc


class Base(ABC):
    @abstractmethod
    def get_page(self):
        pass

    def __init__(self):
        self.data = {}
        self.config = {}

    def get_data(self, key):
        return copy.deepcopy(self.data.get(key))

    def save_data(self, key, value):
        self.data[key] = copy.deepcopy(value)

    def get_config(self):
        return self.config

    def update_config(self, value):
        self.config = value


def load_plugin(version="v3"):
    modules = {}
    def module(name, **attrs):
        parts = name.split('.')
        for i in range(1, len(parts)+1):
            key = '.'.join(parts[:i])
            if key not in modules:
                modules[key] = types.ModuleType(key)
                modules[key].__path__ = []
            if i > 1:
                setattr(modules['.'.join(parts[:i-1])], parts[i-1], modules[key])
        modules[name].__dict__.update(attrs)
    module('pytz')
    module('apscheduler.schedulers.background', BackgroundScheduler=Mock())
    module('apscheduler.triggers.cron', CronTrigger=Mock())
    module('pydantic', BaseModel=type('BaseModel', (), {}), ConfigDict=dict)
    module('app.schemas')
    module('app.plugins', _PluginBase=Base)
    settings = types.SimpleNamespace(TZ='Asia/Shanghai')
    events = dict(Event=type('Event', (), {}), eventmanager=types.SimpleNamespace(register=lambda *a: lambda f: f))
    notification_type = 'NotificationType' if version == 'v2' else 'MessageType'
    module('app.schemas.types', EventType=Enum('EventType', 'PluginAction WebhookMessage'),
           **{notification_type: Enum(notification_type, {'Manual': '手动处理'})})
    if version == 'v2':
        module('app.core.config', settings=settings)
        module('app.core.event', **events)
        module('app.log', logger=Mock())
        module('app.helper.mediaserver', MediaServerHelper=Mock())
    else:
        module('app.schemas.event', PluginActionEventData=type('Payload', (), {}))
        module('app.sdk.config', settings=settings)
        module('app.sdk.events', **events)
        module('app.sdk.logging', logger=Mock())
        module('app.sdk.services', MediaServerHelper=Mock())
    path = Path(__file__).resolve().parents[1] / f'plugins.{version}/cd2assistant/__init__.py'
    spec = importlib.util.spec_from_file_location('cd2_test_plugin', path,
                                                  submodule_search_locations=[str(path.parent)])
    plugin = importlib.util.module_from_spec(spec)
    modules['cd2_test_plugin'] = plugin
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(plugin)
        plugin.connection_module = sys.modules['cd2_test_plugin.cd2client']
    return plugin


class PluginContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_plugin()

    def make_plugin(self, **config):
        p = self.module.Cd2Assistant()
        with patch.object(self.module, 'CloudDriveConnection', return_value=Mock()):
            p.init_plugin(dict(enabled=True, playback_pause=True, cd2_confs='test#http://cd2:19798#u#p', **config))
        return p

    def test_playback_schedule_independent_of_monitor_cron(self):
        p = self.make_plugin(cron='')
        services = p.get_service()
        self.assertEqual([s['id'] for s in services], ['Cd2AssistantPlayback'])
        self.assertEqual(services[0]['kwargs']['seconds'], 10)
        p.stop_service()
        self.assertFalse(p._clients)

    def test_config_update_preserves_new_fields(self):
        p = self.make_plugin(playback_interval=7, playback_resume_delay=60)
        p._Cd2Assistant__update_config()
        self.assertTrue(p.config['playback_pause'])
        self.assertEqual(p.config['playback_interval'], 7)
        self.assertEqual(p.config['playback_resume_delay'], 60)
        p.stop_service()

    def test_disabled_plugin_schedules_pending_recovery(self):
        p = self.module.Cd2Assistant()
        import hashlib
        target = hashlib.sha256(b'http://cd2:19798\nu').hexdigest()
        p.data['playback_owned_uploads'] = {target: {'k': {'path': '/x', 'size': '1'}}}
        scheduler = Mock()
        with patch.object(self.module, 'CloudDriveConnection', return_value=Mock()), \
             patch.object(self.module, 'BackgroundScheduler', return_value=scheduler):
            p.init_plugin({'enabled': False, 'cd2_confs': 'test#http://cd2:19798#u#p'})
        self.assertEqual(p.get_service(), [])
        self.assertEqual(scheduler.add_job.call_args.kwargs['id'], 'recover_uploads')
        p.stop_service()
        self.assertTrue(p.data['playback_owned_uploads'])

    def test_webhook_triggers_check_but_does_not_assume_stopped(self):
        p = self.make_plugin()
        p.check_playback = Mock()
        event = types.SimpleNamespace(snapshot=lambda: types.SimpleNamespace(
            valid=True, payload=types.SimpleNamespace(event='playback.stop')))
        asyncio.run(p.playback_webhook(event))
        p.check_playback.assert_called_once()
        p.stop_service()

    def test_form_exposes_opt_in_and_clamps_invalid_intervals(self):
        p = self.make_plugin(playback_interval='bad', playback_resume_delay=-1)
        form, defaults = p.get_form()
        self.assertFalse(defaults['playback_pause'])
        self.assertEqual(p._playback_interval, 10)
        self.assertEqual(p._playback_resume_delay, 0)
        self.assertIn('忽略内网', str(form))
        p.stop_service()

    def test_compact_form_preserves_existing_settings(self):
        for version in ('v2', 'v3'):
            with self.subTest(version=version):
                p = load_plugin(version).Cd2Assistant()
                connection = 'home#http://nas:19798#test-token'
                p.init_plugin({'playback_upload_limit': 350, 'cd2_confs': connection,
                               'cloud_path': '/legacy-downloads'})
                form, defaults = p.get_form()
                self.assertEqual(defaults['playback_mode'], 'limit')
                self.assertEqual(defaults['playback_upload_limit'], 350)
                self.assertIn('高级设置', str(form))
                self.assertNotIn('连接列表', str(form))
                self.assertNotIn('多台连接或旧版配置', str(form))
                self.assertNotIn('cloud_path', str(form))
                self.assertNotIn('test-token', str(form))
                p._Cd2Assistant__update_config()
                self.assertEqual(p.config['cloud_path'], '/legacy-downloads')
                self.assertEqual(p.config['cd2_confs'], connection)
                p.init_plugin({**p.config, 'playback_mode': 'pause'})
                self.assertEqual(p._playback_upload_limit, 0)
                p.init_plugin({**p.config, 'playback_mode': 'limit', 'playback_upload_limit': ''})
                self.assertEqual(p._playback_upload_limit, 200)

    def test_expired_token_refreshes_once_and_retries(self):
        class Expired(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.UNAUTHENTICATED

        proto = self.module.proto
        transport = Mock()
        transport.stub.GetToken.side_effect = [proto.JWTToken(success=True, token=token)
                                              for token in ('old', 'new')]
        transport.stub.PauseUploadFiles.side_effect = [Expired(), None]
        with patch.object(self.module.connection_module, 'CloudDriveClient', return_value=transport) as factory:
            client = self.module.CloudDriveConnection('http://[::1]:19798', 'user', 'pass')
        factory.assert_called_once_with('[::1]:19798')
        api = self.module.CloudUploadAPI(client, proto)
        api.pause('key')
        self.assertEqual(transport.stub.GetToken.call_count, 2)
        calls = transport.stub.PauseUploadFiles.call_args_list
        self.assertEqual([call.kwargs['metadata'] for call in calls],
                         [(('authorization', 'Bearer old'),), (('authorization', 'Bearer new'),)])
        self.assertEqual([call.kwargs['timeout'] for call in calls], [15, 15])

    def test_shutdown_prevents_further_upload_mutations(self):
        transport = Mock()
        with patch.object(self.module.connection_module, 'CloudDriveClient', return_value=transport):
            client = self.module.CloudDriveConnection('cd2:19798', 'user', 'pass')
        api = self.module.CloudUploadAPI(client, self.module.proto, stopping=lambda: True)
        with self.assertRaises(RuntimeError):
            api.pause('key')
        transport.stub.GetToken.assert_not_called()
        transport.stub.PauseUploadFiles.assert_not_called()


class HealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_plugin()

    def client(self):
        p = self.module.proto
        c = Mock()
        c.public_system_info.return_value = p.CloudDriveSystemInfo(IsLogin=True, SystemReady=True)
        c._username = 'user'
        def rpc(name, request=None, **kwargs):
            if name == 'GetAllCloudApis':
                return p.CloudAPIList(apis=[p.CloudAPI(name='disk', path='/disk')])
            if name == 'GetSubFiles':
                return []
            if name == 'GetUploadFileList':
                rows = [p.UploadFileInfo(key=str(i), destPath='/disk/'+str(i), statusEnum=3) for i in range(201)]
                rows[-1].statusEnum = 10
                rows[-1].status = 'localized text'
                rows[-1].errorMessage = 'failure'
                return p.GetUploadFileListResult(totalCount=201, uploadFiles=rows[request.pageNumber*200:(request.pageNumber+1)*200])
            raise AssertionError(name)
        c.rpc.side_effect = rpc
        return c

    def plugin(self):
        p = self.module.Cd2Assistant()
        p._clients = {'test': self.client()}
        p._cd2_url = {'test': 'http://cd2:19798'}
        p._playback_stopping = False
        p._notify = True
        p.post_message = Mock()
        return p

    def test_settings_only_entry(self):
        plugin = self.module.Cd2Assistant()
        self.assertIsNone(plugin.get_page())
        self.assertFalse(self.module.Cd2Assistant.__abstractmethods__)
        self.assertTrue(plugin.get_form()[0])

    def test_all_pages_numeric_error_and_empty_cloud_valid(self):
        p = self.plugin()
        p._Cd2Assistant__check_health()
        self.assertEqual(p.post_message.call_count, 1)
        self.assertIn('/disk/200', p.post_message.call_args.kwargs['text'])
        self.assertEqual(p.post_message.call_args.kwargs['mtype'].name, 'Manual')
        p._clients['test'].probe_directory.assert_called_once()
        p._Cd2Assistant__check_health()
        self.assertEqual(p.post_message.call_count, 1)

    def test_query_failure_does_not_clear_upload_failures(self):
        p = self.plugin()
        p._Cd2Assistant__check_health()
        p._clients['test'].rpc.side_effect = RuntimeError('secret password')
        p._Cd2Assistant__check_health()
        state = next(v for k,v in p._health_alerts.items() if k.endswith(':uploads'))
        self.assertIn('200', state)
        self.assertIn('query', state)
        self.assertNotIn('secret password', str(p.post_message.call_args_list))

    def test_disabled_notifications_and_failed_delivery(self):
        p = self.plugin()
        p._notify = False
        p._Cd2Assistant__check_health()
        p.post_message.assert_not_called()
        p._notify = True
        p.post_message.side_effect = RuntimeError()
        p._Cd2Assistant__check_health()
        self.assertFalse(any(v for v in p._health_alerts.values()))
        p.post_message.side_effect = None
        p._Cd2Assistant__check_health()
        self.assertIn('/disk/200', p.post_message.call_args.kwargs['text'])

    def test_recovery_and_manual_pause_not_failure(self):
        p = self.plugin()
        p._Cd2Assistant__check_health()
        original = p._clients['test'].rpc.side_effect
        def rpc(name, request=None, **kwargs):
            result = original(name, request, **kwargs)
            if name == 'GetUploadFileList':
                for task in result.uploadFiles:
                    task.statusEnum = 4
            return result
        p._clients['test'].rpc.side_effect = rpc
        p._Cd2Assistant__check_health()
        self.assertEqual(p.post_message.call_count, 2)
        self.assertIn('已不再出现', p.post_message.call_args.kwargs['text'])

    def test_cloud_failure_does_not_block_upload_check(self):
        p = self.plugin()
        p._clients['test'].probe_directory.side_effect = TimeoutError()
        p._Cd2Assistant__check_health()
        text = str(p.post_message.call_args_list)
        self.assertIn('/disk/200', text)
        self.assertIn('TimeoutError', text)
        self.assertNotIn('Cookie过期', text)


class V2ContractTests(HealthTests):
    @classmethod
    def setUpClass(cls):
        cls.module = load_plugin("v2")

    def test_v2_post_message_uses_host_notification_type(self):
        plugin = self.plugin()
        plugin._Cd2Assistant__check_health()
        sent_type = plugin.post_message.call_args.kwargs['mtype']
        self.assertIs(sent_type, self.module.NotificationType.Manual)

    def test_v2_webhook_and_action_payloads(self):
        plugin = self.plugin()
        plugin._enabled = True
        plugin._playback_pause = True
        plugin.check_playback = Mock()
        for data in ({'event': 'playback.start'}, types.SimpleNamespace(event='media.stop')):
            asyncio.run(plugin.playback_webhook(types.SimpleNamespace(event_data=data)))
        self.assertEqual(plugin.check_playback.call_count, 2)

        command = types.SimpleNamespace(event_data=dict(action='cd2_restart', arg_str='test',
                                                        channel='telegram', user='operator'))
        asyncio.run(plugin.restart_cd2(command))
        plugin._clients['test'].rpc.assert_called_once_with('RestartService')
        self.assertEqual(plugin.post_message.call_args.kwargs['userid'], 'operator')

    def test_disabled_v2_plugin_recovers_owned_uploads_using_local_scheduler(self):
        import hashlib

        plugin = self.module.Cd2Assistant()
        target = hashlib.sha256(b'http://cd2:19798\nu').hexdigest()
        plugin.data['playback_owned_uploads'] = {target: {'key': {'path': '/x', 'size': '1'}}}
        client = Mock()
        task = self.module.proto.UploadFileInfo(key='key', destPath='/x', size=1, statusEnum=4)
        client.rpc.return_value = self.module.proto.GetUploadFileListResult(totalCount=1, uploadFiles=[task])
        scheduler = Mock()
        with patch.object(self.module, 'CloudDriveConnection', return_value=client), \
             patch.object(self.module, 'BackgroundScheduler', return_value=scheduler):
            plugin.init_plugin(dict(enabled=False, cd2_confs='test#http://cd2:19798#u#p'))
        self.assertFalse(plugin.get_state())
        self.assertEqual(plugin.get_service(), [])
        scheduler.add_job.assert_called_once()
        recovery = scheduler.add_job.call_args.args[0]
        recovery()
        self.assertEqual(client.rpc.call_args.args[0], 'ResumeUploadFiles')
        self.assertEqual(plugin.data['playback_owned_uploads'], {})
        scheduler.get_job.return_value.remove.assert_called_once()
        plugin.stop_service()


class APITokenTests(unittest.TestCase):
    def test_direct_bearer_for_unary_and_streaming_without_password_fallback(self):
        class Expired(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.UNAUTHENTICATED

        plugin = load_plugin()
        transport = Mock()
        stream = Mock()
        stream.__iter__ = Mock(return_value=iter(()))
        transport.stub.GetSubFiles.return_value = stream
        with patch.object(plugin.connection_module, 'CloudDriveClient', return_value=transport) as factory:
            client = plugin.CloudDriveConnection('https://cd2.example', api_token=' test-api-token ')
        factory.assert_called_once_with('cd2.example:443', tls=True)
        client.rpc('GetAllCloudApis')
        client.probe_directory('/disk')
        self.assertEqual(transport.stub.GetAllCloudApis.call_args.kwargs['metadata'],
                         (('authorization', 'Bearer test-api-token'),))
        self.assertEqual(transport.stub.GetSubFiles.call_args.kwargs['metadata'],
                         (('authorization', 'Bearer test-api-token'),))
        stream.cancel.assert_called_once()
        transport.stub.GetAllCloudApis.side_effect = Expired()
        with self.assertRaises(Expired):
            client.rpc('GetAllCloudApis')
        self.assertEqual(transport.stub.GetAllCloudApis.call_count, 2)
        transport.stub.GetToken.assert_not_called()

    def test_both_hosts_accept_mixed_configs_and_token_rotation_keeps_pending_key(self):
        for edition in ('v2', 'v3'):
            with self.subTest(edition=edition):
                module = load_plugin(edition)
                plugin = module.Cd2Assistant()
                config = dict(enabled=True, cron='', cd2_confs='home#cd2:19798#first\nlegacy#backup#user#old-pass')
                with patch.object(module, 'CloudDriveConnection', side_effect=lambda *args, **kwargs: Mock()) as connect:
                    plugin.init_plugin(config)
                    target = next(key for key, value in plugin._playback_targets.items() if value[0] == 'home')
                    plugin.init_plugin({**config, 'cd2_confs': config['cd2_confs'].replace('first', 'second')})
                self.assertEqual(connect.call_args_list[0].kwargs, {'api_token': 'first'})
                self.assertEqual(connect.call_args_list[1].args, ('backup', 'user', 'old-pass'))
                self.assertEqual(connect.call_args_list[2].kwargs, {'api_token': 'second'})
                self.assertEqual(plugin._playback_targets[target][0], 'home')
                plugin.stop_service()


class CopyTaskTests(unittest.TestCase):
    def test_copy_keeps_original_paths_and_does_not_resume_replaced_or_manual_tasks(self):
        module = load_plugin()
        p = module.proto
        copy = p.CopyTask(sourcePath='/Source：A/A:B', destPath='/Target/Title', status=2)
        copy.startTime.seconds = 10
        manual = p.CopyTask(sourcePath='/Source/Manual', destPath='/Target/Manual', status=1, paused=True)
        replaced = p.CopyTask(sourcePath='/Source/Replaced', destPath='/Target/Replaced', status=1)
        replaced.startTime.seconds = 20
        rows = [copy, manual, replaced]
        requests = []
        def rpc(name, request, **kwargs):
            if name == 'GetCopyTasks':
                return p.GetCopyTaskResult(copyTasks=rows)
            self.assertEqual(name, 'PauseCopyTask')
            task = next(row for row in rows if row.sourcePath == request.sourcePath)
            self.assertEqual(task.destPath, request.destPath)
            task.paused = request.pause
            requests.append((task.sourcePath, request.pause))
        api = module.CloudCopyAPI(types.SimpleNamespace(rpc=rpc), p)
        guard = module.UploadGuard({}, Mock(), Mock())
        targets = {'instance:copy': ('test', api)}
        guard.tick(True, targets)
        replaced.startTime.seconds = 21
        guard.tick(False, targets, delay=0)
        self.assertEqual(requests, [(copy.sourcePath, True), (replaced.sourcePath, True),
                                    (copy.sourcePath, False)])
        self.assertTrue(manual.paused)


if __name__ == '__main__':
    unittest.main()
