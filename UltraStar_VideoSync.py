# -*- coding: utf-8 -*-
"""UltraStar Video Sync v3.3 - comparação áudio MP3 / vídeo MP4.

Aplicação local. Nunca recodifica os ficheiros TXT: os ficheiros de 8 bits são
editados byte a byte (através de mapeamento Latin-1 reversível); UTF-16/32 com
BOM mantém exatamente a mesma codificação e BOM.
"""
from __future__ import annotations

import os
import hashlib
import json
import sys
import webbrowser
import queue
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import numpy as np
    from scipy.signal import correlate, butter, sosfilt
except ImportError:
    np = None
    correlate = None
    butter = None
    sosfilt = None

APP_TITLE = "UltraStar Video Sync"
APP_VERSION = "3.3"
SAMPLE_RATE = 11025
FRAME_SAMPLES = 220
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE  # exact clock; avoids drift
MAX_OFFSET_SECONDS = 180
MIN_CONFIDENCE = 0.42
MIN_MARGIN = 0.045
SAME_GAP_TOLERANCE = 0.10  # 100 ms: diferencas inferiores sao consideradas equivalentes

BG = "#eef3f9"
WHITE = "#ffffff"
NAVY = "#14233b"
NAVY_MUTED = "#becce1"
TEXT = "#17283e"
MUTED = "#64748b"
ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
BORDER = "#dce5ef"
GREEN = "#12784a"
AMBER = "#9c6500"
RED = "#b42318"


class NoAudioError(RuntimeError):
    """Um ficheiro MP4 nao contem nenhuma faixa de audio."""


def ffmpeg_path():
    found = shutil.which("ffmpeg")
    if found:
        return found
    local_folder = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
                    else Path(__file__).resolve().parent)
    for name in ("ffmpeg.exe", "ffmpeg"):
        candidate = local_folder / name
        if candidate.is_file():
            return str(candidate)
    return None


def ffprobe_path(ffmpeg):
    found = shutil.which("ffprobe")
    if found:
        return found
    ff = Path(ffmpeg)
    for name in ("ffprobe.exe", "ffprobe"):
        candidate = ff.parent / name
        if candidate.is_file():
            return str(candidate)
    return None


def video_has_audio(path, ffmpeg):
    """True/False; None quando nao e possivel consultar as faixas."""
    ffprobe = ffprobe_path(ffmpeg)
    if not ffprobe:
        return None
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=60,
    )
    if proc.returncode != 0:
        return None
    return bool(proc.stdout.strip())


def decode_header(raw):
    """Descodifica so para LEITURA dos metadados; nao e usado na escrita."""
    for bom, encoding in (
        (b"\xff\xfe\x00\x00", "utf-32"),
        (b"\x00\x00\xfe\xff", "utf-32"),
        (b"\xff\xfe", "utf-16"),
        (b"\xfe\xff", "utf-16"),
        (b"\xef\xbb\xbf", "utf-8-sig"),
    ):
        if raw.startswith(bom):
            return raw.decode(encoding)
    # Formatos sem BOM: preferir UTF-8; nunca assumir que o ficheiro e UTF-8
    # na gravacao, pois os bytes originais sao mantidos de qualquer maneira.
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1")


def parse_header(text, tag):
    match = re.search(r"(?im)^[ \t]*#" + re.escape(tag) + r"[ \t]*:[ \t]*(.*?)[ \t]*$", text)
    return match.group(1).strip() if match else None


def videgap_is_before_bpm_gap(text):
    """A tag tem de estar imediatamente antes da primeira linha BPM/GAP."""
    lines = text.splitlines()
    tag_index = [i for i, line in enumerate(lines)
                 if re.match(r"(?i)^[ \t]*#VIDEOGAP[ \t]*:", line)]
    bpm_gap_index = next((i for i, line in enumerate(lines)
                          if re.match(r"(?i)^[ \t]*#(?:BPM|GAP)[ \t]*:", line)), None)
    return (len(tag_index) == 1 and bpm_gap_index is not None
            and tag_index[0] == bpm_gap_index - 1)


def parse_gap(value):
    if value is None:
        return None
    try:
        return float(value.strip().replace(",", "."))
    except (ValueError, AttributeError):
        return None


def find_txt_files(folder):
    """Cada ficheiro TXT aparece uma so vez, mesmo no Windows."""
    found, seen = [], set()
    for parent, _, names in os.walk(folder):
        for name in names:
            if not name.casefold().endswith(".txt"):
                continue
            path = Path(parent) / name
            key = str(path.resolve()).casefold()
            if key not in seen and path.is_file():
                seen.add(key)
                found.append(path)
    return sorted(found, key=lambda p: str(p).casefold())


def resolve_media(txt_path, declared, extension):
    folder = txt_path.parent
    if declared:
        declared = declared.strip().strip('"')
        candidate = Path(declared.replace("/", os.sep).replace("\\", os.sep))
        if not candidate.is_absolute():
            candidate = folder / candidate
        if candidate.is_file():
            return candidate
        name = Path(declared.replace("\\", "/")).name
        candidate = folder / name
        if candidate.is_file():
            return candidate
        for item in folder.iterdir():
            if item.is_file() and item.name.casefold() == name.casefold():
                return item
    # Basename identico, independentemente de maiusculas da extensao.
    for item in folder.iterdir():
        if (item.is_file() and item.stem.casefold() == txt_path.stem.casefold()
                and item.suffix.casefold() == extension):
            return item
    options = [p for p in folder.iterdir() if p.is_file() and p.suffix.casefold() == extension]
    return options[0] if len(options) == 1 else None


def extract_pcm(path, ffmpeg, workdir, video=False):
    raw = Path(workdir) / ("video.f32" if video else "mp3.f32")
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-nostdin", "-i", str(path), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "f32le", str(raw),
    ]
    proc = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    err = proc.stderr.decode("utf-8", "replace")[-600:]
    if video and ("does not contain any stream" in err.lower()
                  or "matches no streams" in err.lower()):
        raise NoAudioError("O vídeo não tem faixa de áudio.")
    if proc.returncode != 0 or not raw.exists() or raw.stat().st_size < SAMPLE_RATE:
        if video and raw.exists() and raw.stat().st_size == 0:
            raise NoAudioError("O vídeo não tem áudio utilizável.")
        raise RuntimeError("Falha na extração de áudio pelo FFmpeg. " + err.strip())
    data = np.fromfile(raw, dtype="<f4")
    raw.unlink(missing_ok=True)
    return data


def _feature_envelope(samples):
    """Four distinct time-domain features, robust to encoding and equalisation.

    Three band-limited energy envelopes and a full-band transient envelope.
    Each feature is centred/normalised separately, and only 20 ms resolution
    is retained. This makes comparisons affordable even for a large library.
    """
    hop = FRAME_SAMPLES
    n = len(samples) // hop
    if n < 120:
        raise RuntimeError("Áudio demasiado curto para análise.")
    samples = samples[:n * hop].astype(np.float32, copy=False)
    if not np.all(np.isfinite(samples)):
        samples = np.nan_to_num(samples)

    def feature(x, transient=False):
        frames = x.reshape(n, hop)
        rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
        z = np.log1p(rms * 80.0)
        if transient:
            z = np.diff(z, prepend=z[0])
        else:
            # Remove slow changes in mastering, volume and dynamics.
            window = np.ones(41, dtype=np.float32) / 41
            z = z - np.convolve(z, window, mode="same")
        z = z.astype(np.float32)
        z -= z.mean()
        energy = float(np.linalg.norm(z))
        if energy < 1e-5:
            return None
        return z / energy

    # Biquads are applied identically to MP3 and MP4, preserving phase alignment.
    variants = [feature(samples, transient=True)]
    for edges, kind in ((240, "lowpass"), ((240, 1700), "bandpass"),
                        (1700, "highpass")):
        sos = butter(3, edges, btype=kind, fs=SAMPLE_RATE, output="sos")
        band = sosfilt(sos, samples).astype(np.float32)
        variants.append(feature(band))
    if sum(x is not None for x in variants) < 2:
        raise RuntimeError("Áudio silencioso ou sem variação suficiente.")
    return variants


def _normalized_correlations(a, b, lags):
    """Lag-wise normalized cosine, computed by fast convolution + prefix sums."""
    dots = correlate(a, b, mode="full", method="fft")
    # Our lags are a contiguous subset of full_lags.
    indexes = lags + len(b) - 1
    dots = dots[indexes].astype(np.float64)
    n_a, n_b = len(a), len(b)
    left_a = np.maximum(0, lags)
    right_a = np.minimum(n_a, lags + n_b)
    left_b = left_a - lags
    right_b = right_a - lags
    overlap = np.maximum(0, right_a - left_a)
    c_a = np.concatenate(([0.0], np.cumsum(a.astype(np.float64) ** 2)))
    c_b = np.concatenate(([0.0], np.cumsum(b.astype(np.float64) ** 2)))
    e_a = c_a[right_a] - c_a[left_a]
    e_b = c_b[right_b] - c_b[left_b]
    norms = np.sqrt(np.maximum(0, e_a) * np.maximum(0, e_b))
    score = np.divide(dots, norms, out=np.zeros_like(dots), where=norms > 1e-8)
    score *= np.sqrt(overlap / max(1, min(n_a, n_b)))
    return np.clip(score, -1.0, 1.0)


