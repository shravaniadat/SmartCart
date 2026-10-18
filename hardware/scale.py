"""Load cell (10 kg strain gauge + HX711) and the weight-verification logic.

Drivers share one tiny interface, so the HX711-on-GPIO driver can be swapped
for an Arduino-over-serial reader with SCALE_DRIVER=serial:

    read_raw() -> int | None     median raw count, None on a bad read

Grams = (raw - offset) / factor; offset comes from Tare, factor from
Calibrate (known weight). Both are stored in the settings table by app.py.
"""
import statistics
import sys
import threading
import time
from collections import deque

from . import DeviceStatus

# ------------------------------------------------------------------ pure logic

def allowed_deviation(items, min_tolerance_g=30.0):
    """items: iterable of (weight_g, tolerance_pct). Returns (expected_g, allowed_g, unweighed_count)."""
    expected = allowed = 0.0
    unweighed = 0
    for weight_g, tolerance_pct in items:
        weight_g = float(weight_g or 0)
        if weight_g <= 0:
            unweighed += 1
            continue
        expected += weight_g
        allowed += weight_g * float(tolerance_pct if tolerance_pct is not None else 10) / 100.0
    return round(expected, 1), round(max(allowed, min_tolerance_g), 1), unweighed


def classify(expected_g, allowed_g, measured_g):
    if measured_g is None:
        return 'no_scale'
    return 'ok' if abs(measured_g - expected_g) <= allowed_g else 'mismatch'


class WeightJudge:
    """Turns noisy instant readings into a settled status. A new status is only
    committed after it has held for settle_s, and any cart change (scan, clear,
    payment, reader moved) restarts the settle window."""

    def __init__(self, settle_s=2.0):
        self.settle_s = settle_s
        self.status = 'settling'
        self._candidate = None
        self._since = None
        self._changed_at = None

    def cart_changed(self, moment):
        self._changed_at = moment
        self._candidate = None
        self.status = 'settling'

    def update(self, instant, moment):
        """Feed an instant classification; returns (status, changed: bool)."""
        if instant == 'no_scale':
            changed = self.status != 'no_scale'
            self.status = 'no_scale'
            self._candidate = None
            return self.status, changed
        if self._changed_at is not None and moment - self._changed_at < self.settle_s:
            changed = self.status != 'settling'
            self.status = 'settling'
            return self.status, changed
        if instant == self.status:
            self._candidate = None
            return self.status, False
        if self.status in ('settling', 'no_scale'):
            # settle window after a cart change already elapsed: commit now
            self.status = instant
            self._candidate = None
            return self.status, True
        if instant != self._candidate:
            self._candidate, self._since = instant, moment
        if moment - self._since >= self.settle_s:
            self.status = instant
            self._candidate = None
            return self.status, True
        return self.status, False


# ------------------------------------------------------------------ drivers

class HX711Driver:
    """Bit-banged HX711, channel A, gain 128. Linux can pre-empt us mid-read
    (SCK high > 60 us powers the chip down), so every read is a median of
    several samples and saturated/invalid samples are discarded."""

    def __init__(self, dt_pin=5, sck_pin=6, samples=7):
        from .gpio import get_gpio
        self.GPIO = get_gpio()
        self.dt, self.sck, self.samples = dt_pin, sck_pin, samples
        self.GPIO.setup(self.sck, self.GPIO.OUT, initial=self.GPIO.LOW)
        self.GPIO.setup(self.dt, self.GPIO.IN)
        if self._read_once(timeout=1.0) is None:
            raise RuntimeError(f'HX711 not answering on DT=GPIO{dt_pin}, SCK=GPIO{sck_pin}')

    def _read_once(self, timeout=0.25):
        G = self.GPIO
        deadline = time.monotonic() + timeout
        while G.input(self.dt):
            if time.monotonic() > deadline:
                return None
            time.sleep(0.001)
        value = 0
        for _ in range(24):
            G.output(self.sck, True)
            G.output(self.sck, False)
            value = (value << 1) | G.input(self.dt)
        G.output(self.sck, True)   # 25th pulse selects channel A / gain 128
        G.output(self.sck, False)
        if value & 0x800000:
            value -= 0x1000000
        if value in (0x7FFFFF, -0x800000):
            return None            # saturated or garbage clock
        return value

    def read_raw(self):
        values = [v for v in (self._read_once() for _ in range(self.samples)) if v is not None]
        if len(values) < self.samples // 2 + 1:
            return None
        return int(statistics.median(values))


