"""Pure interval logic: no torch or silero imports here (D1)."""

FRAME_SAMPLES = 512  # Silero frame at 16 kHz, 32 ms
VAD_SR = 16000


def n_frames(n16k: int) -> int:
    """Frames in the 16 kHz copy; the last partial frame is zero-padded (D6)."""
    return -(-n16k // FRAME_SAMPLES)
