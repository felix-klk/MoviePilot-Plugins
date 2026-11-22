"""Parse local JAV identifiers for the Hyrule-based fork."""

import re
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path

VIDEOS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v", ".ts", ".m2ts", ".webm",
          ".flv", ".rmvb", ".mpg", ".mpeg", ".asf", ".strm"}
NUMBER = re.compile(
    r"(?<![a-z0-9])(?:FC2[ ._-]*(?:PPV[ ._-]*)?[0-9]{5,9}"
    r"|[0-9]{6}[-_][0-9]{2,3}|N[0-9]{4,6}"
    r"|(?:T28|MKD-S|[A-Z]{2,10})[-_.]?[0-9]{2,5})(?![a-z0-9])",
    re.I | re.ASCII,
)
PART = re.compile(r"(?<![a-z0-9])(?:cd|disc|disk|part|pt)[ ._-]*0*([1-9][0-9]?)(?![a-z0-9])", re.I)
NON_RELEASE_PREFIX = {"AVC", "HEVC", "AAC", "DDP", "DTS", "HDR", "WEB", "XVID", "VOL", "CD", "DISC", "PART"}
SAMPLE = re.compile(r"(?:^|[ ._-])(?:sample|trailer|preview)(?:$|[ ._-])", re.I)


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


def _nfo_number(path: Path) -> str:
    try:
        with path.open("rb") as file:
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
    return ""


def video_number(video: Path) -> str:
    """Use the file's NFO or name, then inspect a single-video directory's metadata."""
    number = _nfo_number(video.with_suffix(".nfo")) or next(jav_numbers(video.stem), "")
    if number:
        return number
    if not any(file != video and file.suffix.lower() in VIDEOS and file.is_file() for file in video.parent.iterdir()):
        return _nfo_number(video.parent / "movie.nfo") or next(jav_numbers(video.parent.name), "")
    return ""


def jav_movie_fields(entry: dict, *, label_field: str = "target_label") -> dict:
    """Correct season and label fields carried over by older catalog and queue snapshots."""
    if not entry.get("jav_number"):
        return {}
    fields = {"media_type": "movie", "season": 0, "episode": 0}
    filename = str(entry.get("filename") or "") or Path(str(entry.get("path") or "")).name
    if filename:
        fields[label_field] = filename
    return fields


def release_part(name: str) -> int | None:
    """Match explicit split markers used in organized and Subtitle Cat filenames."""
    if match := PART.search(name):
        return int(match[1])
    match = re.search(r"(?<=[0-9])[-_.]([ab])(?:$|[-_. ])", name, re.I)
    return "ab".index(match[1].lower()) + 1 if match else None
