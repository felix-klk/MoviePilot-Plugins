"""Identify JAV releases and detect Chinese subtitle tracks in local media."""

import errno
import json
import os
import re
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from tempfile import NamedTemporaryFile


VIDEOS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v", ".ts", ".m2ts", ".webm",
          ".flv", ".rmvb", ".mpg", ".mpeg", ".asf", ".strm"}
TEXT_SUBTITLES = {".srt", ".ass", ".ssa", ".vtt", ".smi", ".sami", ".ttml", ".dfxp", ".sub"}
SUBTITLES = TEXT_SUBTITLES | {".sup", ".idx"}
TEXT_CODECS = {"ass", "ssa", "subrip", "mov_text", "webvtt", "text", "ttml", "sami", "microdvd", "subviewer"}
CHINESE_TAG = re.compile(
    r"(?<![a-z])(?:zh|zho|chi|cmn|yue|chs|cht|chinese|mandarin|cantonese)(?![a-z])"
    r"|中文|汉语|漢語|华语|華語|粤语|粵語|简[体中]|簡[體中]|繁[体體中]|中[英日韩韓]|[英日韩韓]中",
    re.I,
)
HAN = re.compile(r"[\u3400-\u9fff]")
KANA_OR_HANGUL = re.compile(r"[\u3041-\u30fa\uac00-\ud7af]")
CHINESE_WORDS = re.compile(r"你好|谢谢|謝謝|對不起|对不起|什[么麼]|[这這那][个個]|[你我他她][们們]|[没沒]有|不是|可以|知道|怎[么麼]|[吗嗎吧呢]")
NUMBER = re.compile(
    r"(?<![a-z0-9])(?:FC2[ ._-]*(?:PPV[ ._-]*)?[0-9]{5,9}"
    r"|[0-9]{6}[-_][0-9]{2,3}|N[0-9]{4,6}"
    r"|(?:T28|MKD-S|[A-Z]{2,10})[-_.]?[0-9]{2,5})(?![a-z0-9])",
    re.I | re.ASCII,
)
PART = re.compile(r"(?<![a-z0-9])(?:cd|disc|disk|part|pt)[ ._-]*0*([1-9][0-9]?)(?![a-z0-9])", re.I)
NON_RELEASE_PREFIX = {"AVC", "HEVC", "AAC", "DDP", "DTS", "HDR", "WEB", "XVID", "VOL", "CD", "DISC", "PART"}
SAMPLE = re.compile(r"(?:^|[ ._-])(?:sample|trailer|preview)(?:$|[ ._-])", re.I)
MAX_SUBTITLE = 2 * 1024 * 1024


def jav_numbers(value: str):
    """Extract canonical numbers for search and exact identity comparison."""
    for match in NUMBER.finditer(unicodedata.normalize("NFKC", value).upper()):
        raw = match[0]
        if fc2 := re.fullmatch(r"FC2[ ._-]*(?:PPV[ ._-]*)?([0-9]{5,9})", raw):
            yield "FC2-" + fc2[1]
        elif raw[0].isdigit():
            yield raw.replace("_", "-")
        else:
            prefix, digits = re.fullmatch(r"(T28|MKD-S|[A-Z]+)[_.-]?([0-9]+)", raw).groups()
            if prefix not in NON_RELEASE_PREFIX:
                yield raw if prefix == "N" else f"{prefix}-{digits}"


def video_number(video: Path) -> str:
    """Prefer the organizer's same-stem NFO when it contains an explicit JAV number."""
    try:
        with video.with_suffix(".nfo").open("rb") as file:
            metadata = file.read(128 * 1024 + 1)
        if len(metadata) <= 128 * 1024 and b"<!DOCTYPE" not in metadata.upper():
            root = ET.fromstring(metadata)
            declared = [root.findtext("num"), root.findtext("number")]
            declared.extend(item.text for item in root.findall("uniqueid")
                            if (item.get("type") or "").lower() in {"jav", "javbus", "javdb", "dmm"})
            for candidate in declared:
                if number := next(jav_numbers(candidate or ""), ""):
                    return number
    except (OSError, ET.ParseError):
        pass
    return next(jav_numbers(video.stem), "")


def release_part(name: str) -> int | None:
    """Match explicit split markers used in organized and Subtitle Cat filenames."""
    if match := PART.search(name):
        return int(match[1])
    match = re.search(r"(?<=[0-9])[-_.]([ab])(?:$|[-_. ])", name, re.I)
    return "ab".index(match[1].lower()) + 1 if match else None


