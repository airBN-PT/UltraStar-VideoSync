"""Regression tests for storage safety, integrity, configuration and translations."""
import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / 'UltraStar_VideoSync.py'
spec = importlib.util.spec_from_file_location('vs3', SRC)
vs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vs)


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.txt = self.dir / 'Áudio com espaços' / 'Som.txt'
        self.txt.parent.mkdir()
        self.raw = b'#TITLE:Original\r\n#BPM:160,3\r\n#GAP:200\r\n: 0 3 10 Te\r\nE\r\n'
        self.txt.write_bytes(self.raw)

    def test_local_backup_and_double_validation(self):
        self.assertTrue(vs.update_txt(self.txt, 3.41))
        modified = self.txt.read_bytes()
        self.assertTrue(vs.validate_txt_change(self.raw, modified))
        self.assertEqual(modified, self.raw.replace(b'#BPM:', b'#VIDEOGAP:3,41\r\n#BPM:'))
        self.assertEqual(self.txt.with_suffix('.txt.videogap.bak').read_bytes(), self.raw)
        self.assertFalse(vs.update_txt(self.txt, 3.41))

    def test_flat_central_backups_never_overwrite(self):
        root = self.dir / 'Central Backup'
        backup = vs.get_backup_path(self.txt, 'central', root)
        self.assertTrue(str(backup).startswith(str(root)))
        self.assertEqual(backup.parent, root)
        self.assertEqual(backup.name, 'Som.txt')
        self.assertTrue(vs.update_txt(self.txt, 1.3, 'central', root))
        self.assertEqual(backup.read_bytes(), self.raw)
        self.assertFalse(self.txt.with_suffix('.txt.videogap.bak').exists())
        vs.update_txt(self.txt, 4.2, 'central', root)
        self.assertEqual(backup.read_bytes(), self.raw)
        self.assertEqual(self.txt.read_bytes().count(b'#VIDEOGAP:'), 1)

    def test_backup_collision_prevents_overwriting_other_song(self):
        root = self.dir / 'Central'
        a = self.dir / 'A' / 'song.txt'
        b = self.dir / 'B' / 'song.txt'
        a.parent.mkdir(parents=True)
        b.parent.mkdir(parents=True)
        src_a = b'#TITLE:First\n#BPM:160\n#GAP:100\n'
        src_b = b'#TITLE:Second\n#BPM:160\n#GAP:100\n'
        a.write_bytes(src_a)
        b.write_bytes(src_b)
        backup = vs.get_backup_path(a, 'central', root)
        self.assertEqual(vs.get_backup_path(b, 'central', root), backup)
        self.assertEqual(backup.name, 'song.txt')
        vs.update_txt(a, 1.2, 'central', root)
        with self.assertRaisesRegex(RuntimeError, 'conflito de nomes'):
            vs.update_txt(b, 1.2, 'central', root)
        self.assertEqual(backup.read_bytes(), src_a)
        self.assertEqual(b.read_bytes(), src_b)
        self.assertEqual(len(list(root.iterdir())), 1)

    def test_central_backup_reused_after_reapplying_videogap(self):
        root = self.dir / 'Central'
        backup = vs.get_backup_path(self.txt, 'central', root)
        vs.update_txt(self.txt, -2.0, 'central', root)
        original_backup = backup.read_bytes()
        vs.update_txt(self.txt, -1.75, 'central', root)
        self.assertEqual(original_backup, self.raw)
        self.assertEqual(backup.read_bytes(), original_backup)
        self.assertIn(b'#VIDEOGAP:-1,75', self.txt.read_bytes())

    def test_central_does_not_replace_manually_existing_file(self):
        root = self.dir / 'Central'
        root.mkdir()
        backup = root / self.txt.name
        backup.write_bytes(b'not a backup of this song')
        with self.assertRaisesRegex(RuntimeError, 'conflito de nomes'):
            vs.update_txt(self.txt, 3.1, 'central', root)
        self.assertEqual(self.txt.read_bytes(), self.raw)
        self.assertEqual(backup.read_bytes(), b'not a backup of this song')

    def test_detects_changes_outside_tag_pre_write(self):
        candidate = vs._insert_videogap(self.raw, 1.2).replace(b'#TITLE:Original', b'#TITLE:Altered')
        with self.assertRaisesRegex(RuntimeError, 'integridade'):
            vs.validate_txt_change(self.raw, candidate)
        with mock.patch.object(vs, '_insert_videogap', return_value=candidate):
            with self.assertRaisesRegex(RuntimeError, 'integridade'):
                vs.update_txt(self.txt, 1.2)
        self.assertEqual(self.txt.read_bytes(), self.raw)
        self.assertFalse(self.txt.with_suffix('.txt.videogap.bak').exists())

    def test_post_write_corruption_is_rolled_back(self):
        real_write = vs._write_atomic
        writes = []
        def sabotage(path, data):
            writes.append(data)
            if len(writes) == 1:
                real_write(path, data + b'corrupted!')
            else:
                real_write(path, data)
        with mock.patch.object(vs, '_write_atomic', side_effect=sabotage):
            with self.assertRaisesRegex(RuntimeError, 'Verifica'):
                vs.update_txt(self.txt, 1.2)
        self.assertEqual(self.txt.read_bytes(), self.raw)
        self.assertEqual(len(writes), 2)

    def test_refuses_stale_analysis(self):
        invalid_digest = hashlib.sha256(b'old').hexdigest()
        with self.assertRaisesRegex(RuntimeError, 'alterado desde a análise'):
            vs.update_txt(self.txt, 1.2, expected_digest=invalid_digest)
        self.assertEqual(self.txt.read_bytes(), self.raw)

    def test_refuses_txt_without_bpm_or_gap(self):
        self.txt.write_bytes(b'#TITLE:Original\n#ARTIST:An Artist\n')
        with self.assertRaisesRegex(RuntimeError, 'sem #BPM ou #GAP'):
            vs.update_txt(self.txt, 5)
        self.assertFalse(self.txt.with_suffix('.txt.videogap.bak').exists())

    def test_backup_failure_never_modifies_txt(self):
        impossible = self.dir / 'exists-as-file'
        impossible.write_bytes(b'x')
        with self.assertRaises((OSError, RuntimeError)):
            vs.update_txt(self.txt, 1.2, 'central', impossible)
        self.assertEqual(self.txt.read_bytes(), self.raw)

    def test_settings_round_trip_and_safe_fallback(self):
        path = self.dir / 'settings.json'
        self.assertEqual(vs.load_settings(path)['language'], 'en')
        vs.save_settings({'language':'fr','backup_mode':'central'}, path)
        self.assertEqual(vs.load_settings(path), {'language':'fr','backup_mode':'central'})
        path.write_text('{"language":"invalid","backup_mode":"bad"}', encoding='utf-8')
        self.assertEqual(vs.load_settings(path), {'language':'en','backup_mode':'local'})

    def test_four_languages_have_complete_ui(self):
        keys = set(vs.TR_PT)
        self.assertEqual(set(vs.LANGUAGES), {'pt','es','en','fr'})
        for locale in ('en','es','fr'):
            self.assertTrue(keys <= set(vs.TR[locale]), locale)


if __name__ == '__main__':
    unittest.main()
