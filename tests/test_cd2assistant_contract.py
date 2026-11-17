"""Load the actual V3 plugin with host boundary doubles, not a running MoviePilot."""
import asyncio
import copy
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch
from enum import Enum

import grpc


class Base:
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


def load_plugin():
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
    module('app.schemas.event', PluginActionEventData=type('Payload', (), {}))
    module('app.schemas.types', EventType=Enum('EventType', 'PluginAction WebhookMessage'),
           MessageType=Enum('MessageType', {'Manual': '手动处理'}))
    module('app.sdk.config', settings=types.SimpleNamespace(TZ='Asia/Shanghai'))
    module('app.sdk.events', Event=type('Event', (), {}),
           eventmanager=types.SimpleNamespace(register=lambda *a: lambda f: f))
    module('app.sdk.logging', logger=Mock())
    module('app.sdk.services', MediaServerHelper=Mock())
    path = Path(__file__).resolve().parents[1] / 'plugins.v3/cd2assistant/__init__.py'
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
        p.data['playback_owned_uploads'] = {'pending': {'k': {'path': '/x', 'size': '1'}}}
        with patch.object(self.module, 'CloudDriveConnection', return_value=Mock()):
            p.init_plugin({'enabled': False, 'cd2_confs': 'test#http://cd2:19798#u#p'})
        self.assertEqual(p.get_service()[0]['id'], 'Cd2AssistantPlayback')
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


if __name__ == '__main__':
    unittest.main()
