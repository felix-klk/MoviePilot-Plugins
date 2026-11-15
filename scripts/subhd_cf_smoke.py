"""Read-only SubHD download smoke test using a supplied CF mirror service."""
import argparse
import importlib.util
import sys
import types
from pathlib import Path
import zipfile
import io

parser = argparse.ArgumentParser()
parser.add_argument("--service", required=True)
parser.add_argument("--page", required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
for name in ("app", "app.sdk", "app.sdk.config", "app.sdk.logging"):
    sys.modules[name] = types.ModuleType(name)
sys.modules["app.sdk.config"].settings = types.SimpleNamespace(PROXY=None, PROXY_SERVER=None)
sys.modules["app.sdk.logging"].logger = types.SimpleNamespace(
    info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None,
)
spec = importlib.util.spec_from_file_location("smoke_online", root / "plugins.v3/subtitlemanualupload/online/online_subtitle.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
client = module.OnlinePageClient(bypass_url=args.service, timeout=60)
provider = module.SubHDProvider(client, root_url="https://subhd.me")
filename, content = provider.download({"page_url": args.page})
print(f"download={filename} bytes={len(content)} magic={content[:8].hex()}")
if content.startswith(b"PK"):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        print(f"zip_members={len(archive.infolist())} crc_ok={archive.testzip() is None}")
assert content and not module._looks_like_html_bytes(content, filename)
