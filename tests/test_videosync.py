"""Testes de seguranca para etiquetas, codificacoes e duplicados."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'UltraStar_VideoSync.py'
spec = importlib.util.spec_from_file_location('videosync', SCRIPT)
vs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vs)


class TestTxtEditing(unittest.TestCase):
    def check_format(self, encoding, bom, newline, order, has_existing, new_val=5.4):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'Canção.txt'
            lines = ['#TITLE:Coração do Mar', '#ARTIST:Inês']
            if has_existing:
                lines += ['#VIDEOGAP:0,3']
            lines += ['#MP3:música.mp3', '#VIDEO:música.mp4']
            lines += order
            lines += [': 0 4 2 a', 'E']
            src = newline.join(lines) + newline
            raw = bom + src.encode(encoding)
            f.write_bytes(raw)
            changed = vs.update_txt(f, new_val)
            actual = f.read_bytes()
            result = actual[len(bom):].decode(encoding)
            self.assertTrue(changed)
            self.assertEqual(actual[:len(bom)], bom)
            self.assertIn('#TITLE:Coração do Mar' + newline, result)
            self.assertIn('#ARTIST:Inês' + newline, result)
            self.assertIn('#VIDEOGAP:5,40' + newline, result)
            self.assertEqual(result.count('#VIDEOGAP:'), 1)
            i = result.index('#VIDEOGAP:')
            for tag in ('#GAP:', '#BPM:'):
                self.assertLess(i, result.index(tag))
            self.assertEqual(f.with_suffix('.txt.videogap.bak').read_bytes(), raw)
            # Quando ja esta corretamente posicionado, nao faz nova alteracao.
            self.assertFalse(vs.update_txt(f, new_val))
            self.assertEqual(f.read_bytes(), actual)

    def test_utf8_without_bom_lf(self):
        self.check_format('utf-8', b'', '\n', ['#BPM:162,8', '#GAP:12622,85'], False)

    def test_utf8_with_bom_crlf(self):
        self.check_format('utf-8', b'\xef\xbb\xbf', '\r\n', ['#GAP:12622,85', '#BPM:162,8'], True)

    def test_cp1252_remains_cp1252(self):
        self.check_format('cp1252', b'', '\r\n', ['#BPM:162,8', '#GAP:12622,85'], True)

    def test_utf16le_with_bom(self):
        self.check_format('utf-16-le', b'\xff\xfe', '\r\n', ['#GAP:12622,85', '#BPM:162,8'], True)

    def test_utf16be_with_bom(self):
        self.check_format('utf-16-be', b'\xfe\xff', '\n', ['#GAP:12622,85', '#BPM:162,8'], False)

    def test_ignores_existing_decimal_dot_for_videogap(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'a.txt'
            f.write_bytes(b'#TITLE:Teste\r\n#BPM:150.5\r\n#VIDEOGAP:2.5\r\n#GAP:1000.0\r\n')
            vs.update_txt(f, 3.25)
            self.assertEqual(f.read_bytes(),
                             b'#TITLE:Teste\r\n#VIDEOGAP:3,25\r\n#BPM:150.5\r\n#GAP:1000.0\r\n')

    def test_no_bom_generated_on_existing_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'a.txt'
            f.write_bytes('#TITLE:Olá\n#GAP:100\n#BPM:150\n'.encode('utf-8'))
            vs.update_txt(f, -0.62)
            self.assertTrue(f.read_bytes().startswith('#TITLE:Olá\n'.encode('utf-8')))
            self.assertFalse(f.read_bytes().startswith(b'\xef\xbb\xbf'))

    def test_unsupported_nul_file_not_altered(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'a.txt'
            original = '#GAP:100\n'.encode('utf-16-le')
            f.write_bytes(original)
            with self.assertRaises(RuntimeError):
                vs.update_txt(f, 1)
            self.assertEqual(f.read_bytes(), original)

    def test_existing_tag_moved_above_bpm_and_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'a.txt'
            original = b'#TITLE:Teste\n#GAP:100\n#VIDEOGAP:3\n#BPM:160\n'
            f.write_bytes(original)
            vs.update_txt(f, 3)
            self.assertEqual(f.read_bytes(), b'#TITLE:Teste\n#VIDEOGAP:3,00\n#GAP:100\n#BPM:160\n')

    def test_duplicate_tags_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'a.txt'
            f.write_bytes(b'#VIDEOGAP:1\n#GAP:100\n#VIDEOGAP:9\n#BPM:150\n')
            vs.update_txt(f, 1.4)
            self.assertEqual(f.read_bytes(), b'#VIDEOGAP:1,40\n#GAP:100\n#BPM:150\n')

    def test_scan_txt_case_insensitive_and_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / 'song.txt').write_text('x')
            (p / 'other.TXT').write_text('x')
            (p / 'song.txt.videogap.bak').write_text('x')
            result = vs.find_txt_files(p)
            self.assertEqual(len(result), 2)
            self.assertEqual({x.name for x in result}, {'song.txt', 'other.TXT'})


if __name__ == '__main__':
    unittest.main()
