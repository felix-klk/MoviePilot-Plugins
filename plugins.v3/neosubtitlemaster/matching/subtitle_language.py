from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .subtitle_encoding import decode_subtitle_bytes
from .subtitle_formats import is_bitmap_subtitle
from .tongwen import load_sami_subtitles, load_ttml_subtitles


DEFAULT_AUTO_LANGUAGE_PRIORITY = ["chi&eng", "chi&jp", "chi&kr", "other_bilingual", "chi", "cht", "eng"]
DEFAULT_AUTO_FORMAT_PRIORITY = [".ass", ".srt", ".ssa", ".vtt"]

CHINESE_BILINGUAL_NAMES = {
    "eng": "英",
    "jp": "日",
    "kr": "韩",
    "fre": "法",
    "rus": "俄",
    "spa": "西",
    "ger": "德",
    "por": "葡",
    "ita": "意",
}

LANGUAGE_SUFFIX_ALIASES = {
    "zh": "chi",
    "zh-hans": "chi",
    "zh_hans": "chi",
    "zh-cn": "chi",
    "zh_cn": "chi",
    "zh-hant": "cht",
    "zh_hant": "cht",
    "zh-tw": "cht",
    "zh_tw": "cht",
    **{
        f"{prefix}{name}双语": f"{chinese}&{foreign}"
        for chinese, prefix in (("chi", "中"), ("cht", "繁"))
        for foreign, name in CHINESE_BILINGUAL_NAMES.items()
    },
    "chs": "chi",
    "cht": "cht",
    "tw": "cht",
    "hk": "cht",
    "zho": "chi",
    "cmn": "chi",
    "cn": "chi",
    "en": "eng",
    "ja": "jpn",
    "jp": "jpn",
    "ko": "kor",
    "kr": "kor",
    "fr": "fre",
    "fra": "fre",
    "de": "ger",
    "deu": "ger",
    "es": "spa",
    "pt": "por",
    "it": "ita",
    "ru": "rus",
}

LANGUAGE_LABELS = {
    "chi": "中文",
    "cht": "繁中",
    "eng": "英文",
    "jpn": "日文",
    "kor": "韩文",
    "fre": "法文",
    "spa": "西文",
    "ger": "德文",
    "por": "葡文",
    "ita": "意文",
    "rus": "俄文",
}

LANGUAGE_PAIR_LABELS = {
    f"{chinese}&{foreign}": f"中{name}双语"
    for chinese in ("chi", "cht")
    for foreign, name in CHINESE_BILINGUAL_NAMES.items()
}

LEGACY_BILINGUAL_BUCKETS = ["chi&eng", "chi&jp", "chi&kr", "other_bilingual"]
LANGUAGE_PAIR_ALIASES = {
    "eng": "eng",
    "en": "eng",
    "english": "eng",
    "jpn": "jp",
    "jap": "jp",
    "japanese": "jp",
    "ja": "jp",
    "jp": "jp",
    "kor": "kr",
    "korean": "kr",
    "ko": "kr",
    "kr": "kr",
    "fre": "fre",
    "fra": "fre",
    "french": "fre",
    "fr": "fre",
    "spa": "spa",
    "spanish": "spa",
    "es": "spa",
    "ger": "ger",
    "deu": "ger",
    "german": "ger",
    "de": "ger",
}


def normalize_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_language_suffix(value: Any) -> str:
    suffix = normalize_text(value).strip().lower()
    suffix = LANGUAGE_SUFFIX_ALIASES.get(suffix, suffix)
    if not suffix:
        return "und"
    if any(separator in suffix for separator in ("&", "+", "/", ",")):
        parts = []
        for part in re.split(r"[&+/,]+", suffix):
            normalized = LANGUAGE_SUFFIX_ALIASES.get(part.strip(), part.strip())
            normalized = {"jpn": "jp", "kor": "kr"}.get(normalized, normalized)
            normalized = re.sub(r"[^a-z0-9-]", "", normalized)
            if normalized and normalized not in parts:
                parts.append(normalized)
        return "&".join(parts) or "und"
    return re.sub(r"[^a-z0-9-]", "", suffix) or "und"