def _window_agreement(mp3_features, video_features, lag):
    """Check the candidate offset in different parts, not just one chorus."""
    n_a = len(next(x for x in mp3_features if x is not None))
    n_b = len(next(x for x in video_features if x is not None))
    lo, hi = max(0, lag), min(n_a, lag + n_b)
    overlap = hi - lo
    if overlap < 8 / FRAME_SECONDS:
        return 0.0, 0
    segment = int(min(22 / FRAME_SECONDS, overlap * 0.22))
    if segment < int(3 / FRAME_SECONDS):
        return 0.0, 0
    scores = []
    for fraction in (0.08, 0.33, 0.58, 0.83):
        x = lo + min(int((overlap - segment) * fraction), overlap - segment)
        y = x + segment
        corr_features = []
        for a, b in zip(mp3_features, video_features):
            if a is None or b is None:
                continue
            aa = a[x:y].astype(np.float64)
            bb = b[x-lag:y-lag].astype(np.float64)
            aa -= aa.mean()
            bb -= bb.mean()
            norm = np.linalg.norm(aa) * np.linalg.norm(bb)
            if norm > 1e-8:
                corr_features.append(float(np.dot(aa, bb) / norm))
        if corr_features:
            scores.append(float(np.mean(corr_features)))
    if not scores:
        return 0.0, 0
    return float(np.mean(np.clip((np.asarray(scores) - 0.05) / 0.65, 0, 1))), sum(s >= 0.16 for s in scores)


def _spectral_agreement(mp3, video, lag):
    """Independent spectral fingerprint cross-check at four song locations.

    Unlike loudness/beat profiles, spectral *changes* distinguish two songs
    with a similar rhythm but different notes and instruments. Uses a short
    FFT and spectral flux, so MP3/AAC lossy encoding is tolerated.
    """
    shift = lag * FRAME_SAMPLES
    start = max(0, shift)
    stop = min(len(mp3), len(video) + shift)
    if stop - start < SAMPLE_RATE * 7:
        return 0.0
    block_len = min(10 * SAMPLE_RATE, int((stop - start) * .18))
    fft_size = 768
    hop = FRAME_SAMPLES
    distances = []
    for fraction in (0.06, 0.31, 0.56, 0.81):
        pos = start + int((stop-start-block_len) * fraction)
        x = mp3[pos:pos+block_len]
        y = video[pos-shift:pos-shift+block_len]
        def flux(audio):
            count = 1 + (len(audio) - fft_size) // hop
            if count < 16:
                return None
            view = np.lib.stride_tricks.sliding_window_view(audio, fft_size)[::hop][:count]
            # A Hann window reduces spectral leakage and encoder delay noise.
            spectrum = np.abs(np.fft.rfft(view * np.hanning(fft_size), axis=1))
            z = np.log1p(6.0 * spectrum[:, 24:320])  # approx 340–4600 Hz
            z = np.diff(z, axis=0)
            # Clamp outlier transients, centre over time; keep timbral changes.
            return z - z.mean(axis=0, keepdims=True)
        f_a, f_b = flux(x), flux(y)
        if f_a is None or f_b is None:
            continue
        length = min(len(f_a), len(f_b))
        best = 0.0
        for delta in range(-2, 3):
            if delta < 0:
                aa, bb = f_a[-delta:length], f_b[:length+delta]
            elif delta > 0:
                aa, bb = f_a[:length-delta], f_b[delta:length]
            else:
                aa, bb = f_a[:length], f_b[:length]
            norm = np.linalg.norm(aa) * np.linalg.norm(bb)
            if norm > 1e-9:
                best = max(best, float(np.sum(aa * bb) / norm))
        distances.append(best)
    if not distances:
        return 0.0
    # Consider the median rather than one chorus with identical drums.
    return float(np.median(distances))


def _waveform_agreement(mp3, video, lag):
    """Very selective final check: high-frequency waveform fingerprint.

    For two encodings of the *same audio*, their waveform remains coherent
    across small sections; different songs with the same beats do not.
    Low scores deliberately keep ambiguous comparisons manual-only.
    """
    offset = lag * FRAME_SAMPLES
    tolerance = int(SAMPLE_RATE * .14)
    start = max(0, offset) + tolerance
    end = min(len(mp3), len(video) + offset) - tolerance
    overlap = end - start
    if overlap < SAMPLE_RATE * 6:
        return 0.0
    duration = min(SAMPLE_RATE * 5, int(overlap * .20))
    if duration < SAMPLE_RATE * 1.5:
        return 0.0
    sos = butter(3, (250, 4000), btype="bandpass", fs=SAMPLE_RATE, output="sos")
    checks = []
    for fraction in (.08, .33, .58, .83):
        pos = start + int((overlap - duration) * fraction)
        x = sosfilt(sos, mp3[pos:pos+duration]).astype(np.float64)
        y_start = pos - offset - tolerance
        y = sosfilt(sos, video[y_start:y_start+duration+2*tolerance]).astype(np.float64)
        if len(y) < len(x):
            continue
        x -= x.mean()
        x_norm = np.linalg.norm(x)
        if x_norm < 1e-8:
            continue
        raw_corr = correlate(y, x, mode="valid", method="fft")
        sums = np.concatenate(([0.0], np.cumsum(y)))
        squares = np.concatenate(([0.0], np.cumsum(y*y)))
        k = len(x)
        norms = np.sqrt(np.maximum(0, squares[k:]-squares[:-k]
                                    - (sums[k:]-sums[:-k])**2/k)) * x_norm
        vals = np.divide(np.abs(raw_corr), norms, out=np.zeros_like(raw_corr),
                         where=norms > 1e-8)
        checks.append(float(np.clip(np.max(vals), 0, 1)))
    return float(np.median(checks)) if checks else 0.0


def _global_waveform_candidate(mp3, video):
    """Seek strong matching samples even when an intro/part of video was cut.

    Decimates to ~1.38 kHz after band limiting. Used only when the normal
    feature match fails its independent waveform check, to save CPU time.
    """
    factor = 8
    sos = butter(4, (130, 610), btype="bandpass", fs=SAMPLE_RATE, output="sos")
    a = sosfilt(sos, mp3).astype(np.float32)[::factor]
    b = sosfilt(sos, video).astype(np.float32)[::factor]
    a -= a.mean()
    b -= b.mean()
    if np.linalg.norm(a) < 1e-5 or np.linalg.norm(b) < 1e-5:
        return None
    resolution = factor / SAMPLE_RATE
    limit = int(MAX_OFFSET_SECONDS / resolution)
    lags = np.arange(max(-len(b)+1, -limit), min(len(a)-1, limit)+1, dtype=np.int32)
    overlap = np.maximum(0, np.minimum(len(a), lags + len(b)) - np.maximum(0, lags))
    minimum = min(int(20/resolution), int(.50*min(len(a),len(b))))
    scores = np.abs(_normalized_correlations(a, b, lags))
    scores[overlap < minimum] = -1
    idx = int(np.argmax(scores))
    best = float(scores[idx])
    outside = np.abs(lags - lags[idx]) > int(1.2/resolution)
    runner_up = float(np.max(scores[outside])) if np.any(outside) else 0.0
    return float(lags[idx] * resolution), best, best - runner_up


def _lag_to_videogap(lag_seconds):
    """Convert the correlation lag to the UltraStar-ES VIDEOGAP convention.

    Correlation matches mp3[t] with video[t - lag]. Therefore a video with
    12.47 seconds of extra introduction has lag=-12.47, but its VIDEOGAP
    must be +12.47: skip those first 12.47 seconds of the VIDEO on playback.

    When video starts *ahead* of the MP3, it requires a negative VIDEOGAP
    so the video can be delayed. This sign conversion must happen ONLY at
    the public estimator boundary: all internal checks use correlation lags.
    """
    return -float(lag_seconds)


