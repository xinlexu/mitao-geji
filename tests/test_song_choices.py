"""Offline regressions for real picker metadata adapters and their payloads."""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "zeta_bot"


def load_adapters():
    # Use the real dataclass while avoiding Discord UI imports in a unit test.
    picker = ast.parse((SOURCE / "song_picker.py").read_text(encoding="utf-8"))
    definitions = [node for node in picker.body if isinstance(node, ast.ClassDef) and node.name == "SongChoice"]
    namespace = {"dataclass": dataclass, "Any": Any, "__name__": __name__}
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(SOURCE / "song_picker.py"), "exec"), namespace)
    tree = ast.parse((SOURCE / "song_choices.py").read_text(encoding="utf-8"))
    tree.body = [node for node in tree.body
                 if not (isinstance(node, ast.ImportFrom) and node.module == "zeta_bot.song_picker")]
    exec(compile(tree, str(SOURCE / "song_choices.py"), "exec"), namespace)
    return SimpleNamespace(**{key: namespace[key] for key in ("SongChoice", "imported_choices", "queue_choices")})


ADAPTERS = load_adapters()


class Queue:
    def __init__(self, *items):
        self.items = items

    def __len__(self):
        return len(self.items)

    def get_audio(self, index):
        return self.items[index]


def audio(title="song", source="youtube_single", duration=60):
    return SimpleNamespace(get_title=lambda: title, get_source=lambda: source, get_duration=lambda: duration)


class SongChoicesTests(unittest.TestCase):
    def test_missing_rows_keep_original_youtube_positions(self):
        rows = ADAPTERS.imported_choices("youtube_playlist", {"entries": [
            {"id": "first", "title": "其他歌曲", "duration": 30}, None,
            {"id": "third", "title": "想听的歌", "duration": None},
        ]})
        selected = [row for row in rows if "想听" in row.title]
        self.assertEqual([row.payload for row in rows], [1, 3])
        self.assertEqual(selected[0].payload, 3)
        self.assertIsNone(selected[0].duration)
        self.assertEqual(selected[0].key, "youtube_playlist:3:third")

    def test_same_title_and_repeated_id_have_distinct_choices(self):
        rows = ADAPTERS.imported_choices("youtube_playlist", {"entries": [
            {"id": "first", "title": "同名歌"}, {"id": "second", "title": "同名歌"},
            {"id": "first", "title": "同名歌"},
        ]})
        self.assertEqual(len({row.key for row in rows}), 3)
        self.assertEqual([row.payload for row in rows], [1, 2, 3])

    def test_flat_metadata_names_support_lists_dicts_and_fallbacks(self):
        entries = [
            {"title": {"title": "  歌名  "}, "artists": [{"name": "甲"}, "乙", None, {"name": "甲"}]},
            {"title": [None, {"name": "第二首"}], "artist": None, "creator": [{"name": "创作者"}]},
            {"title": None, "uploader": {"name": "上传者", "url": "unused"}},
            {"title": "第四首", "uploader": None, "channel": "频道"},
        ]
        rows = ADAPTERS.imported_choices("youtube_playlist", {"entries": entries})
        self.assertEqual(rows[0].title, "歌名")
        self.assertIn("甲 / 乙", rows[0].subtitle)
        self.assertEqual(rows[1].title, "第二首")
        self.assertIn("创作者", rows[1].subtitle)
        self.assertIn("标题未知", rows[2].title)
        self.assertIn("上传者", rows[2].subtitle)
        self.assertNotIn("unused", rows[2].subtitle)
        self.assertIn("频道", rows[3].subtitle)

    def test_unknown_and_invalid_durations_remain_selectable(self):
        values = [None, "", "unknown", float("nan"), float("inf"), -1, True, 0, "61.5"]
        rows = ADAPTERS.imported_choices("netease_playlist", {"entries": [
            {"id": str(index), "title": "song", "duration": value}
            for index, value in enumerate(values)
        ]})
        self.assertEqual(len(rows), len(values))
        self.assertEqual([row.duration for row in rows], [None] * 7 + [0, 61.5])

    def test_netease_uses_existing_song_artist_metadata_without_reindexing(self):
        rows = ADAPTERS.imported_choices("netease_playlist", {"entries": [None, {
            "id": "100", "url": "https://music.163.com/song?id=100", "title": "歌曲",
            "artist": [{"name": "歌手A"}, {"name": "歌手B"}],
        }]})
        self.assertEqual(rows[0].payload, 2)
        self.assertIn("歌手A / 歌手B", rows[0].subtitle)
        self.assertIsNone(rows[0].duration)

    def test_bilibili_parts_keep_page_index_and_parent_owner(self):
        rows = ADAPTERS.imported_choices("bilibili_p", {
            "bvid": "BVvideo", "owner": {"name": "UP主"}, "pages": [
                {"cid": 101, "part": "P1", "duration": 10}, None,
                {"cid": 103, "part": {"name": "P3"}, "duration": None},
            ],
        })
        self.assertEqual([row.payload for row in rows], [1, 3])
        self.assertEqual(rows[1].title, "P3")
        self.assertIn("UP主", rows[1].subtitle)
        self.assertEqual(rows[1].key, "bilibili_p:3:103")

    def test_bilibili_collection_only_maps_first_section(self):
        rows = ADAPTERS.imported_choices("bilibili_collection", {"ugc_season": {"sections": [
            {"episodes": [None, {"bvid": "BVfirst", "title": "第一分区", "arc": {
                "duration": 125, "owner": {"name": "作者"},
            }}]},
            {"episodes": [{"bvid": "BVsecond", "title": "不支持的第二分区"}]},
        ]}})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].payload, 2)
        self.assertEqual(rows[0].duration, 125)
        self.assertIn("作者", rows[0].subtitle)
        self.assertIn("BVfirst", rows[0].key)

    def test_empty_malformed_or_unsupported_playlists_are_empty(self):
        for source, info in [
            ("youtube_playlist", None), ("youtube_playlist", {"entries": "bad"}),
            ("bilibili_p", {"pages": None}), ("bilibili_collection", {}),
            ("bilibili_collection", {"ugc_season": {"sections": [None]}}),
            ("bilibili_collection", {"ugc_season": {"sections": []}}),
            ("unsupported", {"entries": [{"title": "song"}]}),
        ]:
            with self.subTest(source=source, info=info):
                self.assertEqual(ADAPTERS.imported_choices(source, info), [])

    def test_queue_payloads_keep_identity_and_repeated_objects_have_unique_keys(self):
        first, same_name = audio(), audio()
        rows = ADAPTERS.queue_choices(Queue(first, same_name, first))
        self.assertIs(rows[0].payload, first)
        self.assertIs(rows[1].payload, same_name)
        self.assertIs(rows[2].payload, first)
        self.assertEqual(len({row.key for row in rows}), 3)
        self.assertTrue(rows[2].key.endswith(":2"))
        self.assertIn("队列第 3 首", rows[2].subtitle)
        self.assertNotIn("歌手", rows[0].subtitle)
        self.assertEqual([row.key for row in rows], [row.key for row in ADAPTERS.queue_choices(Queue(first, same_name, first))])

    def test_queue_unknown_duration_and_title_remain_selectable(self):
        item = audio(title=None, source="bilibili_p", duration=None)
        rows = ADAPTERS.queue_choices(Queue(None, item))
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0].payload, item)
        self.assertIsNone(rows[0].duration)
        self.assertIn("第 2 首", rows[0].title)
        self.assertIn("哔哩哔哩分P", rows[0].subtitle)


if __name__ == "__main__":
    unittest.main()