def subtitle_filename_has_forced_tag(file_name: Any, video_stem: str) -> bool:
    if not video_stem:
        return False
    stem = Path(normalize_text(file_name).replace("\\", "/")).stem.casefold()
    prefix = f"{video_stem.casefold()}."
    return stem.startswith(prefix) and "forced" in stem[len(prefix):].split(".")


def language_suffix_from_filename(file_name: str, subtitle_exts: Iterable[str]) -> Dict[str, str]:
    name = Path(normalize_text(file_name)).name
    ext = Path(name).suffix.lower()
    if ext not in set(subtitle_exts):
        return {"suffix": "und", "label": "未知"}

    stem = name[: -len(ext)]
    tokens = [
        re.sub(r"[^a-z0-9&+/,_-]", "", LANGUAGE_SUFFIX_ALIASES.get(token, token)).strip("_-")
        for token in re.split(r"[\s.\[\](){}]+", stem.lower())
    ]
    aliases = {
        **{key: key for key in LANGUAGE_LABELS},
        **LANGUAGE_SUFFIX_ALIASES,
        "chi": "chi",
        "eng": "eng",
        "jpn": "jpn",
        "kor": "kor",
        "english": "eng",
        "chinese": "chi",
        "japanese": "jpn",
        "korean": "kor",
    }
    ignored_tail_tokens = {"forced", "sdh", "hi", "cc", "default", "full", "signs", "songs", "commentary"}
    found: List[str] = []
    for token in reversed([item for item in tokens if item]):
        parts = re.split(r"[&+/,]", token)
        if len(parts) > 1 and all(
            part in aliases or re.fullmatch(r"[a-z]{2,3}(?:-[a-z]{2})?", part)
            for part in parts
        ):
            normalized = normalize_language_suffix(token)
        else:
            normalized = aliases.get(token, "")
        if normalized and normalized != "und":
            found.append(normalized)
            continue
        if token in ignored_tail_tokens and not found:
            continue
        if found:
            break
        return {"suffix": "und", "label": "未知"}

    if not found:
        return {"suffix": "und", "label": "未知"}

    suffix = normalize_language_suffix("&".join(reversed(found)))
    label = LANGUAGE_PAIR_LABELS.get(suffix) or LANGUAGE_LABELS.get(suffix, "未知")
    return {"suffix": suffix, "label": label}


_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_KANA = re.compile(r"[\u3041-\u3096\u30a1-\u30fa\uff66-\uff9d]")
_HANGUL = re.compile(r"[\uac00-\ud7a3\u1100-\u11ff\u3131-\u318e]")
_SIMPLIFIED = re.compile(r"[这们吗让个给说还为从没对]")
_TRADITIONAL = re.compile(r"[這們嗎讓個給說還為從沒對]")
_LATIN_WORD = re.compile(r"[^\W\d_]+")
_LATIN_LANGUAGE_WORDS = {
    "eng": set("the and you your are was were have not that this with what when where will would can for our they we my me it is to of in".split()),
    "fre": set("vous nous avec dans pourquoi mais cette parce merci vraiment bonjour alors".split()),
    "spa": set("pero usted ustedes gracias cómo dónde entonces nosotros estamos quiero puedo contigo".split()),
    "por": set("não você vocês então também mesmo ninguém com isto isso vamos obrigado aconteceu".split()),
    "ger": set("ich nicht sind wir euch ihnen wirklich warum haben noch dich und bitte".split()),
    "ita": set("sono siete questo questa quello allora perché davvero grazie nella abbiamo voglio".split()),
}


