from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterable, List, Tuple

from .target_normalizers import NormalizeText, SafeInt, _default_normalize_text


TitleAliasExtractor = Callable[[Any], List[str]]


def _latin_title(text: str) -> bool:
    return bool(text and re.search(r"[A-Za-z]", text) and not re.search(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", text))


def _chinese_title(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text) and not re.search(r"[\u3040-\u30ff\uac00-\ud7af]", text))


def english_title_from_aliases(aliases: List[str]) -> str:
    for item in aliases or []:
        if _latin_title(item):
            return item
    return ""


def tmdb_aliases(
    *values: Any,
    extract_title_aliases_func: TitleAliasExtractor,
) -> List[str]:
    aliases: List[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
            return
        aliases.extend(extract_title_aliases_func(value))

    for value in values:
        walk(value)
    result: List[str] = []
    seen = set()
    for item in aliases:
        key = item.lower()
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return result[:80]


def _tmdb_title_records(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _tmdb_title_records(item)
    elif isinstance(value, dict):
        yield value
        for key in ("data", "titles", "results", "translations", "alternative_titles", "aliases"):
            yield from _tmdb_title_records(value.get(key))


def _localized_tmdb_title(
    values: Iterable[Any],
    *,
    locale_priority: Callable[[str, str], int],
    valid_title: Callable[[str], bool],
    extract_title_aliases_func: TitleAliasExtractor,
    normalize_text: NormalizeText,
) -> str:
    candidates: List[Tuple[int, str]] = []
    for record in _tmdb_title_records(tuple(values)):
        language = normalize_text(record.get("iso_639_1")).lower()
        country = normalize_text(record.get("iso_3166_1")).lower()
        priority = locale_priority(language, country)
        if priority:
            data = record.get("data")
            if isinstance(data, dict):
                titles = extract_title_aliases_func({key: data.get(key) for key in ("title", "name")})
            else:
                titles = []
            titles.extend(extract_title_aliases_func({key: record.get(key) for key in ("title", "name")}))
            candidates.extend((priority, title) for title in titles if valid_title(title))
    return max(candidates, key=lambda item: item[0], default=(0, ""))[1]


def english_title_from_tmdb_values(
    *values: Any,
    extract_title_aliases_func: TitleAliasExtractor,
    normalize_text: NormalizeText = _default_normalize_text,
) -> str:
    return _localized_tmdb_title(
        values,
        locale_priority=lambda language, country: int(language == "en" or country in {"us", "gb", "uk"}),
        valid_title=_latin_title,
        extract_title_aliases_func=extract_title_aliases_func,
        normalize_text=normalize_text,
    )


def _chinese_locale_priority(language: str, country: str) -> int:
    if language and language not in {"zh", "cn", "cmn", "zh-cn", "zh-hans", "zh-tw", "zh-hant", "yue"}:
        return 0
    if country == "cn" or language in {"zh-cn", "zh-hans"}:
        return 2
    return int(bool(language) or country in {"tw", "hk"})


def tmdb_detail_payload(
    detail: Any,
    *,
    extract_title_aliases_func: TitleAliasExtractor,
    normalize_text: NormalizeText = _default_normalize_text,
) -> Dict[str, Any]:
    if not detail:
        return {}

    def value(*keys: str) -> Any:
        for key in keys:
            if isinstance(detail, dict) and key in detail:
                return detail.get(key)
            if hasattr(detail, key):
                return getattr(detail, key)
        return None

    translations = value("translations")
    alternative_titles = value("alternative_titles")
    aliases = tmdb_aliases(
        translations,
        alternative_titles,
        extract_title_aliases_func=extract_title_aliases_func,
    )
    local_title = normalize_text(value("title") or value("name"))
    original_language = normalize_text(value("original_language")).lower()
    original_title = normalize_text(value("original_title") or value("original_name"))
    chinese_title = _localized_tmdb_title(
        (translations, alternative_titles),
        locale_priority=_chinese_locale_priority,
        valid_title=_chinese_title,
        extract_title_aliases_func=extract_title_aliases_func,
        normalize_text=normalize_text,
    )
    if not chinese_title and _chinese_title(local_title):
        if original_language not in {"ja", "ko"} or local_title != original_title:
            chinese_title = local_title
    return {
        "media_source": normalize_text(value("media_source", "source")),
        "media_id": normalize_text(value("media_id", "tmdb_id", "tmdbid", "id")),
        "tmdb_id": normalize_text(value("tmdb_id", "tmdbid", "media_id", "id")),
        "poster_url": normalize_text(value("poster_path", "poster_url", "image")),
        "backdrop_url": normalize_text(value("backdrop_path", "backdrop_url")),
        "overview": normalize_text(value("overview")),
        "vote_average": value("vote_average"),
        "original_language": value("original_language") or "",
        "origin_country": value("origin_country") or [],
        "production_countries": value("production_countries") or [],
        "original_title": value("original_title", "original_name") or "",
        "zh_title": chinese_title,
        "en_title": english_title_from_tmdb_values(
            translations,
            alternative_titles,
            extract_title_aliases_func=extract_title_aliases_func,
            normalize_text=normalize_text,
        ) or english_title_from_aliases(aliases),
        "tmdb_aliases": aliases,
    }


def apply_tmdb_detail(target: Dict[str, Any], detail: Dict[str, Any]) -> None:
    for key in [
        "media_source", "media_id", "tmdb_id", "poster_url", "backdrop_url", "overview",
        "vote_average", "original_language", "origin_country", "production_countries",
        "original_title", "tmdb_aliases", "zh_title",
    ]:
        value = detail.get(key)
        if value not in (None, "", [], {}) and not target.get(key):
            target[key] = value
    if detail.get("en_title") and not target.get("en_title"):
        target["en_title"] = detail["en_title"]


def flatten_media_values(
    value: Any,
    keys: Tuple[str, ...] = (),
    *,
    normalize_text: NormalizeText = _default_normalize_text,
) -> List[str]:
    values: List[str] = []

    def walk(item: Any) -> None:
        if item is None:
            return
        if isinstance(item, dict):
            for key in keys or ("iso_3166_1", "iso_639_1", "code", "value", "name", "english_name"):
                if key in item:
                    walk(item.get(key))
            return
        if isinstance(item, (list, tuple, set)):
            for child in item:
                walk(child)
            return
        text = normalize_text(item)
        if not text:
            return
        for part in re.split(r"[,/|]+", text):
            clean = normalize_text(part).lower().replace("_", "-")
            if clean:
                values.append(clean)

    walk(value)
    return values


def is_chinese_language_code(
    value: Any,
    *,
    normalize_text: NormalizeText,
    chinese_language_codes: Iterable[str],
) -> bool:
    codes = set(chinese_language_codes)
    code = normalize_text(value).lower().replace("_", "-")
    base = re.split(r"[-\s]+", code, maxsplit=1)[0] if code else ""
    return code in codes or base in codes


def is_chinese_country_value(
    value: Any,
    *,
    normalize_text: NormalizeText,
    chinese_country_codes: Iterable[str],
    chinese_region_names: Iterable[str],
) -> bool:
    country_codes = set(chinese_country_codes)
    region_names = set(chinese_region_names)
    text = normalize_text(value).lower().replace("_", "-")
    base = re.split(r"[-\s]+", text, maxsplit=1)[0] if text else ""
    return text in country_codes or base in country_codes or text in region_names


def chinese_category_evidence(
    entry: Dict[str, Any],
    *,
    normalize_text: NormalizeText,
    chinese_category_pattern: Any,
) -> str:
    values = []
    for key in ["library_name", "media_category", "media_category_name", "category", "category_name", "type_name"]:
        raw = entry.get(key)
        if isinstance(raw, (list, tuple, set)):
            values.extend(normalize_text(item) for item in raw)
        else:
            values.append(normalize_text(raw))
    text = " ".join(item for item in values if item)
    if text and chinese_category_pattern.search(text):
        return "MP 分类/库名包含华语标识"
    return ""


def auto_media_for_entry(
    entry: Dict[str, Any],
    *,
    tmdb_detail_for_media_func: Callable[[Dict[str, Any]], Dict[str, Any]],
) -> Dict[str, Any]:
    media = {
        "media_type": entry.get("media_type"),
        "title": entry.get("title"),
        "year": entry.get("year"),
        "tmdb_id": entry.get("tmdb_id"),
        "douban_id": entry.get("douban_id"),
        "original_language": entry.get("original_language"),
        "origin_country": entry.get("origin_country"),
        "production_countries": entry.get("production_countries"),
        "original_title": entry.get("original_title") or entry.get("original_name"),
        "en_title": entry.get("en_title"),
        "zh_title": entry.get("zh_title"),
        "tmdb_aliases": entry.get("tmdb_aliases"),
    }
    tmdb_detail = tmdb_detail_for_media_func(media)
    if tmdb_detail:
        apply_tmdb_detail(media, tmdb_detail)
        apply_tmdb_detail(entry, tmdb_detail)
    return media


def is_chinese_transfer_media(
    entry: Dict[str, Any],
    *,
    auto_media_for_entry_func: Callable[[Dict[str, Any]], Dict[str, Any]],
    normalize_text: NormalizeText,
    chinese_language_codes: Iterable[str],
    chinese_country_codes: Iterable[str],
    chinese_region_names: Iterable[str],
    chinese_category_pattern: Any,
) -> Tuple[bool, str]:
    category_reason = chinese_category_evidence(
        entry,
        normalize_text=normalize_text,
        chinese_category_pattern=chinese_category_pattern,
    )
    if category_reason:
        return True, category_reason

    media = auto_media_for_entry_func(entry)
    languages = flatten_media_values(
        media.get("original_language"),
        ("iso_639_1", "code", "value", "name", "english_name"),
        normalize_text=normalize_text,
    )
    for language in languages:
        if is_chinese_language_code(
            language,
            normalize_text=normalize_text,
            chinese_language_codes=chinese_language_codes,
        ):
            return True, f"TMDB original_language={language}"

    country_values = [
        *flatten_media_values(
            media.get("origin_country"),
            ("iso_3166_1", "code", "value", "name", "english_name"),
            normalize_text=normalize_text,
        ),
        *flatten_media_values(
            media.get("production_countries"),
            ("iso_3166_1", "code", "value", "name", "english_name"),
            normalize_text=normalize_text,
        ),
    ]
    for country in country_values:
        if is_chinese_country_value(
            country,
            normalize_text=normalize_text,
            chinese_country_codes=chinese_country_codes,
            chinese_region_names=chinese_region_names,
        ):
            return True, f"TMDB country={country}"

    if media.get("tmdb_id"):
        return False, "TMDB 未提供中文语种/地区证据"
    return False, "中文识别证据不足"


class MediaMetadataService:
    def __init__(
        self,
        *,
        tmdb_chain_factory: Any,
        media_type_tv: Any,
        media_type_movie: Any,
        tmdb_detail_cache: Dict[str, Dict[str, Any]],
        logger_warning: Callable[..., None],
        normalize_text: NormalizeText,
        safe_int: SafeInt,
        media_type_text_func: Callable[[Any], str],
        extract_title_aliases_func: TitleAliasExtractor,
        chinese_language_codes: Iterable[str],
        chinese_country_codes: Iterable[str],
        chinese_region_names: Iterable[str],
        chinese_category_pattern: Any,
    ) -> None:
        self._tmdb_chain_factory = tmdb_chain_factory
        self._media_type_tv = media_type_tv
        self._media_type_movie = media_type_movie
        self._tmdb_detail_cache = tmdb_detail_cache
        self._logger_warning = logger_warning
        self._normalize_text = normalize_text
        self._safe_int = safe_int
        self._media_type_text = media_type_text_func
        self._extract_title_aliases = extract_title_aliases_func
        self._chinese_language_codes = set(chinese_language_codes)
        self._chinese_country_codes = set(chinese_country_codes)
        self._chinese_region_names = set(chinese_region_names)
        self._chinese_category_pattern = chinese_category_pattern

    def tmdb_detail_for_media(self, media: Dict[str, Any]) -> Dict[str, Any]:
        tmdb_id = self._safe_int(media.get("tmdb_id"), 0)
        media_type = self._media_type_text(media.get("media_type"))
        if not tmdb_id or self._tmdb_chain_factory is None:
            return {}
        cache_key = f"{media_type}:{tmdb_id}"
        if cache_key in self._tmdb_detail_cache:
            return dict(self._tmdb_detail_cache[cache_key])
        try:
            mp_type = self._media_type_tv if media_type == "tv" else self._media_type_movie
            detail = self._tmdb_chain_factory().tmdb_info(tmdbid=tmdb_id, mtype=mp_type)
        except TypeError:
            try:
                mp_type = self._media_type_tv if media_type == "tv" else self._media_type_movie
                detail = self._tmdb_chain_factory().tmdb_info(tmdb_id=tmdb_id, mtype=mp_type)
            except Exception as exc:
                self._logger_warning("[JavSubtitleAssistant] 读取 TMDB 详情失败 tmdb=%s type=%s error=%s", tmdb_id, media_type, exc)
                return {}
        except Exception as exc:
            self._logger_warning("[JavSubtitleAssistant] 读取 TMDB 详情失败 tmdb=%s type=%s error=%s", tmdb_id, media_type, exc)
            return {}
        payload = self.tmdb_detail_payload(detail)
        self._tmdb_detail_cache[cache_key] = payload
        return dict(payload)

    def enrich_media(self, media: Dict[str, Any]) -> Dict[str, Any]:
        if media.get("jav_number"):
            return dict(media)
        """补全无媒体 ID 的本地条目，复用 MoviePilot 的 TMDB Chain 和缓存。"""
        result = dict(media or {})
        title = self._normalize_text(result.get("title"))
        media_type = self._media_type_text(result.get("media_type"))
        year = self._normalize_text(result.get("year"))
        if not title or media_type not in {"movie", "tv"}:
            result["metadata_resolution"] = "unresolved"
            return result

        if not self._safe_int(result.get("tmdb_id"), 0) and self._tmdb_chain_factory is not None:
            cache_key = f"match:{media_type}:{title.casefold()}:{year}"
            cached = self._tmdb_detail_cache.get(cache_key)
            if cached is None:
                try:
                    mp_type = self._media_type_tv if media_type == "tv" else self._media_type_movie
                    matched = self._tmdb_chain_factory().match_tmdbinfo(
                        name=title,
                        mtype=mp_type,
                        year=year or None,
                        season=self._safe_int(result.get("season"), 0) or None,
                    )
                    cached = self.tmdb_detail_payload(matched) if matched else {}
                    if cached and not cached.get("tmdb_id"):
                        matched_id = matched.get("tmdb_id") if isinstance(matched, dict) else getattr(matched, "tmdb_id", None)
                        if matched_id:
                            cached["tmdb_id"] = self._normalize_text(matched_id)
                            cached["media_id"] = cached["tmdb_id"]
                    if cached.get("tmdb_id"):
                        cached["media_source"] = "themoviedb"
                        cached["media_id"] = self._normalize_text(cached["tmdb_id"])
                except Exception as exc:
                    self._logger_warning(
                        "[JavSubtitleAssistant] MoviePilot 媒体匹配失败 title=%s year=%s type=%s error=%s",
                        title, year or "-", media_type, exc,
                    )
                    cached = {}
                self._tmdb_detail_cache[cache_key] = cached
            if cached:
                apply_tmdb_detail(result, cached)

        if self._safe_int(result.get("tmdb_id"), 0) and not result.get("poster_url"):
            detail = self.tmdb_detail_for_media(result)
            if detail:
                apply_tmdb_detail(result, detail)
        tmdb_id = self._safe_int(result.get("tmdb_id"), 0)
        if tmdb_id:
            result["media_source"] = "themoviedb"
            result["media_id"] = str(tmdb_id)
            result["tmdb_id"] = tmdb_id
        result["metadata_resolution"] = "resolved" if tmdb_id else "unresolved"
        return result

    def tmdb_detail_payload(self, detail: Any) -> Dict[str, Any]:
        return tmdb_detail_payload(
            detail,
            extract_title_aliases_func=self._extract_title_aliases,
            normalize_text=self._normalize_text,
        )

    def auto_media_for_entry(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        return auto_media_for_entry(entry, tmdb_detail_for_media_func=self.tmdb_detail_for_media)

    def is_chinese_transfer_media(self, entry: Dict[str, Any]) -> Tuple[bool, str]:
        return is_chinese_transfer_media(
            entry,
            auto_media_for_entry_func=self.auto_media_for_entry,
            normalize_text=self._normalize_text,
            chinese_language_codes=self._chinese_language_codes,
            chinese_country_codes=self._chinese_country_codes,
            chinese_region_names=self._chinese_region_names,
            chinese_category_pattern=self._chinese_category_pattern,
        )


__all__ = [name for name in globals() if not name.startswith("__")]
