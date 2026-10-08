"""Convert existing playlist metadata into picker choices without fetching media."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from zeta_bot.song_picker import SongChoice


_SOURCE_LABELS = {
    "bilibili": "哔哩哔哩",
    "bilibili_single": "哔哩哔哩",
    "bilibili_p": "哔哩哔哩分P",
    "bilibili_collection": "哔哩哔哩合集",
    "youtube": "YouTube",
    "youtube_single": "YouTube",
    "youtube_playlist": "YouTube",
    "netease": "网易云",
    "netease_single": "网易云",
    "netease_playlist": "网易云",
}


def _text(value) -> str:
    """Use human labels from nested metadata, never a dict's representation."""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, Mapping):
        for key in ("name", "title", "text", "display_name", "artist"):
            label = _text(value.get(key))
            if label:
                return label
        return ""
    if isinstance(value, (list, tuple)):
        labels = []
        for item in value:
            label = _text(item)
            if label and label not in labels:
                labels.append(label)
        return " / ".join(labels)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return str(value)
    return ""


def _duration(value) -> float | int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return int(seconds) if seconds.is_integer() else seconds


def _credit(entry: Mapping) -> str:
    for field in ("artist", "artists", "creator", "creators", "uploader", "channel", "owner"):
        label = _text(entry.get(field))
        if label:
            return label
    return ""


def _sequence(value) -> Sequence:
    return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else ()


def imported_choices(source: str, info_dict: Mapping) -> list[SongChoice]:
    """Keep the original 1-based position expected by EpisodeSelectMenu.

    The legacy Bilibili collection downloader addresses only its first section;
    expanding that range here would silently download a different song.
    """
    if not isinstance(info_dict, Mapping):
        return []
    if source == "bilibili_p":
        entries = _sequence(info_dict.get("pages"))
    elif source == "bilibili_collection":
        season = info_dict.get("ugc_season")
        sections = _sequence(season.get("sections")) if isinstance(season, Mapping) else ()
        section = sections[0] if sections and isinstance(sections[0], Mapping) else {}
        entries = _sequence(section.get("episodes"))
    elif source in ("youtube_playlist", "netease_playlist"):
        entries = _sequence(info_dict.get("entries"))
    else:
        return []

    choices = []
    for original_index, entry in enumerate(entries, start=1):
        if not isinstance(entry, Mapping):
            continue
        arc = entry.get("arc")
        arc = arc if isinstance(arc, Mapping) else {}
        title = _text(entry.get("part") if source == "bilibili_p" else entry.get("title"))
        title = title or _text(arc.get("title")) or f"第 {original_index} 首（标题未知）"
        identity = next((_text(entry.get(field)) for field in ("id", "bvid", "cid", "aid")
                         if _text(entry.get(field))), "unknown")
        credit = _credit(entry) or _credit(arc)
        if not credit and source.startswith("bilibili_"):
            credit = _credit(info_dict)
        subtitle = f"原第 {original_index} 首 · {_SOURCE_LABELS[source]}"
        if credit:
            subtitle += f" · {credit}"
        duration = _duration(entry.get("duration"))
        if duration is None:
            duration = _duration(arc.get("duration"))
        choices.append(SongChoice(
            key=f"{source}:{original_index}:{identity}", title=title,
            subtitle=subtitle, duration=duration, payload=original_index,
        ))
    return choices


def queue_choices(queue) -> list[SongChoice]:
    """Snapshot queued Audio references, including repeated references to one file."""
    choices = []
    occurrences = {}
    for index in range(len(queue)):
        item = queue.get_audio(index)
        if item is None:
            continue
        identity = id(item)
        occurrences[identity] = occurrences.get(identity, 0) + 1
        source = _text(item.get_source())
        choices.append(SongChoice(
            key=f"queue:{identity}:{occurrences[identity]}",
            title=_text(item.get_title()) or f"第 {index + 1} 首（标题未知）",
            subtitle=f"队列第 {index + 1} 首 · {_SOURCE_LABELS.get(source, source or '音频')}",
            duration=_duration(item.get_duration()), payload=item,
        ))
    return choices