def detect_language_profile(file_name: str, raw_bytes: bytes, subtitle_exts: Iterable[str]) -> Dict[str, str]:
    declared = language_suffix_from_filename(file_name, subtitle_exts)
    if not raw_bytes or is_bitmap_subtitle(file_name, raw_bytes):
        return declared
    try:
        extension = Path(file_name).suffix.lower()
        if extension in {".smi", ".sami", ".ttml", ".dfxp"}:
            import pysubs2

            load = load_sami_subtitles if extension in {".smi", ".sami"} else load_ttml_subtitles
            subtitles = load(pysubs2, raw_bytes)
            dialogue = "\n".join(event.plaintext for event in subtitles if event.end > event.start)
        else:
            text, _ = decode_subtitle_bytes(raw_bytes)
            dialogue = _subtitle_dialogue_text(file_name, text)
    except (ValueError, ET.ParseError, KeyError, NotImplementedError):
        return {"suffix": "und", "label": "未知"}
    if not dialogue.strip():
        return {"suffix": "und", "label": "未知"}

    suffix = _dialogue_language_suffix(dialogue, declared["suffix"])
    label = LANGUAGE_PAIR_LABELS.get(suffix) or LANGUAGE_LABELS.get(suffix)
    if label is None:
        label = "中外双语" if is_chinese_language_suffix(suffix) and "&" in suffix else "未知"
    return {"suffix": suffix, "label": label}


def _dialogue_language_suffix(dialogue: str, declared: str) -> str:
    lines = [line.strip() for line in dialogue.splitlines() if line.strip()]
    han_count = len(_HAN.findall(dialogue))
    kana_count = len(_KANA.findall(dialogue))
    hangul_count = len(_HANGUL.findall(dialogue))
    han_only_lines = [line for line in lines if not (_KANA.search(line) or _HANGUL.search(line))]
    han_only = "\n".join(han_only_lines)
    simplified_markers = _SIMPLIFIED.findall(han_only)
    traditional_markers = _TRADITIONAL.findall(han_only)
    simplified = len(simplified_markers)
    traditional = len(traditional_markers)
    separated_han = len(_HAN.findall(han_only))

    # Japanese names and Korean Hanja can also occupy whole lines.
    has_chinese = han_count >= 8 and (
        max(kana_count, hangul_count) < 4
        or (separated_han >= 20 and len(set(simplified_markers + traditional_markers)) >= 2)
        or (separated_han >= 20 and separated_han * 3 >= han_count
            and sum(len(_HAN.findall(line)) >= 6 for line in han_only_lines) >= 2)
    )
    latin_suffix = _latin_dialogue_language(lines)
    if not latin_suffix and "rus" in declared.split("&") and len(re.findall(r"[А-Яа-яЁё]", dialogue)) >= 8:
        latin_suffix = "rus"

    if has_chinese:
        # A few Chinese signs or isolated foreign words do not establish a second dialogue language.
        if latin_suffix and (han_count < 20 or sum(len(_HAN.findall(line)) >= 4 for line in lines) < 2):
            return latin_suffix
        foreign = set()
        if han_count >= 20:
            for script, count, suffix in ((_KANA, kana_count, "jp"), (_HANGUL, hangul_count, "kr")):
                if count >= 20 and sum(len(script.findall(line)) >= 4 for line in lines) >= 2:
                    foreign.add(suffix)
            if latin_suffix:
                foreign.add(latin_suffix)
        if len(foreign) > 1:
            return "und"
        use_traditional = traditional > simplified if simplified + traditional >= 3 else "cht" in declared.split("&")
        chinese = "cht" if use_traditional else "chi"
        return f"{chinese}&{next(iter(foreign))}" if foreign else chinese

    if max(kana_count, hangul_count) >= 4:
        if min(kana_count, hangul_count) >= 4:
            return "und"
        return "jpn" if kana_count > hangul_count else "kor"
    return latin_suffix or "und"


def _latin_dialogue_language(lines: List[str]) -> str:
    evidence: Dict[str, set[str]] = {code: set() for code in _LATIN_LANGUAGE_WORDS}
    for line in lines:
        words = _LATIN_WORD.findall(line.casefold())
        if len(words) < 4:
            continue
        distinct = set(words)
        for code, markers in _LATIN_LANGUAGE_WORDS.items():
            if len(distinct & markers) >= 2:
                evidence[code].add(" ".join(words))
    scores = sorted(((len(matches), code) for code, matches in evidence.items()), reverse=True)
    count, code = scores[0]
    runner_up = scores[1][0]
    return code if count >= 3 and (runner_up < 3 or count >= 2 * runner_up) else ""


