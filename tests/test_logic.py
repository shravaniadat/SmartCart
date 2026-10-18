"""SmartMart logic + HTTP tests. Run: python -m pytest -q"""
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app as sm  # noqa: E402
from hardware.rfid import Debouncer, RFIDReader, uid_to_number  # noqa: E402
from hardware.scale import WeightJudge, allowed_deviation, classify  # noqa: E402

RICE_TAG, MILK_TAG = '799992525454', '619249934221'


@pytest.fixture
def m(tmp_path, monkeypatch):
    for key in ('ADMIN_PASSWORD', 'MERCHANT_UPI_ID', 'SECRET_KEY'):
        monkeypatch.delenv(key, raising=False)
    sm.configure(demo=True, db_path=tmp_path / 'test.db')
    sm.init_db()
    sm.scale.__init__(demo=True)
    sm.weight_judge.__init__(settle_s=2.0)
    sm.weight_info.update(cart=None, status='no_scale')
    sm.login_failures.clear()
    yield sm


def product(m, sku):
    with m.db_conn() as db:
        return next(p for p in m.products_list(db) if p['sku'] == sku)


def events(m, type_):
    with m.db_conn() as db:
        return [dict(r) for r in db.execute('SELECT * FROM events WHERE type=? ORDER BY id', (type_,))]


def cart(m, cart_id='CART-01'):
    with m.db_conn() as db:
        return m.cart_detail(db, cart_id)


# ------------------------------------------------------------------ seed + scan flow

def test_seed_catalogue_and_tags(m):
    expected = {'APPLE-01': (90, 20), 'RICE-01': (80, 22), 'SOAP-01': (40, 17), 'BISC-01': (25, 26),
                'MILK-01': (35, 30), 'JUICE-01': (55, 22), 'CHIPS-01': (20, 25), 'SHAM-01': (95, 15),
                'TEA-01': (120, 18), 'TISSUE-01': (60, 14), 'BREAD-01': (45, 16)}
    with m.db_conn() as db:
        ps = {p['sku']: p for p in m.products_list(db)}
        tags = {r['uid']: r['product_id'] for r in db.execute('SELECT * FROM tags')}
    assert {k: (p['price'], p['on_hand']) for k, p in ps.items()} == expected
    assert all(p['weight_g'] > 0 and p['tolerance_pct'] == 10 for p in ps.values())
    assert tags[RICE_TAG] == ps['RICE-01']['id'] and tags[MILK_TAG] == ps['MILK-01']['id']
    assert 'D-TEA-01' in tags  # demo virtual tags


def test_scan_flow_unknown_duplicate_and_clear(m):
    rice = product(m, 'RICE-01')
    out, code = m.scan_tag('NOT-A-TAG')
    assert code == 404 and not out['ok']
    assert events(m, 'unknown')[-1]['tag_uid'] == 'NOT-A-TAG'  # early-return event was committed
    assert m.scan_tag('')[1] == 400 and m.scan_tag('bad uid!')[1] == 400

    out, code = m.scan_tag(RICE_TAG)
    assert code == 200 and out['added'] and out['total'] == 80
    out, code = m.scan_tag(RICE_TAG)
    assert code == 200 and out['duplicate']
    after = product(m, 'RICE-01')
    assert (after['on_hand'], after['reserved'], after['available']) == (rice['on_hand'], 1, rice['available'] - 1)
    assert cart(m)['count'] == 1

    assert m.clear_cart('CART-01')[0]['ok']
    assert product(m, 'RICE-01')['reserved'] == 0 and cart(m)['count'] == 0
    assert m.clear_cart('NOPE')[1] == 404


def test_scan_goes_to_assigned_reader_cart(m):
    with m.db_conn(write=True) as db:
        m.set_setting(db, 'reader_cart', 'CART-02')
    m.scan_tag(RICE_TAG)
    assert cart(m, 'CART-02')['count'] == 1 and cart(m, 'CART-01')['count'] == 0


# ------------------------------------------------------------------ stock math

