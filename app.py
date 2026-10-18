"""SmartMart: three connected portals, one SQLite backend, Raspberry Pi hardware support.

Local educational prototype. Cart position is admin-set, not automatically tracked.
UPI QR is a payment *request*, never evidence of a completed payment.

Start:  python app.py --demo          (any computer, no hardware)
        python app.py                 (Raspberry Pi with RC522/HX711/LCD/speaker)
"""
import argparse
import base64
import hmac
import io
import os
import re
import secrets
import socket
import sqlite3
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode, urlparse

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
import qrcode

from hardware.lcd import LCD
from hardware.rfid import RFIDReader
from hardware.scale import ScaleWorker, WeightJudge, allowed_deviation, classify
from hardware.speaker import Speaker

ROOT = Path(__file__).resolve().parent


def load_dotenv(path):
    """Minimal .env reader (KEY=value lines). Real environment variables win."""
    try:
        lines = Path(path).read_text(encoding='utf-8').splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip().removeprefix('export ').strip()
        os.environ.setdefault(key, value.strip().strip('"').strip("'"))


load_dotenv(ROOT / '.env')

CONFIG = {}


def configure(demo=None, db_path=None):
    """Read settings from the environment. Called by main() and by tests."""
    env = os.environ.get
    if demo is None:
        demo = env('SMARTMART_DEMO') == '1'
    CONFIG.update(
        demo=bool(demo),
        db_path=Path(db_path or env('SMARTMART_DB') or ROOT / 'smartmart.db'),
        admin_password=env('ADMIN_PASSWORD') or ('demo1234' if demo else ''),
        merchant_upi=(env('MERCHANT_UPI_ID') or '').strip(),
        merchant_name=(env('MERCHANT_NAME') or 'SmartMart Demo').strip(),
        hx711_dt=int(env('HX711_DT_PIN') or 5),
        hx711_sck=int(env('HX711_SCK_PIN') or 6),
        scale_driver=(env('SCALE_DRIVER') or 'hx711').lower(),
        scale_serial_port=env('SCALE_SERIAL_PORT') or '/dev/ttyUSB0',
        scale_serial_baud=int(env('SCALE_SERIAL_BAUD') or 57600),
        lcd_address=int(env('LCD_ADDRESS'), 16) if env('LCD_ADDRESS') else None,
        min_tolerance_g=float(env('WEIGHT_MIN_TOLERANCE_G') or 30),
        settle_s=float(env('WEIGHT_SETTLE_S') or 2),
    )
    app.secret_key = load_secret(CONFIG['db_path'].parent)
    weight_judge.settle_s = CONFIG['settle_s']
    return CONFIG


def load_secret(folder):
    """SECRET_KEY env > .session_secret beside the DB > random (sessions reset on restart)."""
    if os.environ.get('SECRET_KEY'):
        return os.environ['SECRET_KEY']
    path = Path(folder) / '.session_secret'
    try:
        if path.exists():
            value = path.read_text().strip()
            if len(value) >= 32:
                return value
        value = secrets.token_hex(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return value
    except OSError as exc:
        print(f'Warning: cannot store session secret at {path} ({exc}); admin logins reset on restart.')
        return secrets.token_hex(32)


app = Flask(__name__)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                  MAX_CONTENT_LENGTH=64 * 1024)
write_lock = threading.RLock()

# Devices. Each only reports a status badge when it fails; start_hardware() starts them.
lcd = LCD()
speaker = Speaker()
scale = ScaleWorker(driver_name='none')
rfid = RFIDReader(on_scan=lambda uid: scan_tag(uid, 'hardware'))
rfid.status.set('off', 'Not started')
weight_judge = WeightJudge()
weight_lock = threading.Lock()
weight_info = {'cart': None, 'expected': 0.0, 'allowed': 0.0, 'measured': None,
               'status': 'no_scale', 'unweighed': 0}

REGISTRATION_WINDOW_S = 30
ZONES = [
    {'id': 'entrance', 'name': 'Entrance', 'icon': 'door-open', 'row': 1, 'col': 1},
    {'id': 'produce', 'name': 'Fresh produce', 'icon': 'leaf', 'row': 1, 'col': 2},
    {'id': 'dairy', 'name': 'Dairy & bakery', 'icon': 'milk', 'row': 1, 'col': 3},
    {'id': 'pantry', 'name': 'Rice & essentials', 'icon': 'shopping-basket', 'row': 2, 'col': 1},
    {'id': 'snacks', 'name': 'Snacks', 'icon': 'cookie', 'row': 2, 'col': 2},
    {'id': 'beverages', 'name': 'Beverages', 'icon': 'coffee', 'row': 2, 'col': 3},
    {'id': 'care', 'name': 'Personal care', 'icon': 'sparkles', 'row': 3, 'col': 1},
    {'id': 'home', 'name': 'Home supplies', 'icon': 'house', 'row': 3, 'col': 2},
    {'id': 'checkout', 'name': 'Checkout', 'icon': 'credit-card', 'row': 3, 'col': 3},
]
ZONE_LOOKUP = {z['id']: z['name'] for z in ZONES}
# sku, name, category, price, stock, zone, emoji, expected weight (g)
SEED_PRODUCTS = [
    ('RICE-01', 'Basmati rice', 'Grocery', 80.00, 22, 'pantry', '🍚', 1000),
    ('MILK-01', 'Fresh milk', 'Dairy', 35.00, 30, 'dairy', '🥛', 520),
    ('BISC-01', 'Butter biscuits', 'Snacks', 25.00, 26, 'snacks', '🍪', 200),
    ('SOAP-01', 'Bath soap', 'Personal care', 40.00, 17, 'care', '🧼', 125),
    ('BREAD-01', 'Whole wheat bread', 'Bakery', 45.00, 16, 'dairy', '🍞', 400),
    ('TEA-01', 'Tea powder', 'Grocery', 120.00, 18, 'pantry', '🍵', 250),
    ('CHIPS-01', 'Potato chips', 'Snacks', 20.00, 25, 'snacks', '🥔', 55),
    ('JUICE-01', 'Fruit juice', 'Beverages', 55.00, 22, 'beverages', '🧃', 1050),
    ('SHAM-01', 'Shampoo', 'Personal care', 95.00, 15, 'care', '🧴', 360),
    ('APPLE-01', 'Apples (pack)', 'Produce', 90.00, 20, 'produce', '🍎', 1000),
    ('TISSUE-01', 'Tissue box', 'Home supplies', 60.00, 14, 'home', '🧻', 150),
]
SEED_WEIGHTS = {row[0]: row[7] for row in SEED_PRODUCTS}
# The two physical tags already read on the Pi with rfid_test.py.
SEED_TAGS = [('799992525454', 'RICE-01'), ('619249934221', 'MILK-01')]