def _subtitle_dialogue_text(file_name: str, text: str) -> str:
    """Remove common subtitle metadata so format headers cannot imply languages."""
    extension = Path(file_name).suffix.lower()
    if re.match(r"(?is)^\s*(?:<!doctype\s+html\b|<html\b)", text):
        return ""
    if extension in {".vtt", ".webvtt"}:
        dialogue = []
        for block in re.split(r"\r?\n[ \t]*\r?\n", text):
            rows = block.strip().splitlines()
            if not rows or re.match(r"^(?:NOTE|STYLE|REGION|WEBVTT)(?:\s|$)", rows[0]):
                continue
            for index, row in enumerate(rows):
                if re.match(r"^\d{2,}:\d{2}(?::\d{2})?\.\d{3}\s+-->", row):
                    dialogue.extend(rows[index + 1:])
                    break
        text = "\n".join(dialogue)
    elif extension == ".sub":
        text = text.replace("|", "\n")
    elif extension == ".sbv":
        text = re.sub(r"(?m)^\d+:\d{2}:\d{2}\.\d{3},\d+:\d{2}:\d{2}\.\d{3}\s*$", "", text)
    if extension in {".ass", ".ssa"}:
        in_events = False
        dialogue = []
        field_count = 10
        text_index = 9
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.lower() == "[events]":
                in_events = True
                continue
            if stripped.startswith("[") and stripped.endswith("]"):
                in_events = False
                continue
            if in_events and stripped.lower().startswith("format:"):
                fields = [field.strip().lower() for field in stripped.split(":", 1)[1].split(",")]
                if "text" in fields:
                    field_count = len(fields)
                    text_index = fields.index("text")
            elif in_events and stripped.lower().startswith("dialogue:"):
                payload = stripped.split(":", 1)[1]
                fields = payload.split(",", field_count - 1)
                if len(fields) == field_count:
                    dialogue.append(fields[text_index])
        text = "\n".join(dialogue)
        text = re.sub(r"\\[Nn]", "\n", text)
    text = re.sub(r"(?m)^\s*\d+\s*$", " ", text)
    text = re.sub(r"(?m)^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{2,3}\s*-->.*$", " ", text)
    text = re.sub(r"\{[^}]*\}|<[^>]*>", " ", text)
    return text


def is_chinese_language_suffix(suffix: Any) -> bool:
    return any(part in {"chi", "cht"} for part in normalize_language_suffix(suffix).split("&"))


def target_has_chinese_subtitle(
    target: Dict[str, Any],
    *,
    is_chinese_language_suffix_func: Callable[[Any], bool] = is_chinese_language_suffix,
) -> bool:
    subtitles = target.get("subtitles") or []
    if any(
        is_chinese_language_suffix_func((item or {}).get("language_suffix") or (item or {}).get("language"))
        for item in subtitles
        if isinstance(item, dict)
    ):
        return True
    embedded_subtitles = target.get("embedded_subtitles") or []
    return any(
        bool((item or {}).get("is_chinese"))
        or is_chinese_language_suffix_func((item or {}).get("language_suffix") or (item or {}).get("language"))
        for item in embedded_subtitles
        if isinstance(item, dict)
    )


def auto_target_has_chinese_subtitle(
    entry: Dict[str, Any],
    target: Dict[str, Any],
    *,
    subtitle_inventory: Any,
    is_chinese_language_suffix_func: Callable[[Any], bool] = is_chinese_language_suffix,
    language_priority: Optional[List[str]] = None,
    strict: bool = False,
) -> bool:
    required = required_auto_languages(language_priority, strict=strict)

    if target_satisfies_auto_subtitle_policy(target, required, is_chinese_language_suffix_func):
        return True
    embedded_subtitles = subtitle_inventory.embedded_subtitle_tracks_for_target(entry)
    if embedded_subtitles:
        target["has_embedded_subtitle"] = True
        target["embedded_subtitle_count"] = len(embedded_subtitles)
        target["embedded_subtitles"] = embedded_subtitles
    return target_satisfies_auto_subtitle_policy(target, required, is_chinese_language_suffix_func)