def test_reservation_out_of_stock_and_restock(m):
    tea = product(m, 'TEA-01')
    with m.db_conn(write=True) as db:  # leave exactly one unit
        db.execute('UPDATE products SET on_hand=1 WHERE id=?', (tea['id'],))
        db.execute('INSERT INTO tags(uid,product_id,registered_at) VALUES(?,?,?)', ('TEA-2', tea['id'], m.now()))
    assert m.scan_tag('D-TEA-01')[1] == 200
    out, code = m.scan_tag('TEA-2')
    assert code == 409 and 'out of stock' in out['message']
    assert events(m, 'out_of_stock')[-1]['tag_uid'] == 'TEA-2'
    assert product(m, 'TEA-01')['available'] == 0

    client = login(m.app.test_client())
    r = post(client, '/api/inventory/restock', {'product_id': tea['id'], 'amount': 4})
    assert r.status_code == 200
    t = product(m, 'TEA-01')
    assert (t['on_hand'], t['reserved'], t['available']) == (5, 1, 4)
    assert m.scan_tag('TEA-2')[1] == 200
    assert post(client, '/api/inventory/restock', {'product_id': tea['id'], 'amount': 0}).status_code == 400


# ------------------------------------------------------------------ checkout + payment

def test_checkout_and_manager_payment(m):
    rice0, milk0 = product(m, 'RICE-01'), product(m, 'MILK-01')
    assert m.make_checkout('CART-01')[1] == 400  # empty cart
    m.scan_tag(RICE_TAG)
    m.scan_tag(MILK_TAG)
    out, code = m.make_checkout('CART-01')
    assert code == 200 and out['order']['status'] == 'pending' and out['order']['amount'] == 115.0
    assert out['payment']['qr'].startswith('data:image/png;base64,')
    assert not out['payment']['configured'] and 'DEMO ONLY' in out['payment']['message']
    order_id = out['order']['id']
    assert m.make_checkout('CART-01')[0]['order']['id'] == order_id  # idempotent

    assert m.scan_tag('D-TEA-01')[1] == 409  # cart locked while awaiting payment
    assert events(m, 'blocked')

    assert m.mark_paid('SM-NOPE')[1] == 404
    paid, code = m.mark_paid(order_id)
    assert code == 200 and paid['ok']
    assert m.mark_paid(order_id)[1] == 409
    rice, milk = product(m, 'RICE-01'), product(m, 'MILK-01')
    assert (rice['on_hand'], rice['reserved']) == (rice0['on_hand'] - 1, 0)
    assert (milk['on_hand'], milk['reserved']) == (milk0['on_hand'] - 1, 0)
    assert cart(m)['count'] == 0 and cart(m)['order'] is None
    with m.db_conn() as db:
        assert db.execute('SELECT status FROM tags WHERE uid=?', (RICE_TAG,)).fetchone()[0] == 'sold'
        assert db.execute('SELECT status FROM orders WHERE id=?', (order_id,)).fetchone()[0] == 'paid'
    assert m.scan_tag(RICE_TAG)[1] == 409  # sold tag cannot be billed again


def test_clear_cancels_pending_order(m):
    m.scan_tag(RICE_TAG)
    order_id = m.make_checkout('CART-01')[0]['order']['id']
    m.clear_cart('CART-01')
    with m.db_conn() as db:
        assert db.execute('SELECT status FROM orders WHERE id=?', (order_id,)).fetchone()[0] == 'cancelled'
    assert m.mark_paid(order_id)[1] == 409


def test_real_upi_qr_when_merchant_configured(m):
    m.CONFIG.update(merchant_upi='shop@okbank', merchant_name='Group 21 Mart')
    payload = m.payment_payload({'id': 'SM-1', 'amount': 115.0})
    assert payload['configured'] and payload['merchant'] == 'Group 21 Mart'
    assert 'not confirmed automatically' in payload['message']


# ------------------------------------------------------------------ tag rules

