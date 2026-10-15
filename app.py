"""SmartMart: three connected portals, one SQLite backend, Raspberry Pi RC522 support.

Local educational prototype. Cart position is admin-set, not automatically tracked.
UPI QR is a payment *request*, never evidence of a completed payment.
"""
import argparse
import base64
import hmac
import io
import os
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
import qrcode

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get('SMARTMART_DB', str(ROOT / 'smartmart.db')))
DEMO = '--demo' in __import__('sys').argv or os.environ.get('SMARTMART_DEMO') == '1'
MERCHANT_UPI_ID = os.environ.get('MERCHANT_UPI_ID', '').strip()
MERCHANT_NAME = os.environ.get('MERCHANT_NAME', 'SmartMart Demo').strip()
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'demo1234' if DEMO else '')
SECRET_PATH = ROOT / '.session_secret'
if SECRET_PATH.exists():
    SECRET = SECRET_PATH.read_text().strip()
else:
    SECRET = secrets.token_hex(32)
    SECRET_PATH.write_text(SECRET)
    try: os.chmod(SECRET_PATH, 0o600)
    except OSError: pass

app = Flask(__name__)
app.secret_key = SECRET
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')
write_lock = threading.RLock()
scanner_status = {'connected': False, 'error': 'Not started', 'last_seen': None}

ZONES = [
    {'id':'entrance', 'name':'Entrance', 'icon':'door-open', 'row':1, 'col':1},
    {'id':'produce', 'name':'Fresh produce', 'icon':'leaf', 'row':1, 'col':2},
    {'id':'dairy', 'name':'Dairy & bakery', 'icon':'milk', 'row':1, 'col':3},
    {'id':'pantry', 'name':'Rice & essentials', 'icon':'shopping-basket', 'row':2, 'col':1},
    {'id':'snacks', 'name':'Snacks', 'icon':'cookie', 'row':2, 'col':2},
    {'id':'beverages', 'name':'Beverages', 'icon':'coffee', 'row':2, 'col':3},
    {'id':'care', 'name':'Personal care', 'icon':'sparkles', 'row':3, 'col':1},
    {'id':'home', 'name':'Home supplies', 'icon':'house', 'row':3, 'col':2},
    {'id':'checkout', 'name':'Checkout', 'icon':'credit-card', 'row':3, 'col':3},
]
ZONE_LOOKUP = {z['id']:z['name'] for z in ZONES}
SEED_PRODUCTS = [
  ('RICE-01', 'Basmati rice', 'Grocery', 80.00, 22, 'pantry', '🍚'),
  ('MILK-01', 'Fresh milk', 'Dairy', 35.00, 30, 'dairy', '🥛'),
  ('BISC-01', 'Butter biscuits', 'Snacks', 25.00, 26, 'snacks', '🍪'),
  ('SOAP-01', 'Bath soap', 'Personal care', 40.00, 17, 'care', '🧼'),
  ('BREAD-01', 'Whole wheat bread', 'Bakery', 45.00, 16, 'dairy', '🍞'),
  ('TEA-01', 'Tea powder', 'Grocery', 120.00, 18, 'pantry', '🍵'),
  ('CHIPS-01', 'Potato chips', 'Snacks', 20.00, 25, 'snacks', '🥔'),
  ('JUICE-01', 'Fruit juice', 'Beverages', 55.00, 22, 'beverages', '🧃'),
  ('SHAM-01', 'Shampoo', 'Personal care', 95.00, 15, 'care', '🧴'),
  ('APPLE-01', 'Apples (pack)', 'Produce', 90.00, 20, 'produce', '🍎'),
  ('TISSUE-01', 'Tissue box', 'Home supplies', 60.00, 14, 'home', '🧻'),
]

def now(): return datetime.now().isoformat(timespec='seconds')

def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH), timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=15000')
    return db