SCHEMA = '''
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    sku TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    price REAL NOT NULL CHECK(price >= 0),
    on_hand INTEGER NOT NULL CHECK(on_hand >= 0),
    reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved >= 0),
    zone TEXT NOT NULL,
    emoji TEXT NOT NULL DEFAULT '📦',
    weight_g REAL NOT NULL DEFAULT 0,
    tolerance_pct REAL NOT NULL DEFAULT 10
);
CREATE TABLE IF NOT EXISTS tags (
    uid TEXT PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','sold')),
    registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS carts (
    id TEXT PRIMARY KEY,
    zone TEXT NOT NULL DEFAULT 'entrance',
    checkout_order TEXT,
    updated_at TEXT NOT NULL,
    weight_flag INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cart_items (
    cart_id TEXT NOT NULL REFERENCES carts(id),
    tag_uid TEXT NOT NULL REFERENCES tags(uid),
    product_id INTEGER NOT NULL REFERENCES products(id),
    scanned_at TEXT NOT NULL,
    PRIMARY KEY (cart_id,tag_uid),
    UNIQUE(tag_uid)
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    cart_id TEXT NOT NULL,
    amount REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','paid','cancelled')),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    weight_flag INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS order_items (
    order_id TEXT NOT NULL REFERENCES orders(id),
    tag_uid TEXT NOT NULL,
    product_name TEXT NOT NULL,
    price REAL NOT NULL,
    PRIMARY KEY(order_id,tag_uid)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    message TEXT NOT NULL,
    tag_uid TEXT,
    cart_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cart_items_cart ON cart_items(cart_id);
'''
# Columns added after the first release; ALTERed into older smartmart.db files.
MIGRATIONS = [
    ('products', 'weight_g', 'REAL NOT NULL DEFAULT 0'),
    ('products', 'tolerance_pct', 'REAL NOT NULL DEFAULT 10'),
    ('carts', 'weight_flag', 'INTEGER NOT NULL DEFAULT 0'),
    ('orders', 'weight_flag', 'INTEGER NOT NULL DEFAULT 0'),
]
DEFAULT_SETTINGS = {'reader_cart': 'CART-01', 'muted': '0', 'reg_until': '0', 'reg_uid': '',
                    'scale_offset': '0', 'scale_factor': ''}


def now(): return datetime.now().isoformat(timespec='seconds')


@contextmanager
def db_conn(write=False):
    """One short-lived connection. write=True takes the process write lock and an
    IMMEDIATE transaction; it COMMITs on any normal exit (early returns included)
    and ROLLs BACK on exceptions. The connection is always closed."""
    db = sqlite3.connect(str(CONFIG['db_path']), timeout=15, isolation_level=None,
                         check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=15000')
    try:
        if write:
            with write_lock:
                db.execute('BEGIN IMMEDIATE')
                try:
                    yield db
                except BaseException:
                    db.execute('ROLLBACK')
                    raise
                db.execute('COMMIT')
        else:
            db.execute('BEGIN')  # consistent snapshot for multi-query reads
            try:
                yield db
            finally:
                db.execute('ROLLBACK')
    finally:
        db.close()


def get_setting(db, key, default=None):
    row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row[0] if row else (DEFAULT_SETTINGS.get(key) if default is None else default)


def set_setting(db, key, value):
    db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               (key, str(value)))


def migrate(db):
    """Add missing columns without touching existing rows. Returns added column names."""
    added = []
    for table, column, ddl in MIGRATIONS:
        cols = {r[1] for r in db.execute(f'PRAGMA table_info({table})')}
        if column not in cols:
            db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}')
            added.append(f'{table}.{column}')
    if 'products.weight_g' in added:  # give the seed catalogue sensible weights
        db.executemany('UPDATE products SET weight_g=? WHERE sku=? AND weight_g=0',
                       [(w, sku) for sku, w in SEED_WEIGHTS.items()])
    return added


_ready_lock = threading.Lock()
_db_ready = False


def init_db():
    global _db_ready
    path = CONFIG['db_path']
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    try:
        try:
            db.execute('PRAGMA journal_mode=WAL')  # readers never block the scanner
        except sqlite3.OperationalError:
            pass
        db.executescript(SCHEMA)
    finally:
        db.close()
    with db_conn(write=True) as db:
        added = migrate(db)
        fresh = db.execute('SELECT COUNT(*) FROM products').fetchone()[0] == 0
        if fresh:
            db.executemany('''INSERT INTO products(sku,name,category,price,on_hand,zone,emoji,weight_g)
                              VALUES (?,?,?,?,?,?,?,?)''', SEED_PRODUCTS)
            ids = {r['sku']: r['id'] for r in db.execute('SELECT id,sku FROM products')}
            tags = list(SEED_TAGS)
            if CONFIG['demo']:  # one virtual tag per product so every item can be demo-scanned
                tags += [(f'D-{sku}', sku) for sku in ids]
            db.executemany('INSERT OR IGNORE INTO tags(uid,product_id,registered_at) VALUES(?,?,?)',
                           [(uid, ids[sku], now()) for uid, sku in tags])
        for cart_id in ['CART-01', 'CART-02', 'CART-03']:
            db.execute('INSERT OR IGNORE INTO carts(id,zone,updated_at) VALUES (?,?,?)', (cart_id, 'entrance', now()))
        for key, value in DEFAULT_SETTINGS.items():
            db.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)', (key, value))
        db.execute('DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - 5000')
    _db_ready = True
    return added


@app.before_request
def ensure_ready():
    """Lets the app also be served by an external WSGI server (DB init on first request)."""
    if not _db_ready:
        with _ready_lock:
            if not CONFIG:
                configure()
            if not _db_ready:
                init_db()


def emit(db, type_, message, uid=None, cart=None):
    db.execute('INSERT INTO events(type,message,tag_uid,cart_id,created_at) VALUES (?,?,?,?,?)',
               (type_, message, uid, cart, now()))


def rowdict(row): return dict(row) if row else None