def test_tag_register_unregister_reactivate_rules(m):
    sham = product(m, 'SHAM-01')
    assert m.register_tag('', sham['id'])[1] == 400
    assert m.register_tag('has space!', sham['id'])[1] == 400
    assert m.register_tag('NEW-1', 99999)[1] == 404
    assert m.register_tag(' new-1 ', sham['id'])[1] == 200  # normalized to NEW-1
    out, code = m.register_tag('NEW-1', sham['id'])
    assert code == 409 and 'already registered' in out['error']
    assert product(m, 'SHAM-01')['active_tags'] == 2  # D-SHAM-01 + NEW-1

    m.scan_tag('NEW-1')
    out, code = m.unregister_tag('NEW-1')
    assert code == 409 and 'CART-01' in out['error']  # blocked while in a cart
    m.clear_cart('CART-01')
    assert m.unregister_tag('NEW-1')[1] == 200
    assert m.unregister_tag('NEW-1')[1] == 404
    assert m.scan_tag('NEW-1')[1] == 404

    assert m.reactivate_tag(RICE_TAG)[1] == 409  # already active
    m.scan_tag(RICE_TAG)
    m.mark_paid(m.make_checkout('CART-01')[0]['order']['id'])
    assert m.scan_tag(RICE_TAG)[1] == 409
    assert m.reactivate_tag(RICE_TAG)[1] == 200
    assert m.scan_tag(RICE_TAG)[1] == 200
    assert {e['type'] for e in events(m, 'reactivate')} == {'reactivate'}


def test_scan_to_register_captures_next_scan(m):
    client = login(m.app.test_client())
    assert post(client, '/api/admin/scan-register/start').get_json()['active']
    out, code = m.scan_tag('FRESH-TAG')
    assert code == 200 and out['captured']
    assert cart(m)['count'] == 0 and not events(m, 'unknown')  # captured, not an error
    status = client.get('/api/admin/scan-register').get_json()
    assert status == {'active': False, 'remaining': 0, 'uid': 'FRESH-TAG', 'registered_to': None}
    # mode ended: the next unknown scan is a normal alert again
    assert m.scan_tag('FRESH-TAG')[1] == 404
    # a known tag scanned during registration is captured (not billed) and identified
    post(client, '/api/admin/scan-register/start')
    m.scan_tag(RICE_TAG)
    assert client.get('/api/admin/scan-register').get_json()['registered_to'] == 'Basmati rice'
    assert cart(m)['count'] == 0
    post(client, '/api/admin/scan-register/start')
    post(client, '/api/admin/scan-register/cancel')
    assert m.scan_tag('FRESH-TAG')[1] == 404


# ------------------------------------------------------------------ weight

def test_weight_tolerance_math():
    assert allowed_deviation([], 30) == (0, 30, 0)
    assert allowed_deviation([(1000, 10), (520, 10)], 30) == (1520, 152, 0)
    assert allowed_deviation([(55, 10), (0, 10)], 30) == (55, 30, 1)  # minimum tolerance, unweighed item
    assert classify(1000, 100, 1099) == 'ok'
    assert classify(1000, 100, 1101) == 'mismatch'
    assert classify(1000, 100, None) == 'no_scale'


def test_weight_judge_settles():
    j = WeightJudge(settle_s=2)
    assert j.update('ok', 0) == ('ok', True)
    j.cart_changed(10)
    assert j.update('mismatch', 11)[0] == 'settling'   # inside settle window
    assert j.update('mismatch', 12.1) == ('mismatch', True)
    assert j.update('ok', 13) == ('mismatch', False)   # must hold for settle_s
    assert j.update('ok', 15.1) == ('ok', True)
    assert j.update('mismatch', 16) == ('ok', False)    # brief noise spike ignored
    assert j.update('ok', 16.5) == ('ok', False)
    assert j.update('no_scale', 17) == ('no_scale', True)