def estimate_gap(mp3_path, mp4_path, ffmpeg):
    """Return (UltraStar-ES VIDEOGAP seconds, confidence, peak-margin).

    Positive VIDEOGAP skips time at the beginning of the video; negative
    VIDEOGAP delays the video. Internal correlation lags use opposite signs.

    Fuses transient + three frequency-band energy profiles. Uses properly
    normalised overlap scores, checks the margin to other remote peaks, and
    validates the same delay in multiple independent song sections.
    Ambiguous matches remain review-only; no estimator can infer edits/cuts.
    """
    with tempfile.TemporaryDirectory(prefix="us_sync_") as tmp:
        a = extract_pcm(mp3_path, ffmpeg, tmp)
        b = extract_pcm(mp4_path, ffmpeg, tmp, video=True)
    if not len(b) or np.max(np.abs(b)) < 1e-5:
        raise NoAudioError("O áudio do vídeo está silencioso.")
    features_a = _feature_envelope(a)
    features_b = _feature_envelope(b)
    n_a = len(next(x for x in features_a if x is not None))
    n_b = len(next(x for x in features_b if x is not None))
    max_lag = int(MAX_OFFSET_SECONDS / FRAME_SECONDS)
    lags = np.arange(max(-n_b + 1, -max_lag), min(n_a - 1, max_lag) + 1, dtype=np.int32)
    start = np.maximum(0, lags)
    end = np.minimum(n_a, lags + n_b)
    overlap = np.maximum(0, end - start)
    min_overlap = min(int(20 / FRAME_SECONDS), int(min(n_a, n_b) * 0.50))
    eligible = overlap >= min_overlap
    if not np.any(eligible):
        raise RuntimeError("Áudio demasiado curto para uma correspondência fiável.")
    parts = []
    weights = []
    for weight, f_a, f_b in zip((1.10, 0.85, 1.25, 1.0), features_a, features_b):
        if f_a is not None and f_b is not None:
            parts.append(_normalized_correlations(f_a, f_b, lags))
            weights.append(weight)
    if len(parts) < 2:
        raise RuntimeError("Não foi possível obter características comparáveis.")
    combined = np.average(np.asarray(parts), axis=0, weights=weights)
    combined[~eligible] = -2
    idx = int(np.argmax(combined))
    peak = float(combined[idx])
    peak_lag = int(lags[idx])
    precise_lag_seconds = None
    initial_waveform = _waveform_agreement(a, b, peak_lag)
    if initial_waveform < .35:
        wave_candidate = _global_waveform_candidate(a, b)
        if wave_candidate is not None:
            wave_seconds, wave_strength, wave_margin = wave_candidate
            candidate_lag = int(round(wave_seconds / FRAME_SECONDS))
            candidate_waveform = _waveform_agreement(a, b, candidate_lag)
            if wave_strength >= .48 and candidate_waveform >= .38 and candidate_waveform > initial_waveform + .17:
                peak_lag = candidate_lag
                idx = int(np.argmin(np.abs(lags - peak_lag)))
                peak = max(float(combined[idx]), .9 * wave_strength)
                precise_lag_seconds = wave_seconds
    distant = np.abs(lags - peak_lag) > int(2.0 / FRAME_SECONDS)
    second = float(np.max(combined[distant])) if np.any(distant) else 0.0
    margin = max(0.0, peak - max(0.0, second))
    if precise_lag_seconds is not None:
        margin = max(margin, wave_margin)
    agreement, agreeing_windows = _window_agreement(features_a, features_b, peak_lag)

    # Conservative confidence: strength, uniqueness and 2+ separated checks.
    strength = np.clip((peak - 0.09) / 0.72, 0.0, 1.0)
    uniqueness = np.clip(margin / 0.14, 0.0, 1.0)
    consistency = 0.30 + 0.70 * agreement
    if agreeing_windows < 2:
        consistency *= 0.25
    spectral = _spectral_agreement(a, b, peak_lag)
    spectral_support = np.clip((spectral - 0.04) / 0.30, 0.0, 1.0)
    waveform = _waveform_agreement(a, b, peak_lag)
    waveform_support = np.clip((waveform - 0.10) / 0.32, 0.0, 1.0)
    confidence = float(np.clip(strength * (0.4 + 0.6 * uniqueness)
                               * consistency * spectral_support * waveform_support, 0, 0.99))

    # Quadratic interpolation around the strongest 20 ms score (sub-frame).
    fraction = 0.0
    if 0 < idx < len(combined) - 1:
        left, centre, right = combined[idx-1:idx+2]
        denominator = left - 2 * centre + right
        if denominator < -1e-8:
            fraction = float(np.clip(0.5 * (left - right) / denominator, -0.5, 0.5))
    matched_lag = (float(precise_lag_seconds) if precise_lag_seconds is not None
                   else float((peak_lag + fraction) * FRAME_SECONDS))
    # Invert exactly once; consistency checks and waveform refinement above
    # all operate in correlation-lag coordinates, NOT user-facing VIDEOGAP.
    return _lag_to_videogap(matched_lag), confidence, margin


def _decode_losslessly(raw):
    """Retorna (texto, prefixo, codificacao) sem perder nem converter bytes.

    Nos formatos comuns de 8 bits, Latin-1 e um transporte reversivel de bytes:
    permite alterar apenas linhas ASCII das tags, sem tocar nos restantes bytes.
    """
    for bom, encoding in (
        (b"\xff\xfe\x00\x00", "utf-32-le"),
        (b"\x00\x00\xfe\xff", "utf-32-be"),
        (b"\xff\xfe", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be"),
        (b"\xef\xbb\xbf", "latin-1"),
    ):
        if raw.startswith(bom):
            return raw[len(bom):].decode(encoding), bom, encoding
    # NULs sao sinal de uma codificacao nao reconhecida; e mais seguro nao
    # editar do que correr o risco de danificar ficheiros UTF-16 sem BOM.
    if b"\x00" in raw[:256]:
        raise RuntimeError("Codificação sem BOM não reconhecida. TXT não alterado.")
    return raw.decode("latin-1"), b"", "latin-1"


def _preferred_newline(lines):
    for line in lines:
        if line.endswith("\r\n"):
            return "\r\n"
        if line.endswith("\n"):
            return "\n"
        if line.endswith("\r"):
            return "\r"
    return os.linesep


def _insert_videogap(raw, gap):
    text, bom, encoding = _decode_losslessly(raw)
    lines = text.splitlines(keepends=True)
    # Formato fixo UltraStar solicitado: duas casas decimais e virgula,
    # independentemente de #GAP, #BPM ou VIDEOGAP anteriores terem ponto.
    number = f"{gap:.2f}"
    if number == "-0.00":
        number = "0.00"
    line = "#VIDEOGAP:" + number.replace(".", ",")

    # Retirar qualquer valor antigo, sem alterar as outras linhas.
    tag_pattern = re.compile(r"(?i)^[ \t]*#VIDEOGAP[ \t]*:")
    lines = [item for item in lines if not tag_pattern.match(item)]
    bpm_gap_pattern = re.compile(r"(?i)^[ \t]*#(?:BPM|GAP)[ \t]*:")
    insertion = next((i for i, item in enumerate(lines) if bpm_gap_pattern.match(item)), None)

    if insertion is None:
        raise RuntimeError("Ficheiro sem #BPM ou #GAP: atualização cancelada por segurança.")

    newline = _preferred_newline(lines)
    # Se o ficheiro nao termina com newline e a insercao e no fim, evitar
    # colar a nova tag ao texto da ultima linha.
    if insertion == len(lines) and lines and not lines[-1].endswith(("\r\n", "\n", "\r")):
        lines[-1] += newline
    lines.insert(insertion, line + newline)
    encoded = "".join(lines).encode(encoding)
    return bom + encoded


def _strip_videogap_only(raw):
    """Extract all non-VIDEOGAP bytes, preserving original encoding and BOM."""
    body, bom, encoding = _decode_losslessly(raw)
    tag_rx = re.compile(r"(?i)^[ \t]*#VIDEOGAP[ \t]*:")
    lines = body.splitlines(keepends=True)
    tag_count = sum(bool(tag_rx.match(line)) for line in lines)
    non_tag = "".join(line for line in lines if not tag_rx.match(line))
    return bom + non_tag.encode(encoding), tag_count


def validate_txt_change(before, after):
    """Mandatory strict invariant: ONLY one VIDEOGAP tag may be added/changed.

    Called both before writing and after re-reading from disk.
    If the source already contained a tag, moving/replacing it is permitted,
    but the exact bytes of every other line must remain identical.
    """
    before_untouched, count_before = _strip_videogap_only(before)
    after_untouched, count_after = _strip_videogap_only(after)
    if before_untouched != after_untouched:
        raise RuntimeError("Falha de integridade: foi detetada uma alteração fora do #VIDEOGAP.")
    if count_after != 1:
        raise RuntimeError("Falha de integridade: deve existir exatamente uma etiqueta #VIDEOGAP.")
    new_text = decode_header(after)
    if not videgap_is_before_bpm_gap(new_text):
        raise RuntimeError("Falha de integridade: o #VIDEOGAP não ficou antes do #BPM/#GAP.")
    if count_before == 0 and count_after != count_before + 1:
        raise RuntimeError("Falha de integridade: a etiqueta não foi acrescentada como esperado.")
    return True


def settings_file():
    base = Path(os.environ.get("APPDATA", str(Path.home() / ".config"))) if os.name == "nt" else Path.home() / ".config"
    return base / "UltraStar_VideoSync" / "settings.json"


def load_settings(path=None):
    settings = {"language": "en", "backup_mode": "local"}
    try:
        raw = json.loads(Path(path or settings_file()).read_text(encoding="utf-8"))
        if raw.get("language") in ("pt", "en", "es", "fr"):
            settings["language"] = raw["language"]
        if raw.get("backup_mode") in ("local", "central"):
            settings["backup_mode"] = raw["backup_mode"]
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return settings


def save_settings(settings, path=None):
    dest = Path(path or settings_file())
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=dest.parent,
                                         prefix="settings_", suffix=".tmp", delete=False) as handle:
            temp_path = Path(handle.name)
            json.dump(settings, handle, indent=2, ensure_ascii=False)
        os.replace(temp_path, dest)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def get_backup_path(txt_path, mode="local", central_root=None):
    txt_path = Path(txt_path).resolve()
    if mode == "local":
        return txt_path.with_suffix(txt_path.suffix + ".videogap.bak")
    if mode != "central":
        raise ValueError("Modo de backup desconhecido.")
    # Central backups are plain copies of the TXT, retaining its EXACT filename.
    # No subdirectories, hashes or .bak extensions are used in this mode.
    root = Path(central_root) if central_root is not None else Path("C:/UltraStar_VideoSync Backup")
    return root / txt_path.name


