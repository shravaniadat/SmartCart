# SmartMart / SmartCart — RFID & IoT Based Smart Retail Billing System

Final-year B.Tech project (Group 21). A smart shopping cart: every product carries a 13.56 MHz RFID tag, an
**MFRC522 (RC522)** reader on a **Raspberry Pi 4B** detects it, the bill is computed live, a **10 kg load cell + HX711**
verifies the cart weight, a **16×2 I2C LCD** and a **speaker** show/announce item, price and total, and everything
is stored in one SQLite database that three web portals read live (2-second polling).

| Portal | URL | Who |
|---|---|---|
| Customer | `/customer?cart=CART-01` | shopper's phone / cart display: floor map, live bill, weight status, clear cart, checkout QR |
| Admin | `/admin` (password) | RFID tag registration (type or **Scan to register**), tag table, alerts, carts on the map, reader assignment, load-cell tare/calibrate, mute, payment verification |
| Inventory | `/inventory` (password) | product table, search, add product, restock, edit price/weight, reserved/in-cart/available, active tags, low stock |

Stack: Python 3 + Flask + SQLite (`smartmart.db`) + vanilla JS. No Node, no build step, no internet needed
(the DM Sans font is bundled in `static/fonts`).

---

## 1. Quick start on Windows (demo mode, no hardware)

```powershell
cd C:\Users\91996\Desktop\Webdev\SmartCart
powershell -ExecutionPolicy Bypass -File .\run_demo.ps1
```

The script creates `.venv`, installs `requirements.txt` and starts `python app.py --demo`. Open
<http://127.0.0.1:5000/admin> — password **demo1234**. In demo mode the Admin page has a **Test scan** box and
one-click product chips (simulated reader), plus a **fake weight** control for the load cell.

Manual equivalent: `py -3 -m venv .venv; .venv\Scripts\pip install -r requirements.txt; .venv\Scripts\python app.py --demo`

---

## 2. Wiring (Raspberry Pi 4B, physical pin numbers)

**Use 3.3 V for the RC522 and the HX711.** A 5 V HX711 drives 5 V into the Pi's GPIO and can damage it.

### RC522 RFID reader (SPI0)
| RC522 | Pi pin | BCM |
|---|---:|---|
| SDA / SS | 24 | GPIO8 (CE0) |
| SCK | 23 | GPIO11 |
| MOSI | 19 | GPIO10 |
| MISO | 21 | GPIO9 |
| IRQ | — | not connected |
| GND | 6 | GND |
| RST | 22 | GPIO25 |
| 3.3V | 1 | 3.3 V |

### HX711 + 10 kg load cell
| From | To | Pi pin |
|---|---|---:|
| Load cell red | HX711 E+ | |
| Load cell black | HX711 E− | |
| Load cell white | HX711 A− | |
| Load cell green | HX711 A+ | |
| HX711 VCC | 3.3 V | 17 |
| HX711 GND | GND | 9 |
| HX711 DT (DOUT) | GPIO5 | 29 |
| HX711 SCK (PD_SCK) | GPIO6 | 31 |

Wire colours vary between cells: if weight goes *down* when you load it, swap A+ / A−. Pins can be changed
with `HX711_DT_PIN` / `HX711_SCK_PIN`. Mount the cell so the cart basket rests on it only through the cell.

### 16×2 LCD with PCF8574 I2C backpack
| Backpack | Pi pin |
|---|---:|
| GND | 14 |
| VCC | 2 (5 V, needed for contrast) |
| SDA | 3 (GPIO2) |
| SCL | 5 (GPIO3) |

Most backpacks pull SDA/SCL up to VCC. That usually works, but the safe choice is to remove the backpack's
pull-up resistors or use a level shifter. The address (0x27 or 0x3F) is detected automatically; check with
`i2cdetect -y 1`. Turn the blue potentiometer if you see only blocks or nothing.

### Speaker
Plug a **powered** speaker (or a PAM8403 amp + speaker) into the Pi's **3.5 mm jack**. Announcements use
`espeak-ng` and run on a background thread, so scanning never waits for audio. Mute from Admin → Devices.