def test_weight_mismatch_flags_cart_and_order(m):
    m.scale.demo_follow = False
    m.scale.demo_grams = 0
    assert m.weight_tick(0) == 'settling'              # scale just attached to CART-01
    assert m.weight_tick(2.5) == 'ok'                  # empty cart, 0 g
    m.scan_tag(RICE_TAG)                               # expected 1000 g +/- 100 g
    m.weight_judge.cart_changed(100)
    assert m.weight_tick(101) == 'settling'
    assert m.weight_tick(102.5) == 'mismatch'          # still 0 g after settle time
    assert cart(m)['weight_flag'] and cart(m)['weight']['status'] == 'mismatch'
    assert len(events(m, 'weight_mismatch')) == 1
    m.weight_tick(103)
    assert len(events(m, 'weight_mismatch')) == 1      # alert logged once per mismatch
    out, code = m.make_checkout('CART-01')             # not blocked, but flagged for the manager
    assert code == 200 and out['order']['weight_flag'] == 1
    m.clear_cart('CART-01')
    m.scan_tag(RICE_TAG)
    m.scale.demo_grams = 1060
    m.weight_judge.cart_changed(200)
    assert m.weight_tick(202.5) == 'ok'
    assert not cart(m)['weight_flag']
    assert m.make_checkout('CART-01')[0]['order']['weight_flag'] == 0


def test_weight_recovery_logs_ok_event(m):
    m.scale.demo_follow = False
    m.weight_tick(-10)
    m.scan_tag(RICE_TAG)
    m.weight_judge.cart_changed(0)
    m.scale.demo_grams = 1500
    assert m.weight_tick(3) == 'mismatch'
    m.scale.demo_grams = 1000
    assert m.weight_tick(4) == 'mismatch'
    assert m.weight_tick(6.5) == 'ok'
    assert events(m, 'weight_ok') and not cart(m)['weight_flag']


# ------------------------------------------------------------------ migrations

OLD_SCHEMA = '''
CREATE TABLE products (id INTEGER PRIMARY KEY, sku TEXT UNIQUE NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL,
  price REAL NOT NULL CHECK(price >= 0), on_hand INTEGER NOT NULL CHECK(on_hand >= 0),
  reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved >= 0), zone TEXT NOT NULL, emoji TEXT NOT NULL DEFAULT '📦');
CREATE TABLE tags (uid TEXT PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','sold')), registered_at TEXT NOT NULL);
CREATE TABLE carts (id TEXT PRIMARY KEY, zone TEXT NOT NULL DEFAULT 'entrance', checkout_order TEXT, updated_at TEXT NOT NULL);
CREATE TABLE cart_items (cart_id TEXT NOT NULL REFERENCES carts(id), tag_uid TEXT NOT NULL REFERENCES tags(uid),
  product_id INTEGER NOT NULL REFERENCES products(id), scanned_at TEXT NOT NULL, PRIMARY KEY (cart_id,tag_uid), UNIQUE(tag_uid));
CREATE TABLE orders (id TEXT PRIMARY KEY, cart_id TEXT NOT NULL, amount REAL NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','paid','cancelled')), created_at TEXT NOT NULL, completed_at TEXT);
CREATE TABLE order_items (order_id TEXT NOT NULL REFERENCES orders(id), tag_uid TEXT NOT NULL, product_name TEXT NOT NULL,
  price REAL NOT NULL, PRIMARY KEY(order_id,tag_uid));
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, message TEXT NOT NULL, tag_uid TEXT,
  cart_id TEXT, created_at TEXT NOT NULL);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO products VALUES (1,'RICE-01','Basmati rice','Grocery',80,21,1,'pantry','🍚');
INSERT INTO products VALUES (2,'CUSTOM-9','Old custom item','Misc',12.5,3,0,'home','📦');
INSERT INTO tags VALUES ('799992525454',1,'active','2026-10-01T10:00:00');
INSERT INTO carts VALUES ('CART-01','dairy',NULL,'2026-10-01T10:00:00');
INSERT INTO cart_items VALUES ('CART-01','799992525454',1,'2026-10-01T10:01:00');
INSERT INTO settings VALUES ('reader_cart','CART-01');
INSERT INTO events(type,message,created_at) VALUES ('scan','old event','2026-10-01T10:01:00');
'''


