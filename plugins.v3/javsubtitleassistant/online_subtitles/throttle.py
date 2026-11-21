"""Serialize downloads and pace every Subtitle Cat request within the plugin."""

from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from threading import Lock
from time import monotonic, sleep, time


class SerialThrottle:
    def __init__(self, gap):
        self._gap = gap
        self._lock = Lock()
        self._next = 0.0
        self._paused = 0.0

    @contextmanager
    def slot(self):
        with self._lock:
            wait = self._next - monotonic()
            if self._paused > monotonic():
                raise ValueError(f"Subtitle Cat 暂停请求，请在 {int(self._paused - monotonic() + 1)} 秒后再试")
            if wait > 0:
                sleep(wait)
            try:
                yield
            finally:
                self._next = monotonic() + self._gap

    def back_off(self, status, retry_after=""):
        # Called inside the site slot so concurrent requests see the new cooldown.
        if status not in {403, 429, 503}:
            return
        seconds = 900
        try:
            if str(retry_after).isdigit():
                seconds = max(seconds, int(retry_after))
            elif retry_after:
                seconds = max(seconds, parsedate_to_datetime(retry_after).timestamp() - time())
        except (ValueError, TypeError, OverflowError):
            pass
        self._paused = max(self._paused, monotonic() + seconds)


download_gate = SerialThrottle(8)
subtitlecat_gate = SerialThrottle(3)
