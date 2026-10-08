import os
import tempfile
import threading
import unittest
from pathlib import Path

from lockbox import engine
from lockbox.engine import (
    CHUNK, decrypt_path, describe, encrypt_path, generate_password, is_locked, safe_name, strength, unique_path,
)

PASSWORD = "correct horse battery"
FAST = {"memory": 8192, "iterations": 1}


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def lock(self, path, **kwargs):
        result = encrypt_path(path, PASSWORD, **FAST, **kwargs)
        self.assertEqual(result.error, "")
        return result

    def make(self, name, data):
        path = self.dir / name
        path.write_bytes(data)
        return path

    def test_round_trip(self):
        path = self.make("notes.txt", b"my secret notes")
        locked = self.lock(path)
        self.assertTrue(is_locked(locked.output))
        self.assertNotIn(b"my secret notes", locked.output.read_bytes())
        path.unlink()
        opened = decrypt_path(locked.output, PASSWORD)
        self.assertEqual(opened.error, "")
        self.assertEqual(opened.output.read_bytes(), b"my secret notes")
        self.assertEqual(opened.output.name, "notes.txt")

    def test_empty_file(self):
        path = self.make("empty.bin", b"")
        locked = self.lock(path)
        path.unlink()
        self.assertEqual(decrypt_path(locked.output, PASSWORD).output.read_bytes(), b"")

    def test_many_chunks(self):
        for size in (CHUNK - 1, CHUNK, CHUNK + 1, CHUNK * 3):
            data = os.urandom(size)
            path = self.make("big%d.bin" % size, data)
            locked = self.lock(path)
            path.unlink()
            self.assertEqual(decrypt_path(locked.output, PASSWORD).output.read_bytes(), data)

    def test_wrong_password(self):
        locked = self.lock(self.make("a.txt", b"data"))
        result = decrypt_path(locked.output, "not the password")
        self.assertIn("Wrong password", result.error)
        self.assertIsNone(result.output)

    def test_tampering_is_detected(self):
        locked = self.lock(self.make("a.txt", b"x" * 5000))
        raw = bytearray(locked.output.read_bytes())
        raw[-20] ^= 1
        locked.output.write_bytes(bytes(raw))
        self.assertIn("damaged", decrypt_path(locked.output, PASSWORD).error)

    def test_truncation_is_detected(self):
        locked = self.lock(self.make("a.bin", os.urandom(CHUNK * 2 + 10)))
        raw = locked.output.read_bytes()
        locked.output.write_bytes(raw[: len(raw) - (CHUNK + engine.TAG)])
        self.assertNotEqual(decrypt_path(locked.output, PASSWORD).error, "")

    def test_header_tampering_is_detected(self):
        locked = self.lock(self.make("a.txt", b"hello"))
        raw = bytearray(locked.output.read_bytes())
        raw[6] ^= 1
        locked.output.write_bytes(bytes(raw))
        self.assertNotEqual(decrypt_path(locked.output, PASSWORD).error, "")

    def test_key_file(self):
        key = self.make("my.key", os.urandom(64))
        other = self.make("other.key", os.urandom(64))
        locked = self.lock(self.make("a.txt", b"data"), keyfile=key)
        self.assertNotEqual(decrypt_path(locked.output, PASSWORD).error, "")
        self.assertNotEqual(decrypt_path(locked.output, PASSWORD, keyfile=other).error, "")
        self.assertEqual(decrypt_path(locked.output, PASSWORD, keyfile=key).error, "")

    def test_folder(self):
        folder = self.dir / "photos"
        (folder / "inner").mkdir(parents=True)
        (folder / "a.txt").write_bytes(b"one")
        (folder / "inner" / "b.txt").write_bytes(b"two")
        locked = self.lock(folder)
        self.assertTrue(locked.output.name.startswith("photos"))
        import shutil

        shutil.rmtree(folder)
        opened = decrypt_path(locked.output, PASSWORD)
        self.assertEqual(opened.error, "")
        self.assertEqual((opened.output / "a.txt").read_bytes(), b"one")
        self.assertEqual((opened.output / "inner" / "b.txt").read_bytes(), b"two")

    def test_hidden_name(self):
        locked = self.lock(self.make("diary.txt", b"dear diary"), hide_name=True)
        self.assertNotIn("diary", locked.output.name)
        self.assertNotIn(b"diary.txt", locked.output.read_bytes())
        (self.dir / "diary.txt").unlink()
        self.assertEqual(decrypt_path(locked.output, PASSWORD).output.name, "diary.txt")

    def test_never_overwrites(self):
        path = self.make("a.txt", b"data")
        first = self.lock(path)
        second = self.lock(path)
        self.assertNotEqual(first.output, second.output)
        self.assertTrue(first.output.exists() and second.output.exists())
        restored = decrypt_path(first.output, PASSWORD)
        self.assertNotEqual(restored.output, path)
        self.assertEqual(path.read_bytes(), b"data")

    def test_delete_original_after_check(self):
        path = self.make("a.txt", b"data")
        locked = self.lock(path, delete_original=True, shred=True)
        self.assertFalse(path.exists())
        self.assertTrue(locked.output.exists())

    def test_short_password_refused(self):
        result = encrypt_path(self.make("a.txt", b"x"), "short", **FAST)
        self.assertIn("at least", result.error)

    def test_already_locked_refused(self):
        locked = self.lock(self.make("a.txt", b"x"))
        result = encrypt_path(locked.output, PASSWORD, **FAST)
        self.assertIn("already locked", result.error)

    def test_not_a_lockbox_file(self):
        path = self.make("plain.txt.lockbox", b"just text, nothing special in here")
        self.assertIn("not a Lockbox", decrypt_path(path, PASSWORD).error)

    def test_cancel(self):
        stop = threading.Event()
        stop.set()
        result = encrypt_path(self.make("a.bin", os.urandom(CHUNK * 2)), PASSWORD, cancel=stop, **FAST)
        self.assertEqual(result.error, "Cancelled")
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.startswith(".lockbox")], [])

    def test_no_leftover_temp_files(self):
        locked = self.lock(self.make("a.txt", b"x"))
        decrypt_path(locked.output, "wrong password here")
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.startswith(".lockbox")], [])

    def test_header_info(self):
        locked = self.lock(self.make("a.txt", b"x"))
        details = describe(locked.output)
        self.assertEqual(details["memory_mib"], 8)
        self.assertFalse(details["key_file"])

    def test_helpers(self):
        self.assertEqual(safe_name("..\\..\\evil.txt"), "evil.txt")
        self.assertEqual(safe_name("CON"), "unlocked")
        self.assertEqual(safe_name('a<b>.txt'), "a_b_.txt")
        self.assertEqual(len(generate_password()), 20)
        self.assertLess(strength("abc"), strength("Tr0ub4dor&3-horse-stable"))
        path = self.make("same.txt", b"")
        self.assertEqual(unique_path(path).name, "same (1).txt")


if __name__ == "__main__":
    unittest.main()

_credit = "Built by Unkn0wngb"
