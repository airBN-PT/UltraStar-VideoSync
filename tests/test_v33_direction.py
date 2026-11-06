"""Regression for VIDEOGAP direction: video introduction must be positive."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

SCRIPT = Path(__file__).resolve().parents[1] / 'UltraStar_VideoSync.py'
spec = importlib.util.spec_from_file_location('videosync_v33', SCRIPT)
vs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vs)


class VideoGapDirectionTests(unittest.TestCase):
    def test_sign_conversion(self):
        self.assertAlmostEqual(vs._lag_to_videogap(-12.47), 12.47)
        self.assertAlmostEqual(vs._lag_to_videogap(12.47), -12.47)
        self.assertEqual(vs._lag_to_videogap(0), 0)

    def _synthetic_audio(self):
        rate = vs.SAMPLE_RATE
        seconds = 32
        rng = np.random.default_rng(335)
        samples = np.zeros(seconds * rate, dtype=np.float32)
        # Nonrepeating rhythmic events with unique spectra and amplitudes.
        for t in np.arange(.12, seconds - .3, .23):
            length = int(.095 * rate)
            pos = int(t * rate)
            freq = rng.uniform(240, 3100)
            amp = rng.uniform(.18, .8)
            wave = np.sin(2*np.pi*freq*np.arange(length)/rate)
            env = np.exp(-np.linspace(0, 4.8, length))
            samples[pos:pos+length] += (amp * wave * env).astype(np.float32)
        return samples

    def _estimate_with_pcm(self, mp3, video):
        # Real audio matching, but bypasses the container/FFmpeg extraction.
        # First call returns the MP3, second returns the video PCM.
        with mock.patch.object(vs, 'extract_pcm', side_effect=[mp3, video]):
            return vs.estimate_gap('music.mp3', 'clip.mp4', 'ffmpeg')

    def test_video_has_1247_seconds_extra_intro(self):
        audio = self._synthetic_audio()
        delay = int(round(12.47 * vs.SAMPLE_RATE))
        video = np.concatenate((np.zeros(delay, dtype=np.float32), audio))
        gap, confidence, margin = self._estimate_with_pcm(audio, video)
        self.assertAlmostEqual(gap, 12.47, delta=.07)
        self.assertGreaterEqual(confidence, 0)

    def test_mp3_has_913_seconds_extra_intro(self):
        audio = self._synthetic_audio()
        delay = int(round(9.13 * vs.SAMPLE_RATE))
        mp3 = np.concatenate((np.zeros(delay, dtype=np.float32), audio))
        gap, confidence, margin = self._estimate_with_pcm(mp3, audio)
        self.assertAlmostEqual(gap, -9.13, delta=.07)
        self.assertGreaterEqual(confidence, 0)

    def test_equal_start_produces_zero(self):
        audio = self._synthetic_audio()
        gap, confidence, margin = self._estimate_with_pcm(audio, audio.copy())
        self.assertAlmostEqual(gap, 0.0, delta=.07)

    def test_positive_value_written_in_videogap(self):
        with tempfile.TemporaryDirectory() as d:
            txt = Path(d) / 'Test.txt'
            txt.write_bytes(b'#TITLE:Example\r\n#BPM:160\r\n#GAP:100\r\n')
            self.assertTrue(vs.update_txt(txt, 12.47))
            self.assertIn(b'#VIDEOGAP:12,47\r\n#BPM:', txt.read_bytes())
            self.assertEqual(txt.with_suffix('.txt.videogap.bak').read_bytes(),
                             b'#TITLE:Example\r\n#BPM:160\r\n#GAP:100\r\n')

    def test_existing_wrong_negative_value_is_corrected_after_analysis(self):
        with tempfile.TemporaryDirectory() as d:
            txt = Path(d) / 'Test.txt'
            txt.write_bytes(b'#TITLE:Example\n#VIDEOGAP:-12,47\n#BPM:160\n#GAP:100\n')
            self.assertTrue(vs.update_txt(txt, 12.47))
            self.assertIn(b'#VIDEOGAP:12,47\n#BPM:', txt.read_bytes())
            self.assertEqual(txt.read_bytes().count(b'#VIDEOGAP:'), 1)


if __name__ == '__main__':
    unittest.main()
