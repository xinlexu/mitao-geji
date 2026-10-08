"""Offline round-trip coverage of real Guild panel metadata and old saved queues."""
from __future__ import annotations

import ast
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


SOURCE = Path(__file__).resolve().parents[1] / 'zeta_bot'


def load_definitions(filename, names, namespace):
    tree = ast.parse((SOURCE / filename).read_text(encoding='utf-8-sig'))
    nodes = [item for item in tree.body
             if isinstance(item, (ast.ClassDef, ast.FunctionDef)) and item.name in names]
    if len(nodes) != len(names):
        raise AssertionError(f'Missing production definitions: {names}')
    unit = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)]
                          + nodes, type_ignores=[])
    exec(compile(ast.fix_missing_locations(unit), str(SOURCE / filename), 'exec'), namespace)


class ControlPanelStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='zeta-panel-storage-', dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.writes = []
        self.locks = []

        def save(path, value):
            encoded = json.dumps(value, default=lambda obj: obj.encode(), ensure_ascii=False)
            Path(path).write_text(encoded, encoding='utf-8')
            self.writes.append(json.loads(encoded))

        self.ns = dict(asyncio=asyncio, os=os, errors=SimpleNamespace(JSONFileError=RuntimeError),
                       utils=SimpleNamespace(create_folder=lambda path: Path(path).mkdir(parents=True, exist_ok=True),
                                             json_save=save,
                                             json_load=lambda path: json.loads(Path(path).read_text(encoding='utf-8')),
                                             convert_duration_to_str=str))
        load_definitions('audio.py', {'Audio', 'audio_decoder'}, self.ns)
        self.ns['audio'] = SimpleNamespace(Audio=self.ns['Audio'], audio_decoder=self.ns['audio_decoder'])
        load_definitions('playlist.py', {'Playlist', 'playlist_decoder'}, self.ns)
        self.ns['playlist'] = SimpleNamespace(Playlist=self.ns['Playlist'], playlist_decoder=self.ns['playlist_decoder'])
        load_definitions('guild.py', {'Guild', 'GuildPlaylist', 'guild_playlist_loader'}, self.ns)
        self.library = SimpleNamespace(lock_audio=lambda key, item: self.locks.append((key, item.get_title())),
                                       unlock_audio=lambda *args: None)
        self.guild = self.new_guild()
        self.a = self.ns['Audio']('保留的歌曲', 'test', 'A', 'audio/A.mp3', 73)
        self.guild.get_playlist().append_audio(self.a)
        self.guild.playedlist.append_audio(self.a)
        self.guild.set_play_mode(2)

    def new_guild(self):
        return self.ns['Guild'](SimpleNamespace(id=123, name='测试服务器'), str(self.root), self.library)

    def saved(self):
        return json.loads(Path(self.guild._path).read_text(encoding='utf-8'))

    def rewrite_saved(self, value):
        Path(self.guild._path).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def assert_queue_restored(self, restored):
        self.assertEqual(restored.get_playlist().get_audio(0).get_title(), '保留的歌曲')
        self.assertEqual(restored.get_playlist().get_audio(0).get_duration(), 73)
        self.assertEqual(restored.get_playedlist().get_audio(0).get_title(), '保留的歌曲')
        self.assertEqual(restored.get_play_mode(), 2)

    def test_new_guild_has_empty_metadata(self):
        self.assertEqual(self.guild.get_control_panel(), {})
        self.assertEqual(self.saved()['control_panel'], {})

    def test_valid_snowflakes_round_trip_without_losing_queue(self):
        panel = {'channel_id': 123456789012345678, 'message_id': 987654321098765432}
        self.guild.set_control_panel(**panel)
        restored = self.new_guild()
        self.assertEqual(restored.get_control_panel(), panel)
        self.assertEqual(self.saved()['control_panel'], panel)
        self.assert_queue_restored(restored)

    def test_get_and_encode_cannot_mutate_internal_metadata(self):
        self.guild.set_control_panel(12, 34)
        copy = self.guild.get_control_panel()
        copy['message_id'] = 56
        copy['unexpected'] = True
        encoded = self.guild.encode()
        encoded['control_panel'].clear()
        self.assertEqual(self.guild.get_control_panel(), {'channel_id': 12, 'message_id': 34})

    def test_clear_is_persisted_and_keeps_existing_queue(self):
        self.guild.set_control_panel(12, 34)
        self.guild.set_control_panel()
        restored = self.new_guild()
        self.assertEqual(restored.get_control_panel(), {})
        self.assertEqual(self.saved()['control_panel'], {})
        self.assert_queue_restored(restored)

    def test_invalid_set_does_not_mutate_or_save(self):
        self.guild.set_control_panel(12, 34)
        before_disk = Path(self.guild._path).read_bytes()
        before_writes = len(self.writes)
        invalid = [(None, 1), (1, None), (0, 1), (1, 0), (-1, 2), (2, -1),
                   (True, 2), (2, False), ('12', 34), (12, '34'), (1.0, 2), (2, 1.0),
                   ([], 2), (2, {}), (object(), 2)]
        for pair in invalid:
            with self.subTest(pair=pair), self.assertRaises(ValueError):
                self.guild.set_control_panel(*pair)
            self.assertEqual(self.guild.get_control_panel(), {'channel_id': 12, 'message_id': 34})
            self.assertEqual(Path(self.guild._path).read_bytes(), before_disk)
            self.assertEqual(len(self.writes), before_writes)

    def test_old_json_without_metadata_loads_without_rewriting_queue(self):
        data = self.saved()
        data.pop('control_panel')
        self.rewrite_saved(data)
        before_writes = len(self.writes)
        restored = self.new_guild()
        self.assertEqual(restored.get_control_panel(), {})
        self.assert_queue_restored(restored)
        self.assertEqual(len(self.writes), before_writes)
        self.assertNotIn('control_panel', self.saved())

    def test_bad_loaded_metadata_is_ignored_without_resetting_queue(self):
        original = self.saved()
        bad_values = [None, True, 1, 'bad', [], {}, {'channel_id': 1},
                      {'channel_id': True, 'message_id': 2}, {'channel_id': 1, 'message_id': False},
                      {'channel_id': 1, 'message_id': '2'}, {'channel_id': 1, 'message_id': 0}]
        for bad in bad_values:
            with self.subTest(metadata=bad):
                self.rewrite_saved(dict(original, control_panel=bad))
                before_writes = len(self.writes)
                restored = self.new_guild()
                self.assertEqual(restored.get_control_panel(), {})
                self.assertEqual(restored.encode()['control_panel'], {})
                self.assert_queue_restored(restored)
                self.assertEqual(len(self.writes), before_writes)

    def test_unknown_metadata_fields_are_not_retained_or_encoded(self):
        self.rewrite_saved(dict(self.saved(), control_panel={'channel_id': 12, 'message_id': 34,
                                                           'unknown': 'must not persist'}))
        restored = self.new_guild()
        self.assertEqual(restored.get_control_panel(), {'channel_id': 12, 'message_id': 34})
        restored.save()
        self.assertEqual(self.saved()['control_panel'], {'channel_id': 12, 'message_id': 34})
        self.assert_queue_restored(restored)

    def test_get_and_encode_filter_corrupted_internal_state(self):
        for value in ({'channel_id': True, 'message_id': 2}, None, {'channel_id': 3}):
            with self.subTest(value=value):
                self.guild._control_panel = value
                self.assertEqual(self.guild.get_control_panel(), {})
                self.assertEqual(self.guild.encode()['control_panel'], {})


if __name__ == '__main__':
    unittest.main(verbosity=2)