def test_migration_keeps_old_database(tmp_path, monkeypatch):
    monkeypatch.delenv('ADMIN_PASSWORD', raising=False)
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as db:
        db.executescript(OLD_SCHEMA)
    sm.configure(demo=False, db_path=path)
    added = sm.init_db()
    assert set(added) == {'products.weight_g', 'products.tolerance_pct', 'carts.weight_flag', 'orders.weight_flag'}
    assert sm.init_db() == []  # idempotent
    with sm.db_conn() as db:
        ps = {p['sku']: p for p in sm.products_list(db)}
        assert len(ps) == 2  # no re-seeding over existing data
        assert ps['RICE-01']['weight_g'] == 1000 and ps['RICE-01']['reserved'] == 1
        assert ps['CUSTOM-9']['weight_g'] == 0 and ps['CUSTOM-9']['tolerance_pct'] == 10
        c = sm.cart_detail(db, 'CART-01')
        assert c['count'] == 1 and c['zone'] == 'dairy' and not c['weight_flag']
        assert db.execute("SELECT COUNT(*) FROM events WHERE message='old event'").fetchone()[0] == 1
        assert sm.get_setting(db, 'muted') == '0'
    sm.weight_info.update(cart=None)
    assert sm.scan_tag('799992525454')[0]['duplicate']  # still in cart, still works


# ------------------------------------------------------------------ HTTP layer

def login(client, password='demo1234'):
    with client.session_transaction() as s:
        s['csrf'] = 'tok'
    client.post('/login', data={'password': password, 'csrf': 'tok'})
    return client


def post(client, url, json=None, headers=None):
    return client.post(url, json=json or {}, headers={'X-SmartMart': '1', **(headers or {})})


def test_login_wrong_password_rate_limit_and_csrf(m):
    c = m.app.test_client()
    assert c.get('/admin').status_code == 302
    assert c.get('/api/admin/state').status_code == 401
    c.get('/login')
    with c.session_transaction() as s:
        token = s['csrf']
    assert c.post('/login', data={'password': 'demo1234'}).status_code == 403          # no CSRF token
    assert c.post('/login', data={'password': 'demo1234', 'csrf': token},
                  headers={'Origin': 'http://evil.example'}).status_code == 403          # cross-site
    for _ in range(5):
        r = c.post('/login', data={'password': 'wrong', 'csrf': token})
        assert r.status_code == 403 and b'Incorrect admin password' in r.data
    r = c.post('/login', data={'password': 'demo1234', 'csrf': token})
    assert r.status_code == 429  # locked out even with the right password
    m.login_failures.clear()
    r = c.post('/login?next=/inventory', data={'password': 'demo1234', 'csrf': token})
    assert r.status_code == 302 and r.headers['Location'].endswith('/inventory')
    assert c.get('/api/admin/state').status_code == 200


def test_api_posts_need_header_and_same_origin(m):
    c = login(m.app.test_client())
    assert c.post('/api/admin/demo-scan', json={'uid': RICE_TAG}).status_code == 403
    assert post(c, '/api/admin/demo-scan', {'uid': RICE_TAG}, {'Origin': 'http://evil.example'}).status_code == 403
    r = post(c, '/api/admin/demo-scan', {'uid': RICE_TAG}, {'Origin': 'http://localhost'})
    assert r.status_code == 200 and r.get_json()['added']
    r = post(c, '/api/admin/demo-scan', {'uid': 'NOT-REGISTERED'})
    assert r.status_code == 200 and r.get_json()['status'] == 404 and not r.get_json()['ok']
    assert post(c, '/api/customer/CART-01/checkout').status_code == 200
    state = c.get('/api/admin/state').get_json()
    assert state['carts'][0]['order'] and state['hardware']['scale']['state'] == 'demo'