class SerialDriver:
    """Arduino (or any MCU) printing one raw HX711 count per line over USB serial."""

    def __init__(self, port='/dev/ttyUSB0', baud=57600):
        import serial  # pyserial
        self.ser = serial.Serial(port, baud, timeout=1)
        self.buffer = deque(maxlen=7)

    def read_raw(self):
        for _ in range(7):
            line = self.ser.readline().decode('ascii', 'ignore').strip()
            try:
                self.buffer.append(int(float(line)))
            except ValueError:
                continue
        if len(self.buffer) < 3:
            return None
        return int(statistics.median(self.buffer))


class ScaleWorker:
    """Background reader. Keeps a short rolling median of grams for the app."""

    def __init__(self, driver_name='hx711', dt_pin=5, sck_pin=6, serial_port='/dev/ttyUSB0',
                 serial_baud=57600, offset=0.0, factor=None, demo=False):
        self.status = DeviceStatus('Load cell', *(('demo', 'Demo scale - set weight in Admin') if demo else ()))
        self.driver_name, self.dt_pin, self.sck_pin = driver_name, dt_pin, sck_pin
        self.serial_port, self.serial_baud = serial_port, serial_baud
        self.offset, self.factor = float(offset or 0), (float(factor) if factor else None)
        self.demo = demo
        self.demo_grams = 0.0
        self.demo_follow = True     # demo scale mirrors the expected weight
        self.expected_hint = 0.0    # set by app so demo_follow can mirror it
        self._driver = None
        self._raw = None
        self._recent = deque(maxlen=5)
        self._lock = threading.Lock()

    # --- public API used by app.py
    def start(self):
        if self.demo:
            self.status.set('demo', 'Demo scale - set weight in Admin')
            return
        if self.driver_name == 'none':
            self.status.set('off', 'Disabled (SCALE_DRIVER=none)')
            return
        if self.driver_name == 'hx711' and not sys.platform.startswith('linux'):
            self.status.set('off', 'HX711 needs a Raspberry Pi (use --demo on this computer)')
            return
        threading.Thread(target=self._run, name='scale', daemon=True).start()

    def grams(self):
        """Settled measurement in grams, or None when no usable scale."""
        if self.demo:
            return round(self.expected_hint if self.demo_follow else self.demo_grams, 1)
        with self._lock:
            if not self._recent or self.factor in (None, 0):
                return None
            return round(statistics.median(self._recent), 1)

    def tare(self):
        if self.demo:
            self.demo_grams = 0.0
            return 0.0
        raw = self._fresh_raw()
        self.offset = float(raw)
        with self._lock:
            self._recent.clear()
        return self.offset

    def calibrate(self, known_g):
        if known_g <= 0:
            raise ValueError('Known weight must be positive')
        if self.demo:
            return 1.0
        raw = self._fresh_raw()
        factor = (raw - self.offset) / known_g
        if abs(factor) < 1e-6:
            raise RuntimeError('Reading did not change - is the weight on the scale and tare done first?')
        self.factor = factor
        with self._lock:
            self._recent.clear()
        return factor

    def raw(self):
        return self._raw

    # --- internals
    def _fresh_raw(self):
        if self._driver is None:
            raise RuntimeError('Scale not connected: ' + self.status.snapshot()['detail'])
        values = [v for v in (self._driver.read_raw() for _ in range(5)) if v is not None]
        if not values:
            raise RuntimeError('Scale gave no valid reading; check HX711 wiring')
        return statistics.median(values)

    def _open(self):
        if self.driver_name == 'serial':
            return SerialDriver(self.serial_port, self.serial_baud)
        return HX711Driver(self.dt_pin, self.sck_pin)

    def _run(self):
        bad = 0
        while True:
            if self._driver is None:
                try:
                    self._driver = self._open()
                    self.status.set('ok' if self.factor else 'error',
                                    'Ready' if self.factor else 'Connected - not calibrated yet (Tare, then Calibrate)')
                except Exception as exc:
                    hint = ' - run: pip install pyserial' if isinstance(exc, ModuleNotFoundError) and self.driver_name == 'serial' else ''
                    self.status.set('error', f'{type(exc).__name__}: {exc}{hint}')
                    time.sleep(10)
                    continue
            try:
                raw = self._driver.read_raw()
                if raw is None:
                    bad += 1
                    if bad >= 10:
                        self.status.set('error', 'Repeated bad HX711 reads (noise/wiring?)')
                else:
                    bad = 0
                    self._raw = raw
                    if self.factor:
                        with self._lock:
                            self._recent.append((raw - self.offset) / self.factor)
                        if not self.status.ok:
                            self.status.set('ok', 'Ready')
                    else:
                        self.status.set('error', 'Connected - not calibrated yet (Tare, then Calibrate)')
                time.sleep(0.2)
            except Exception as exc:
                self.status.set('error', f'{type(exc).__name__}: {exc}')
                self._driver = None
                time.sleep(3)