Nothing else shares these pins: SPI uses 19/21/23/24, I2C uses 3/5, HX711 uses 29/31, RST uses 22.

---

## 3. Raspberry Pi setup (Raspberry Pi OS Bookworm 64-bit)

```bash
# 1. enable SPI and I2C, install system packages
sudo raspi-config nonint do_spi 0
sudo raspi-config nonint do_i2c 0
sudo apt update
sudo apt install -y git python3-venv python3-dev swig liblgpio-dev python3-lgpio espeak-ng i2c-tools
sudo reboot

# 2. get the code
cd ~
git clone https://github.com/shravaniadat/SmartCart.git
cd SmartCart          # (until the PR is merged: git checkout smartcart-integration)

# 3. configure
cp .env.example .env
nano .env             # set ADMIN_PASSWORD, optionally MERCHANT_UPI_ID / MERCHANT_NAME

# 4. run (first run creates ./venv and installs everything)
chmod +x run_pi.sh
./run_pi.sh           # real hardware
./run_pi.sh --demo    # same Pi, no hardware needed
```

`run_pi.sh` does the following:
`python3 -m venv --system-site-packages venv` → `pip install -r requirements.txt -r requirements-pi.txt` →
`pip install --no-deps mfrc522` → `python app.py --server waitress`.

**GPIO on Bookworm:** the old `RPi.GPIO` package often fails on Bookworm kernels. This project uses
**`rpi-lgpio`**, a drop-in that provides the same `import RPi.GPIO` API. Do not have both installed
(`pip uninstall -y RPi.GPIO` if you did). `mfrc522` must be installed with `--no-deps`; otherwise it pulls
`RPi.GPIO` back in. The app never calls `GPIO.cleanup()` while running (the stock `mfrc522` example does that,
and it would also release the HX711 pins). Cleanup runs once at exit.

**Stop `rfid_test.py`** or any other RFID program before starting. Only one process can own SPI/GPIO.

### Auto-start at boot (systemd)

```bash
sudo cp deploy/smartmart.service /etc/systemd/system/
# edit User= / paths in the file if your username is not "pi" or the folder is elsewhere
sudo systemctl daemon-reload
sudo systemctl enable --now smartmart
systemctl status smartmart
journalctl -u smartmart -f      # live log (scans are printed here)
```

### Open the portals from a phone or laptop

```bash
hostname -I       # e.g. 192.168.1.42
```
The startup log also prints the address. On any device on the **same Wi-Fi**:
`http://192.168.1.42:5000/customer?cart=CART-01`, `http://192.168.1.42:5000/admin` and
`http://192.168.1.42:5000/inventory`. `http://<hostname>.local:5000` often works too.

---

## 4. Configuration (`.env` or environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `ADMIN_PASSWORD` | — (demo: `demo1234`) | Admin/Inventory password. Required outside demo mode. |
| `MERCHANT_UPI_ID` | empty | e.g. `shop@okaxis`. Empty means a clearly labelled **DEMO-ONLY** QR. |
| `MERCHANT_NAME` | `SmartMart Demo` | Payee name in the UPI request |
| `SMARTMART_DB` | `./smartmart.db` | Database path (`.session_secret` is stored next to it) |
| `PORT` / `HOST` | `5000` / `0.0.0.0` | Web server (also `--port`, `--host`) |
| `SMARTMART_SERVER` | `flask` | `waitress` for a sturdier server (`--server waitress`) |
| `SCALE_DRIVER` | `hx711` | `hx711`, `serial` (Arduino, see `hardware/arduino_hx711`) or `none` |
| `HX711_DT_PIN` / `HX711_SCK_PIN` | `5` / `6` | BCM pin numbers |
| `SCALE_SERIAL_PORT` / `SCALE_SERIAL_BAUD` | `/dev/ttyUSB0` / `57600` | for `SCALE_DRIVER=serial` |
| `WEIGHT_MIN_TOLERANCE_G` | `30` | minimum allowed ± grams (load-cell noise) |
| `WEIGHT_SETTLE_S` | `2` | seconds the reading must settle before judging |
| `LCD_ADDRESS` | auto | hex, e.g. `27` or `3F` |
| `SECRET_KEY` | generated | fixed session secret (optional) |