def _backup_original(src, data, backup_path, verify_same_song=False):
    """Back up exactly once and prevent accidental overwriting of unrelated songs.

    A central backup shares its filename with its source, so two distinct
    songs may collide. An existing central backup can be reused safely only
    when all non-VIDEOGAP bytes match the current source. This also permits
    reapplying a new VIDEOGAP to the SAME song while retaining its first backup.
    """
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with backup_path.open("xb") as handle:
            created = True
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if backup_path.is_symlink() or not backup_path.is_file() or backup_path.stat().st_size == 0:
            raise RuntimeError("O ficheiro de backup existente está inválido.")
        if verify_same_song:
            existing_data = backup_path.read_bytes()
            try:
                existing_base, _ = _strip_videogap_only(existing_data)
                current_base, _ = _strip_videogap_only(data)
            except Exception as exc:
                raise RuntimeError("Não foi possível validar o backup central existente.") from exc
            if existing_base != current_base:
                raise RuntimeError(
                    f"Já existe um backup central com o nome '{backup_path.name}', "
                    "mas o conteúdo não corresponde a este TXT. "
                    "Para evitar substituir o backup de outra música, o TXT não foi alterado. "
                    "Usa o backup na pasta original ou resolve o conflito de nomes."
                )
        return
    except Exception:
        if created:
            backup_path.unlink(missing_ok=True)
        raise
    try:
        shutil.copystat(src, backup_path)
    except OSError:
        pass
    if backup_path.read_bytes() != data:
        backup_path.unlink(missing_ok=True)
        raise RuntimeError("Não foi possível verificar o backup; TXT não alterado.")


def _write_atomic(path, data):
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                         prefix=".videogap_", suffix=".tmp",
                                         delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        shutil.copymode(path, temp_path)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def update_txt(txt_path, gap, backup_mode="local", central_root=None,
               expected_digest=None):
    """Apply a VIDEOGAP in a checked, atomic and reversible operation.

    Before and after file-writing, compare byte-for-byte all NON-VIDEOGAP
    lines. The original bytes are first saved locally or on C:.
    """
    txt_path = Path(txt_path)
    original = txt_path.read_bytes()
    if expected_digest is not None and hashlib.sha256(original).hexdigest() != expected_digest:
        raise RuntimeError("O TXT foi alterado desde a análise. Volta a analisar antes de aplicar.")
    changed = _insert_videogap(original, gap)
    validate_txt_change(original, changed)
    if changed == original:
        return False
    backup = get_backup_path(txt_path, backup_mode, central_root)
    _backup_original(txt_path, original, backup, verify_same_song=(backup_mode == "central"))
    if txt_path.read_bytes() != original:
        raise RuntimeError("O TXT mudou durante a criação do backup. Volta a analisar.")
    try:
        _write_atomic(txt_path, changed)
        on_disk = txt_path.read_bytes()
        if on_disk != changed:
            raise RuntimeError("Verificação de leitura: os bytes gravados não correspondem ao previsto.")
        validate_txt_change(original, on_disk)
    except Exception:
        # Recovery is attempted only when this operation has changed the TXT.
        if txt_path.read_bytes() != original:
            _write_atomic(txt_path, original)
        raise
    return True


def fmt_gap(gap, sign=False):
    if gap is None:
        return "—"
    if abs(gap) < 0.0005:
        gap = 0
    value = (f"{gap:+.2f}" if sign else f"{gap:.2f}")
    return value.replace(".", ",") + " s"


