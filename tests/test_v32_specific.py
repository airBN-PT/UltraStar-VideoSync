"""Version 3.2 requirements: flat backups, tag format, language and headings."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "UltraStar_VideoSync.py"
spec = importlib.util.spec_from_file_location("videosync31", MODULE)
vs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vs)


class Version32Tests(unittest.TestCase):
    def test_negative_gap_two_places_and_comma_even_if_old_tags_have_dot(self):
        raw = b"#TITLE:TEST\r\n#VIDEOGAP:5.999\r\n#GAP:12622.85\r\n#BPM:162.8\r\nE\r\n"
        changed = vs._insert_videogap(raw, -12.474)
        self.assertEqual(changed, b"#TITLE:TEST\r\n#VIDEOGAP:-12,47\r\n#GAP:12622.85\r\n#BPM:162.8\r\nE\r\n")
        self.assertTrue(vs.validate_txt_change(raw, changed))

    def test_zero_and_negative_zero_are_written_with_two_places(self):
        original = b"#TITLE:A\n#BPM:160\n#GAP:0\n"
        self.assertIn(b"#VIDEOGAP:0,00\n", vs._insert_videogap(original, -0.001))
        self.assertIn(b"#VIDEOGAP:1,20\n", vs._insert_videogap(original, 1.2))

    def test_central_backups_use_original_txt_filenames_without_subfolders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            central = root / "C_Backup"
            for location in ("Karaoke_A", "Karaoke_B"):
                song = root / location / f"Canção_{location}.txt"
                song.parent.mkdir(parents=True)
                old = b"#TITLE:Song\n#GAP:100\n#BPM:150\n"
                song.write_bytes(old)
                vs.update_txt(song, 2.345, "central", central)
                self.assertEqual((central / song.name).read_bytes(), old)
                self.assertEqual(song.read_bytes(), b"#TITLE:Song\n#VIDEOGAP:2,35\n#GAP:100\n#BPM:150\n")
            backups = list(central.iterdir())
            self.assertEqual({p.name for p in backups}, {"Canção_Karaoke_A.txt", "Canção_Karaoke_B.txt"})
            self.assertTrue(all(p.is_file() and p.parent == central for p in backups))

    def test_first_launch_english_and_existing_choice_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = Path(directory) / "settings.json"
            self.assertEqual(vs.load_settings(cfg)["language"], "en")
            vs.save_settings({"language": "pt", "backup_mode": "central"}, cfg)
            self.assertEqual(vs.load_settings(cfg), {"language": "pt", "backup_mode": "central"})
            self.assertEqual(vs.TR_PT["Hero"], "Sincronização entre música e vídeo")
            self.assertIn("Synchronization", vs.TR["en"]["Hero"])


if __name__ == "__main__":
    unittest.main()