def test_admin_api_routes(m):
    c = login(m.app.test_client())
    assert post(c, '/api/admin/register', {'uid': 'ABC-1', 'product_id': 'x'}).status_code == 400
    rice = product(m, 'RICE-01')
    assert post(c, '/api/admin/register', {'uid': 'ABC-1', 'product_id': rice['id']}).status_code == 200
    assert post(c, '/api/admin/register', {'uid': 'ABC-1', 'product_id': rice['id']}).status_code == 409
    assert post(c, '/api/admin/unregister', {'uid': 'ABC-1'}).status_code == 200
    assert post(c, '/api/admin/reader-cart', {'cart_id': 'CART-03'}).status_code == 200
    assert post(c, '/api/admin/reader-cart', {'cart_id': 'CART-99'}).status_code == 404
    assert post(c, '/api/admin/cart-location', {'cart_id': 'CART-03', 'zone': 'snacks'}).status_code == 200
    assert post(c, '/api/admin/cart-location', {'cart_id': 'CART-03', 'zone': 'moon'}).status_code == 400
    assert post(c, '/api/admin/mute', {'muted': True}).get_json()['muted'] and m.speaker.muted
    assert post(c, '/api/admin/scale/demo', {'grams': 750}).get_json() == {'ok': True, 'follow': False, 'grams': 750}
    assert post(c, '/api/admin/scale/tare').status_code == 200
    assert post(c, '/api/admin/scale/calibrate', {'known_g': 'abc'}).status_code == 400
    assert post(c, '/api/admin/confirm-payment', {'order_id': 'X'}).status_code == 400
    r = post(c, '/api/inventory/product', {'name': 'Ghee', 'sku': 'ghee-01', 'category': 'Dairy', 'price': '550',
                                           'stock': '0', 'zone': 'dairy', 'weight_g': '500', 'tolerance_pct': '8'})
    assert r.status_code == 200
    ghee = product(m, 'GHEE-01')
    assert (ghee['weight_g'], ghee['tolerance_pct'], ghee['available']) == (500, 8, 0)
    assert post(c, '/api/inventory/product', {'name': 'Dup', 'sku': 'GHEE-01', 'price': 1, 'stock': 1,
                                              'zone': 'dairy'}).status_code == 409
    assert post(c, '/api/inventory/product', {'name': 'Bad', 'sku': 'B-1', 'price': -1, 'stock': 1,
                                              'zone': 'dairy'}).status_code == 400
    assert post(c, f'/api/inventory/product/{ghee["id"]}', {'price': '525.5', 'weight_g': '510'}).status_code == 200
    assert product(m, 'GHEE-01')['price'] == 525.5 and product(m, 'GHEE-01')['weight_g'] == 510
    assert post(c, '/api/inventory/product/99999', {'price': 1}).status_code == 404
    inv = c.get('/api/inventory/state').get_json()
    assert len(inv['products']) == 12 and inv['events']


def test_demo_scan_disabled_in_hardware_mode(m):
    c = login(m.app.test_client())
    m.CONFIG['demo'] = False
    assert post(c, '/api/admin/demo-scan', {'uid': RICE_TAG}).status_code == 403
    assert post(c, '/api/admin/scale/demo', {'grams': 1}).status_code == 403


def test_pages_render(m):
    c = m.app.test_client()
    assert b'data-page="customer"' in c.get('/customer?cart=CART-02').data
    assert c.get('/customer?cart=NOPE').status_code == 302
    login(c)
    for url in ('/admin', '/inventory'):
        page = c.get(url)
        assert page.status_code == 200 and b'googleapis' not in page.data and b'name="csrf"' in page.data


# ------------------------------------------------------------------ concurrency

