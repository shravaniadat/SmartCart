"""Voice announcements through espeak-ng (sudo apt install espeak-ng).

Runs on a worker thread so scanning is never delayed. Silently disabled when
espeak-ng is missing (e.g. on Windows).
"""
import queue
import shutil
import subprocess
import threading

from . import DeviceStatus


class Speaker:
    def __init__(self, voice='en', speed=150):
        self.status = DeviceStatus('Speaker')
        self.voice, self.speed = voice, speed
        self.muted = False
        self._q = queue.Queue(maxsize=3)
        self._exe = None

    def start(self):
        self._exe = shutil.which('espeak-ng') or shutil.which('espeak')
        if not self._exe:
            self.status.set('off', 'espeak-ng not installed (sudo apt install espeak-ng)')
            return
        self.status.set('ok', 'espeak-ng ready')
        threading.Thread(target=self._run, name='speaker', daemon=True).start()

    def say(self, text):
        if self.muted or not self._exe:
            return
        try:
            self._q.put_nowait(text)
        except queue.Full:
            pass  # drop announcements rather than lag behind the cart

    def _run(self):
        while True:
            text = self._q.get()
            try:
                subprocess.run([self._exe, '-v', self.voice, '-s', str(self.speed), text],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            except Exception as exc:
                self.status.set('error', f'espeak failed: {exc}')
