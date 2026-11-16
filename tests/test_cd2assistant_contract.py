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
    module('clouddrive', Client=Mock(side_effect=lambda *a: Mock()),
           CloudDriveClient=Mock(side_effect=lambda *a: Mock()))
    module('clouddrive.proto', CloudDrive_pb2=Mock())
    module('google.protobuf.json_format', MessageToDict=Mock())
    module('pydantic', BaseModel=type('BaseModel', (), {}), ConfigDict=dict)
    module('app.schemas')
    module('app.plugins', _PluginBase=Base)
    module('app.schemas.event', PluginActionEventData=type('Payload', (), {}))
    module('app.schemas.types', EventType=Enum('EventType', 'PluginAction WebhookMessage'),
           MessageType=Enum('MessageType', {'Manual': '�ֶ�����'}))
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
    return plugin


class PluginContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_plugin()

    def make_plugin(self, **config):
        p = self.module.Cd2Assistant()
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
        self.assertIn('��������', str(form))
        p.stop_service()


if __name__ == '__main__':
    unittest.main()