def target_satisfies_auto_subtitle_policy(
    target: Dict[str, Any],
    required: Optional[List[str]],
    is_chinese_language_suffix_func: Callable[[Any], bool] = is_chinese_language_suffix,
) -> bool:
    subtitles = target.get("subtitles")
    video_stem = normalize_text(target.get("basename"))
    if subtitles and video_stem:
        target = {**target, "subtitles": [
            item for item in subtitles
            if isinstance(item, dict)
            and not subtitle_filename_has_forced_tag(item.get("name") or item.get("path"), video_stem)
        ]}
    if required is not None:
        return target_has_preferred_subtitle(target, required)
    return target_has_chinese_subtitle(target, is_chinese_language_suffix_func=is_chinese_language_suffix_func)


def auto_language_bucket(suffix: Any) -> str:
    parts = [item for item in normalize_language_suffix(suffix).split("&") if item]
    if len(parts) == 2 and sum(item in {"chi", "cht"} for item in parts) == 1:
        other = next(item for item in parts if item not in {"chi", "cht"})
        if other in {"eng", "en"}:
            return "chi&eng"
        if other in {"jp", "jpn"}:
            return "chi&jp"
        if other in {"kr", "kor"}:
            return "chi&kr"
        return "other_bilingual"
    if len(parts) > 1:
        return "und"
    if "chi" in parts:
        return "chi"
    if "cht" in parts:
        return "cht"
    if any(item in {"eng", "en"} for item in parts):
        return "eng"
    return parts[0] if parts else "und"


def auto_subtitle_sort_key(
    item: Dict[str, Any],
    *,
    language_priority: Optional[List[str]] = None,
    format_priority: Optional[List[str]] = None,
) -> Tuple[int, int, int, str]:
    language_bucket = auto_language_bucket(item.get("language_suffix"))
    language_order = normalize_auto_language_priority(language_priority)
    format_order = list(format_priority or DEFAULT_AUTO_FORMAT_PRIORITY)
    try:
        language_rank = language_order.index(language_bucket)
    except ValueError:
        language_rank = len(language_order)
    ext = normalize_text(item.get("ext")).lower()
    try:
        format_rank = format_order.index(ext)
    except ValueError:
        format_rank = len(format_order)
    bilingual_bonus = 0 if "&" in language_bucket else 1
    return (language_rank, format_rank, bilingual_bonus, normalize_text(item.get("source_name")).lower())


def autosub_lang_from_suffix(suffix: Any) -> str:
    normalized = normalize_language_suffix(suffix)
    first = next((part for part in normalized.split("&") if part and part not in {"chi", "cht"}), "")
    return {
        "eng": "en",
        "en": "en",
        "jpn": "ja",
        "jp": "ja",
        "kor": "ko",
        "kr": "ko",
        "fre": "fr",
        "fra": "fr",
        "spa": "es",
        "ger": "de",
        "deu": "de",
        "por": "pt",
        "ita": "it",
        "rus": "ru",
    }.get(first, first or "en")


def normalize_auto_language_key(value: Any) -> str:
    text = normalize_text(value).lower()
    aliases = {
        "双语": "bilingual",
        "bilingual": "bilingual",
        "简中": "chi",
        "简体": "chi",
        "中文": "chi",
        "zh": "chi",
        "chs": "chi",
        "chi": "chi",
        "繁中": "cht",
        "繁体": "cht",
        "zh-tw": "cht",
        "cht": "cht",
        "英文": "eng",
        "英语": "eng",
        "en": "eng",
        "eng": "eng",
        "中英双语": "chi&eng",
        "简英双语": "chi&eng",
        "繁英双语": "cht&eng",
        "中日双语": "chi&jp",
        "中韩双语": "chi&kr",
        "其他中外双语": "other_bilingual",
    }
    normalized = aliases.get(text, text if text == "other_bilingual" else normalize_language_suffix(text))
    if "&" in normalized:
        return auto_language_bucket(normalized)
    return normalized