---

## 5. How it works

* **Scan:** reader thread → `scan_tag()`. A tag held on the reader is billed once: it must be away for 1.5 s
  before it counts again, and each UID can only be in one cart. The scan reserves stock immediately
  (available = on hand − in carts), then the LCD shows `Name price / Total Rs …` and the speaker says
  "Basmati rice, rupees 80. Total 115."
* **Refused scans** (unknown tag, sold tag, out of stock, cart awaiting payment) are written to the alerts
  feed and shown on the LCD and through the speaker.
* **Scan to register:** Admin presses *Scan to register*. For 30 s the next tag on the reader is **captured**
  into the UID box instead of being billed or logged as unknown. Choose the product and press Save tag.
* **Weight check:** expected weight = sum of `weight_g` for the items in the reader's cart. Allowed difference
  = sum of `weight_g × tolerance_pct` (default 10 %), but at least 30 g. Each reading is a median of 7
  HX711 samples, then a rolling median of 5. After any change, the reading must settle for 2 s. A mismatch
  shows "Weight mismatch – please rescan" on the Customer page and LCD and raises an alert. Checkout is
  **not** blocked; the order is flagged ⚠ for the manager instead.
* **Payment:** checkout freezes the cart and shows a QR for the exact amount (a real `upi://pay` request
  only when `MERCHANT_UPI_ID` is set). **The QR is not proof of payment.** The manager checks the merchant
  app or receives cash, then presses *Verify payment*. Only then do reserved items become sold (on-hand
  stock decreases, tags marked *sold*). Sold tags can be re-activated in Admin after the item is restocked.
* **Concurrency:** every write is one short `BEGIN IMMEDIATE` transaction under a process lock, in WAL
  mode. A transaction always commits, including on early returns, so refusal alerts are never lost, and
  rolls back on errors. Readers never block the scanner.
* **Failure isolation:** each device lives in `hardware/` with lazy imports. If one is missing or fails, its
  badge in Admin → Devices turns OFF/ERROR with the reason, and the rest keeps working. All of them import
  safely on Windows.

---

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| Admin badge: *SPI disabled?* / `No such file /dev/spidev0.0` | `sudo raspi-config nonint do_spi 0 && sudo reboot`, then check `ls /dev/spidev*` |
| *RC522 not responding (version register 0x00/0xFF)* | wiring, 3.3 V, try another jumper; RST must be pin 22 |
| *GPIO busy* | another program (rfid_test.py, an old app.py) is running: `pkill -f rfid_test.py`, `sudo systemctl stop smartmart` |
| `ModuleNotFoundError: RPi` / `lgpio` | `source venv/bin/activate && pip install rpi-lgpio` (venv must be created with `--system-site-packages`, or `sudo apt install liblgpio-dev swig` and reinstall) |
| *Port 5000 is already in use* | an old server is still running: `sudo systemctl stop smartmart` or `pkill -f app.py`; or `./run_pi.sh --port 5001` |
| `database is locked` | shouldn't happen (WAL + IMMEDIATE + 15 s busy timeout). Don't put the DB on a USB/network drive, and don't open it in DB Browser while the app writes. |
| LCD shows blocks / nothing | adjust the contrast pot; `i2cdetect -y 1` should show 27 or 3f; set `LCD_ADDRESS=3F` if needed |
| LCD badge *I2C disabled?* | `sudo raspi-config nonint do_i2c 0 && sudo reboot` |
| HX711 weight jumps / noisy | Tare with an empty basket, then Calibrate with a known weight (e.g. a 1 kg rice pack). Keep wires short and twisted, power the HX711 from 3.3 V, make sure the cell is mounted rigidly. Raise `WEIGHT_MIN_TOLERANCE_G`. If it's still unreliable, move the HX711 to an Arduino and set `SCALE_DRIVER=serial`. |
| *Connected – not calibrated yet* | Admin → Weight check → **Tare (empty cart)**, then put a known weight on, enter grams, press **Calibrate**. Values are saved in the DB. |
| No sound | `espeak-ng "hello"` in a terminal; choose Headphones in `sudo raspi-config` → System → Audio; check `alsamixer`. If it works in a terminal but not under systemd, add `Environment=XDG_RUNTIME_DIR=/run/user/1000` to the service. |
| Admin login "Session expired" | the page was open across a restart; reload it |
| "Too many failed attempts" | 5 wrong passwords lock that IP for 5 minutes |

