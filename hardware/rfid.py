"""MFRC522 (RC522) reader thread over SPI0/CE0, 3.3 V only.

Wiring (physical pins): SDA=24 (GPIO8/CE0), SCK=23, MOSI=19, MISO=21,
RST=22 (GPIO25), GND=6, 3.3V=1, IRQ unused.
"""
import sys
import threading
import time

from . import DeviceStatus

RST_BCM = 25          # physical pin 22
POLL_S = 0.10         # time between reads
RELEASE_S = 1.5       # tag must be away this long before it can be billed again


def uid_to_number(uid_bytes):
    """Same numbering as mfrc522.SimpleMFRC522 (5 bytes incl. BCC), so UIDs
    such as 799992525454 match what rfid_test.py printed."""
    n = 0
    for b in uid_bytes[:5]:
        n = n * 256 + b
    return n


class Debouncer:
    """A tag held on the reader is billed once. read_id_no_block() alternates
    between the UID and None while a tag sits still, so a UID is only accepted
    again after it has been absent for RELEASE_S seconds."""

    def __init__(self, release_s=RELEASE_S):
        self.release_s = release_s
        self.last_seen = {}

    def accept(self, uid, moment):
        previous = self.last_seen.get(uid)
        self.last_seen[uid] = moment
        # forget stale entries so the dict stays small
        for key in [k for k, t in self.last_seen.items() if moment - t > 60]:
            del self.last_seen[key]
        return previous is None or moment - previous > self.release_s


class RFIDReader:
    def __init__(self, on_scan):
        self.on_scan = on_scan
        self.status = DeviceStatus('RFID reader')
        self.last_uid = None
        self._reader = None

    def start(self):
        threading.Thread(target=self._run, name='rc522-reader', daemon=True).start()

    def _open(self):
        from .gpio import get_gpio
        get_gpio()  # BCM mode must be set before MFRC522() picks its RST pin
        from mfrc522 import MFRC522
        try:
            reader = MFRC522(pin_rst=RST_BCM)
        except TypeError:  # very old mfrc522 releases have no pin_rst argument
            reader = MFRC522()
        version = reader.Read_MFRC522(0x37)
        if version in (0x00, 0xFF):
            raise RuntimeError(f'RC522 not responding (version register 0x{version:02X}); check wiring and 3.3 V')
        return reader

    def _read_no_block(self):
        r = self._reader
        status, _ = r.MFRC522_Request(r.PICC_REQIDL)
        if status != r.MI_OK:
            return None
        status, uid = r.MFRC522_Anticoll()
        if status != r.MI_OK:
            return None
        return str(uid_to_number(uid))

    def _run(self):
        if not sys.platform.startswith('linux'):
            self.status.set('off', 'RC522 needs a Raspberry Pi (use --demo on this computer)')
            return
        debounce = Debouncer()
        last_health = 0
        while True:
            if self._reader is None:
                try:
                    self._reader = self._open()
                    self.status.set('ok', 'RC522 ready on SPI0 CE0')
                    print('RFID reader connected. Scan one tag at a time.')
                except Exception as exc:
                    self.status.set('error', hint_for(exc))
                    print('RFID init failed:', exc)
                    time.sleep(10)
                    continue
            try:
                uid = self._read_no_block()
                moment = time.monotonic()
                if uid is not None and debounce.accept(uid, moment):
                    self.last_uid = uid
                    self.on_scan(uid)
                if moment - last_health > 5:
                    last_health = moment
                    version = self._reader.Read_MFRC522(0x37)
                    if version in (0x00, 0xFF):
                        raise RuntimeError('RC522 stopped responding; check wiring')
                    if not self.status.ok:
                        self.status.set('ok', 'RC522 ready on SPI0 CE0')
                time.sleep(POLL_S)
            except Exception as exc:
                self.status.set('error', hint_for(exc))
                print('RFID read error:', exc)
                self._reader = None
                time.sleep(3)


def hint_for(exc):
    text = f'{type(exc).__name__}: {exc}'
    if isinstance(exc, ModuleNotFoundError):
        return f'{text} - run: pip install -r requirements-pi.txt && pip install --no-deps mfrc522'
    if isinstance(exc, FileNotFoundError) or 'spidev' in text:
        return f'{text} - SPI disabled? run: sudo raspi-config nonint do_spi 0 && sudo reboot'
    if 'busy' in text.lower():
        return f'{text} - another program (rfid_test.py?) holds the GPIO; stop it'
    return text
