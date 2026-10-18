"""Shared RPi.GPIO access (BCM numbering) for the RC522 reset pin and the HX711.

On Raspberry Pi OS Bookworm install `rpi-lgpio` (a drop-in that provides the
`RPi.GPIO` module on top of lgpio). Do not install `RPi.GPIO` at the same time.
"""
import atexit
import threading

_lock = threading.Lock()
_gpio = None


def get_gpio():
    """Return the RPi.GPIO module configured for BCM numbering, or raise."""
    global _gpio
    with _lock:
        if _gpio is None:
            import RPi.GPIO as GPIO  # noqa: N814 - provided by rpi-lgpio on Bookworm
            GPIO.setwarnings(False)
            if GPIO.getmode() is None:
                GPIO.setmode(GPIO.BCM)
            _gpio = GPIO
            atexit.register(cleanup)
        return _gpio


def cleanup():
    """Release GPIO once at process exit. The mfrc522 package and rpi-lgpio can
    both raise here (e.g. nothing set up yet), so failures are ignored."""
    if _gpio is None:
        return
    try:
        _gpio.cleanup()
    except Exception:
        pass
