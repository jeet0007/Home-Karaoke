import hashlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import artifacts  # noqa: E402


class ArtifactStoreTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = artifacts.ArtifactStore(tmp.name)

    def test_write_and_read_json(self):
        self.store.write_json(1, artifacts.KIND_MELODY, {"notes": [1, 2, 3]})
        self.assertEqual(self.store.read_json(1, artifacts.KIND_MELODY), {"notes": [1, 2, 3]})

    def test_write_bytes_and_size(self):
        path = self.store.write_bytes(5, artifacts.KIND_MIDI, b"MThd1234")
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(self.store.size(5, artifacts.KIND_MIDI), 8)

    def test_exists_is_false_before_write_and_true_after(self):
        self.assertFalse(self.store.exists(2, artifacts.KIND_VOCALS))
        self.store.write_bytes(2, artifacts.KIND_VOCALS, b"RIFF")
        self.assertTrue(self.store.exists(2, artifacts.KIND_VOCALS))

    def test_songs_are_isolated_by_id(self):
        self.store.write_bytes(1, artifacts.KIND_MIX, b"a")
        self.assertFalse(self.store.exists(2, artifacts.KIND_MIX))

    def test_path_has_expected_filename(self):
        path = self.store.path(9, artifacts.KIND_MIDI)
        self.assertTrue(path.endswith(os.path.join("9", "melody.mid")))

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            self.store.path(1, "bogus")

    def test_size_zero_when_missing(self):
        self.assertEqual(self.store.size(1, artifacts.KIND_MIDI), 0)

    def test_remove_song_clears_all_artifacts(self):
        self.store.write_bytes(3, artifacts.KIND_MIX, b"a")
        self.store.write_bytes(3, artifacts.KIND_VOCALS, b"b")
        self.store.remove_song(3)
        self.assertFalse(self.store.exists(3, artifacts.KIND_MIX))
        self.assertFalse(self.store.exists(3, artifacts.KIND_VOCALS))

    def test_remove_song_safe_when_nothing_written(self):
        self.store.remove_song(999)  # no raise


class ContentHashTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = artifacts.ArtifactStore(tmp.name)

    def test_missing_file_hashes_to_none(self):
        self.assertIsNone(self.store.content_hash(1, artifacts.KIND_MIX))

    def test_hash_matches_known_bytes(self):
        self.store.write_bytes(1, artifacts.KIND_MIX, b"hello world")
        expected = hashlib.sha256(b"hello world").hexdigest()
        self.assertEqual(self.store.content_hash(1, artifacts.KIND_MIX), expected)

    def test_hash_changes_after_rewrite(self):
        self.store.write_bytes(1, artifacts.KIND_MIX, b"first")
        first_hash = self.store.content_hash(1, artifacts.KIND_MIX)
        self.store.write_bytes(1, artifacts.KIND_MIX, b"second")
        second_hash = self.store.content_hash(1, artifacts.KIND_MIX)
        self.assertNotEqual(first_hash, second_hash)
        self.assertEqual(second_hash, hashlib.sha256(b"second").hexdigest())


if __name__ == "__main__":
    unittest.main()