# Interface strings: keys are deliberately stable Portuguese source text.
# Fallback to the source key makes untranslated errors readable.
LANGUAGES = {"en": "English", "pt": "Português (Portugal)", "es": "Español", "fr": "Français"}
TR = {
"en": {
 "Created by airBN": "Created by airBN", "Settings": "Options", "Community": "UltraStar community",
 "Save": "Save", "Cancel": "Cancel", "Language": "Language", "Backup location": "Backup location",
 "Same folder": "Beside each song TXT (.videogap.bak)", "Central folder": "C:\\UltraStar_VideoSync Backup",
 "Central note": "Backups in C: keep the original TXT filename, with no subfolders. Same-name conflicts are blocked.",
 "Options saved": "Options saved. Changes will apply to future edits.",
 "Hero": "Synchronization between music and video.",
 "Library folder": "Main song library folder", "Browse": "Browse…", "Scan": "Analyze songs",
 "Auto": "Automatically apply only reliable suggestions", "Threshold": "Minimum confidence",
 "Recommended": "(0.42 recommended)", "Found": "TXT files", "Analyzed": "Analyzed",
 "Suggestions": "Suggestions", "Correct": "Already correct", "Ignored": "Skipped / errors",
 "Results": "Results by song", "Hint": "Apply per row · Multiple selection available",
 "Song": "Song / TXT", "Current": "Current", "Suggested": "Suggested",
 "Difference": "Difference", "Confidence": "Confidence", "Status": "Status", "Action": "Action",
 "Apply": "Apply", "Apply selected": "Apply selected", "Open song folder": "Open song folder",
 "Clear": "Clear list", "Choose folder": "Select the main UltraStar songs folder",
 "Ready": "Choose the main song folder to begin.",
 "Dependencies": "Missing dependencies", "Missing libs": "Install Python packages using instalar.bat.",
 "Missing ffmpeg": "FFmpeg not found", "FFmpeg hint": "Add FFmpeg to PATH or place ffmpeg.exe beside the program.",
 "Invalid folder": "Invalid folder", "Select valid": "Select a valid folder.",
 "No songs": "No songs found", "No TXT": "No TXT files were found in this folder.",
 "Invalid confidence": "Invalid confidence", "Enter threshold": "Enter a value between 0.10 and 0.95.",
 "Scanning": "Analyzing {total} unique TXT files…", "Progress": "Analyzing {done} of {total} songs…",
 "Finished": "Done: {total} unique TXT files. Files change only when you apply or enable automatic mode.",
 "Cleared": "List cleared. Choose a folder and run a new analysis.",
 "Weak title": "Review this result", "Weak question": "This suggestion has low confidence.\n\nSong: {song}\nSuggested VIDEOGAP: {gap}\n\nApply anyway?",
 "Applied status": "Applied: {song} · {gap} · backup and TXT verified",
 "Save error": "Could not save", "No selected": "No suggestions selected",
 "Select rows": "Select one or more songs with pending suggestions.",
 "Confirm apply": "Apply suggestions", "Confirm multi": "Update {n} TXT files?\nBackups: {backup}.{warn}",
 "Low warning": "\nWarning: {n} result(s) have low confidence.",
 "Completed": "Operation complete", "Done errors": "Applied: {done}\nErrors: {errors}",
 "Open folder error": "Open folder", "Batch failure": "Failed: {name} — {error}",
 "Restored": "Original restored after a failed integrity check.",
 "Info": "Result details", "Info body": "Song: {name}\nCurrent: {current}\nSuggested: {suggested}\nConfidence: {confidence}\nPeak margin: {margin:.3f}\n\nThe estimate is not a guarantee if the video audio has edits, cuts or time-stretching.",
 "About": "About / links", "Backup info": "Backup mode: {mode}",
 "applied": "Applied", "correct": "Already correct", "same_review": "Matches · review",
 "reposition": "Move tag into position", "improved": "Improvement suggested",
 "difference_review": "Different · review", "ready": "Ready to apply", "review": "Review manually",
 "missing": "Skipped · MP3/MP4 missing", "silent": "Skipped · video has no audio",
 "write_error": "Error writing TXT", "analysis_error": "Analysis error"
},
"es": {
 "Created by airBN": "Programa creado por airBN", "Settings": "Opciones", "Community": "Comunidad UltraStar",
 "Save": "Guardar", "Cancel": "Cancelar", "Language": "Idioma", "Backup location": "Ubicación de copias de seguridad",
 "Same folder": "Junto a cada TXT (.videogap.bak)", "Central folder": "C:\\UltraStar_VideoSync Backup",
 "Central note": "Las copias en C: conservan el nombre original del TXT, sin subcarpetas. Se bloquean nombres duplicados incompatibles.",
 "Options saved": "Opciones guardadas. Se aplicarán a las próximas ediciones.",
 "Hero": "Sincronización entre música y vídeo.",
 "Library folder": "Carpeta principal de canciones", "Browse": "Examinar…", "Scan": "Analizar canciones",
 "Auto": "Aplicar automáticamente solo sugerencias fiables", "Threshold": "Confianza mínima",
 "Recommended": "(0,42 recomendado)", "Found": "Archivos TXT", "Analyzed": "Analizados",
 "Suggestions": "Sugerencias", "Correct": "Ya correctos", "Ignored": "Omitidos / errores",
 "Results": "Resultados por canción", "Hint": "Aplicar en cada fila · Selección múltiple disponible",
 "Song": "Canción / TXT", "Current": "Actual", "Suggested": "Sugerido",
 "Difference": "Diferencia", "Confidence": "Confianza", "Status": "Estado", "Action": "Acción",
 "Apply": "Aplicar", "Apply selected": "Aplicar seleccionadas", "Open song folder": "Abrir carpeta de canción",
 "Clear": "Limpiar lista", "Choose folder": "Selecciona la carpeta principal de canciones UltraStar",
 "Ready": "Selecciona la carpeta principal para comenzar.",
 "Dependencies": "Faltan dependencias", "Missing libs": "Instala las bibliotecas con instalar.bat.",
 "Missing ffmpeg": "No se encuentra FFmpeg", "FFmpeg hint": "Añade FFmpeg a PATH o pon ffmpeg.exe junto al programa.",
 "Invalid folder": "Carpeta no válida", "Select valid": "Selecciona una carpeta válida.",
 "No songs": "Sin canciones", "No TXT": "No se encontraron archivos TXT en esta carpeta.",
 "Invalid confidence": "Confianza no válida", "Enter threshold": "Introduce un valor entre 0,10 y 0,95.",
 "Scanning": "Analizando {total} archivos TXT únicos…", "Progress": "Analizando {done} de {total} canciones…",
 "Finished": "Terminado: {total} archivos TXT únicos. Solo se modifican al aplicar o activar el modo automático.",
 "Cleared": "Lista vacía. Selecciona una carpeta y vuelve a analizar.",
 "Weak title": "Revisar resultado", "Weak question": "Esta sugerencia tiene poca confianza.\n\nCanción: {song}\nVIDEOGAP sugerido: {gap}\n\n¿Aplicar igualmente?",
 "Applied status": "Aplicado: {song} · {gap} · copia y TXT verificados",
 "Save error": "No se pudo guardar", "No selected": "No hay sugerencias seleccionadas",
 "Select rows": "Selecciona canciones con sugerencias pendientes.",
 "Confirm apply": "Aplicar sugerencias", "Confirm multi": "¿Actualizar {n} archivos TXT?\nCopias: {backup}.{warn}",
 "Low warning": "\nAtención: {n} resultado(s) tienen poca confianza.",
 "Completed": "Operación terminada", "Done errors": "Aplicados: {done}\nErrores: {errors}",
 "Open folder error": "Abrir carpeta", "Batch failure": "Error: {name} — {error}",
 "Info": "Detalles del resultado", "Info body": "Canción: {name}\nActual: {current}\nSugerido: {suggested}\nConfianza: {confidence}\nMargen entre picos: {margin:.3f}\n\nLa estimación puede fallar si el vídeo contiene cortes, ediciones o cambios de velocidad.",
 "About": "Acerca de / enlaces", "Backup info": "Copias: {mode}",
 "applied": "Aplicado", "correct": "Ya correcto", "same_review": "Coincide · revisar",
 "reposition": "Recolocar etiqueta", "improved": "Mejora sugerida", "difference_review": "Diferencia · revisar",
 "ready": "Listo para aplicar", "review": "Revisar manualmente",
 "missing": "Omitido · falta MP3/MP4", "silent": "Omitido · vídeo sin audio",
 "write_error": "Error al escribir TXT", "analysis_error": "Error de análisis"
},
"fr": {
 "Created by airBN": "Programme créé par airBN", "Settings": "Options", "Community": "Communauté UltraStar",
 "Save": "Enregistrer", "Cancel": "Annuler", "Language": "Langue", "Backup location": "Emplacement des sauvegardes",
 "Same folder": "À côté de chaque TXT (.videogap.bak)", "Central folder": "C:\\UltraStar_VideoSync Backup",
 "Central note": "Dans C:, les sauvegardes gardent le nom original du TXT, sans sous-dossiers. Les conflits de noms sont bloqués.",
 "Options saved": "Options enregistrées pour les prochaines modifications.",
 "Hero": "Synchronisation entre musique et vidéo.",
 "Library folder": "Dossier principal des chansons", "Browse": "Parcourir…", "Scan": "Analyser les chansons",
 "Auto": "Appliquer automatiquement les résultats fiables", "Threshold": "Confiance minimale",
 "Recommended": "(0,42 recommandé)", "Found": "Fichiers TXT", "Analyzed": "Analysés",
 "Suggestions": "Suggestions", "Correct": "Déjà corrects", "Ignored": "Ignorés / erreurs",
 "Results": "Résultats par chanson", "Hint": "Appliquer par ligne · Sélection multiple disponible",
 "Song": "Chanson / TXT", "Current": "Actuel", "Suggested": "Suggéré",
 "Difference": "Différence", "Confidence": "Confiance", "Status": "État", "Action": "Action",
 "Apply": "Appliquer", "Apply selected": "Appliquer la sélection", "Open song folder": "Ouvrir le dossier",
 "Clear": "Vider la liste", "Choose folder": "Sélectionnez le dossier principal UltraStar",
 "Ready": "Sélectionnez le dossier principal pour commencer.",
 "Dependencies": "Dépendances manquantes", "Missing libs": "Installez les bibliothèques avec instalar.bat.",
 "Missing ffmpeg": "FFmpeg introuvable", "FFmpeg hint": "Ajoutez FFmpeg au PATH ou placez ffmpeg.exe à côté du programme.",
 "Invalid folder": "Dossier invalide", "Select valid": "Sélectionnez un dossier valide.",
 "No songs": "Aucune chanson", "No TXT": "Aucun fichier TXT trouvé dans ce dossier.",
 "Invalid confidence": "Confiance invalide", "Enter threshold": "Indiquez une valeur entre 0,10 et 0,95.",
 "Scanning": "Analyse de {total} fichiers TXT uniques…", "Progress": "Analyse de {done} chanson(s) sur {total}…",
 "Finished": "Terminé : {total} fichiers TXT uniques. Aucune modification sans application ou mode automatique.",
 "Cleared": "Liste vidée. Choisissez un dossier et relancez l'analyse.",
 "Weak title": "Résultat à vérifier", "Weak question": "Cette suggestion est peu fiable.\n\nChanson : {song}\nVIDEOGAP suggéré : {gap}\n\nL'appliquer quand même ?",
 "Applied status": "Appliqué : {song} · {gap} · sauvegarde et TXT vérifiés",
 "Save error": "Enregistrement impossible", "No selected": "Aucune suggestion sélectionnée",
 "Select rows": "Sélectionnez une ou plusieurs chansons avec des suggestions à appliquer.",
 "Confirm apply": "Appliquer les suggestions", "Confirm multi": "Modifier {n} fichiers TXT ?\nSauvegardes : {backup}.{warn}",
 "Low warning": "\nAttention : {n} résultat(s) peu fiables.",
 "Completed": "Opération terminée", "Done errors": "Appliqués : {done}\nErreurs : {errors}",
 "Open folder error": "Ouvrir le dossier", "Batch failure": "Échec : {name} — {error}",
 "Info": "Détails du résultat", "Info body": "Chanson : {name}\nActuel : {current}\nSuggéré : {suggested}\nConfiance : {confidence}\nMarge des pics : {margin:.3f}\n\nLa valeur peut être incorrecte si la vidéo est coupée, remontée ou accélérée.",
 "About": "À propos / liens", "Backup info": "Sauvegarde : {mode}",
 "applied": "Appliqué", "correct": "Déjà correct", "same_review": "Correspond · vérifier",
 "reposition": "Replacer le tag", "improved": "Amélioration suggérée", "difference_review": "Différence · vérifier",
 "ready": "Prêt à appliquer", "review": "Vérification manuelle",
 "missing": "Ignoré · MP3/MP4 absent", "silent": "Ignoré · vidéo sans audio",
 "write_error": "Erreur d'écriture TXT", "analysis_error": "Erreur d'analyse"
}}
TR_PT = {
 "Created by airBN": "Programa criado por airBN", "Settings": "Opções", "Community": "Comunidade UltraStar",
 "Save": "Guardar", "Cancel": "Cancelar", "Language": "Idioma", "Backup location": "Local do backup",
 "Same folder": "Na pasta de cada TXT (.videogap.bak)", "Central folder": "C:\\UltraStar_VideoSync Backup",
 "Central note": "Backups em C: com o nome original do TXT, sem subpastas. Se houver nomes iguais e conteúdos diferentes, a gravação é bloqueada.",
 "Options saved": "Opções guardadas. Serão aplicadas nas próximas alterações.",
 "Hero": "Sincronização entre música e vídeo",
 "Library folder": "Pasta principal das músicas", "Browse": "Procurar…", "Scan": "Analisar músicas",
 "Auto": "Aplicar automaticamente apenas sugestões fiáveis", "Threshold": "Confiança mínima",
 "Recommended": "(0,42 recomendado)", "Found": "TXT encontrados", "Analyzed": "Analisados",
 "Suggestions": "Sugestões", "Correct": "Já corretos", "Ignored": "Ignorados / erros",
 "Results": "Resultados por música", "Hint": "Cada linha tem Aplicar · Seleção múltipla disponível",
 "Song": "Música / TXT", "Current": "Atual", "Suggested": "Sugerido",
 "Difference": "Diferença", "Confidence": "Confiança", "Status": "Estado", "Action": "Ação",
 "Apply": "Aplicar", "Apply selected": "Aplicar selecionadas", "Open song folder": "Abrir pasta da música",
 "Clear": "Limpar lista", "Choose folder": "Seleciona a pasta principal das músicas UltraStar",
 "Ready": "Escolhe a pasta principal da biblioteca para começar.",
 "Dependencies": "Dependências em falta", "Missing libs": "Instala as bibliotecas com instalar.bat.",
 "Missing ffmpeg": "FFmpeg não encontrado", "FFmpeg hint": "Adiciona o FFmpeg ao PATH ou coloca ffmpeg.exe ao lado do programa.",
 "Invalid folder": "Pasta inválida", "Select valid": "Seleciona uma pasta válida.",
 "No songs": "Sem músicas", "No TXT": "Não encontrei ficheiros TXT nesta pasta.",
 "Invalid confidence": "Confiança inválida", "Enter threshold": "Indica um valor entre 0,10 e 0,95.",
 "Scanning": "A analisar {total} ficheiros TXT únicos…", "Progress": "A analisar {done} de {total} músicas…",
 "Finished": "Concluído: {total} ficheiros TXT únicos. Só são alterados ao aplicar ou com o modo automático ativo.",
 "Cleared": "Lista limpa. Escolhe uma pasta e começa uma nova análise.",
 "Weak title": "Resultado a rever", "Weak question": "Esta sugestão tem confiança reduzida.\n\nMúsica: {song}\nVIDEOGAP sugerido: {gap}\n\nTens a certeza de que queres aplicar?",
 "Applied status": "Aplicado: {song} · {gap} · backup e TXT verificados",
 "Save error": "Não foi possível gravar", "No selected": "Sem sugestões selecionadas",
 "Select rows": "Seleciona uma ou mais músicas com sugestões por aplicar.",
 "Confirm apply": "Aplicar sugestões", "Confirm multi": "Atualizar {n} ficheiro(s) TXT?\nBackups: {backup}.{warn}",
 "Low warning": "\nAtenção: {n} resultado(s) têm confiança reduzida.",
 "Completed": "Operação concluída", "Done errors": "Aplicados: {done}\nErros: {errors}",
 "Open folder error": "Abrir pasta", "Batch failure": "Falhou: {name} — {error}",
 "Info": "Detalhes do resultado", "Info body": "Música: {name}\nAtual: {current}\nSugerido: {suggested}\nConfiança: {confidence}\nMargem entre picos: {margin:.3f}\n\nA estimativa pode falhar em vídeos com cortes, edições ou mudanças de velocidade.",
 "About": "Sobre / links", "Backup info": "Backup: {mode}",
 "applied": "Aplicado", "correct": "Já correto", "same_review": "Coincide · rever",
 "reposition": "Reposicionar etiqueta", "improved": "Melhoria sugerida", "difference_review": "Diferença · rever",
 "ready": "Pronto a aplicar", "review": "Rever manualmente",
 "missing": "Ignorado · MP3/MP4 em falta", "silent": "Ignorado · vídeo sem áudio",
 "write_error": "Erro ao gravar", "analysis_error": "Erro na análise"
}


