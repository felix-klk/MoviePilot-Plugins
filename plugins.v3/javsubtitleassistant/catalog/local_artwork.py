from __future__ import annotations

from base64 import b64encode
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Iterable
import xml.etree.ElementTree as ET

from PIL import Image, ImageOps

from ..jav import VIDEOS, jav_numbers


IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
MAX_NFO_BYTES = 128 * 1024


@lru_cache(maxsize=256)
def _thumbnail(path: Path, mtime_ns: int, size: int) -> str:
    # The stat values invalidate cached thumbnails when the scraper replaces an image.
    try:
        with Image.open(path, formats=("JPEG", "PNG", "WEBP")) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                return ""
            image = ImageOps.exif_transpose(image)
            image.thumbnail((216, 324))
            with BytesIO() as output:
                image.convert("RGB").save(output, format="JPEG", quality=82)
                return "data:image/jpeg;base64," + b64encode(output.getvalue()).decode("ascii")
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return ""


def _read_thumbnail(candidate: Path, folder: Path) -> str:
    if candidate.suffix.lower() not in IMAGE_SUFFIXES:
        return ""
    try:
        path = candidate.resolve(strict=True)
        if not path.is_relative_to(folder) or not path.is_file():
            return ""
        info = path.stat()
        if 0 < info.st_size <= MAX_IMAGE_BYTES:
            return _thumbnail(path, info.st_mtime_ns, info.st_size)
    except (OSError, RuntimeError, ValueError):
        pass
    return ""


def _nfo_posters(path: Path, folder: Path):
    try:
        path = path.resolve(strict=True)
        if path.parent != folder or not path.is_file():
            return
        with path.open("rb") as source:
            raw = source.read(MAX_NFO_BYTES + 1)
        if len(raw) > MAX_NFO_BYTES or b"<!DOCTYPE" in raw.upper().replace(b"\0", b""):
            return
        root = ET.fromstring(raw)
        nodes = root.findall("art/poster") + root.findall("poster")
        nodes.extend(thumb for thumb in root.findall("thumb") if thumb.get("aspect", "poster").strip().lower() == "poster")
        for node in nodes:
            value = (node.text or "").strip()
            if value and "://" not in value:
                yield folder / value
    except (OSError, RuntimeError, ValueError, ET.ParseError):
        return


def _is_title_folder(video: Path, number: str) -> bool:
    try:
        return all(
            file == video or file.suffix.lower() not in VIDEOS or not file.is_file()
            or number in jav_numbers(file.stem)
            for file in video.parent.iterdir()
        )
    except OSError:
        return False


def _named_artwork(folder: Path, stems: Iterable[str]):
    for stem in dict.fromkeys(stems):
        for suffix in IMAGE_SUFFIXES:
            yield folder / (stem + suffix)
            yield folder / (stem + suffix.upper())


def _poster_for_video(video: Path, number: str) -> str:
    folder = video.parent
    for candidate in _named_artwork(folder, (
        f"{video.stem}-poster", f"{number}-poster", f"{video.stem}-cover", f"{number}-cover",
        video.stem, number,
    )):
        if thumbnail := _read_thumbnail(candidate, folder):
            return thumbnail

    for candidate in _nfo_posters(video.with_suffix(".nfo"), folder):
        if thumbnail := _read_thumbnail(candidate, folder):
            return thumbnail

    generic = [candidate for candidate in _named_artwork(folder, ("poster", "cover", "folder", "movie", "thumb"))
               if candidate.is_file()]
    movie_nfo = folder / "movie.nfo"
    if (generic or movie_nfo.is_file()) and _is_title_folder(video, number):
        for candidate in generic:
            if thumbnail := _read_thumbnail(candidate, folder):
                return thumbnail
        for candidate in _nfo_posters(movie_nfo, folder):
            if thumbnail := _read_thumbnail(candidate, folder):
                return thumbnail

    for candidate in _named_artwork(folder, (f"{video.stem}-thumb", f"{number}-thumb")):
        if thumbnail := _read_thumbnail(candidate, folder):
            return thumbnail
    return ""


def local_poster_thumbnail(entries: Iterable[tuple[str, str]], roots: Iterable[str]) -> str:
    resolved_roots = []
    for value in roots:
        try:
            root = Path(value)
            if root.is_absolute() and root != root.parent:
                resolved_roots.append(root.resolve())
        except (OSError, RuntimeError, ValueError):
            continue
    for path, number in entries:
        if not path or not number:
            continue
        try:
            video = Path(path)
            folder = video.parent.resolve(strict=True)
            if not any(folder.is_relative_to(root) for root in resolved_roots):
                continue
            if thumbnail := _poster_for_video(folder / video.name, number):
                return thumbnail
        except (OSError, RuntimeError, ValueError):
            continue
    return ""