def test_no_database_locked_under_concurrent_load(m):
    ids = []
    with m.db_conn(write=True) as db:
        pid = db.execute("SELECT id FROM products WHERE sku='BISC-01'").fetchone()[0]
        for i in range(40):
            db.execute('INSERT INTO tags(uid,product_id,registered_at) VALUES(?,?,?)', (f'LOAD-{i}', pid, m.now()))
            ids.append(f'LOAD-{i}')
    errors = []

    def scanner():
        try:
            for uid in ids:
                m.scan_tag(uid)
        except Exception as exc:
            errors.append(exc)

    def poller():
        try:
            for _ in range(60):
                with m.db_conn() as db:
                    m.state(db)
                m.weight_tick()
        except Exception as exc:
            errors.append(exc)

    def clearer():
        try:
            for _ in range(15):
                m.clear_cart('CART-01')
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=f) for f in (scanner, poller, poller, clearer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    with m.db_conn() as db:  # reservations always equal the items sitting in carts
        reserved = db.execute('SELECT SUM(reserved) FROM products').fetchone()[0]
        in_carts = db.execute('SELECT COUNT(*) FROM cart_items').fetchone()[0]
    assert reserved == in_carts


# ------------------------------------------------------------------ hardware helpers

def test_hardware_modules_are_import_safe_and_fail_soft(m):
    assert uid_to_number([0xBA, 0x42, 0x4C, 0x0F, 0xAB]) == 0xBA424C0FAB
    d = Debouncer(release_s=1.5)
    assert d.accept('A', 0) and not d.accept('A', 0.5) and not d.accept('A', 1.9)
    assert d.accept('A', 4) and d.accept('B', 4.1)
    if not sys.platform.startswith('linux'):
        r = RFIDReader(on_scan=lambda uid: None)
        r._run()
        assert r.status.snapshot()['state'] == 'off'
    from hardware.lcd import ascii16
    assert ascii16('Rice ₹80.00 — long product name') == 'Rice Rs80.00 - l'


class FakeHX711GPIO:
    """Simulates the HX711 serial protocol on a fake RPi.GPIO module."""
    BCM, OUT, IN, LOW, HIGH = 11, 0, 1, 0, 1

    def __init__(self, values):
        self.values, self.bits, self.clock = list(values), [], False

    def setwarnings(self, flag): pass
    def getmode(self): return self.BCM
    def setmode(self, mode): pass
    def setup(self, pin, mode, initial=None): pass
    def cleanup(self): pass

    def output(self, pin, level):
        if level and not self.clock and self.bits:
            self.bits.pop(0)              # rising edge shifts the next bit out
        self.clock = bool(level)

    def input(self, pin):
        if not self.bits:                 # conversion ready: DT low, load next sample (+1 bit offset)
            raw = self.values.pop(0) if self.values else 0
            self.bits = [0] + [(raw >> (23 - i)) & 1 for i in range(24)]
            return 0
        return self.bits[0]


def test_hx711_driver_decodes_twos_complement_and_medians(monkeypatch):
    import hardware.gpio as hwgpio
    from hardware.scale import HX711Driver
    samples = [0x000100, 0xFFFF00, 0x7FFFFF] + [1000, 1002, 999, 5000, 1001, 998, 1000]
    fake = FakeHX711GPIO([s & 0xFFFFFF for s in samples])
    monkeypatch.setattr(hwgpio, '_gpio', fake)
    drv = HX711Driver(dt_pin=5, sck_pin=6, samples=7)   # init consumes 0x000100 as a probe
    assert drv._read_once() == -256                     # 0xFFFF00 is -256 in two's complement
    assert drv._read_once() is None                     # saturated reading rejected
    assert drv.read_raw() == 1000                       # median ignores the 5000 spike


def test_rfid_read_uses_simple_mfrc522_numbering():
    class FakeReader:
        MI_OK, PICC_REQIDL = 0, 0x26
        def MFRC522_Request(self, mode): return 0, 0x10
        def MFRC522_Anticoll(self): return 0, [0xBA, 0x42, 0x4C, 0x0F, 0xAB]
    r = RFIDReader(on_scan=lambda uid: None)
    r._reader = FakeReader()
    assert r._read_no_block() == str(0xBA424C0FAB)
    FakeReader.MFRC522_Request = lambda self, mode: (2, None)
    assert r._read_no_block() is None
