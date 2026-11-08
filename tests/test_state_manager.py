import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import state_manager
from state_manager import StateManager


class SaveStateTest(unittest.TestCase):
    def test_interrupted_write_leaves_the_previous_state_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = StateManager.__new__(StateManager)
            manager.state_file = Path(tmp) / "state.json"
            manager.state = {"processed_videos": {"a": "1"}}
            manager._save_state()

            manager.state = {"processed_videos": {"b": "2"}}
            with patch.object(state_manager.json, "dump", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    manager._save_state()

            self.assertEqual(
                json.loads(manager.state_file.read_text(encoding="utf-8")),
                {"processed_videos": {"a": "1"}},
            )

    def test_two_processes_marking_videos_keep_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            managers = []
            for _ in range(2):
                manager = StateManager.__new__(StateManager)
                manager.state_file = Path(tmp) / "state.json"
                manager.state = manager._load_state()
                managers.append(manager)

            managers[0].mark_video_processed("first", "t", "c", "a.json")
            managers[1].mark_video_processed("second", "t", "c", "b.json")

            saved = json.loads(managers[0].state_file.read_text(encoding="utf-8"))
            self.assertEqual(set(saved["processed_videos"]), {"first", "second"})


if __name__ == "__main__":
    unittest.main()