def rupees(n): return f'₹{n:,.2f}'


def spoken_amount(n):
    return f'{n:.0f}' if float(n).is_integer() else f'{n:.2f}'


def normalize_uid(uid):
    uid = re.sub(r'\s+', '', str(uid or '')).upper()
    return uid if re.fullmatch(r'[A-Z0-9:_\-]{1,40}', uid) else None


def products_list(db):
    return [dict(row) for row in db.execute('''SELECT *,on_hand-reserved AS available,
        (SELECT COUNT(*) FROM tags WHERE product_id=products.id AND status='active') AS active_tags
        FROM products ORDER BY name''')]


def weight_view(cart_id):
    with weight_lock:
        info = dict(weight_info)
    if info['cart'] != cart_id:
        return {'status': 'no_scale', 'measured': None, 'expected': None, 'allowed': None, 'unweighed': 0}
    return info


def cart_detail(db, cart_id):
    cart = db.execute('SELECT * FROM carts WHERE id=?', (cart_id,)).fetchone()
    if not cart:
        return None
    items = [dict(row) for row in db.execute('''SELECT ci.tag_uid AS uid,p.name,p.price,p.emoji,p.zone,ci.scanned_at
        FROM cart_items ci JOIN products p ON p.id=ci.product_id
        WHERE ci.cart_id=? ORDER BY ci.scanned_at DESC,ci.rowid DESC''', (cart_id,))]
    summary = []
    for item in items:
        found = next((r for r in summary if r['name'] == item['name'] and r['price'] == item['price']), None)
        if found:
            found['qty'] += 1
        else:
            summary.append({'name': item['name'], 'price': item['price'], 'qty': 1, 'emoji': item['emoji']})
    total = round(sum(r['price'] for r in items), 2)
    order = rowdict(db.execute('SELECT * FROM orders WHERE id=?', (cart['checkout_order'],)).fetchone()) if cart['checkout_order'] else None
    return {'id': cart['id'], 'zone': cart['zone'], 'zone_name': ZONE_LOOKUP.get(cart['zone'], cart['zone']),
            'items': items, 'summary': summary, 'total': total, 'count': len(items),
            'order': order, 'updated_at': cart['updated_at'], 'weight_flag': bool(cart['weight_flag']),
            'weight': weight_view(cart['id'])}


def registration_state(db):
    until = float(get_setting(db, 'reg_until') or 0)
    uid = get_setting(db, 'reg_uid') or None
    product = None
    if uid:
        row = db.execute('SELECT p.name FROM tags t JOIN products p ON p.id=t.product_id WHERE t.uid=?', (uid,)).fetchone()
        product = row['name'] if row else None
    remaining = max(0, int(until - time.time() + 0.999))
    return {'active': remaining > 0, 'remaining': remaining, 'uid': uid, 'registered_to': product}


def hardware_status():
    return {'rfid': rfid.status.snapshot() if not CONFIG.get('demo') else
            {'name': 'RFID reader', 'state': 'demo', 'detail': 'Demo mode - use Test scan'},
            'scale': scale.status.snapshot(), 'lcd': lcd.status.snapshot(), 'speaker': speaker.status.snapshot()}


