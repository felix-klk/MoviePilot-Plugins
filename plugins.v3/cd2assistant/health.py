"""Read-only health checks using the official CloudDrive2 gRPC fields."""
import re
from grpc import RpcError, StatusCode
from .playback import CloudUploadAPI, upload_status


def error_text(error):
    if isinstance(error, RpcError):
        labels = {
            StatusCode.UNAUTHENTICATED: '认证失败，请检查登录凭据',
            StatusCode.PERMISSION_DENIED: '访问被拒绝，请检查账户或令牌权限',
            StatusCode.RESOURCE_EXHAUSTED: '请求受限或资源不足',
            StatusCode.DEADLINE_EXCEEDED: '请求超时，暂时无法确认状态',
            StatusCode.UNAVAILABLE: '服务不可达，暂时无法确认状态',
            StatusCode.UNIMPLEMENTED: '服务端不支持此接口，请检查 CloudDrive2 版本',
        }
        return labels.get(error.code(), 'CloudDrive2 接口请求失败')
    return '检测失败（' + type(error).__name__ + '），请检查服务与配置'


def safe_text(value):
    text = re.sub(r'https?://\S+', '[链接已隐藏]', str(value))
    text = re.sub(r'(?i)(authorization|cookie|password|token)\s*[:=]\s*[^\s,;]+', r'\1=[隐藏]', text)
    return text[:500]


def inspect_health(client, proto, blacklist, stopping):
    """Yield completed scopes independently; a failed query is never a recovery."""
    try:
        system = client.public_system_info()
        problems = []
        if not system.IsLogin:
            problems.append('CloudDrive2 服务未登录')
        if not system.SystemReady:
            problems.append('CloudDrive2 服务尚未就绪')
        if system.hasError:
            problems.append('服务报告异常：' + safe_text(system.SystemMessage))
        yield 'service', True, {'state': '；'.join(problems)} if problems else {}
    except Exception as error:
        yield 'service', False, {'query': error_text(error)}

    if stopping():
        return
    try:
        apis = client.rpc('GetAllCloudApis').apis
        roots = []
        if any(not api.path and not api.isLocked for api in apis):
            roots = [item for batch in client.rpc('GetSubFiles', proto.ListSubFileRequest(path='/'), stream=True)
                     for item in batch.subFiles if item.isCloudRoot]
        paths = {item.CloudAPI.name + '\n' + item.CloudAPI.userName: item.fullPathName for item in roots}
        for api in apis:
            if stopping():
                return
            path = api.path or paths.get(api.name + '\n' + api.userName)
            if api.name in blacklist or (path and path.strip('/') in blacklist):
                continue
            key = 'cloud:' + api.name + '\n' + api.userName
            if api.isLocked:
                yield key, True, {'locked': api.name + '：网盘被锁定，请检查会员或账户限制'}
            elif not path:
                yield key, False, {'query': api.name + '：无法确定网盘路径，未执行访问检测'}
            else:
                try:
                    # Force a provider read: a cached directory is not a health check.
                    client.probe_directory(path, stopping)
                    yield key, True, {}
                except Exception as error:
                    yield key, False, {'query': api.name + ' (' + path + ')：' + error_text(error)}
        yield 'cloud-list', True, {} if apis else {'empty': 'CloudDrive2 尚未添加网盘'}
    except Exception as error:
        yield 'cloud-list', False, {'query': error_text(error)}

    if stopping():
        return
    try:
        tasks = CloudUploadAPI(client, proto, stopping).list_uploads()
        failures = {}
        for key, task in tasks.items():
            status = upload_status(task)
            if status in ('Error', 'FatalError'):
                label = '上传出错（可能重试）' if status == 'Error' else '上传失败'
                failures[key] = label + '：' + safe_text(task.destPath) + '\n原因：' + safe_text(task.errorMessage or '服务端未提供原因')
        yield 'uploads', True, failures
    except Exception as error:
        yield 'uploads', False, {'query': error_text(error)}
