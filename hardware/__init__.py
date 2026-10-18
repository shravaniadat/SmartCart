"""Optional Raspberry Pi devices for SmartMart.

Every module here is import-safe on Windows/macOS: Pi-only libraries
(RPi.GPIO, mfrc522, spidev, smbus2, RPLCD) are imported lazily inside the
device classes. A device that fails only updates its status dict; it never
raises into the web server.
"""
import threading


class DeviceStatus:
    """Thread-safe status badge shown in Admin: state is ok | off | error | demo."""

    def __init__(self, name, state='off', detail='Not started'):
        self._lock = threading.Lock()
        self._data = {'name': name, 'state': state, 'detail': detail}

    def set(self, state, detail=''):
        with self._lock:
            self._data.update(state=state, detail=str(detail)[:200])

    def snapshot(self):
        with self._lock:
            return dict(self._data)

    @property
    def ok(self):
        return self.snapshot()['state'] == 'ok'
