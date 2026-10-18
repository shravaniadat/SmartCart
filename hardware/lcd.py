"""16x2 character LCD on I2C (PCF8574 backpack). Optional.

Address auto-detected (0x27, then 0x3F) unless LCD_ADDRESS is set.
Writes happen on a worker thread so a slow/missing LCD never delays a scan.
"""
import queue
import sys
import threading

from . import DeviceStatus

CANDIDATES = (0x27, 0x3F)


def ascii16(text):
    text = str(text).replace('₹', 'Rs').replace('•', '-').replace('—', '-')
    return text.encode('ascii', 'replace').decode('ascii').replace('?', ' ')[:16]


class LCD:
    def __init__(self, address=None, port=1):
        self.status = DeviceStatus('LCD 16x2')
        self.address, self.port = address, port
        self._q = queue.Queue(maxsize=4)
        self._lcd = None

    def start(self):
        if not sys.platform.startswith('linux'):
            self.status.set('off', 'I2C LCD needs a Raspberry Pi')
            return
        threading.Thread(target=self._run, name='lcd', daemon=True).start()

    def show(self, line1, line2=''):
        try:
            self._q.put_nowait((ascii16(line1), ascii16(line2)))
        except queue.Full:  # keep only fresh messages
            try:
                self._q.get_nowait()
                self._q.put_nowait((ascii16(line1), ascii16(line2)))
            except (queue.Empty, queue.Full):
                pass

    def _detect(self):
        from smbus2 import SMBus
        with SMBus(self.port) as bus:
            for addr in ([self.address] if self.address else CANDIDATES):
                try:
                    bus.read_byte(addr)
                    return addr
                except OSError:
                    continue
        raise RuntimeError('No LCD at 0x27/0x3F - run: i2cdetect -y 1')

    def _open(self):
        from RPLCD.i2c import CharLCD
        addr = self._detect()
        lcd = CharLCD('PCF8574', addr, port=self.port, cols=16, rows=2, auto_linebreaks=False)
        lcd.clear()
        self.status.set('ok', f'I2C address 0x{addr:02X}')
        return lcd

    def _run(self):
        try:
            self._lcd = self._open()
            self._write('SmartMart ready', 'Scan a product')
        except Exception as exc:
            hint = ' - I2C disabled? sudo raspi-config nonint do_i2c 0' if isinstance(exc, FileNotFoundError) else ''
            self.status.set('error' if not isinstance(exc, ModuleNotFoundError) else 'off', f'{type(exc).__name__}: {exc}{hint}')
            return
        while True:
            line1, line2 = self._q.get()
            try:
                self._write(line1, line2)
            except Exception as exc:
                self.status.set('error', f'Write failed: {exc}')
                try:
                    self._lcd = self._open()
                except Exception:
                    pass

    def _write(self, line1, line2):
        self._lcd.cursor_pos = (0, 0)
        self._lcd.write_string(line1.ljust(16))
        self._lcd.cursor_pos = (1, 0)
        self._lcd.write_string(line2.ljust(16))
