from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from math import log10
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..matching.subtitle_language import (
    auto_candidate_language_bucket,
    auto_candidate_language_rank,
    auto_rank_candidates,
    required_auto_languages,
)
from .keyword_builder import (
    _is_ambiguous_single_title_alias,
    _is_cjk_text,
    _is_generic_language_alias,
    _normalize_title_for_match,
    _series_title_aliases,
    _strong_title_matches,
    _title_aliases,
    _unique_keywords,
)
from .language import _guess_language_label, _language_priority
from .catalog import PROVIDER_SPECS


def _episode_from_text(value: str) -> Optional[Tuple[int, int]]:
    text = value or ""
    patterns = [
        re.compile(r"(?i)\bS(?P<season>\d{1,2})[\s._-]*E(?P<episode>\d{1,3})\b"),
        re.compile(r"(?i)\b(?P<season>\d{1,2})x(?P<episode>\d{1,3})\b"),
        re.compile(r"第\s*(?P<season>\d{1,2})\s*季.*?第\s*(?P<episode>\d{1,3})\s*[集话話]"),
        re.compile(r"第\s*(?P<episode>\d{1,3})\s*[集话話]"),
    ]
    for pattern in patterns:
        match = pattern.search(text)
        if not match:
            continue
        season = int(match.groupdict().get("season") or 0)
        episode = int(match.groupdict().get("episode") or 0)
        if episode:
            return season, episode
    return None


def _score_result(title: str, keyword: str, targets: List[Dict[str, Any]]) -> int:
    return int(_assess_result_match(title=title, keyword=keyword, targets=targets)["score"])


def _filter_relevant_results(
    results: Iterable[Any],
    keyword: str,
    targets: List[Dict[str, Any]],
) -> List[Any]:
    return [item for item in results if _has_relevance_signal(item.title, keyword, targets)]


def _has_relevance_signal(title: str, keyword: str, targets: List[Dict[str, Any]]) -> bool:
    return _assess_result_match(title=title, keyword=keyword, targets=targets)["identity_status"] != "failed"


def _provider_priority(item: Any) -> int:
    return PROVIDER_SPECS.get(item.provider, {}).get("priority", 0)


def _identity_priority(item: Any) -> int:
    if item.provider == "assrt" and not item.identity_status:
        return 30
    return {
        "strong": 30,
        "weak": 10,
        "failed": 0,
    }.get(item.identity_status or "", 0)


def online_search_sort_key(item: Any) -> Tuple[int, int, int, int, str]:
    return _provider_priority(item), _identity_priority(item), _language_priority(item), item.score, item.title


def _release_profile(value: str) -> Tuple[str, str]:
    value = value.replace("_", " ")
    source = re.search(
        r"\b(?:web[ .-]*dl|web[ .-]*rip|remux|blu[ .-]*ray|b[dr][ .-]*rip|hdtv|dvd[ .-]*rip)\b",
        value,
        re.I,
    )
    kind = re.sub(r"[ ._-]+", "", source.group(0).lower()) if source else ""
    if kind in {"bdrip", "brrip"}:
        kind = "bluray"
    resolution = re.search(r"\b(?:4320|2160|1080|720|576|480)[pi]\b", value, re.I)
    return kind, resolution.group(0).lower() if resolution else ""


_SUBTITLE_TYPE_ORDER = {"官方字幕": 0, "AI校对": 1, "其他来源": 2}
AUTO_DOWNLOAD_ATTEMPT_LIMIT = 5
AUTO_SEASON_ATTEMPT_LIMIT = 3
_RELEASE_EDITION = re.compile(
    r"(?P<theatrical>院线版|院線版|公映版|公映(?=[简繁雙双])|剧场版|劇場版|\btheatrical[ .-]+(?:cut|edition|version)\b)"
    r"|(?P<directors>导演剪辑版|導演剪輯版|\bdirector(?:['’]s|s)?[ .-]+cut\b)"
    r"|(?P<extended>加长版|加長版|延长版|延長版|\bextended[ .-]+(?:cut|edition|version)\b)",
    re.I,
)


def _release_edition(title: str) -> str:
    editions = {match.lastgroup for match in _RELEASE_EDITION.finditer(title.replace("_", " "))}
    return next(iter(editions)) if len(editions) == 1 else ""


