"""CloudDrive2 gRPC connection shared by monitoring and upload control."""

from threading import Lock
from urllib.parse import urlsplit

from .cd2protocol import CloudDriveClient, proto
from google.protobuf.empty_pb2 import Empty
from grpc import RpcError, StatusCode


class CloudDriveConnection:
    def __init__(self, url, username, password):
        address = url.strip()
        parsed = urlsplit(address if "://" in address else f"//{address}")
        if not parsed.hostname or parsed.username is not None or parsed.scheme not in ("", "http", "https"):
            raise ValueError("CloudDrive2 地址应为主机名和 gRPC 端口")
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        self._client = CloudDriveClient(f"{host}:{parsed.port or 19798}")
        self._username = username
        self._password = password
        self._token = None
        self._token_lock = Lock()

    def close(self):
        self._client.close()

    def _authenticate(self, previous, stopping):
        with self._token_lock:
            if stopping():
                raise RuntimeError("插件正在停止")
            if self._token and self._token != previous:
                return self._token
            request = proto.GetTokenRequest(userName=self._username, password=self._password)
            result = self._client.stub.GetToken(request, timeout=15)
            if not result.success or not result.token:
                raise RuntimeError("CloudDrive2 登录失败，请检查用户名和密码")
            self._token = result.token
            return self._token

    def rpc(self, name, request=None, *, stream=False, stopping=lambda: False):
        method = getattr(self._client.stub, name)
        request = request if request is not None else Empty()

        def invoke(token):
            if stopping():
                raise RuntimeError("插件正在停止")
            response = method(request, metadata=(("authorization", f"Bearer {token}"),), timeout=15)
            return list(response) if stream else response

        token = self._token or self._authenticate(None, stopping)
        try:
            return invoke(token)
        except RpcError as error:
            if error.code() != StatusCode.UNAUTHENTICATED:
                raise
        return invoke(self._authenticate(token, stopping))

    def listdir(self, path="/"):
        batches = self.rpc("GetSubFiles", proto.ListSubFileRequest(path=path), stream=True)
        return [item.name for batch in batches for item in batch.subFiles]