class App:
    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        self.lang = self.settings["language"]
        self.backup_mode = self.settings["backup_mode"]
        self.root.title(f"{APP_TITLE} · v{APP_VERSION}")
        self.root.geometry("1320x830")
        self.root.minsize(1060, 660)
        self.root.configure(bg=BG)
        self.q = queue.Queue()
        self.results = []
        self.row_by_iid = {}
        self.folder = tk.StringVar()
        self.apply_var = tk.BooleanVar(value=False)
        self.conf_var = tk.DoubleVar(value=MIN_CONFIDENCE)
        self.status = tk.StringVar(value=self.t("Ready"))
        self.stats = {k: tk.StringVar(value="0") for k in
                      ("total", "analyzed", "suggestions", "correct", "ignored")}
        self.running = False
        self.buttons = []
        self.layout_pending = False
        self._setup_style()
        self._build()
        self.root.after(120, self._poll)

    def t(self, key):
        return (TR_PT if self.lang == "pt" else TR.get(self.lang, {})).get(key, key)

    def _setup_style(self):
        s = ttk.Style(self.root)
        if "clam" in s.theme_names():
            s.theme_use("clam")
        s.configure("TFrame", background=BG)
        s.configure("Card.TFrame", background=WHITE)
        s.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        s.configure("Card.TLabel", background=WHITE, foreground=TEXT, font=("Segoe UI", 10))
        s.configure("Muted.TLabel", background=WHITE, foreground=MUTED, font=("Segoe UI", 9))
        s.configure("TCheckbutton", background=WHITE, foreground=TEXT, font=("Segoe UI", 10))
        s.map("TCheckbutton", background=[("active", WHITE)])
        s.configure("TEntry", padding=7, fieldbackground=WHITE, foreground=TEXT)
        s.configure("TSpinbox", padding=5, fieldbackground=WHITE)
        s.configure("TButton", padding=(10, 7), font=("Segoe UI", 10),
                    background="#e7edf5", foreground=TEXT, borderwidth=0)
        s.map("TButton", background=[("active", "#d6e2f0")])
        s.configure("Primary.TButton", background=ACCENT, foreground=WHITE,
                    padding=(14, 9), font=("Segoe UI", 10, "bold"), borderwidth=0)
        s.map("Primary.TButton", background=[("active", ACCENT_DARK),
                                            ("disabled", "#9fb6da")],
              foreground=[("disabled", WHITE)])
        s.configure("Action.TButton", background=ACCENT, foreground=WHITE,
                    padding=(6, 2), font=("Segoe UI", 9, "bold"), borderwidth=0)
        s.map("Action.TButton", background=[("active", ACCENT_DARK)])
        s.configure("Treeview", background=WHITE, fieldbackground=WHITE,
                    foreground=TEXT, rowheight=36, borderwidth=0,
                    font=("Segoe UI", 9))
        s.configure("Treeview.Heading", background="#e9eff8", foreground=TEXT,
                    font=("Segoe UI", 9, "bold"), padding=(10, 10), borderwidth=0)
        s.map("Treeview", background=[("selected", "#dceaff")],
              foreground=[("selected", TEXT)])
        s.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#dce7f4",
                    bordercolor="#dce7f4", lightcolor=ACCENT, darkcolor=ACCENT)

    def _build(self):
        hero = tk.Frame(self.root, bg=NAVY, padx=24, pady=16)
        hero.pack(fill="x")
        h_left = tk.Frame(hero, bg=NAVY)
        h_left.pack(side="left", fill="x", expand=True)
        tk.Label(h_left, text="UltraStar  /  Video Sync", bg=NAVY, fg=WHITE,
                 font=("Segoe UI", 21, "bold")).pack(anchor="w")
        tk.Label(h_left, text=self.t("Hero"), bg=NAVY, fg=NAVY_MUTED,
                 font=("Segoe UI", 10)).pack(anchor="w", pady=(4, 0))
        self.settings_btn = ttk.Button(hero, text="⚙  " + self.t("Settings"),
                                       command=self.open_options)
        self.settings_btn.pack(side="right", padx=(15, 0))
        outer = ttk.Frame(self.root, padding=(17, 14, 17, 10))
        outer.pack(fill="both", expand=True)

        finder = ttk.Frame(outer, style="Card.TFrame", padding=(15, 12))
        finder.pack(fill="x")
        ttk.Label(finder, text=self.t("Library folder"), style="Card.TLabel",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 7))
        searchline = ttk.Frame(finder, style="Card.TFrame")
        searchline.pack(fill="x")
        ttk.Entry(searchline, textvariable=self.folder).pack(side="left", fill="x", expand=True)
        ttk.Button(searchline, text=self.t("Browse"), command=self.choose).pack(side="left", padx=(8, 0))
        self.scan_btn = ttk.Button(searchline, text=self.t("Scan"), style="Primary.TButton",
                                   command=self.start_scan)
        self.scan_btn.pack(side="left", padx=(8, 0))
        optline = ttk.Frame(finder, style="Card.TFrame")
        optline.pack(fill="x", pady=(10, 0))
        ttk.Checkbutton(optline, text=self.t("Auto"),
                        variable=self.apply_var).pack(side="left")
        ttk.Label(optline, text=self.t("Threshold"), style="Muted.TLabel").pack(side="left", padx=(22, 8))
        ttk.Spinbox(optline, from_=0.10, to=0.95, increment=0.05,
                    textvariable=self.conf_var, width=6).pack(side="left")
        ttk.Label(optline, text=self.t("Recommended"), style="Muted.TLabel").pack(side="left", padx=(8, 0))

        statsline = ttk.Frame(outer)
        statsline.pack(fill="x", pady=(12, 12))
        for key, label in (("total", "Found"), ("analyzed", "Analyzed"),
                           ("suggestions", "Suggestions"), ("correct", "Correct"),
                           ("ignored", "Ignored")):
            card = ttk.Frame(statsline, style="Card.TFrame", padding=(13, 8))
            card.pack(side="left", fill="both", expand=True, padx=(0, 8) if key != "ignored" else 0)
            ttk.Label(card, textvariable=self.stats[key], style="Card.TLabel",
                      font=("Segoe UI", 17, "bold")).pack(anchor="w")
            ttk.Label(card, text=self.t(label), style="Muted.TLabel").pack(anchor="w")

        table_title = ttk.Frame(outer)
        table_title.pack(fill="x", pady=(0, 7))
        ttk.Label(table_title, text=self.t("Results"),
                  font=("Segoe UI", 12, "bold")).pack(side="left")
        ttk.Label(table_title, text=self.t("Hint"),
                  foreground=MUTED, font=("Segoe UI", 9)).pack(side="right")

        footer = ttk.Frame(outer)
        footer.pack(side="bottom", fill="x", pady=(9, 0))
        self.progress = ttk.Progressbar(footer, mode="determinate")
        self.progress.pack(fill="x")
        ttk.Label(footer, textvariable=self.status, foreground=MUTED).pack(anchor="w", pady=(5, 8))
        actionbar = ttk.Frame(footer)
        actionbar.pack(fill="x")
        ttk.Button(actionbar, text=self.t("Apply selected"), style="Primary.TButton",
                   command=self.apply_selected).pack(side="left")
        ttk.Button(actionbar, text=self.t("Open song folder"), command=self.open_selected).pack(side="left", padx=8)
        ttk.Button(actionbar, text=self.t("Clear"), command=self.clear).pack(side="right")

        brand = ttk.Frame(footer)
        brand.pack(fill="x", pady=(9, 0))
        ttk.Label(brand, text=self.t("Created by airBN") + "   ·   ", foreground=MUTED,
                  font=("Segoe UI", 9, "bold")).pack(side="left")
        for label, url in ((self.t("Community"), "https://ultrastar-es.org/pt"),
                           ("Buy Me a Coffee", "https://buymeacoffee.com/airbn"),
                           ("Instagram", "https://www.instagram.com/airbn_ultrastares_pt/")):
            link = tk.Label(brand, text=label + "   ↗", bg=BG, fg=ACCENT,
                            cursor="hand2", font=("Segoe UI", 9, "underline"))
            link.pack(side="left", padx=(0, 16))
            link.bind("<Button-1>", lambda _event, target=url: webbrowser.open(target))

        tablewrap = ttk.Frame(outer, style="Card.TFrame")
        cols = ("song", "current", "suggested", "delta", "confidence", "status", "action")
        self.tree = ttk.Treeview(tablewrap, columns=cols, show="headings", selectmode="extended")
        headings = dict(zip(cols, ("Song", "Current", "Suggested", "Difference",
                                   "Confidence", "Status", "Action")))
        widths = {"song": 350, "current": 96, "suggested": 101, "delta": 100,
                  "confidence": 94, "status": 190, "action": 98}
        for col in cols:
            self.tree.heading(col, text=self.t(headings[col]))
            self.tree.column(col, width=widths[col], minwidth=65,
                             anchor="w" if col in ("song", "status") else "center",
                             stretch=(col == "song"))
        self.tree.pack(side="left", fill="both", expand=True)
        self.scrollbar = ttk.Scrollbar(tablewrap, orient="vertical", command=self._tree_scroll)
        self.scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=self._tree_yview)
        self.tree.tag_configure("odd", background="#f6f9fe")
        self.tree.tag_configure("even", background=WHITE)
        self.tree.bind("<Configure>", lambda _e: self._schedule_buttons())
        self.tree.bind("<ButtonRelease-1>", lambda _e: self._schedule_buttons())
        self.tree.bind("<MouseWheel>", lambda _e: self._schedule_buttons())
        self.tree.bind("<Button-4>", lambda _e: self._schedule_buttons())
        self.tree.bind("<Button-5>", lambda _e: self._schedule_buttons())
        self.tree.bind("<KeyRelease>", lambda _e: self._schedule_buttons())
        self.tree.bind("<Double-Button-1>", self.show_details)
        tablewrap.pack(side="top", fill="both", expand=True, after=footer)

    def open_options(self):
        if self.running:
            return
        dlg = tk.Toplevel(self.root)
        dlg.title(self.t("Settings"))
        dlg.configure(bg=WHITE)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()
        outer = ttk.Frame(dlg, style="Card.TFrame", padding=22)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=self.t("Settings"), style="Card.TLabel",
                  font=("Segoe UI", 16, "bold")).pack(anchor="w", pady=(0, 16))
        ttk.Label(outer, text=self.t("Language"), style="Card.TLabel",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        chosen_lang = tk.StringVar(value=LANGUAGES[self.lang])
        combos = ttk.Combobox(outer, textvariable=chosen_lang, state="readonly",
                              values=list(LANGUAGES.values()), width=33)
        combos.pack(anchor="w", pady=(6, 16), fill="x")
        ttk.Label(outer, text=self.t("Backup location"), style="Card.TLabel",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 7))
        chosen_mode = tk.StringVar(value=self.backup_mode)
        for value, label in (("local", "Same folder"), ("central", "Central folder")):
            ttk.Radiobutton(outer, text=self.t(label), variable=chosen_mode,
                            value=value).pack(anchor="w", pady=3)
        ttk.Label(outer, text=self.t("Central note"), style="Muted.TLabel",
                  wraplength=410).pack(anchor="w", pady=(12, 18))
        actions = ttk.Frame(outer, style="Card.TFrame")
        actions.pack(fill="x")

        def save():
            code = next((k for k, name in LANGUAGES.items() if name == chosen_lang.get()), self.lang)
            chosen = {"language": code, "backup_mode": chosen_mode.get()}
            try:
                save_settings(chosen)
            except OSError as exc:
                messagebox.showerror(self.t("Save error"), str(exc), parent=dlg)
                return
            changed_language = code != self.lang
            self.settings = chosen
            self.lang = code
            self.backup_mode = chosen_mode.get()
            dlg.destroy()
            if changed_language:
                self._rebuild()
            self.status.set(self.t("Options saved"))

        ttk.Button(actions, text=self.t("Cancel"), command=dlg.destroy).pack(side="right")
        ttk.Button(actions, text=self.t("Save"), style="Primary.TButton",
                   command=save).pack(side="right", padx=(0, 8))
        dlg.update_idletasks()
        dlg.geometry(f"{max(480, dlg.winfo_reqwidth())}x{dlg.winfo_reqheight()}"
                     f"+{self.root.winfo_rootx()+80}+{self.root.winfo_rooty()+70}")
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        dlg.focus_set()

    def _rebuild(self):
        for button in self.buttons:
            button.destroy()
        self.buttons = []
        self.layout_pending = False
        for child in self.root.winfo_children():
            child.destroy()
        self.row_by_iid.clear()
        self._setup_style()
        self._build()
        for index, result in enumerate(self.results):
            self._add_row(result, index)
        self._update_stats()
        self._schedule_buttons()

    def _tree_scroll(self, *args):
        self.tree.yview(*args)
        self._schedule_buttons()

    def _tree_yview(self, first, last):
        self.scrollbar.set(first, last)
        self._schedule_buttons()

    def _schedule_buttons(self):
        if self.layout_pending:
            return
        self.layout_pending = True
        self.root.after(45, self._layout_buttons)

    def _layout_buttons(self):
        self.layout_pending = False
        if not self.tree.winfo_exists():
            return
        height = self.tree.winfo_height()
        visible = []
        seen = set()
        for y in range(24, max(25, height), 15):
            iid = self.tree.identify_row(y)
            if iid and iid not in seen:
                seen.add(iid)
                row = self.row_by_iid.get(iid)
                if row and self._can_apply(row):
                    bounds = self.tree.bbox(iid, "action")
                    if bounds:
                        visible.append((iid, bounds))
        while len(self.buttons) < len(visible):
            self.buttons.append(ttk.Button(self.tree, text=self.t("Apply"), style="Action.TButton"))
        for index, button in enumerate(self.buttons):
            if index >= len(visible):
                button.place_forget()
                continue
            iid, (x, y, w, h) = visible[index]
            if w < 56:
                button.place_forget()
                continue
            button.configure(command=lambda target=iid: self.apply_one(target))
            button.place(x=x+5, y=y+5, width=w-10, height=max(20, h-10))

    def _can_apply(self, row):
        return (row.get("gap") is not None
                and row.get("state") not in ("applied", "correct", "same_review")
                and row.get("error") is None)

    def choose(self):
        path = filedialog.askdirectory(title=self.t("Choose folder"))
        if path:
            self.folder.set(path)

    def start_scan(self):
        if self.running:
            return
        if np is None:
            messagebox.showerror(self.t("Dependencies"), self.t("Missing libs"))
            return
        ffmpeg = ffmpeg_path()
        if not ffmpeg:
            messagebox.showerror(self.t("Missing ffmpeg"), self.t("FFmpeg hint"))
            return
        folder = Path(self.folder.get().strip())
        if not folder.is_dir():
            messagebox.showwarning(self.t("Invalid folder"), self.t("Select valid"))
            return
        txts = find_txt_files(folder)
        if not txts:
            messagebox.showinfo(self.t("No songs"), self.t("No TXT"))
            return
        try:
            threshold = float(self.conf_var.get())
        except (ValueError, tk.TclError):
            messagebox.showwarning(self.t("Invalid confidence"), self.t("Enter threshold"))
            return
        if not 0.1 <= threshold <= 0.95:
            messagebox.showwarning(self.t("Invalid confidence"), self.t("Enter threshold"))
            return
        self.clear()
        self.running = True
        self.scan_btn.configure(state="disabled")
        self.settings_btn.configure(state="disabled")
        self.progress.configure(maximum=len(txts), value=0)
        self.stats["total"].set(str(len(txts)))
        self.status.set(self.t("Scanning").format(total=len(txts)))
        thread = threading.Thread(target=self._worker,
                                  args=(txts, ffmpeg, self.apply_var.get(), threshold,
                                        self.backup_mode), daemon=True)
        thread.start()

    def _worker(self, txts, ffmpeg, auto_apply, threshold, backup_mode):
        for index, txt in enumerate(txts, 1):
            row = {"txt": txt, "current_gap": None, "gap": None, "confidence": 0.0,
                   "margin": 0.0, "good": False, "state": "", "error": None,
                   "digest": None}
            try:
                raw = txt.read_bytes()
                row["digest"] = hashlib.sha256(raw).hexdigest()
                content = decode_header(raw)
                row["current_gap"] = parse_gap(parse_header(content, "VIDEOGAP"))
                position_ok = videgap_is_before_bpm_gap(content)
                mp3 = resolve_media(txt, parse_header(content, "MP3"), ".mp3")
                video = resolve_media(txt, parse_header(content, "VIDEO"), ".mp4")
                if not mp3 or not video:
                    row["state"] = "missing"
                elif video_has_audio(video, ffmpeg) is False:
                    row["state"] = "silent"
                else:
                    gap, confidence, margin = estimate_gap(mp3, video, ffmpeg)
                    row.update({"gap": gap, "confidence": confidence, "margin": margin,
                                "good": confidence >= threshold and margin >= MIN_MARGIN})
                    current = row["current_gap"]
                    if current is not None and abs(gap - current) <= SAME_GAP_TOLERANCE:
                        if not position_ok:
                            row["state"] = "reposition"
                        else:
                            row["state"] = "correct" if row["good"] else "same_review"
                    elif current is not None:
                        row["state"] = "improved" if row["good"] else "difference_review"
                    else:
                        row["state"] = "ready" if row["good"] else "review"
                    if auto_apply and row["good"] and row["state"] not in ("correct", "same_review"):
                        try:
                            update_txt(txt, gap, backup_mode=backup_mode,
                                       expected_digest=row["digest"])
                            row["state"] = "applied"
                            row["current_gap"] = gap
                            row["digest"] = hashlib.sha256(txt.read_bytes()).hexdigest()
                        except Exception as ex:
                            row["state"] = "write_error"
                            row["error"] = str(ex)
            except NoAudioError:
                row["state"] = "silent"
            except Exception as ex:
                row["state"] = "analysis_error"
                row["error"] = str(ex)
            self.q.put(("row", row))
            self.q.put(("progress", (index, len(txts))))
        self.q.put(("done", len(txts)))

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "row":
                    self.results.append(payload)
                    self._add_row(payload, len(self.results)-1)
                elif kind == "progress":
                    n, total = payload
                    self.progress["value"] = n
                    self.status.set(self.t("Progress").format(done=n, total=total))
                    self.stats["analyzed"].set(str(n))
                    self._update_stats()
                elif kind == "done":
                    self.running = False
                    self.scan_btn.configure(state="normal")
                    self.settings_btn.configure(state="normal")
                    self._update_stats()
                    self.status.set(self.t("Finished").format(total=payload))
                    self._schedule_buttons()
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def _update_stats(self):
        suggestions = sum(1 for r in self.results if r.get("gap") is not None
                          and r["state"] not in ("correct", "same_review", "applied"))
        correct = sum(1 for r in self.results if r["state"] in ("correct", "same_review"))
        ignored = sum(1 for r in self.results if r["state"] in
                      ("missing", "silent", "analysis_error", "write_error"))
        self.stats["suggestions"].set(str(suggestions))
        self.stats["correct"].set(str(correct))
        self.stats["ignored"].set(str(ignored))

    def _row_values(self, row):
        gap, current = row.get("gap"), row.get("current_gap")
        delta = gap - current if gap is not None and current is not None else None
        try:
            rel = str(row["txt"].relative_to(Path(self.folder.get().strip())))
        except ValueError:
            rel = str(row["txt"])
        return (rel, fmt_gap(current), fmt_gap(gap), fmt_gap(delta, sign=True),
                f"{row['confidence'] * 100:.0f}%" if gap is not None else "—",
                self.t(row["state"]), "")

    def _add_row(self, row, index):
        style = "even" if index % 2 else "odd"
        iid = self.tree.insert("", "end", values=self._row_values(row), tags=(style,))
        row["iid"] = iid
        self.row_by_iid[iid] = row
        self._schedule_buttons()

    def _refresh_row(self, row):
        iid = row.get("iid")
        if iid and self.tree.exists(iid):
            self.tree.item(iid, values=self._row_values(row))
        self._update_stats()
        self._schedule_buttons()

    def _apply(self, row):
        update_txt(row["txt"], row["gap"], backup_mode=self.backup_mode,
                   expected_digest=row.get("digest"))
        row["state"] = "applied"
        row["current_gap"] = row["gap"]
        row["digest"] = hashlib.sha256(row["txt"].read_bytes()).hexdigest()
        row["error"] = None
        self._refresh_row(row)

    def apply_one(self, iid):
        row = self.row_by_iid.get(iid)
        if not row or not self._can_apply(row):
            return
        if not row["good"] and not messagebox.askyesno(
                self.t("Weak title"), self.t("Weak question").format(
                    song=row["txt"].stem, gap=fmt_gap(row["gap"]))):
            return
        try:
            self._apply(row)
            self.status.set(self.t("Applied status").format(
                song=row["txt"].name, gap=fmt_gap(row["gap"])))
        except Exception as exc:
            messagebox.showerror(self.t("Save error"), f"{row['txt'].name}\n\n{exc}")

    def apply_selected(self):
        rows = [self.row_by_iid[iid] for iid in self.tree.selection()
                if iid in self.row_by_iid and self._can_apply(self.row_by_iid[iid])]
        if not rows:
            messagebox.showinfo(self.t("No selected"), self.t("Select rows"))
            return
        low = sum(not row["good"] for row in rows)
        warn = self.t("Low warning").format(n=low) if low else ""
        backup = self.t("Same folder" if self.backup_mode == "local" else "Central folder")
        if not messagebox.askyesno(self.t("Confirm apply"), self.t("Confirm multi").format(
                n=len(rows), backup=backup, warn=warn)):
            return
        done, errors = 0, []
        for row in rows:
            try:
                self._apply(row)
                done += 1
            except Exception as exc:
                errors.append(self.t("Batch failure").format(name=row["txt"].name,
                                                               error=str(exc)))
        summary = self.t("Done errors").format(done=done, errors=len(errors))
        if errors:
            summary += "\n\n" + "\n".join(errors[:8])
            if len(errors) > 8:
                summary += f"\n… +{len(errors) - 8}"
        messagebox.showinfo(self.t("Completed"), summary)

    def show_details(self, event):
        iid = self.tree.identify_row(event.y)
        row = self.row_by_iid.get(iid)
        if row is None:
            return
        messagebox.showinfo(self.t("Info"), self.t("Info body").format(
            name=row["txt"].name, current=fmt_gap(row.get("current_gap")),
            suggested=fmt_gap(row.get("gap")), confidence=f"{row['confidence']:.0%}",
            margin=row.get("margin", 0.0)))

    def open_selected(self):
        selected = self.tree.selection()
        if not selected:
            return
        row = self.row_by_iid.get(selected[0])
        if not row:
            return
        folder = row["txt"].parent
        try:
            if os.name == "nt":
                os.startfile(str(folder))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except OSError as exc:
            messagebox.showerror(self.t("Open folder error"), str(exc))

    def clear(self):
        if self.running:
            return
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.results.clear()
        self.row_by_iid.clear()
        for value in self.stats.values():
            value.set("0")
        self.progress["value"] = 0
        self.status.set(self.t("Cleared"))
        self._schedule_buttons()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