def init_db():
    with write_lock:
        with connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY,
                sku TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                price REAL NOT NULL CHECK(price >= 0),
                on_hand INTEGER NOT NULL CHECK(on_hand >= 0),
                reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved >= 0),
                zone TEXT NOT NULL,
                emoji TEXT NOT NULL DEFAULT '📦'
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
                updated_at TEXT NOT NULL
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
                completed_at TEXT
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
            ''')
            n = db.execute('SELECT COUNT(*) FROM products').fetchone()[0]
            if n == 0:
                db.executemany('''INSERT INTO products
                    (sku,name,category,price,on_hand,zone,emoji) VALUES (?,?,?,?,?,?,?)''', SEED_PRODUCTS)
            for cart_id in ['CART-01','CART-02','CART-03']:
                db.execute('INSERT OR IGNORE INTO carts(id,zone,updated_at) VALUES (?,?,?)',
                           (cart_id, 'entrance', now()))
            db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('reader_cart','CART-01')")
            db.commit()


def emit(db, type_, message, uid=None, cart=None):
    db.execute('INSERT INTO events(type,message,tag_uid,cart_id,created_at) VALUES (?,?,?,?,?)',
               (type_,message,uid,cart,now()))


def rowdict(row): return dict(row) if row else None


def products_list(db):
    p = [dict(row) for row in db.execute('''SELECT *,on_hand-reserved AS available,
        (SELECT COUNT(*) FROM tags WHERE product_id=products.id AND status='active') AS active_tags
        FROM products ORDER BY name''')]
    return p


def cart_detail(db, cart_id):
    cart = db.execute('SELECT * FROM carts WHERE id=?', (cart_id,)).fetchone()
    if not cart: return None
    items = [dict(row) for row in db.execute('''SELECT ci.tag_uid AS uid,p.name,p.price,p.emoji,p.zone,ci.scanned_at
        FROM cart_items ci JOIN products p ON p.id=ci.product_id
        WHERE ci.cart_id=? ORDER BY ci.scanned_at DESC,ci.rowid DESC''', (cart_id,))]
    summary = []
    for item in items:
        found = next((r for r in summary if r['name']==item['name'] and r['price']==item['price']), None)
        if found: found['qty'] += 1
        else: summary.append({'name':item['name'], 'price':item['price'], 'qty':1, 'emoji':item['emoji']})
    total = round(sum(r['price'] for r in items),2)
    order = rowdict(db.execute('SELECT * FROM orders WHERE id=?', (cart['checkout_order'],)).fetchone()) if cart['checkout_order'] else None
    return {'id':cart['id'],'zone':cart['zone'],'zone_name':ZONE_LOOKUP.get(cart['zone'],cart['zone']),
            'items':items,'summary':summary,'total':total,'count':len(items),
            'order':order,'updated_at':cart['updated_at']}


def state(db):
    carts = [cart_detail(db,c['id']) for c in db.execute('SELECT id FROM carts ORDER BY id')]
    return {'carts':carts,'products':products_list(db),
            'events':[dict(r) for r in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 35')],
            'tags':[dict(r) for r in db.execute('''SELECT t.*,p.name AS product_name,p.sku FROM tags t
                JOIN products p ON p.id=t.product_id ORDER BY t.registered_at DESC,t.uid LIMIT 200''')],
            'reader_cart':db.execute("SELECT value FROM settings WHERE key='reader_cart'").fetchone()[0],
            'scanner':dict(scanner_status),'demo':DEMO,'zones':ZONES,'merchant_configured':bool(MERCHANT_UPI_ID)}


def scan_tag(uid, source='hardware'):
    """One physical scan, one cart item. Safe when scanner thread and web requests overlap."""
    uid = str(uid).strip()
    if not uid or len(uid)>80: return {'ok':False,'message':'Invalid tag UID'},400
    with write_lock:
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cart_id = db.execute("SELECT value FROM settings WHERE key='reader_cart'").fetchone()[0]
            current_cart = db.execute('SELECT checkout_order FROM carts WHERE id=?',(cart_id,)).fetchone()
            if current_cart['checkout_order']:
                return {'ok':False,'message':f'{cart_id} awaiting payment; cannot scan new items'},409
            tag = db.execute('''SELECT t.status,p.* FROM tags t JOIN products p ON p.id=t.product_id WHERE t.uid=?''',(uid,)).fetchone()
            if not tag:
                emit(db,'unknown',f'Unknown tag {uid} scanned — registration required',uid,cart_id)
                return {'ok':False,'message':f'Unregistered RFID tag {uid}'},404
            if tag['status']!='active':
                emit(db,'blocked',f'Sold tag {uid} scanned again',uid,cart_id)
                return {'ok':False,'message':'RFID tag belongs to a completed sale. Restock/re-register it first.'},409
            existing = db.execute('SELECT cart_id FROM cart_items WHERE tag_uid=?',(uid,)).fetchone()
            if existing:
                return {'ok':True,'duplicate':True,'message':f'Already scanned for {existing["cart_id"]}'},200
            if tag['on_hand']-tag['reserved']<1:
                emit(db,'out_of_stock',f'No available stock for {tag["name"]}',uid,cart_id)
                return {'ok':False,'message':f'{tag["name"]} is out of stock'},409
            db.execute('INSERT INTO cart_items(cart_id,tag_uid,product_id,scanned_at) VALUES(?,?,?,?)',
                       (cart_id,uid,tag['id'],now()))
            db.execute('UPDATE products SET reserved=reserved+1 WHERE id=?',(tag['id'],))
            db.execute('UPDATE carts SET updated_at=? WHERE id=?',(now(),cart_id))
            emit(db,'scan',f'{tag["name"]} scanned into {cart_id} • ₹{tag["price"]:,.2f}',uid,cart_id)
            return {'ok':True,'message':f'{tag["name"]} added','cart':cart_id,'product':tag['name']},200


def clear_cart(cart_id):
    with write_lock:
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cart = db.execute('SELECT * FROM carts WHERE id=?',(cart_id,)).fetchone()
            if not cart: return {'error':'Cart not found'},404
            rows = db.execute('SELECT product_id,COUNT(*) n FROM cart_items WHERE cart_id=? GROUP BY product_id',(cart_id,)).fetchall()
            for row in rows:
                db.execute('UPDATE products SET reserved=reserved-? WHERE id=?',(row['n'],row['product_id']))
            db.execute('DELETE FROM cart_items WHERE cart_id=?',(cart_id,))
            if cart['checkout_order']:
                db.execute("UPDATE orders SET status='cancelled',completed_at=? WHERE id=? AND status='pending'",(now(),cart['checkout_order']))
            db.execute('UPDATE carts SET checkout_order=NULL,updated_at=? WHERE id=?',(now(),cart_id))
            emit(db,'clear',f'{cart_id} cleared; {sum(r["n"] for r in rows)} reserved items restored',cart=cart_id)
            return {'ok':True,'message':'Cart cleared and stock restored'},200


def make_checkout(cart_id):
    with write_lock:
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cart = cart_detail(db,cart_id)
            if not cart: return {'error':'Cart not found'},404
            if not cart['count']: return {'error':'Scan at least one RFID product first'},400
            if cart['order']:
                order = cart['order']
            else:
                order_id = 'SM-' + uuid.uuid4().hex[:10].upper()
                db.execute('INSERT INTO orders(id,cart_id,amount,status,created_at) VALUES(?,?,?,?,?)',
                           (order_id,cart_id,cart['total'],'pending',now()))
                db.executemany('INSERT INTO order_items(order_id,tag_uid,product_name,price) VALUES(?,?,?,?)',
                               [(order_id,i['uid'],i['name'],i['price']) for i in cart['items']])
                db.execute('UPDATE carts SET checkout_order=?,updated_at=? WHERE id=?',(order_id,now(),cart_id))
                emit(db,'checkout',f'{cart_id} checkout started • ₹{cart["total"]:,.2f}',cart=cart_id)
                order = dict(db.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone())
            return {'ok':True,'order':order,'payment':payment_payload(order)},200


def payment_payload(order):
    # Without an explicitly configured merchant UPI ID, QR is a demo checkout token.
    configured = bool(MERCHANT_UPI_ID)
    if configured:
        text = 'upi://pay?' + urlencode({'pa':MERCHANT_UPI_ID,'pn':MERCHANT_NAME,
                           'am':f'{order["amount"]:.2f}','cu':'INR','tn':order['id']})
    else:
        text = f'SMARTMART-DEMO-ONLY | ORDER {order["id"]} | INR {order["amount"]:.2f} | NOT A PAYMENT QR'
    qr = qrcode.QRCode(box_size=7,border=2)
    qr.add_data(text)
    qr.make(fit=True)
    image = qr.make_image(fill_color='#10212b',back_color='white')
    stream = io.BytesIO()
    image.save(stream,format='PNG')
    return {'qr':'data:image/png;base64,'+base64.b64encode(stream.getvalue()).decode(),
            'configured':configured,'amount':order['amount'], 'order_id':order['id'],
            'merchant':MERCHANT_NAME if configured else '',
            'message':'UPI payment request. Payment is not confirmed automatically.' if configured
                      else 'DEMO ONLY: set MERCHANT_UPI_ID to create a real UPI payment request.'}


def mark_paid(order_id):
    with write_lock:
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            order = db.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone()
            if not order: return {'error':'Order not found'},404
            if order['status']!='pending': return {'error':f'Order already {order["status"]}'},409
            cart = db.execute('SELECT * FROM carts WHERE id=?',(order['cart_id'],)).fetchone()
            if not cart or cart['checkout_order']!=order_id: return {'error':'Order not active'},409
            rows = db.execute('SELECT product_id,COUNT(*) n FROM cart_items WHERE cart_id=? GROUP BY product_id',(order['cart_id'],)).fetchall()
            for row in rows:
                db.execute('UPDATE products SET on_hand=on_hand-?, reserved=reserved-? WHERE id=?',
                           (row['n'],row['n'],row['product_id']))
            db.execute('''UPDATE tags SET status='sold' WHERE uid IN
                (SELECT tag_uid FROM cart_items WHERE cart_id=?)''',(order['cart_id'],))
            db.execute('DELETE FROM cart_items WHERE cart_id=?',(order['cart_id'],))
            db.execute('UPDATE carts SET checkout_order=NULL,updated_at=? WHERE id=?',(now(),order['cart_id']))
            db.execute("UPDATE orders SET status='paid',completed_at=? WHERE id=?",(now(),order_id))
            emit(db,'paid',f'Payment manually confirmed: {order_id} • ₹{order["amount"]:,.2f}',cart=order['cart_id'])
            return {'ok':True,'message':'Payment marked as verified by manager'},200


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args,**kwargs):
        if session.get('admin'): return fn(*args,**kwargs)
        if request.path.startswith('/api/'): return jsonify({'error':'Admin login required'}),401
        return redirect(url_for('login',next=request.path))
    return wrapper


def result(payload,code): return jsonify(payload),code

@app.get('/')
def index(): return redirect('/customer?cart=CART-01')

@app.get('/customer')
def customer():
    cart_id = request.args.get('cart','CART-01')
    with connect() as db:
        if not db.execute('SELECT 1 FROM carts WHERE id=?',(cart_id,)).fetchone():
            return redirect('/customer?cart=CART-01')
    return render_template('customer.html',cart_id=cart_id,zones=ZONES)

@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        pwd = request.form.get('password','')
        if ADMIN_PASSWORD and hmac.compare_digest(pwd,ADMIN_PASSWORD):
            session['admin']=True
            next_url = request.args.get('next','/admin')
            return redirect(next_url if next_url.startswith('/') and not next_url.startswith('//') else '/admin')
        return render_template('login.html',error='Incorrect admin password'),403
    return render_template('login.html',error=None)

@app.post('/logout')
def logout(): session.clear(); return redirect('/login')

@app.get('/admin')
@require_admin
def admin(): return render_template('admin.html',zones=ZONES)

@app.get('/inventory')
@require_admin
def inventory(): return render_template('inventory.html',zones=ZONES)

@app.get('/api/customer/<cart_id>')
def customer_state(cart_id):
    with connect() as db:
        cart = cart_detail(db,cart_id)
        if not cart:return jsonify({'error':'Cart not found'}),404
        return jsonify({'cart':cart,'zones':ZONES,
                        'products':[{'name':p['name'],'zone':p['zone'],'emoji':p['emoji']} for p in products_list(db)],
                        'scanner_ready':bool(scanner_status['connected'])})

@app.post('/api/customer/<cart_id>/clear')
def clear_route(cart_id): return result(*clear_cart(cart_id))

@app.post('/api/customer/<cart_id>/checkout')
def checkout_route(cart_id): return result(*make_checkout(cart_id))

@app.get('/api/customer/<cart_id>/payment')
def payment_route(cart_id):
    with connect() as db:
        cart=cart_detail(db,cart_id)
        if not cart:return jsonify({'error':'Cart not found'}),404
        if not cart['order']:return jsonify({'error':'No pending order'}),404
        return jsonify(payment_payload(cart['order']))

@app.get('/api/admin/state')
@require_admin
def admin_state():
    with connect() as db:return jsonify(state(db))

@app.get('/api/inventory/state')
@require_admin
def inventory_state():
    with connect() as db:return jsonify(state(db))

@app.post('/api/admin/reader-cart')
@require_admin
def reader_cart_route():
    cart_id=request.get_json(silent=True) or {}
    cart_id=cart_id.get('cart_id','')
    with write_lock:
        with connect() as db:
            if not db.execute('SELECT 1 FROM carts WHERE id=?',(cart_id,)).fetchone():
                return jsonify({'error':'Cart not found'}),404
            db.execute("UPDATE settings SET value=? WHERE key='reader_cart'",(cart_id,))
            emit(db,'reader',f'RC522 reader assigned to {cart_id}',cart=cart_id)
    return jsonify({'ok':True,'reader_cart':cart_id})

@app.post('/api/admin/cart-location')
@require_admin
def location_route():
    data=request.get_json(silent=True) or {}
    cart_id,zone=data.get('cart_id'),data.get('zone')
    if zone not in ZONE_LOOKUP:return jsonify({'error':'Invalid shop zone'}),400
    with write_lock:
        with connect() as db:
            cur=db.execute('UPDATE carts SET zone=?,updated_at=? WHERE id=?',(zone,now(),cart_id))
            if not cur.rowcount:return jsonify({'error':'Cart not found'}),404
            emit(db,'location',f'{cart_id} moved to {ZONE_LOOKUP[zone]} (manager-reported)',cart=cart_id)
    return jsonify({'ok':True})

@app.post('/api/admin/register')
@require_admin
def register_route():
    data=request.get_json(silent=True) or {}
    uid=str(data.get('uid','')).strip()
    try: product_id=int(data.get('product_id'))
    except (TypeError,ValueError):return jsonify({'error':'Select a product'}),400
    if not uid or len(uid)>80:return jsonify({'error':'Enter a valid RFID UID'}),400
    with write_lock:
        with connect() as db:
            product=db.execute('SELECT * FROM products WHERE id=?',(product_id,)).fetchone()
            if not product:return jsonify({'error':'Product not found'}),404
            if db.execute('SELECT 1 FROM tags WHERE uid=?',(uid,)).fetchone():
                return jsonify({'error':'This RFID UID is already registered. Unregister it first.'}),409
            db.execute('INSERT INTO tags(uid,product_id,registered_at) VALUES(?,?,?)',(uid,product_id,now()))
            emit(db,'register',f'RFID {uid} registered to {product["name"]}',uid)
    return jsonify({'ok':True,'message':'RFID tag registered'})

@app.post('/api/admin/unregister')
@require_admin
def unregister_route():
    data=request.get_json(silent=True) or {}
    uid=str(data.get('uid','')).strip()
    with write_lock:
        with connect() as db:
            if db.execute('SELECT 1 FROM cart_items WHERE tag_uid=?',(uid,)).fetchone():
                return jsonify({'error':'Tag is in a cart. Clear the cart before unregistering.'}),409
            cur=db.execute('DELETE FROM tags WHERE uid=?',(uid,))
            if not cur.rowcount:return jsonify({'error':'Unknown UID'}),404
            emit(db,'unregister',f'RFID {uid} unregistered',uid)
    return jsonify({'ok':True,'message':'Tag unregistered'})

@app.post('/api/admin/confirm-payment')
@require_admin
def confirm_payment_route():
    data=request.get_json(silent=True) or {}
    order_id=str(data.get('order_id','')).strip()
    confirmed=bool(data.get('verified'))
    if not confirmed:return jsonify({'error':'Verify the payment independently first'}),400
    return result(*mark_paid(order_id))

@app.post('/api/inventory/product')
@require_admin
def create_product_route():
    data=request.get_json(silent=True) or {}
    try:
        sku=str(data['sku']).strip().upper(); name=str(data['name']).strip()
        category=str(data.get('category','Grocery')).strip()
        price=round(float(data['price']),2)
        stock=int(data['stock']); zone=str(data['zone'])
        emoji=str(data.get('emoji','📦')).strip()[:8] or '📦'
    except (KeyError,ValueError,TypeError):return jsonify({'error':'Check product details'}),400
    if not sku or not name or price<0 or stock<0 or zone not in ZONE_LOOKUP:
        return jsonify({'error':'Invalid product data'}),400
    with write_lock:
        with connect() as db:
            try:
                db.execute('''INSERT INTO products(sku,name,category,price,on_hand,zone,emoji)
                    VALUES(?,?,?,?,?,?,?)''',(sku,name,category or 'General',price,stock,zone,emoji))
            except sqlite3.IntegrityError:return jsonify({'error':'SKU already exists'}),409
            emit(db,'product',f'New inventory product: {name} ({sku})')
    return jsonify({'ok':True,'message':'Product created'})

@app.post('/api/inventory/restock')
@require_admin
def restock_route():
    data=request.get_json(silent=True) or {}
    try: product_id=int(data['product_id']); amount=int(data['amount'])
    except (KeyError,TypeError,ValueError):return jsonify({'error':'Invalid product or quantity'}),400
    if amount<=0:return jsonify({'error':'Enter a positive restock quantity'}),400
    with write_lock:
        with connect() as db:
            product=db.execute('SELECT name FROM products WHERE id=?',(product_id,)).fetchone()
            if not product:return jsonify({'error':'Product not found'}),404
            db.execute('UPDATE products SET on_hand=on_hand+? WHERE id=?',(amount,product_id))
            emit(db,'restock',f'{product["name"]} restocked +{amount}')
    return jsonify({'ok':True,'message':'Stock added'})

@app.post('/api/admin/demo-scan')
@require_admin
def demo_scan_route():
    if not DEMO:return jsonify({'error':'Demo scanning is disabled in hardware mode'}),403
    data=request.get_json(silent=True) or {}
    return result(*scan_tag(data.get('uid',''),'demo'))


def reader_loop():
    # Reader owns SPI on this process. Never run rfid_test.py at the same time.
    try:
        from mfrc522 import SimpleMFRC522
        import RPi.GPIO as GPIO
        reader=SimpleMFRC522()
    except Exception as exc:
        scanner_status.update(connected=False,error=f'RFID initialization failed: {exc}')
        print(scanner_status['error'])
        return
    scanner_status.update(connected=True,error='')
    previous_uid=None
    last_seen=0
    print('RFID reader connected. Scan one tag at a time.')
    try:
        while True:
            try:
                uid=reader.read_id_no_block()
                moment=time.monotonic()
                if uid is not None:
                    uid=str(uid)
                    if uid!=previous_uid:
                        payload,code=scan_tag(uid)
                        print('RFID',uid,code,payload.get('message'))
                        scanner_status['last_seen']=now()
                    previous_uid=uid
                    last_seen=moment
                elif moment-last_seen>0.8:
                    previous_uid=None
                scanner_status['connected'] = True
                scanner_status['error'] = ''
                time.sleep(.12)
            except Exception as exc:
                scanner_status['error']=str(exc)
                scanner_status['connected']=False
                print('RFID read error:',exc)
                time.sleep(1)
    finally:
        GPIO.cleanup()

init_db()

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--demo',action='store_true',help='No GPIO required; enable admin demo scan')
    parser.add_argument('--host',default='0.0.0.0')
    parser.add_argument('--port',type=int,default=5000)
    args=parser.parse_args()
    if not ADMIN_PASSWORD:
        raise SystemExit('Please set ADMIN_PASSWORD first; example: export ADMIN_PASSWORD="choose-a-strong-password"')
    if args.demo or DEMO:
        scanner_status.update(connected=False,error='Demo mode — use admin test scans')
    else:
        threading.Thread(target=reader_loop,name='rc522-reader',daemon=True).start()
    print(f'SmartMart: http://127.0.0.1:{args.port}/customer?cart=CART-01')
    print(f'Admin: http://127.0.0.1:{args.port}/admin')
    print(f'Inventory: http://127.0.0.1:{args.port}/inventory')
    if args.demo: print('Demo admin password: '+ADMIN_PASSWORD)
    if not MERCHANT_UPI_ID: print('Payment QR in DEMO ONLY mode (merchant UPI ID not configured).')
    app.run(host=args.host,port=args.port,debug=False,use_reloader=False,threaded=True)