---

## 7. Ten-step demo script (review presentation)

1. Start the Pi (the service auto-starts). Open **Admin** on the laptop and **Customer** (`/customer?cart=CART-01`) on a phone. Show the four green device badges.
2. **Inventory:** walk through the catalogue table (On hand, In carts, Available, Active tags, Weight) and search "rice".
3. **Admin → Scan to register:** press the button, tap a new tag on the reader, and watch the UID appear. Pick a product → **Save tag**. Show it in the tag table.
4. Place the cart's basket on the load cell. **Tare** with it empty (already calibrated earlier).
5. Scan *Basmati rice* (799992525454). The LCD shows name + price + total, the speaker announces it, the phone bill updates within 2 s, and Inventory's *In carts* goes up.
6. Scan *Fresh milk* (619249934221), then put the milk in the basket. The weight badge shows **Weight OK**.
7. Drop an **unscanned** item into the basket. After 2 s: "Weight mismatch – please rescan" on the phone + LCD, and an alert in Admin. Remove it and the status returns to OK.
8. Scan an **unregistered** tag: "Unknown tag" on the LCD and an alert in Admin with a *Register* shortcut. Try **Remove** on the rice tag: it's blocked because the tag is in a cart.
9. **Checkout** on the phone: exact-amount QR (UPI, or clearly labelled DEMO). Explain that the QR is a request, not proof.
10. **Admin → Verify payment** after checking the money. The phone shows "Payment verified", stock is finalized in Inventory, and the tags become *Sold* (re-activate after restock).

No hardware at the review? Run `--demo`. Steps 3, 5–8 then work with **Test scan**, the product chips and
*Simulate unscanned item (+500 g)*.

---

## 8. Tests

```bash
python -m pytest -q                       # 23 logic/HTTP/migration/concurrency tests
pip install playwright                    # optional browser check (uses installed Chrome)
python app.py --demo --port 5055          # in another terminal, with a fresh SMARTMART_DB
python tests/e2e_browser.py http://127.0.0.1:5055
```

## 9. Project layout

```
app.py                 Flask app: routes, DB, scan/checkout/payment logic, weight monitor
hardware/              optional devices, import-safe everywhere
  rfid.py              RC522 reader thread (debounce, health check)
  scale.py             HX711 / serial drivers, tare+calibration, weight tolerance logic
  lcd.py               16x2 I2C LCD (auto 0x27/0x3F), non-blocking
  speaker.py           espeak-ng announcements, non-blocking
  gpio.py              shared RPi.GPIO (rpi-lgpio) access, BCM mode, cleanup at exit
  arduino_hx711/       optional Arduino sketch for SCALE_DRIVER=serial
templates/, static/    three portals (vanilla JS, local font)
tests/                 pytest suite + Playwright end-to-end script
deploy/smartmart.service   systemd unit
run_demo.ps1, run_pi.sh    one-command start
requirements.txt, requirements-pi.txt, .env.example
```

## 10. Honest limitations

* Cart location is entered by the manager (no indoor positioning hardware).
* One RC522 and one load cell serve one cart at a time (assign it in Admin). CART-02/03 are ready for more hardware.
* Payment is verified manually by design. Automatic verification would need a payment-gateway integration with signed callbacks.
* LAN prototype: plain HTTP. Admin is protected with a password, CSRF/same-origin checks and a login rate limit, but there are no customer accounts.