def decode_subtitle(data: bytes) -> str:
    """Read Unicode and common legacy subtitle encodings."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        text = data.decode("utf-8-sig")
        return text if "\0" not in text else ""
    except UnicodeDecodeError:
        pass
    decoded = []
    for encoding in ("gb18030", "big5", "cp932"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\0" not in text:
            decoded.append(text)
            if encoding == "cp932" and len(re.findall(r"[\u3041-\u3096]", text)) >= 3:
                return text
    return max(decoded, default="", key=lambda text: (
        -len(re.findall(r"[\ue000-\uf8ff]", text)),
        len(CHINESE_WORDS.findall(text)),
        len(HAN.findall(text)),
    ))


def chinese_text(text: str) -> bool:
    """Recognize Chinese dialogue in unlabelled text and mixed-language tracks."""
    if re.search(r"(?im)^Dialogue:", text):
        text = "\n".join(line.split(",", 9)[-1] for line in text.splitlines()
                         if line.lower().startswith("dialogue:"))
    text = re.sub(r"<[^>\n]+>|\{\\[^}\n]+\}", "", text.replace(r"\N", "\n"))
    lines = [line for line in text.splitlines() if not KANA_OR_HANGUL.search(line)]
    if any(CHINESE_WORDS.search(line) for line in lines):
        return True
    # shortcut: Kanji-only glyphs are ambiguous, use language tags or fuller language detection when needed.
    return not KANA_OR_HANGUL.search(text) and sum(len(HAN.findall(line)) for line in lines) >= 12


def has_chinese_sidecar(video: Path) -> bool:
    """Check only subtitle files that share the video's full stem."""
    stem = video.stem.casefold()
    siblings = list(video.parent.iterdir())
    other_versions = {
        sibling.stem.casefold() for sibling in siblings
        if sibling.suffix.lower() in VIDEOS and sibling.stem.casefold().startswith(stem)
        and len(sibling.stem) > len(video.stem)
    }
    for subtitle in siblings:
        extension = subtitle.suffix.lower()
        name = subtitle.stem.casefold()
        if extension not in SUBTITLES or not name.startswith(stem):
            continue
        suffix = name[len(stem):]
        if suffix and (suffix[0] not in ".-_ " or release_part(name) != release_part(stem)):
            continue
        if any(name == other or (name.startswith(other) and name[len(other)] in ".-_ ")
               for other in other_versions):
            continue
        if not subtitle.is_file() or not subtitle.stat().st_size:
            continue
        if extension == ".idx" and not subtitle.with_suffix(".sub").is_file():
            continue
        if CHINESE_TAG.search(suffix):
            return True
        if extension in TEXT_SUBTITLES or extension == ".idx":
            try:
                with subtitle.open("rb") as file:
                    text = decode_subtitle(file.read(MAX_SUBTITLE))
            except UnicodeDecodeError:
                continue
            if extension == ".idx":
                if any(CHINESE_TAG.search(line) for line in text.splitlines() if line.startswith("id:")):
                    return True
            elif chinese_text(text):
                return True
    return False


def has_chinese_embedded(video: Path, ffprobe: str, ffmpeg: str | None) -> bool:
    """Use subtitle stream language/title first, then inspect unlabelled or mislabeled text."""
    if video.suffix.lower() == ".strm":
        return False
    result = subprocess.run(
        [ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe", "-select_streams", "s",
         "-show_entries", "stream=index,codec_name:stream_tags=language,title", "-of", "json", str(video)],
        capture_output=True, timeout=12, check=True,
    )
    streams = json.loads(result.stdout).get("streams", [])
    for stream in streams:
        tags = stream.get("tags", {})
        if any(CHINESE_TAG.search(value) for value in tags.values() if isinstance(value, str)):
            return True
    for stream in streams:
        if stream.get("codec_name") not in TEXT_CODECS:
            continue
        if not ffmpeg:
            raise OSError("ffmpeg 不可用，无法检查内嵌字幕正文")
        # shortcut: sample a bounded amount of text; image tracks require a language tag until OCR is supported.
        result = subprocess.run(
            [ffmpeg, "-v", "error", "-nostdin", "-protocol_whitelist", "file,pipe", "-i", str(video),
             "-map", f"0:{stream['index']}", "-t", "600", "-fs", "131072", "-c:s", "srt", "-f", "srt", "pipe:1"],
            capture_output=True, timeout=15, check=True,
        )
        if chinese_text(decode_subtitle(result.stdout)):
            return True
    return False


def write_subtitle(video: Path, language: str, data: bytes) -> Path | None:
    """Publish a complete sidecar without replacing any existing file."""
    destination = video.with_name(f"{video.stem}.{language}.srt")
    with NamedTemporaryFile(dir=video.parent, prefix=".javsubtitle-", delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(data)
            file.close()
            temporary.chmod(0o200 | (video.stat().st_mode & 0o444))
            try:
                os.link(temporary, destination)
            except OSError as error:
                if error.errno not in {errno.EPERM, errno.ENOTSUP, errno.ENOSYS, errno.EXDEV}:
                    raise
                # shortcut: mounts without hard links use exclusive creation; upgrade if the mount offers rename-no-replace.
                output = destination.open("xb")
                try:
                    with output:
                        output.write(data)
                except OSError:
                    destination.unlink(missing_ok=True)
                    raise
        except FileExistsError:
            return None
        finally:
            temporary.unlink(missing_ok=True)
    return destination
