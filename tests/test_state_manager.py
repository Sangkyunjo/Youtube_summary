import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from state_manager import StateManager


def state_at(path):
    manager = StateManager.__new__(StateManager)
    manager.state_file = path / 'state.json'
    manager.refresh()
    return manager


class SaveStateTest(unittest.TestCase):
    def test_interrupted_write_leaves_the_previous_state_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = StateManager.__new__(StateManager)
            manager.state_file = Path(tmp) / "state.json"
            manager.state = {"processed_videos": {"a": "1"}}
            manager._save_state()

            manager.state = {"processed_videos": {"b": "2"}}
            with patch.object(Path, "replace", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    manager._save_state()

            self.assertEqual(
                json.loads(manager.state_file.read_text(encoding="utf-8")),
                {"processed_videos": {"a": "1"}},
            )

    def test_concurrent_writers_keep_every_video_and_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            writes = [('first', 'daily', 'a.txt'), ('first', 'narrative', 'first.json'),
                      ('second', 'narrative', 'second.json')]
            start = Barrier(len(writes))

            def write(args):
                video_id, destination, filename = args
                manager = state_at(root)
                start.wait(timeout=5)
                manager.mark_video_processed(video_id, 't', 'c', filename, destination=destination)

            with ThreadPoolExecutor(len(writes)) as pool:
                list(pool.map(write, writes))
            saved = state_at(root)
            self.assertEqual(saved.get_all_processed_video_ids('daily'), {'first'})
            self.assertEqual(saved.get_all_processed_video_ids('narrative'), {'first', 'second'})

    def test_legacy_outputs_identify_only_their_own_destinations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = {'regular': 'local.txt', 'radar': 'radar.json', 'unavailable': ''}
            (root / 'state.json').write_text(json.dumps({
                'processed_videos': dict.fromkeys(files, '2026-11-08T00:00:00'),
                'video_metadata': {vid: {'summary_file': name} for vid, name in files.items()},
            }))
            state = state_at(root)
            self.assertEqual(state.get_all_processed_video_ids('daily'), {'regular'})
            self.assertEqual(state.get_all_processed_video_ids('narrative'), {'radar', 'unavailable'})


if __name__ == "__main__":
    unittest.main()
