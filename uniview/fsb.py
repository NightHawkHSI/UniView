"""Sound files -> FMOD sound banks (FSB5) the way Unity stores AudioClips, for Mod Maker.

Unity keeps an AudioClip's sound as an FSB5 bank in a .resource file. We write uncompressed 16-bit PCM
banks: no encoder needed, every Unity version plays them (they're just bigger than Vorbis).
"""

import io
import struct
import wave

import numpy as np

FSB5_PCM16 = 2
# FSB5 sample header frequency codes
FREQUENCIES = {8000: 1, 11000: 2, 11025: 3, 16000: 4, 22050: 5, 24000: 6, 32000: 7, 44100: 8, 48000: 9}
CHUNK_CHANNELS, CHUNK_FREQUENCY = 1, 2


def _chunk(kind, payload, more):
    return struct.pack("<I", int(more) | (len(payload) << 1) | (kind << 25)) + payload


def fsb5_pcm16(pcm, channels, frequency):
    """One-sample FSB5 bank (version 1) holding interleaved 16-bit little-endian PCM."""
    samples = len(pcm) // (2 * channels)
    chunks = []
    if channels > 2:
        chunks.append((CHUNK_CHANNELS, struct.pack("<B", channels)))
    code = FREQUENCIES.get(frequency)
    if code is None:
        code = FREQUENCIES[44100]  # overridden by the frequency chunk
        chunks.append((CHUNK_FREQUENCY, struct.pack("<I", frequency)))
    raw = (int(bool(chunks)) | (code << 1) | ((1 if channels == 2 else 0) << 5)
           | (0 << 6) | (samples << 34))  # data offset 0 (in 16-byte units)
    sample_header = struct.pack("<Q", raw) + b"".join(
        _chunk(kind, payload, i + 1 < len(chunks)) for i, (kind, payload) in enumerate(chunks))
    data = bytes(pcm) + bytes(-len(pcm) % 32)
    header = struct.pack("<4s6I", b"FSB5", 1, 1, len(sample_header), 0, len(data), FSB5_PCM16)
    header += bytes(8) + bytes(16) + bytes(8)  # flags, hash, dummy (60-byte version 1 header)
    return header + sample_header + data


def _wav_pcm16(data):
    """(interleaved int16 PCM bytes, channels, frequency) of WAV bytes (8/16/24/32-bit integer PCM)."""
    with wave.open(io.BytesIO(data)) as w:
        channels, rate, width = w.getnchannels(), w.getframerate(), w.getsampwidth()
        frames = w.readframes(w.getnframes())
    if width == 2:
        return frames, channels, rate
    if width == 1:
        samples = (np.frombuffer(frames, np.uint8).astype(np.int16) - 128) << 8
    elif width == 3:
        b = np.frombuffer(frames, np.uint8).reshape(-1, 3)
        samples = (b[:, 1].astype(np.int16) | (b[:, 2].astype(np.int16) << 8))
    elif width == 4:
        samples = (np.frombuffer(frames, "<i4") >> 16).astype(np.int16)
    else:
        raise ValueError(f"{width * 8}-bit WAV isn't supported")
    return samples.astype("<i2").tobytes(), channels, rate


def decode(path):
    """(interleaved 16-bit PCM bytes, channels, frequency) of a sound file. Plain WAVs are read directly;
    anything else (OGG, MP3, FLAC, float WAV...) is decoded by FMOD (shipped with UnityPy)."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] == b"RIFF":
        try:
            return _wav_pcm16(data)
        except (wave.Error, ValueError, EOFError):
            pass  # float or compressed WAV: let FMOD decode it
    import fmod_toolkit
    from fmod_toolkit.fmod import pyfmodex
    system, lock = fmod_toolkit.get_pyfmodex_system_instance(2, pyfmodex.flags.INIT_FLAGS.NORMAL)
    with lock:
        try:
            sound = system.create_sound(path, pyfmodex.flags.MODE.CREATESAMPLE)
        except Exception as e:
            raise ValueError(f"FMOD couldn't read this sound file ({e}).") from e
        try:
            wav = fmod_toolkit.subsound_to_wav(sound)
        finally:
            sound.release()
    return _wav_pcm16(wav)
