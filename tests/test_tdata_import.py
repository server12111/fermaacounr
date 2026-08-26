import tempfile
import unittest
import zipfile
from pathlib import Path

from tdata_import import extract_import_batch, extract_tdata_archive, extract_tdata_batch


class TdataArchiveTests(unittest.TestCase):
    def test_finds_wrapped_tdata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "account.zip"
            with zipfile.ZipFile(archive, "w") as target:
                target.writestr("export/tdata/key_data", b"key")
                target.writestr("export/tdata/D877F783D5D3EF8C/map0", b"map")
            found = extract_tdata_archive(archive, root / "out")
            self.assertEqual(found.name, "tdata")
            self.assertTrue((found / "key_data").is_file())

    def test_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "account.zip"
            with zipfile.ZipFile(archive, "w") as target:
                target.writestr("../outside/key_data", b"key")
            with self.assertRaisesRegex(ValueError, "небезопасный путь"):
                extract_tdata_archive(archive, root / "out")


class TdataBatchTests(unittest.TestCase):
    def test_extracts_multiple_nested_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.zip"
            second = root / "second.zip"
            for archive in (first, second):
                with zipfile.ZipFile(archive, "w") as target:
                    target.writestr("tdata/key_data", b"key")
            outer = root / "all.zip"
            with zipfile.ZipFile(outer, "w") as target:
                target.write(first, "account-1/tdata.zip")
                target.write(second, "account-2/tdata.zip")
            found = extract_tdata_batch(outer, root / "out")
            self.assertEqual(len(found), 2)
            self.assertTrue(all((path / "key_data").is_file() for _, path in found))

    def test_keeps_single_tdata_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "single.zip"
            with zipfile.ZipFile(archive, "w") as target:
                target.writestr("tdata/key_data", b"key")
            found = extract_tdata_batch(archive, root / "out")
            self.assertEqual(len(found), 1)


    def test_extracts_multiple_session_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outer = root / "sessions.zip"
            with zipfile.ZipFile(outer, "w") as target:
                target.writestr("account-1/main.session", b"sqlite")
                target.writestr("account-2/main.session", b"sqlite")
            found = extract_import_batch(outer, root / "out")
            self.assertEqual(len(found), 2)
            self.assertTrue(all(kind == "session" for _, kind, _ in found))