def normalize_auto_language_priority(
    value: Any,
    default_priority: Optional[List[str]] = None,
) -> List[str]:
    defaults = list(DEFAULT_AUTO_LANGUAGE_PRIORITY if default_priority is None else default_priority)
    if value is None:
        return defaults
    raw_items = value
    if isinstance(raw_items, str):
        raw_items = re.split(r"[\s,，/|]+", raw_items)
    if not isinstance(raw_items, list):
        return []
    result: List[str] = []
    for item in raw_items:
        key = normalize_auto_language_key(item)
        keys = LEGACY_BILINGUAL_BUCKETS if key == "bilingual" else [key]
        for normalized in keys:
            if normalized and normalized != "und" and normalized not in result:
                result.append(normalized)
    return result


def language_matches_preferences(suffix: Any, priority: Optional[List[str]]) -> bool:
    bucket = auto_language_bucket(suffix)
    return bucket != "und" and bucket in normalize_auto_language_priority(priority)


def required_auto_languages(priority: Optional[List[str]], *, strict: bool = False) -> Optional[List[str]]:
    selected = normalize_auto_language_priority(priority)
    bilingual = [item for item in selected if item in LEGACY_BILINGUAL_BUCKETS]
    # Selecting bilingual subtitles is a requirement even when monolingual fallbacks are selected.
    if bilingual:
        return bilingual
    return selected if strict else None


def online_result_language_bucket(item: Dict[str, Any]) -> str:
    title = normalize_text(item.get("title"))
    language = normalize_text(item.get("language"))
    title_bucket = auto_language_bucket(language_profile_from_text(title)["suffix"])
    language_bucket = auto_language_bucket(language_profile_from_text(language)["suffix"])
    for bucket in (title_bucket, language_bucket):
        if bucket in LEGACY_BILINGUAL_BUCKETS:
            return bucket
    # A generic bilingual label does not identify a pair or prove a Chinese track.
    if re.search(r"双语|雙語|\bbilingual\b|\bbi-lingual\b|\bdual[ -]language\b", f"{language} {title}", re.I):
        return "und"
    bucket = language_bucket if language_bucket != "und" else title_bucket
    if bucket in {"chi", "cht", "eng", "jpn", "kor", "und"}:
        return bucket
    return "other"


def auto_candidate_language_bucket(item: Dict[str, Any]) -> str:
    bucket = online_result_language_bucket(item)
    if bucket in LEGACY_BILINGUAL_BUCKETS:
        return bucket
    if (item.get("provider") == "subtitlecat" and item.get("translation_source") == "original_upload"
            and re.search(r"\btrack\d+[\s_.-]*\[eng\]\s*$", normalize_text(item.get("title")).replace("_", " "), re.I)):
        return "eng"
    # Provider language fields are coarse: a Chinese result can contain Chinese-English subtitles.
    return "und"


def auto_candidate_language_rank(item: Dict[str, Any], priority: Optional[List[str]]) -> Tuple[int, int]:
    bucket = auto_candidate_language_bucket(item)
    language_order = normalize_auto_language_priority(priority)
    if bucket in language_order:
        return 0, language_order.index(bucket)
    return (1, len(language_order)) if bucket == "und" else (2, len(language_order))


def auto_rank_candidates(
    candidates: List[Dict[str, Any]], priority: Optional[List[str]], *, strict: bool
) -> List[Dict[str, Any]]:
    required = required_auto_languages(priority, strict=strict)
    if required == []:
        return []
    allowed = []
    for item in candidates:
        bucket = auto_candidate_language_bucket(item)
        if required is None or bucket in required or bucket == "und":
            allowed.append(item)
    return sorted(allowed, key=lambda item: auto_candidate_language_rank(item, required if required is not None else priority))


