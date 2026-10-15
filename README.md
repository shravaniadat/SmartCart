# SmartMart — Three connected websites (Raspberry Pi 4B + RC522)

A ready-to-run **local-network Flask application** with **three separate web portals** that share one SQLite database:

1. **Customer** `/customer?cart=CART-01` — product-section map, reported cart location, RFID-only cart additions, clear cart, and a checkout QR for the exact amount.
2. **Admin** `/admin` — RFID registration/unregistration, alert feed of scanned items, all carts on shop map, reported cart location, switch the one RFID reader between carts, and manager payment verification. Has a link to Inventory.
3. **Inventory** `/inventory` — all products, SKU, price, category, section, on-hand/available/reserved counts, restocking, product creation and scan alerts.

## The key hardware and payment limitations

- **One RC522 reads tags a few centimeters from the reader**. It does not automatically read the full shopping basket. One reader can be assigned to *one* cart at a time from Admin. CART-02 and CART-03 are prepared for future readers/round-robin demonstrations.
- **Cart location is NOT automatically detected.** A manager chooses the cart's current shop section; all portals reflect that position. For true indoor positioning, add BLE beacons, UWB, Wi-Fi positioning, or another tracking system, along with calibration.
- **Scanned items reserve stock immediately** (available = on hand - reserved). Clearing a cart returns them to available stock. Only manager-verified payment completes the sale and permanently decreases on-hand stock.
- **QR does not prove payment.** Set `MERCHANT_UPI_ID` to produce a UPI payment-request QR with the cart amount. Manager must **independently verify receipt** in a merchant app/account before confirming the payment on the Admin page. Automatic verification would require a legitimate gateway integration with signed server-side callbacks and reconciliation.
- Without `MERCHANT_UPI_ID`, checkout shows a **clearly labeled demo-only QR** which cannot be used to pay.
- This is a LAN demonstration, **not a production-secure retail system**. Add HTTPS, customer/cart authentication, CSRF protection, audited payments, robust multi-device locks and a proper inventory/ERP integration before real deployment.

## Install on Raspberry Pi 4B

You already created `~/smart_retail/venv` for your RC522. From a terminal on the Pi:

```bash
# unzip SmartMart_Three_Portals.zip into your home directory
cd ~/SmartMart_Three_Portals
source ~/smart_retail/venv/bin/activate
pip install 'Flask>=2.3,<4' 'qrcode[pil]>=7.4'
# If RC522 python library hasn't been installed yet:
pip install --no-deps mfrc522
```

**When testing without RFID connected**, use demo mode:

```bash
cd ~/SmartMart_Three_Portals
source ~/smart_retail/venv/bin/activate
python3 app.py --demo
```

Demo manager password: `demo1234` (you can override with `ADMIN_PASSWORD`). In the admin portal you can use **Test scan**; only the admin sees that control. The Customer portal has no manual-add button.

**With RFID physically connected**, stop `rfid_test.py` and other RFID programs first, then:

```bash
cd ~/SmartMart_Three_Portals
source ~/smart_retail/venv/bin/activate
export ADMIN_PASSWORD='choose-a-strong-password'
# Optional: supply your own actual verified merchant UPI ID for a real UPI request
# export MERCHANT_UPI_ID='your-upi-id@provider'
# export MERCHANT_NAME='Your Shop'
python3 app.py
```

The Raspberry Pi should already have SPI enabled. Verify it using `ls /dev/spidev*`. The reader uses **3.3V only**, wired as follows:

| RFID-RC522 | Pi 4B physical pin | BCM GPIO |
|---|---:|---|
| SDA/SS | 24 | GPIO8 CE0 |
| SCK | 23 | GPIO11 |
| MOSI | 19 | GPIO10 |
| MISO | 21 | GPIO9 |
| RST | 22 | GPIO25 |
| GND | 6 | GND |
| 3.3V | 1 | 3.3V |
| IRQ | Not connected | — |

Get the Pi IP: `hostname -I`. From your Windows laptop on the **same network**, visit:

- Customer: `http://YOUR_PI_IP:5000/customer?cart=CART-01`
- Admin: `http://YOUR_PI_IP:5000/admin`
- Inventory: `http://YOUR_PI_IP:5000/inventory`

If port 5000 is already in use by an earlier site, stop it with `Ctrl+C`, or run `python3 app.py --port 5001` and use port 5001 in the links.

## First test using your two working RFID UIDs

Your previously detected reader outputs were:

- `799992525454`
- `619249934221`

1. Admin → **RFID tag management** → register `799992525454` for a product, and `619249934221` for another product.
2. Admin → **RFID reader** → assign reader to `CART-01`.
3. Customer → open `/customer?cart=CART-01`.
4. Scan each physical RFID tag **one at a time, removing it before scanning the next**. Only one unit per unique RFID UID can be added to a cart. To bill two physical units of the same product, assign **two different physical tags** to that product.
5. Customer and Inventory auto-update every ~2–2.5 seconds, and Admin shows scan events.
6. Customer → Checkout → see UPI or demo QR. The order is still **unpaid**.
7. Admin → **Awaiting payment verification** → if (and only if) payment has been independently received, click Verify payment and confirm. This converts the reserved items into completed sales.
8. Or Customer → Clear cart to cancel an unpaid order and restore reserved stock. A previously sold tag is not automatically made reusable; it must be manually unregistered and registered after physically restocking.

## Folder structure

```
SmartMart_Three_Portals/
├── app.py                    # Flask API + RFID scanning + SQLite
├── requirements.txt
├── README.md
├── templates/
│   ├── base.html
│   ├── customer.html
│   ├── admin.html
│   ├── inventory.html
│   └── login.html
└── static/
    ├── site.css
    └── app.js
```

The app creates `smartmart.db` when first launched. The initial 11 products and 3 demo carts are sample data. **The database remains on disk between restarts.** Back it up before upgrades. `SMARTMART_DB=/path/to/database.db` changes the database location.

## Development notes

- Single-process Flask server and SQLite are suitable for a demonstrator, not high concurrency production traffic.
- Admin changes are password-protected. Customer cart routes are not yet linked to user accounts or PINs (appropriate only for supervised local testing).
- The fonts are fetched online when the browser has internet; system fonts are used otherwise.
