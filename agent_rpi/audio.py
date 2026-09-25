"""Call audio on the Raspberry Pi: recording via arecord (ALSA).

Two-way live audio between the EC20 PCM card and a local mic/speaker is
handled by alsaloop as a separate systemd service (see setup.md); this module
only handles per-call recording so every call ends up as a WAV that gets
uploaded to the server.
"""

import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("smsync.audio")


class CallRecorder:
    def __init__(self, device: str = "default", rate: int = 8000,
                 channels: int = 1, out_dir: str = "recordings"):
        self.device = device
        self.rate = rate
        self.channels = channels
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._proc: Optional[subprocess.Popen] = None
        self._path: Optional[Path] = None

    @property
    def recording(self) -> bool:
        return self._proc is not None

    def start(self) -> Optional[Path]:
        if self.recording:
            return self._path
        self._path = self.out_dir / time.strftime("call_%Y%m%d_%H%M%S.wav")
        cmd = [
            "arecord", "-q",
            "-D", self.device,
            "-f", "S16_LE",
            "-r", str(self.rate),
            "-c", str(self.channels),
            "-t", "wav",
            str(self._path),
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log.info("recording started: %s (device %s)", self._path, self.device)
            return self._path
        except FileNotFoundError:
            log.error("arecord not found - install alsa-utils")
            self._proc = None
            return None

    def stop(self) -> Optional[Path]:
        """Stop recording; returns the WAV path, or None if nothing usable."""
        if not self._proc:
            return None
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        self._proc = None
        path, self._path = self._path, None
        if path and path.exists() and path.stat().st_size > 44:  # > WAV header
            log.info("recording saved: %s (%d bytes)", path, path.stat().st_size)
            return path
        if path and path.exists():
            path.unlink()
        return None