def state(db):
    carts = [cart_detail(db, c['id']) for c in db.execute('SELECT id FROM carts ORDER BY id')]
    return {'carts': carts, 'products': products_list(db),
            'events': [dict(r) for r in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 40')],
            'tags': [dict(r) for r in db.execute('''SELECT t.*,p.name AS product_name,p.sku,p.emoji,
                (SELECT cart_id FROM cart_items WHERE tag_uid=t.uid) AS in_cart FROM tags t
                JOIN products p ON p.id=t.product_id ORDER BY t.registered_at DESC,t.uid LIMIT 2000''')],
            'reader_cart': get_setting(db, 'reader_cart'),
            'registration': registration_state(db),
            'hardware': hardware_status(), 'muted': get_setting(db, 'muted') == '1',
            'scale': {'grams': scale.grams(), 'raw': scale.raw(), 'calibrated': bool(scale.factor),
                      'demo_follow': scale.demo_follow, 'demo_grams': scale.demo_grams},
            'weight': weight_view(get_setting(db, 'reader_cart')),
            'demo': CONFIG['demo'], 'zones': ZONES, 'merchant_configured': bool(CONFIG['merchant_upi'])}


# ------------------------------------------------------------------ feedback (LCD + speaker)

def announce(feedback):
    if not feedback:
        return
    line1, line2, speech = feedback
    lcd.show(line1, line2)
    if speech:
        speaker.say(speech)


def cart_changed():
    """Any change to the reader cart's contents restarts the weight settle window."""
    with weight_lock:
        weight_judge.cart_changed(time.monotonic())
        weight_info['status'] = 'settling'


# ------------------------------------------------------------------ core logic

def _scan_tx(db, uid):
    """Runs inside one write transaction. Returns (payload, http_code, feedback)."""
    reg = registration_state(db)
    if reg['active']:
        set_setting(db, 'reg_uid', uid)
        set_setting(db, 'reg_until', 0)
        known = db.execute('SELECT p.name FROM tags t JOIN products p ON p.id=t.product_id WHERE t.uid=?', (uid,)).fetchone()
        note = f' (already registered to {known["name"]})' if known else ''
        emit(db, 'registration', f'Tag {uid} captured for registration{note}', uid)
        return ({'ok': True, 'captured': True, 'uid': uid, 'message': f'Tag {uid} captured for registration{note}'},
                200, ('Tag captured', uid, 'Tag captured'))
    cart_id = get_setting(db, 'reader_cart')
    cart = db.execute('SELECT checkout_order FROM carts WHERE id=?', (cart_id,)).fetchone()
    if not cart:
        return {'ok': False, 'message': f'Reader is assigned to missing cart {cart_id}'}, 409, None
    if cart['checkout_order']:
        emit(db, 'blocked', f'Scan of {uid} refused: {cart_id} is awaiting payment', uid, cart_id)
        return ({'ok': False, 'message': f'{cart_id} awaiting payment; cannot scan new items'}, 409,
                ('Checkout pending', 'Ask staff', 'Checkout pending'))
    tag = db.execute('SELECT t.status,p.* FROM tags t JOIN products p ON p.id=t.product_id WHERE t.uid=?', (uid,)).fetchone()
    if not tag:
        emit(db, 'unknown', f'Unknown tag {uid} scanned - registration required', uid, cart_id)
        return ({'ok': False, 'message': f'Unregistered RFID tag {uid}'}, 404,
                ('Unknown tag', uid, 'Unknown tag. Please ask staff.'))
    if tag['status'] != 'active':
        emit(db, 'blocked', f'Sold tag {uid} ({tag["name"]}) scanned again', uid, cart_id)
        return ({'ok': False, 'message': 'RFID tag belongs to a completed sale. Re-activate it in Admin after restocking.'},
                409, ('Tag already sold', tag['name'], 'This tag is already sold.'))
    existing = db.execute('SELECT cart_id FROM cart_items WHERE tag_uid=?', (uid,)).fetchone()
    if existing:
        return ({'ok': True, 'duplicate': True, 'message': f'{tag["name"]} already in {existing["cart_id"]}'}, 200,
                ('Already in cart', tag['name'], None))
    if tag['on_hand'] - tag['reserved'] < 1:
        emit(db, 'out_of_stock', f'No available stock for {tag["name"]} (tag {uid})', uid, cart_id)
        return ({'ok': False, 'message': f'{tag["name"]} is out of stock'}, 409,
                ('Out of stock', tag['name'], f'{tag["name"]} is out of stock.'))
    db.execute('INSERT INTO cart_items(cart_id,tag_uid,product_id,scanned_at) VALUES(?,?,?,?)',
               (cart_id, uid, tag['id'], now()))
    db.execute('UPDATE products SET reserved=reserved+1 WHERE id=?', (tag['id'],))
    db.execute('UPDATE carts SET updated_at=? WHERE id=?', (now(), cart_id))
    total = db.execute('''SELECT COALESCE(SUM(p.price),0) FROM cart_items ci JOIN products p ON p.id=ci.product_id
                          WHERE ci.cart_id=?''', (cart_id,)).fetchone()[0]
    emit(db, 'scan', f'{tag["name"]} scanned into {cart_id} - {rupees(tag["price"])}', uid, cart_id)
    return ({'ok': True, 'added': True, 'message': f'{tag["name"]} added', 'cart': cart_id, 'product': tag['name'],
             'total': round(total, 2)}, 200,
            (f'{tag["name"][:9]} {tag["price"]:.2f}', f'Total Rs {total:.2f}',
             f'{tag["name"]}, rupees {spoken_amount(tag["price"])}. Total {spoken_amount(total)}.'))


def scan_tag(uid, source='hardware'):
    """One physical scan, one cart item. Safe when scanner thread and web requests overlap."""
    clean = normalize_uid(uid)
    if not clean:
        return {'ok': False, 'message': 'Invalid tag UID'}, 400
    with db_conn(write=True) as db:
        payload, code, feedback = _scan_tx(db, clean)
    if payload.get('added'):
        cart_changed()
    announce(feedback)
    if source == 'hardware':
        print('RFID', clean, code, payload.get('message', '').encode('ascii', 'replace').decode())
    return payload, code


def clear_cart(cart_id):
    with db_conn(write=True) as db:
        cart = db.execute('SELECT * FROM carts WHERE id=?', (cart_id,)).fetchone()
        if not cart:
            return {'error': 'Cart not found'}, 404
        rows = db.execute('SELECT product_id,COUNT(*) n FROM cart_items WHERE cart_id=? GROUP BY product_id', (cart_id,)).fetchall()
        for row in rows:
            db.execute('UPDATE products SET reserved=MAX(reserved-?,0) WHERE id=?', (row['n'], row['product_id']))
        db.execute('DELETE FROM cart_items WHERE cart_id=?', (cart_id,))
        if cart['checkout_order']:
            db.execute("UPDATE orders SET status='cancelled',completed_at=? WHERE id=? AND status='pending'", (now(), cart['checkout_order']))
        db.execute('UPDATE carts SET checkout_order=NULL,weight_flag=0,updated_at=? WHERE id=?', (now(), cart_id))
        emit(db, 'clear', f'{cart_id} cleared; {sum(r["n"] for r in rows)} reserved items restored', cart=cart_id)
    cart_changed()
    return {'ok': True, 'message': 'Cart cleared and stock restored'}, 200


def make_checkout(cart_id):
    with db_conn(write=True) as db:
        cart = cart_detail(db, cart_id)
        if not cart:
            return {'error': 'Cart not found'}, 404
        if not cart['count']:
            return {'error': 'Scan at least one RFID product first'}, 400
        if cart['order']:
            order = cart['order']
        else:
            flag = 1 if (cart['weight_flag'] or cart['weight']['status'] == 'mismatch') else 0
            order_id = 'SM-' + uuid.uuid4().hex[:10].upper()
            db.execute('INSERT INTO orders(id,cart_id,amount,status,created_at,weight_flag) VALUES(?,?,?,?,?,?)',
                       (order_id, cart_id, cart['total'], 'pending', now(), flag))
            db.executemany('INSERT INTO order_items(order_id,tag_uid,product_name,price) VALUES(?,?,?,?)',
                           [(order_id, i['uid'], i['name'], i['price']) for i in cart['items']])
            db.execute('UPDATE carts SET checkout_order=?,updated_at=? WHERE id=?', (order_id, now(), cart_id))
            note = ' - WEIGHT MISMATCH, check cart before verifying' if flag else ''
            emit(db, 'checkout', f'{cart_id} checkout started - {rupees(cart["total"])}{note}', cart=cart_id)
            order = dict(db.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone())
    return {'ok': True, 'order': order, 'payment': payment_payload(order)}, 200


def payment_payload(order):
    # Without an explicitly configured merchant UPI ID, QR is a demo checkout token.
    upi, name = CONFIG['merchant_upi'], CONFIG['merchant_name']
    configured = bool(upi)
    if configured:
        text = 'upi://pay?' + urlencode({'pa': upi, 'pn': name, 'am': f'{order["amount"]:.2f}',
                                         'cu': 'INR', 'tn': order['id']})
    else:
        text = f'SMARTMART-DEMO-ONLY | ORDER {order["id"]} | INR {order["amount"]:.2f} | NOT A PAYMENT QR'
    qr = qrcode.QRCode(box_size=7, border=2)
    qr.add_data(text)
    qr.make(fit=True)
    image = qr.make_image(fill_color='#10212b', back_color='white')
    stream = io.BytesIO()
    image.save(stream, format='PNG')
    return {'qr': 'data:image/png;base64,' + base64.b64encode(stream.getvalue()).decode(),
            'configured': configured, 'amount': order['amount'], 'order_id': order['id'],
            'merchant': name if configured else '',
            'message': 'UPI payment request. Payment is not confirmed automatically - show the manager your payment.'
                       if configured else 'DEMO ONLY - this QR cannot be paid. Set MERCHANT_UPI_ID for a real UPI request.'}


def mark_paid(order_id):
    with db_conn(write=True) as db:
        order = db.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
        if not order:
            return {'error': 'Order not found'}, 404
        if order['status'] != 'pending':
            return {'error': f'Order already {order["status"]}'}, 409
        cart = db.execute('SELECT * FROM carts WHERE id=?', (order['cart_id'],)).fetchone()
        if not cart or cart['checkout_order'] != order_id:
            return {'error': 'Order not active'}, 409
        rows = db.execute('SELECT product_id,COUNT(*) n FROM cart_items WHERE cart_id=? GROUP BY product_id', (order['cart_id'],)).fetchall()
        for row in rows:
            db.execute('UPDATE products SET on_hand=MAX(on_hand-?,0), reserved=MAX(reserved-?,0) WHERE id=?',
                       (row['n'], row['n'], row['product_id']))
        db.execute('''UPDATE tags SET status='sold' WHERE uid IN
            (SELECT tag_uid FROM cart_items WHERE cart_id=?)''', (order['cart_id'],))
        db.execute('DELETE FROM cart_items WHERE cart_id=?', (order['cart_id'],))
        db.execute('UPDATE carts SET checkout_order=NULL,weight_flag=0,updated_at=? WHERE id=?', (now(), order['cart_id']))
        db.execute("UPDATE orders SET status='paid',completed_at=? WHERE id=?", (now(), order_id))
        emit(db, 'paid', f'Payment manually confirmed: {order_id} - {rupees(order["amount"])}', cart=order['cart_id'])
    cart_changed()
    announce(('Payment verified', 'Thank you!', 'Payment verified. Thank you for shopping.'))
    return {'ok': True, 'message': 'Payment marked as verified by manager'}, 200


def register_tag(uid, product_id):
    uid = normalize_uid(uid)
    if not uid:
        return {'error': 'Enter a valid RFID UID (letters, digits, : - _ only)'}, 400
    with db_conn(write=True) as db:
        product = db.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
        if not product:
            return {'error': 'Product not found'}, 404
        existing = db.execute('SELECT p.name FROM tags t JOIN products p ON p.id=t.product_id WHERE t.uid=?', (uid,)).fetchone()
        if existing:
            return {'error': f'Tag {uid} is already registered to {existing["name"]}. Remove it first.'}, 409
        db.execute('INSERT INTO tags(uid,product_id,registered_at) VALUES(?,?,?)', (uid, product_id, now()))
        if get_setting(db, 'reg_uid') == uid:
            set_setting(db, 'reg_uid', '')
        emit(db, 'register', f'RFID {uid} registered to {product["name"]}', uid)
    return {'ok': True, 'message': f'Tag {uid} registered to {product["name"]}'}, 200


def unregister_tag(uid):
    uid = normalize_uid(uid)
    with db_conn(write=True) as db:
        in_cart = db.execute('SELECT cart_id FROM cart_items WHERE tag_uid=?', (uid,)).fetchone()
        if in_cart:
            return {'error': f'Tag {uid} is in {in_cart["cart_id"]} right now. Clear that cart (or finish checkout) before removing it.'}, 409
        cur = db.execute('DELETE FROM tags WHERE uid=?', (uid,))
        if not cur.rowcount:
            return {'error': 'Unknown UID'}, 404
        emit(db, 'unregister', f'RFID {uid} unregistered', uid)
    return {'ok': True, 'message': f'Tag {uid} removed'}, 200


def reactivate_tag(uid):
    uid = normalize_uid(uid)
    with db_conn(write=True) as db:
        tag = db.execute('SELECT t.status,p.name FROM tags t JOIN products p ON p.id=t.product_id WHERE t.uid=?', (uid,)).fetchone()
        if not tag:
            return {'error': 'Unknown UID'}, 404
        if tag['status'] == 'active':
            return {'error': 'Tag is already active'}, 409
        db.execute("UPDATE tags SET status='active' WHERE uid=?", (uid,))
        emit(db, 'reactivate', f'RFID {uid} ({tag["name"]}) re-activated after restock', uid)
    return {'ok': True, 'message': f'Tag {uid} is active again'}, 200


def weight_tick(moment=None):
    """One weight-verification step for the cart the reader/scale is attached to."""
    moment = time.monotonic() if moment is None else moment
    with db_conn() as db:
        cart_id = get_setting(db, 'reader_cart')
        rows = db.execute('''SELECT p.weight_g,p.tolerance_pct FROM cart_items ci JOIN products p ON p.id=ci.product_id
                             WHERE ci.cart_id=?''', (cart_id,)).fetchall()
    expected, allowed, unweighed = allowed_deviation([(r[0], r[1]) for r in rows], CONFIG['min_tolerance_g'])
    scale.expected_hint = expected
    measured = scale.grams()
    instant = classify(expected, allowed, measured)
    with weight_lock:
        if weight_info['cart'] != cart_id:
            weight_judge.cart_changed(moment)
        status, changed = weight_judge.update(instant, moment)
        weight_info.update(cart=cart_id, expected=expected, allowed=allowed, measured=measured,
                           status=status, unweighed=unweighed)
    if changed and status in ('ok', 'mismatch'):
        with db_conn(write=True) as db:
            flagged = db.execute('SELECT weight_flag FROM carts WHERE id=?', (cart_id,)).fetchone()
            flagged = bool(flagged and flagged[0])
            if status == 'mismatch' and not flagged:
                db.execute('UPDATE carts SET weight_flag=1 WHERE id=?', (cart_id,))
                emit(db, 'weight_mismatch', f'{cart_id} weight mismatch: measured {measured:.0f} g, '
                     f'expected {expected:.0f} g (+/- {allowed:.0f} g). Customer asked to rescan.', cart=cart_id)
            elif status == 'ok' and flagged:
                db.execute('UPDATE carts SET weight_flag=0 WHERE id=?', (cart_id,))
                emit(db, 'weight_ok', f'{cart_id} weight back within tolerance ({measured:.0f} g)', cart=cart_id)
            else:
                flagged = None
        if status == 'mismatch' and flagged is False:
            announce(('Weight mismatch', 'Please rescan', 'Weight mismatch. Please rescan your items.'))
    return status


def weight_monitor():
    while True:
        try:
            weight_tick()
        except Exception as exc:  # never let the monitor die
            print('Weight monitor error:', exc)
        time.sleep(0.5)


# ------------------------------------------------------------------ security helpers

LOGIN_WINDOW_S, LOGIN_MAX_FAILS = 300, 5
login_failures = {}
login_lock = threading.Lock()


def csrf_token():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_urlsafe(24)
    return session['csrf']


app.jinja_env.globals['csrf_token'] = csrf_token


@app.before_request
def same_origin_guard():
    if request.method != 'POST':
        return None
    source = request.headers.get('Origin') or request.headers.get('Referer')
    if source and urlparse(source).netloc != request.host:
        return jsonify({'error': 'Cross-site request blocked'}), 403
    if request.path.startswith('/api/'):
        # Custom header: browsers cannot send it cross-site without a CORS preflight we never answer.
        if request.headers.get('X-SmartMart') != '1':
            return jsonify({'error': 'Missing X-SmartMart header'}), 403
    elif not session.get('csrf') or not hmac.compare_digest(request.form.get('csrf', '').encode(),
                                                             session['csrf'].encode()):
        return render_template('login.html', error='Session expired - please try again.', password_set=True), 403
    return None


def login_blocked(ip):
    with login_lock:
        attempts = login_failures.setdefault(ip, deque())
        cutoff = time.time() - LOGIN_WINDOW_S
        while attempts and attempts[0] < cutoff:
            attempts.popleft()
        if len(attempts) >= LOGIN_MAX_FAILS:
            return int(attempts[0] + LOGIN_WINDOW_S - time.time()) + 1
        return 0


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if session.get('admin'):
            return fn(*args, **kwargs)
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Admin login required'}), 401
        return redirect(url_for('login', next=request.path))
    return wrapper


def result(payload, code): return jsonify(payload), code


def body():
    return request.get_json(silent=True) or {}


# ------------------------------------------------------------------ pages

@app.get('/')
def index(): return redirect('/customer?cart=CART-01')


@app.get('/customer')
def customer():
    cart_id = request.args.get('cart', 'CART-01')
    with db_conn() as db:
        if not db.execute('SELECT 1 FROM carts WHERE id=?', (cart_id,)).fetchone():
            return redirect('/customer?cart=CART-01')
    return render_template('customer.html', cart_id=cart_id, zones=ZONES)


@app.route('/login', methods=['GET', 'POST'])
def login():
    password_set = bool(CONFIG.get('admin_password'))
    if request.method == 'POST':
        ip = request.remote_addr or '?'
        wait = login_blocked(ip)
        if wait:
            return render_template('login.html', error=f'Too many failed attempts. Try again in {wait} s.',
                                   password_set=password_set), 429
        pwd = request.form.get('password', '')
        if password_set and hmac.compare_digest(pwd.encode(), CONFIG['admin_password'].encode()):
            with login_lock:
                login_failures.pop(ip, None)
            session.clear()
            session['admin'] = True
            next_url = request.args.get('next', '/admin')
            return redirect(next_url if next_url.startswith('/') and not next_url.startswith('//') else '/admin')
        with login_lock:
            login_failures.setdefault(ip, deque()).append(time.time())
        return render_template('login.html', error='Incorrect admin password', password_set=password_set), 403
    return render_template('login.html', error=None, password_set=password_set)


@app.post('/logout')
def logout():
    session.clear()
    return redirect('/login')


@app.get('/admin')
@require_admin
def admin(): return render_template('admin.html', zones=ZONES)


@app.get('/inventory')
@require_admin
def inventory(): return render_template('inventory.html', zones=ZONES)


# ------------------------------------------------------------------ customer API

@app.get('/api/customer/<cart_id>')
def customer_state(cart_id):
    with db_conn() as db:
        cart = cart_detail(db, cart_id)
        if not cart:
            return jsonify({'error': 'Cart not found'}), 404
        is_reader = get_setting(db, 'reader_cart') == cart_id
        return jsonify({'cart': cart, 'zones': ZONES,
                        'products': [{'name': p['name'], 'zone': p['zone'], 'emoji': p['emoji']} for p in products_list(db)],
                        'reader_here': is_reader, 'demo': CONFIG['demo'],
                        'scanner_ready': is_reader and (CONFIG['demo'] or rfid.status.ok)})


@app.post('/api/customer/<cart_id>/clear')
def clear_route(cart_id): return result(*clear_cart(cart_id))


@app.post('/api/customer/<cart_id>/checkout')
def checkout_route(cart_id): return result(*make_checkout(cart_id))


@app.get('/api/customer/<cart_id>/payment')
def payment_route(cart_id):
    with db_conn() as db:
        cart = cart_detail(db, cart_id)
    if not cart:
        return jsonify({'error': 'Cart not found'}), 404
    if not cart['order']:
        return jsonify({'error': 'No pending order'}), 404
    return jsonify(payment_payload(cart['order']))


# ------------------------------------------------------------------ admin API

@app.get('/api/admin/state')
@require_admin
def admin_state():
    with db_conn() as db:
        return jsonify(state(db))


@app.get('/api/inventory/state')
@require_admin
def inventory_state():
    with db_conn() as db:
        return jsonify({'products': products_list(db), 'zones': ZONES,
                        'events': [dict(r) for r in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 40')]})


@app.post('/api/admin/reader-cart')
@require_admin
def reader_cart_route():
    cart_id = str(body().get('cart_id', ''))
    with db_conn(write=True) as db:
        if not db.execute('SELECT 1 FROM carts WHERE id=?', (cart_id,)).fetchone():
            return jsonify({'error': 'Cart not found'}), 404
        set_setting(db, 'reader_cart', cart_id)
        emit(db, 'reader', f'RC522 reader and scale assigned to {cart_id}', cart=cart_id)
    cart_changed()
    return jsonify({'ok': True, 'reader_cart': cart_id})


@app.post('/api/admin/cart-location')
@require_admin
def location_route():
    data = body()
    cart_id, zone = data.get('cart_id'), data.get('zone')
    if zone not in ZONE_LOOKUP:
        return jsonify({'error': 'Invalid shop zone'}), 400
    with db_conn(write=True) as db:
        cur = db.execute('UPDATE carts SET zone=?,updated_at=? WHERE id=?', (zone, now(), cart_id))
        if not cur.rowcount:
            return jsonify({'error': 'Cart not found'}), 404
        emit(db, 'location', f'{cart_id} moved to {ZONE_LOOKUP[zone]} (manager-reported)', cart=cart_id)
    return jsonify({'ok': True})


@app.post('/api/admin/register')
@require_admin
def register_route():
    data = body()
    try:
        product_id = int(data.get('product_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Select a product'}), 400
    return result(*register_tag(data.get('uid', ''), product_id))


@app.post('/api/admin/unregister')
@require_admin
def unregister_route(): return result(*unregister_tag(body().get('uid', '')))


@app.post('/api/admin/reactivate')
@require_admin
def reactivate_route(): return result(*reactivate_tag(body().get('uid', '')))


@app.get('/api/admin/scan-register')
@require_admin
def scan_register_status():
    with db_conn() as db:
        return jsonify(registration_state(db))


@app.post('/api/admin/scan-register/start')
@require_admin
def scan_register_start():
    with db_conn(write=True) as db:
        set_setting(db, 'reg_until', time.time() + REGISTRATION_WINDOW_S)
        set_setting(db, 'reg_uid', '')
        state_ = registration_state(db)
    return jsonify({'ok': True, **state_})


@app.post('/api/admin/scan-register/cancel')
@require_admin
def scan_register_cancel():
    with db_conn(write=True) as db:
        set_setting(db, 'reg_until', 0)
    return jsonify({'ok': True})


@app.post('/api/admin/confirm-payment')
@require_admin
def confirm_payment_route():
    data = body()
    if not data.get('verified'):
        return jsonify({'error': 'Verify the payment independently first'}), 400
    return result(*mark_paid(str(data.get('order_id', '')).strip()))


@app.post('/api/admin/demo-scan')
@require_admin
def demo_scan_route():
    if not CONFIG['demo']:
        return jsonify({'error': 'Demo scanning is disabled in hardware mode'}), 403
    # A refused scan (unknown tag, out of stock...) is a normal outcome of scanning,
    # so the simulated reader always answers 200 and reports the scan status in the body.
    payload, code = scan_tag(body().get('uid', ''), 'demo')
    return jsonify({**payload, 'status': code}), 200


@app.post('/api/admin/mute')
@require_admin
def mute_route():
    muted = bool(body().get('muted'))
    with db_conn(write=True) as db:
        set_setting(db, 'muted', '1' if muted else '0')
    speaker.muted = muted
    return jsonify({'ok': True, 'muted': muted})


@app.post('/api/admin/scale/tare')
@require_admin
def scale_tare_route():
    try:
        offset = scale.tare()
    except Exception as exc:
        return jsonify({'error': str(exc)}), 409
    with db_conn(write=True) as db:
        set_setting(db, 'scale_offset', offset)
        emit(db, 'scale', 'Scale tared (empty cart = 0 g)')
    cart_changed()
    return jsonify({'ok': True, 'message': 'Scale tared'})


@app.post('/api/admin/scale/calibrate')
@require_admin
def scale_calibrate_route():
    try:
        known = float(body().get('known_g'))
        factor = scale.calibrate(known)
    except (TypeError, ValueError) as exc:
        return jsonify({'error': f'Enter the known weight in grams ({exc})'}), 400
    except Exception as exc:
        return jsonify({'error': str(exc)}), 409
    with db_conn(write=True) as db:
        if not CONFIG['demo']:
            set_setting(db, 'scale_factor', factor)
        emit(db, 'scale', f'Scale calibrated with {known:.0f} g reference')
    cart_changed()
    return jsonify({'ok': True, 'message': f'Calibrated (factor {factor:.3f} counts/g)'})


@app.post('/api/admin/scale/demo')
@require_admin
def scale_demo_route():
    if not CONFIG['demo']:
        return jsonify({'error': 'Fake weight is only available in --demo mode'}), 403
    data = body()
    if 'follow' in data:
        scale.demo_follow = bool(data['follow'])
    if data.get('grams') is not None:
        try:
            scale.demo_grams = max(0.0, float(data['grams']))
        except (TypeError, ValueError):
            return jsonify({'error': 'Weight must be a number'}), 400
        scale.demo_follow = False
    return jsonify({'ok': True, 'follow': scale.demo_follow, 'grams': scale.demo_grams})


# ------------------------------------------------------------------ inventory API

def product_fields(data, partial=False):
    """Validate product fields; returns dict or raises ValueError."""
    out = {}
    if 'name' in data or not partial:
        out['name'] = str(data.get('name', '')).strip()[:60]
        if not out['name']:
            raise ValueError('Product name is required')
    if 'category' in data or not partial:
        out['category'] = str(data.get('category') or 'General').strip()[:40] or 'General'
    if 'price' in data or not partial:
        out['price'] = round(float(data['price']), 2)
        if out['price'] < 0:
            raise ValueError('Price cannot be negative')
    if 'zone' in data or not partial:
        out['zone'] = str(data.get('zone'))
        if out['zone'] not in ZONE_LOOKUP:
            raise ValueError('Choose a store section')
    if 'emoji' in data or not partial:
        out['emoji'] = str(data.get('emoji') or '📦').strip()[:8] or '📦'
    if data.get('weight_g') not in (None, '') or not partial:
        out['weight_g'] = round(float(data.get('weight_g') or 0), 1)
        if not 0 <= out['weight_g'] <= 10000:
            raise ValueError('Weight must be 0-10000 g (10 kg load cell)')
    if data.get('tolerance_pct') not in (None, '') or not partial:
        out['tolerance_pct'] = round(float(data.get('tolerance_pct') or 10), 1)
        if not 0 <= out['tolerance_pct'] <= 100:
            raise ValueError('Tolerance must be 0-100 %')
    return out


@app.post('/api/inventory/product')
@require_admin
def create_product_route():
    data = body()
    try:
        fields = product_fields(data)
        sku = str(data['sku']).strip().upper()[:20]
        stock = int(data['stock'])
        if not re.fullmatch(r'[A-Z0-9\-_]+', sku) or stock < 0:
            raise ValueError('SKU must be letters/digits/- and stock must be 0 or more')
    except (KeyError, TypeError) as exc:
        return jsonify({'error': f'Check product details ({exc})'}), 400
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    with db_conn(write=True) as db:
        try:
            db.execute('''INSERT INTO products(sku,name,category,price,on_hand,zone,emoji,weight_g,tolerance_pct)
                VALUES(?,?,?,?,?,?,?,?,?)''', (sku, fields['name'], fields['category'], fields['price'], stock,
                                             fields['zone'], fields['emoji'], fields['weight_g'], fields['tolerance_pct']))
        except sqlite3.IntegrityError:
            return jsonify({'error': 'SKU already exists'}), 409
        emit(db, 'product', f'New inventory product: {fields["name"]} ({sku})')
    return jsonify({'ok': True, 'message': 'Product created'})


@app.post('/api/inventory/product/<int:product_id>')
@require_admin
def update_product_route(product_id):
    try:
        fields = product_fields(body(), partial=True)
    except (TypeError, ValueError) as exc:
        return jsonify({'error': str(exc)}), 400
    if not fields:
        return jsonify({'error': 'Nothing to update'}), 400
    with db_conn(write=True) as db:
        product = db.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
        if not product:
            return jsonify({'error': 'Product not found'}), 404
        db.execute(f'UPDATE products SET {",".join(k + "=?" for k in fields)} WHERE id=?', (*fields.values(), product_id))
        changes = ', '.join(f'{k} {product[k]} -> {v}' for k, v in fields.items() if product[k] != v) or 'no changes'
        emit(db, 'product', f'{product["name"]} updated: {changes}')
    cart_changed()
    return jsonify({'ok': True, 'message': 'Product updated'})


@app.post('/api/inventory/restock')
@require_admin
def restock_route():
    data = body()
    try:
        product_id, amount = int(data['product_id']), int(data['amount'])
    except (KeyError, TypeError, ValueError):
        return jsonify({'error': 'Invalid product or quantity'}), 400
    if not 0 < amount <= 10000:
        return jsonify({'error': 'Enter a positive restock quantity'}), 400
    with db_conn(write=True) as db:
        product = db.execute('SELECT name FROM products WHERE id=?', (product_id,)).fetchone()
        if not product:
            return jsonify({'error': 'Product not found'}), 404
        db.execute('UPDATE products SET on_hand=on_hand+? WHERE id=?', (amount, product_id))
        emit(db, 'restock', f'{product["name"]} restocked +{amount}')
    return jsonify({'ok': True, 'message': 'Stock added'})


# ------------------------------------------------------------------ startup

def start_hardware():
    """Start every device. Any of them may fail; each only reports a status badge."""
    with db_conn() as db:
        muted = get_setting(db, 'muted') == '1'
        offset = float(get_setting(db, 'scale_offset') or 0)
        factor = get_setting(db, 'scale_factor') or None
    speaker.muted = muted
    scale.__init__(driver_name=CONFIG['scale_driver'], dt_pin=CONFIG['hx711_dt'], sck_pin=CONFIG['hx711_sck'],
                   serial_port=CONFIG['scale_serial_port'], serial_baud=CONFIG['scale_serial_baud'],
                   offset=offset, factor=factor, demo=CONFIG['demo'])
    lcd.address = CONFIG['lcd_address']
    for name, device in (('LCD', lcd), ('speaker', speaker), ('scale', scale)):
        try:
            device.start()
        except Exception as exc:
            print(f'{name} failed to start: {exc}')
    if not CONFIG['demo']:
        rfid.start()
    threading.Thread(target=weight_monitor, name='weight-monitor', daemon=True).start()


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('10.255.255.255', 1))  # no packet is sent
            return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'


def port_free(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def main(argv=None):
    try:
        sys.stdout.reconfigure(errors='replace')
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description='SmartMart RFID smart cart server')
    parser.add_argument('--demo', action='store_true', help='No hardware needed; enables Test scan + fake scale in Admin')
    parser.add_argument('--host', default=os.environ.get('HOST', '0.0.0.0'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', 5000)))
    parser.add_argument('--server', choices=['flask', 'waitress'], default=os.environ.get('SMARTMART_SERVER', 'flask'))
    args = parser.parse_args(argv)
    configure(demo=args.demo or None)
    if not CONFIG['admin_password']:
        print('\nADMIN_PASSWORD is not set, so nobody could log in to Admin/Inventory.\n'
              '  Windows PowerShell:  $env:ADMIN_PASSWORD="choose-a-password"; python app.py\n'
              '  Raspberry Pi bash:   ADMIN_PASSWORD="choose-a-password" python3 app.py\n'
              '  ...or put ADMIN_PASSWORD=... in a .env file next to app.py (see .env.example)\n'
              '  ...or try it without hardware: python app.py --demo   (password demo1234)\n')
        return 2
    try:
        init_db()
    except (sqlite3.Error, OSError) as exc:
        print(f'\nCannot open the database at {CONFIG["db_path"]}: {exc}\n'
              'Set SMARTMART_DB to a writable file path, e.g. SMARTMART_DB=~/smartmart.db\n')
        return 3
    if not port_free(args.host, args.port):
        print(f'\nPort {args.port} is already in use (an older SmartMart still running?).\n'
              f'  Stop it, or start on another port:  python app.py --port {args.port + 1}\n')
        return 4
    start_hardware()
    ip = lan_ip()
    base = f'http://{ip}:{args.port}' if args.host == '0.0.0.0' else f'http://{args.host}:{args.port}'
    print(f'\nSmartMart running ({"DEMO mode" if CONFIG["demo"] else "hardware mode"})')
    print(f'  Customer : {base}/customer?cart=CART-01')
    print(f'  Admin    : {base}/admin')
    print(f'  Inventory: {base}/inventory')
    print(f'  (on this machine you can also use http://127.0.0.1:{args.port})')
    if CONFIG['demo'] and not os.environ.get('ADMIN_PASSWORD'):
        print('  Demo admin password: demo1234')
    if not CONFIG['merchant_upi']:
        print('  Payment QR is DEMO ONLY (MERCHANT_UPI_ID not set).')
    print('Press Ctrl+C to stop.\n', flush=True)
    if args.server == 'waitress':
        try:
            from waitress import serve
        except ImportError:
            print('waitress not installed (pip install waitress); using the Flask server.')
        else:
            serve(app, host=args.host, port=args.port, threads=8)
            return 0
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False, threaded=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