def target_has_preferred_subtitle(target: Dict[str, Any], language_priority: Optional[List[str]]) -> bool:
    subtitles = [*(target.get("subtitles") or []), *(target.get("embedded_subtitles") or [])]
    return any(
        isinstance(item, dict)
        and language_matches_preferences(item.get("language_suffix") or item.get("language"), language_priority)
        for item in subtitles
    )


def language_profile_from_text(value: Any) -> Dict[str, str]:
    text = normalize_text(value).lower()
    languages = set()
    explicit_chinese = False
    explicit_traditional = False
    for marker in ("中英", "简英", "繁英", "中日", "中韩", "中法", "中西", "中德"):
        if marker in text:
            languages.add("eng" if marker in {"中英", "简英", "繁英"} else {"中日": "jp", "中韩": "kr", "中法": "fre", "中西": "spa", "中德": "ger"}[marker])
            explicit_chinese = True
            explicit_traditional = marker == "繁英"
    for name, suffix in LANGUAGE_SUFFIX_ALIASES.items():
        if name.endswith("双语") and name in text:
            chinese, foreign = suffix.split("&")
            languages.add(foreign)
            explicit_chinese = True
            explicit_traditional = explicit_traditional or chinese == "cht"
    if any(marker in text for marker in ("中文", "简体", "简中", "繁体", "繁中", "chinese", "chs", "cht", "zh-cn", "zh-hans", "zh-hant")):
        explicit_chinese = True
        explicit_traditional = explicit_traditional or any(marker in text for marker in ("繁体", "繁中", "cht", "zh-hant"))
    if explicit_chinese:
        languages.add("cht" if explicit_traditional else "chi")

    token_text = re.sub(r"(?<=[a-z])[_+/,]+(?=[a-z])", "&", text)
    token_text = re.sub(r"(?<=chinese)-(?=english)|(?<=english)-(?=chinese)", "&", token_text)
    for token in re.findall(r"(?<![a-z0-9-])(?:zh-hans|zh-hant|zh-cn|zh-tw|chs|cht|zho|cmn|chi|zh|english|eng|en|jpn|jap|japanese|ja|jp|kor|korean|ko|kr|fre|fra|french|fr|spa|spanish|es|ger|deu|german|de)(?![a-z0-9-])", token_text):
        if token in {"zh-hans", "zh-cn", "chs", "zho", "cmn", "chi", "zh"}:
            languages.add("chi")
        elif token in {"zh-hant", "zh-tw", "cht"}:
            languages.add("cht")
        else:
            languages.add(LANGUAGE_PAIR_ALIASES.get(token, ""))
    languages.discard("")

    for marker, code in (("英文", "eng"), ("英语", "eng"), ("日文", "jp"), ("日语", "jp"), ("韩文", "kr"), ("韩语", "kr"), ("法文", "fre"), ("法语", "fre")):
        if marker in text:
            languages.add(code)
    if len(languages) == 1:
        suffix = next(iter(languages))
    elif len(languages) == 2 and any(item in {"chi", "cht"} for item in languages):
        suffix = "&".join(sorted(languages))
    else:
        suffix = "und"
    suffix = normalize_language_suffix(suffix)
    label = LANGUAGE_PAIR_LABELS.get(suffix) or LANGUAGE_LABELS.get(suffix, "未知")
    return {"suffix": suffix, "label": label}


def normalize_auto_format_priority(
    value: Any,
    subtitle_exts: Iterable[str],
    default_priority: Optional[List[str]] = None,
) -> List[str]:
    raw_items = value
    if isinstance(raw_items, str):
        raw_items = re.split(r"[\s,，/|]+", raw_items)
    if not isinstance(raw_items, list):
        raw_items = []
    result: List[str] = []
    subtitle_ext_set = set(subtitle_exts)
    for item in raw_items:
        ext = normalize_text(item).lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = f".{ext}"
        if ext in subtitle_ext_set and ext not in result:
            result.append(ext)
    for item in list(default_priority or DEFAULT_AUTO_FORMAT_PRIORITY):
        if item not in result:
            result.append(item)
    for item in sorted(subtitle_ext_set):
        if item not in result:
            result.append(item)
    return result
