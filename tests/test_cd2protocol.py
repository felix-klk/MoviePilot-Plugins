import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from google.protobuf import descriptor_pool, descriptor_pb2


def load_protocol():
    path = Path(__file__).resolve().parents[1] / 'plugins.v3/cd2assistant/cd2protocol.py'
    spec = importlib.util.spec_from_file_location('isolated_protocol_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProtocolTests(unittest.TestCase):
    def test_conflicting_global_schema_and_repeated_load(self):
        pool = descriptor_pool.Default()
        legacy = descriptor_pb2.FileDescriptorProto(name='legacy_conflict_test.proto', package='clouddrive')
        enum = legacy.enum_type.add(name='ProxyType')
        enum.value.add(name='TEST_LEGACY_PROXY', number=0)
        pool.AddSerializedFile(legacy.SerializeToString())
        first, second = load_protocol(), load_protocol()
        self.assertIsNot(first.DESCRIPTOR.pool, pool)
        self.assertIsNot(first.DESCRIPTOR.pool, second.DESCRIPTOR.pool)
        self.assertEqual(pool.FindEnumTypeByName('clouddrive.ProxyType').values[0].name, 'TEST_LEGACY_PROXY')
        request = first.proto.GetTokenRequest(userName='test', password='test')
        self.assertEqual(second.proto.GetTokenRequest.FromString(request.SerializeToString()).userName, 'test')

    def test_rpc_paths_serialization_and_stream_binding(self):
        module = load_protocol()
        channel = Mock()
        with patch.object(module.grpc, 'insecure_channel', return_value=channel):
            client = module.CloudDriveClient('localhost:19798')
        unary = {c.args[0]: c.kwargs for c in channel.unary_unary.call_args_list}
        stream = {c.args[0]: c.kwargs for c in channel.unary_stream.call_args_list}
        login = unary['/clouddrive.CloudDriveFileSrv/GetToken']
        token = module.proto.JWTToken(success=True, token='test')
        self.assertEqual(login['response_deserializer'](token.SerializeToString()).token, 'test')
        request = module.proto.MultpleUploadFileKeyRequest(keys=['one'])
        for name in ('PauseUploadFiles', 'ResumeUploadFiles'):
            self.assertEqual(unary['/clouddrive.CloudDriveFileSrv/' + name]['request_serializer'](request), request.SerializeToString())
        self.assertIn('/clouddrive.CloudDriveFileSrv/GetSubFiles', stream)
        client.close()
        channel.close.assert_called_once()
