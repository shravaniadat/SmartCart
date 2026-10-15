"""Logic tests using a Flask stand-in for offline CI environments without network."""
import os, sys, types, tempfile, importlib.util
from pathlib import Path

if 'flask' not in sys.modules:
    mod = types.ModuleType('flask')
    class FakeFlask:
        def __init__(self,*args,**kwargs):
            self.config = {}
            self.secret_key=None
        def route(self,*args,**kwargs): return lambda f:f
        def get(self,*args,**kwargs): return lambda f:f
        def post(self,*args,**kwargs): return lambda f:f
    mod.Flask=FakeFlask
    mod.jsonify=lambda x:x
    mod.redirect=lambda x:x
    mod.render_template=lambda *a,**k:None
    mod.request=types.SimpleNamespace()
    mod.session={}
    mod.url_for=lambda x,**kwargs:x
    sys.modules['flask']=mod

dirname=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as tmp:
    os.environ['SMARTMART_DB']=str(Path(tmp)/'data.db')
    os.environ['SMARTMART_DEMO']='1'
    os.environ['ADMIN_PASSWORD']='unit-test-password'
    spec=importlib.util.spec_from_file_location('smartmart_app',dirname/'app.py')
    app=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    with app.connect() as db:
        ps=app.products_list(db)
        assert len(ps)==11
        rice=next(p for p in ps if p['sku']=='RICE-01')
        milk=next(p for p in ps if p['sku']=='MILK-01')
        db.execute('INSERT INTO tags(uid,product_id,registered_at) VALUES(?,?,?)',('799992525454',rice['id'],app.now()))
        db.execute('INSERT INTO tags(uid,product_id,registered_at) VALUES(?,?,?)',('619249934221',milk['id'],app.now()))
        original=rice['available']

    err,status=app.scan_tag('unknown')
    assert status==404
    ok,status=app.scan_tag('799992525454')
    assert status==200 and ok['ok']
    again,status=app.scan_tag('799992525454')
    assert status==200 and again['duplicate']
    with app.connect() as db:
        p=next(p for p in app.products_list(db) if p['sku']=='RICE-01')
        assert p['available']==original-1 and p['on_hand']==original and p['reserved']==1
        assert app.cart_detail(db,'CART-01')['count']==1
    cleared,status=app.clear_cart('CART-01')
    assert cleared['ok']
    with app.connect() as db:
        p=next(p for p in app.products_list(db) if p['sku']=='RICE-01')
        assert p['available']==original and p['reserved']==0
    assert app.scan_tag('799992525454')[1]==200
    assert app.scan_tag('619249934221')[1]==200
    checkout,status=app.make_checkout('CART-01')
    assert status==200 and checkout['order']['status']=='pending'
    assert checkout['payment']['qr'].startswith('data:image/png;base64,')
    assert not checkout['payment']['configured']
    order_id=checkout['order']['id']
    assert app.scan_tag('799992525454')[1]==409
    with app.connect() as db:
        assert app.cart_detail(db,'CART-01')['total']==115.0
    paid,status=app.mark_paid(order_id)
    assert status==200 and paid['ok']
    assert app.mark_paid(order_id)[1]==409
    with app.connect() as db:
        r=next(p for p in app.products_list(db) if p['sku']=='RICE-01')
        assert r['on_hand']==original-1 and r['reserved']==0
        assert app.cart_detail(db,'CART-01')['count']==0
        assert db.execute('SELECT status FROM tags WHERE uid=?',('799992525454',)).fetchone()['status']=='sold'
    assert app.scan_tag('799992525454')[1]==409
    print('PASS: RFID scan, duplicate prevention, stock reservation, cart clear, QR, payment, sold tag lock')