def rank_auto_download_candidates(
    candidates: List[Dict[str, Any]],
    targets: List[Dict[str, Any]],
    language_priority: Optional[List[str]],
    *,
    strict: bool,
    min_score: Optional[int] = None,
    rejections: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    required = required_auto_languages(language_priority, strict=strict)
    priority = language_priority if required is None else required
    target_names = [
        str(target.get("basename") or target.get("filename") or target.get("label") or "")
        for target in targets
    ]
    target_profiles = {_release_profile(name) for name in target_names} - {("", "")}
    target_release = next(iter(target_profiles)) if len(target_profiles) == 1 else ("", "")
    target_editions = {_release_edition(name) for name in target_names}
    target_edition = next(iter(target_editions)) if len(target_editions) == 1 else ""

    eligible = []
    provider_order = {}
    identity_order = {}
    for item in candidates:
        edition = _release_edition(str(item.get("title") or ""))
        reason = ""
        if item.get("downloadable") is False:
            reason = "仅支持手动下载"
        elif min_score is not None and _safe_int(item.get("score")) < min_score:
            reason = f"匹配分低于 {min_score}"
        elif item.get("identity_status") == "failed":
            reason = "媒体身份不匹配"
        elif edition and target_edition and edition != target_edition:
            reason = "片源剪辑版本冲突"
        if reason:
            if rejections is not None:
                rejections[reason] = rejections.get(reason, 0) + 1
            continue
        provider = item.get("provider")
        identity = provider, item.get("identity_status")
        provider_order.setdefault(provider, len(provider_order))
        identity_order.setdefault(identity, len(identity_order))
        eligible.append(item)
    allowed = auto_rank_candidates(eligible, language_priority, strict=strict)
    if rejections is not None and len(allowed) < len(eligible):
        rejections["字幕语言不符合要求"] = len(eligible) - len(allowed)
    now = datetime.now(timezone.utc)

    def rank(item: Dict[str, Any]) -> Tuple[int, int, int, Tuple[int, int], int, float, int]:
        title = str(item.get("title") or "")
        release = _release_profile(title)
        edition = _release_edition(title)
        release_rank = 0
        if any(known and found and known != found for known, found in zip(target_release, release)):
            release_rank = -1
        elif (
            target_release[0] and target_edition == edition
            and all(not known or known == found for known, found in zip(target_release, release))
        ):
            release_rank = 1

        score = _safe_int(item.get("score"))
        count = item.get("download_count")
        download_bonus = min(3.0, 0.75 * log10(count + 1)) if type(count) is int and count >= 0 else 0.0
        uploaded_at = item.get("uploaded_at")
        time_bonus = 0.0
        if isinstance(uploaded_at, str) and uploaded_at:
            try:
                uploaded = datetime.fromisoformat(uploaded_at)
                if uploaded.utcoffset() is not None:
                    age = (now - uploaded).total_seconds() / 86400
                    if 0 <= age < 90:
                        time_bonus = 2.0 * (1.0 - age / 90)
            except (ValueError, OverflowError):
                pass
        total = score + download_bonus + time_bonus
        subtitle_type = item.get("subtitle_type") or "未标注"
        item["auto_rank"] = {
            "release": ("片源不同", "片源未确认", "片源匹配")[release_rank + 1],
            "subtitle_type": subtitle_type,
            "language_bucket": auto_candidate_language_bucket(item),
            "match": score,
            "downloads": count,
            "uploaded_at": uploaded_at,
            "download_bonus": round(download_bonus, 3),
            "time_bonus": round(time_bonus, 3),
            "total": round(total, 3),
        }
        provider = item.get("provider")
        return (
            -release_rank,
            provider_order[provider],
            _SUBTITLE_TYPE_ORDER.get(subtitle_type, len(_SUBTITLE_TYPE_ORDER)),
            auto_candidate_language_rank(item, priority),
            identity_order[provider, item.get("identity_status")],
            -total,
            -score,
        )

    return sorted((dict(item) for item in allowed), key=rank)


def log_auto_download_candidate(logger: Any, candidate: Dict[str, Any]) -> None:
    logger.info(
        "[SubtitleManualUpload] 自动字幕候选 provider=%s id=%s title=%s rank=%s",
        candidate.get("provider"),
        candidate.get("result_id"),
        candidate.get("title"),
        candidate["auto_rank"],
    )


def _target_year_from_targets(targets: List[Dict[str, Any]]) -> int:
    for target in targets or []:
        for field in ["year", "title", "basename", "filename", "path"]:
            years = _extract_years(str(target.get(field) or ""))
            if years:
                return years[0]
    return 0


def _target_episode_from_targets(targets: List[Dict[str, Any]]) -> int:
    episodes = {
        _safe_int(target.get("episode"), 0)
        for target in targets or []
        if _safe_int(target.get("episode"), 0)
    }
    return next(iter(episodes)) if len(episodes) == 1 else 0


def _first_target_value(targets: List[Dict[str, Any]], field: str) -> str:
    for target in targets or []:
        value = str(target.get(field) or "").strip()
        if value and value not in {"0", "None", "null"}:
            return value
    return ""


def _normalize_imdb_tt(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    match = re.search(r"tt\d{5,}", text)
    if match:
        return match.group(0)
    digits = re.sub(r"\D+", "", text)
    return f"tt{digits}" if digits else ""


def _normalize_imdb_for_opensubtitles(value: Any) -> str:
    imdb = _normalize_imdb_tt(value)
    return imdb[2:].lstrip("0") or "" if imdb else ""


def _episode_include_for_title(title: str, target_episode: int) -> Tuple[bool, bool]:
    if not target_episode:
        return True, False
    text = title or ""
    tag_re = re.compile(r"(?i)(S\d{1,2}\s*(?:E|EP)\d{1,3})|(\bEP?\d{1,3}\b)|(第\s*\d+\s*[集话話])")
    has_tag = bool(tag_re.search(text))
    tokens = [
        rf"(?i)\bS\d{{1,2}}\s*E0?{target_episode}\b",
        rf"(?i)\bE0?{target_episode}\b",
        rf"(?i)\bEP0?{target_episode}\b",
        rf"第\s*{target_episode}\s*[集话話]",
    ]
    matches = any(re.search(pattern, text) for pattern in tokens)
    return (not has_tag or matches), not has_tag


def _years_from_opensubtitles_attrs(attrs: Dict[str, Any], file_info: Dict[str, Any], title: str) -> List[int]:
    values = [
        title,
        attrs.get("release"),
        attrs.get("movie_name"),
        attrs.get("feature_details"),
        file_info.get("file_name"),
    ]
    years: List[int] = []
    for value in values:
        if isinstance(value, dict):
            for key in ["year", "release_year"]:
                year = _safe_int(value.get(key), 0)
                if year:
                    years.append(year)
            value = " ".join(str(item or "") for item in value.values())
        years.extend(_extract_years(str(value or "")))
    return sorted(set(years))


def _years_from_file_info(file_info: Dict[str, Any]) -> List[int]:
    values = [
        file_info.get("file_name"),
        file_info.get("moviehash_match"),
        file_info.get("release"),
    ]
    years: List[int] = []
    for value in values:
        years.extend(_extract_years(str(value or "")))
    return sorted(set(years))


def _opensubtitles_metadata_conflicts(attrs: Dict[str, Any], targets: List[Dict[str, Any]]) -> bool:
    target_tmdb_ids = {
        str(target.get("tmdb_id") or "").strip()
        for target in targets or []
        if str(target.get("tmdb_id") or "").strip()
    }
    target_imdb_ids = {
        _normalize_imdb_tt(target.get("imdb_id"))
        for target in targets or []
        if _normalize_imdb_tt(target.get("imdb_id"))
    }
    result_tmdb_ids: set[str] = set()
    result_imdb_ids: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                key_lower = str(key or "").lower()
                if key_lower in {"tmdb_id", "feature_tmdb_id"}:
                    text = str(item or "").strip()
                    if text and text not in {"0", "None", "null"}:
                        result_tmdb_ids.add(text)
                    continue
                if key_lower in {"imdb_id", "feature_imdb_id"}:
                    imdb = _normalize_imdb_tt(item)
                    if imdb:
                        result_imdb_ids.add(imdb)
                    continue
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(attrs or {})
    return bool(
        (target_tmdb_ids and result_tmdb_ids and target_tmdb_ids.isdisjoint(result_tmdb_ids))
        or (target_imdb_ids and result_imdb_ids and target_imdb_ids.isdisjoint(result_imdb_ids))
    )


def _safe_opensubtitles_title_identity(
    attrs: Dict[str, Any],
    file_info: Dict[str, Any],
    targets: List[Dict[str, Any]],
) -> bool:
    feature = attrs.get("feature_details") if isinstance(attrs.get("feature_details"), dict) else {}
    values = [
        attrs.get("release"),
        attrs.get("movie_name"),
        feature.get("title"),
        feature.get("movie_name"),
        file_info.get("file_name"),
    ]
    haystacks = [str(value or "") for value in values if str(value or "").strip()]
    for alias in _title_aliases({}, targets):
        if _is_ambiguous_single_title_alias(alias):
            continue
        if any(_strong_title_matches(alias, haystack) for haystack in haystacks):
            return True
    return False


def _extract_years(value: str) -> List[int]:
    current_year = datetime.now().year + 1
    years = []
    for match in re.finditer(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", value or ""):
        year = int(match.group(1))
        if 1900 <= year <= current_year:
            years.append(year)
    return sorted(set(years))


def _year_from_upload_date(value: Any) -> int:
    match = re.match(r"\s*(19\d{2}|20\d{2})", str(value or ""))
    return int(match.group(1)) if match else 0


def _relevance_status(title: str, keyword: str, targets: List[Dict[str, Any]]) -> str:
    return _assess_result_match(title=title, keyword=keyword, targets=targets)["relevance_status"]


def _assess_result_match(
    *,
    title: str,
    keyword: str,
    targets: List[Dict[str, Any]],
    result_years: Optional[List[int]] = None,
    attrs: Optional[Dict[str, Any]] = None,
    file_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    attrs = attrs or {}
    file_info = file_info or {}
    haystack = " ".join(
        str(item or "")
        for item in [
            title,
            attrs.get("movie_name"),
            attrs.get("feature_details"),
            attrs.get("release"),
            file_info.get("file_name"),
        ]
    )
    score = 0
    reasons: List[str] = []
    reject_reason = ""
    year_reject_reason = ""
    title_matched = False
    metadata_matched = False
    file_title_matched = False

    target_aliases = _title_aliases({}, targets)
    keyword_parts = _keyword_match_parts(keyword)
    series_aliases = _series_title_aliases(targets)
    for alias in target_aliases:
        if _strong_title_matches(alias, haystack):
            title_matched = True
            score += 34
            reasons.append("标题别名命中")
            break
    if not title_matched:
        for part in keyword_parts:
            if _is_episode_only_keyword_part(part, keyword, targets):
                continue
            if _strong_title_matches(part, haystack):
                title_matched = True
                score += 18
                reasons.append("搜索词命中")
                break
    series_matched = any(_strong_title_matches(alias, haystack) for alias in series_aliases)
    file_name = str(file_info.get("file_name") or "")
    if file_name:
        file_title_matched = any(_strong_title_matches(alias, file_name) for alias in target_aliases)
        if file_title_matched and not title_matched:
            score += 28
            reasons.append("文件名标题命中")

    for target in targets or []:
        target_tmdb = str(target.get("tmdb_id") or "").strip()
        target_imdb = str(target.get("imdb_id") or "").strip().lower()
        text_values = json.dumps(attrs, ensure_ascii=False, default=str).lower()
        if target_imdb and target_imdb in text_values:
            metadata_matched = True
            score += 40
            reasons.append("IMDb 证据命中")
            break
        if target_tmdb and re.search(rf"(?<!\d){re.escape(target_tmdb)}(?!\d)", text_values):
            metadata_matched = True
            score += 40
            reasons.append("TMDB 证据命中")
            break

    target_year = _target_year_from_targets(targets)
    result_years = result_years if result_years is not None else _extract_years(haystack)
    if target_year and result_years:
        if target_year in result_years:
            score += 18
            reasons.append("年份一致")
        else:
            year_reject_reason = "年份冲突"
            reject_reason = year_reject_reason
    upload_year = _year_from_upload_date(attrs.get("upload_date") or attrs.get("uploaded_at"))
    if target_year and upload_year:
        if upload_year < target_year:
            year_reject_reason = year_reject_reason or "字幕上传时间早于资源年份"
            reject_reason = reject_reason or year_reject_reason
        else:
            reasons.append("上传年份可用")

    episode_ok = False
    episode_required = any(int(target.get("episode") or 0) for target in targets or [])
    hint = _episode_from_text(haystack)
    if hint:
        season, episode = hint
        for target in targets or []:
            target_season = int(target.get("season") or 0)
            target_episode = int(target.get("episode") or 0)
            if episode == target_episode and (not season or not target_season or season == target_season):
                episode_ok = True
                score += 22
                reasons.append("季集一致")
                break
    elif _season_matches_text(haystack, targets):
        episode_ok = True
        score += 10
        reasons.append("季一致")
    elif not episode_required:
        episode_ok = True

    media_type = next((str(target.get("media_type") or "") for target in targets or [] if target.get("media_type")), "")
    if media_type == "tv" and not series_matched and not metadata_matched:
        title_matched = False
        reject_reason = reject_reason or "剧名身份不匹配"
    if media_type == "tv" and episode_required and not episode_ok:
        reject_reason = reject_reason or "季集不匹配"
    safe_tv_title_identity = (
        media_type == "tv"
        and year_reject_reason
        and _safe_opensubtitles_title_identity(attrs, file_info, targets)
    )
    if safe_tv_title_identity and reject_reason == year_reject_reason:
        reject_reason = ""

    if _guess_language_label(title):
        score += 6

    identity_ok = title_matched or metadata_matched or file_title_matched
    if not identity_ok:
        reject_reason = reject_reason or "无标题或媒体身份信号"
    year_ok_for_strong = (
        not target_year
        or not result_years
        or target_year in result_years
        or safe_tv_title_identity
    )
    if reject_reason:
        identity_status = "failed"
    elif metadata_matched or (
        (title_matched or file_title_matched)
        and year_ok_for_strong
        and episode_ok
    ):
        identity_status = "strong"
    else:
        identity_status = "weak"
    return {
        "score": score,
        "identity_status": identity_status,
        "relevance_status": "matched" if identity_status == "strong" else ("weak" if identity_status == "weak" else "failed"),
        "reject_reason": reject_reason,
        "match_detail": " / ".join(_unique_keywords(reasons)) or reject_reason,
    }


def _is_episode_only_keyword_part(part: str, keyword: str, targets: List[Dict[str, Any]]) -> bool:
    if re.fullmatch(r"(?i)s\d{1,2}e\d{1,3}|e\d{1,3}", part or ""):
        return True
    series_aliases = _series_title_aliases(targets)
    if any(_strong_title_matches(alias, part) for alias in series_aliases):
        return False
    return _is_cjk_text(part) and len(part) >= 3 and part in _normalize_title_for_match(keyword) and len(targets or []) == 1


def _season_matches_text(value: str, targets: List[Dict[str, Any]]) -> bool:
    seasons = {int(target.get("season") or 0) for target in targets or [] if int(target.get("season") or 0)}
    if not seasons:
        return False
    text = value or ""
    for season in seasons:
        if re.search(rf"(?i)\bS{season:02d}\b|\bS{season}\b|第\s*{season}\s*季", text):
            return True
    return False


def _keyword_match_parts(value: str) -> List[str]:
    stopwords = {"a", "an", "and", "of", "the", "to", "in", "on", "for", "with", "part"}
    parts: List[str] = []
    for raw in re.split(r"[\s._\-:：,，]+", (value or "").lower()):
        part = raw.strip()
        if not part:
            continue
        if _is_generic_language_alias(part):
            continue
        if re.fullmatch(r"(19\d{2}|20\d{2})", part):
            continue
        if _is_cjk_text(part):
            if len(part) >= 3:
                parts.append(part)
        elif len(part) >= 3 and part not in stopwords:
            parts.append(part)
    return parts


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


__all__ = [name for name in globals() if not name.startswith("__")]
