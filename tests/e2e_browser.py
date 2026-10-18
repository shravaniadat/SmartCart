"""End-to-end browser check of all three portals (demo mode).

    pip install playwright
    python app.py --demo --port 5055          # fresh DB recommended
    python tests/e2e_browser.py http://127.0.0.1:5055

Uses the installed Google Chrome (channel='chrome'); falls back to Playwright's
Chromium. Fails on any console error, page error, failed request or any
request to the internet (the pages must work offline).
"""
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:5055'
SHOTS = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parent / 'screenshots'
SHOTS.mkdir(exist_ok=True)
problems, steps, rejections = [], [], []


def step(name):
    steps.append(name)
    print('  ✓', name, flush=True)


def watch(page, label):
    def on_console(msg):
        if msg.type != 'error':
            return
        if re.search(r'status of 4\d\d', msg.text):  # intentional negative tests (wrong password, duplicates...)
            rejections.append(f'[{label}] {msg.text}')
        else:
            problems.append(f'[{label}] console: {msg.text}')
    page.on('console', on_console)
    page.on('pageerror', lambda e: problems.append(f'[{label}] pageerror: {e}'))
    page.on('requestfailed', lambda r: problems.append(f'[{label}] failed: {r.url} {r.failure}'))
    page.on('dialog', lambda d: d.accept())
    host = urlparse(BASE).netloc

    def guard(route):
        if urlparse(route.request.url).netloc not in (host, ''):
            problems.append(f'[{label}] external request: {route.request.url}')
            return route.abort()
        return route.continue_()
    page.route('**/*', guard)
    return page


def pick(page, select, text):
    """Select the first <option> whose label contains text."""
    sel = page.locator(select)
    expect(sel.locator('option', has_text=text).first).to_be_attached(timeout=5000)
    value = sel.locator('option', has_text=text).first.get_attribute('value')
    sel.select_option(value)


def toast(page, text):
    expect(page.locator('#toast')).to_contain_text(text, timeout=6000)


def main():
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel='chrome', headless=True)
        except Exception:
            browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={'width': 1366, 'height': 768})
        admin = watch(ctx.new_page(), 'admin')

        # --- login
        admin.goto(f'{BASE}/admin')
        expect(admin).to_have_url(re.compile(r'/login'))
        admin.fill('input[name=password]', 'wrong-password')
        admin.click('button[type=submit]')
        expect(admin.locator('#login-error')).to_contain_text('Incorrect admin password')
        step('wrong password rejected')
        admin.fill('input[name=password]', 'demo1234')
        admin.click('button[type=submit]')
        expect(admin).to_have_url(re.compile(r'/admin$'))
        expect(admin.locator('#reader-status')).to_have_text('Demo mode', timeout=5000)
        expect(admin.locator('#hw-grid .hw-item')).to_have_count(4)
        step('login ok, hardware badges shown')

        customer = watch(ctx.new_page(), 'customer')
        customer.goto(f'{BASE}/customer?cart=CART-01')
        expect(customer.locator('#customer-map .map-zone')).to_have_count(9)
        inventory = watch(ctx.new_page(), 'inventory')
        inventory.goto(f'{BASE}/inventory')
        expect(inventory.locator('#inventory-table-body tr')).to_have_count(11)
        rice_row = inventory.locator('#inventory-table-body tr', has_text='Basmati rice')
        expect(rice_row.locator('td').nth(5)).to_have_text('0')
        step('customer + inventory portals loaded')

        # --- register tag manually + duplicate
        admin.bring_to_front()
        admin.fill('#register-uid', 'E2E-MANUAL-1')
        pick(admin, '#register-product', 'Shampoo')
        admin.click('#register-form button[type=submit]')
        toast(admin, 'registered to Shampoo')
        expect(admin.locator('#tag-rows tr', has_text='E2E-MANUAL-1')).to_have_count(1)
        admin.fill('#register-uid', 'E2E-MANUAL-1')
        admin.click('#register-form button[type=submit]')
        toast(admin, 'already registered')
        step('manual tag register + duplicate blocked')

        # --- scan to register (reader simulated with Test scan)
        admin.fill('#register-uid', '')
        admin.click('#scan-register-btn')
        expect(admin.locator('#scan-register-status')).to_contain_text('Waiting for a tag')
        admin.fill('#demo-uid', 'E2E-SCANNED-7')
        admin.click('#demo-form button[type=submit]')
        expect(admin.locator('#register-uid')).to_have_value('E2E-SCANNED-7', timeout=5000)
        expect(admin.locator('#scan-register-status')).to_contain_text('Captured E2E-SCANNED-7')
        pick(admin, '#register-product', 'Tea powder')
        admin.click('#register-form button[type=submit]')
        toast(admin, 'registered to Tea powder')
        step('scan-to-register captured UID and saved')

        # --- scans: known, unknown
        admin.fill('#demo-uid', '799992525454')
        admin.click('#demo-form button[type=submit]')
        toast(admin, 'Basmati rice added')
        admin.fill('#demo-uid', '619249934221')
        admin.click('#demo-form button[type=submit]')
        toast(admin, 'Fresh milk added')
        customer.bring_to_front()
        expect(customer.locator('#bill-total')).to_have_text('₹115.00', timeout=5000)
        expect(customer.locator('#cart-items .cart-item')).to_have_count(2)
        expect(rice_row.locator('td').nth(5)).to_have_text('1', timeout=5000)  # inventory polled
        step('demo scans billed; customer + inventory updated by polling')
        admin.bring_to_front()
        admin.fill('#demo-uid', 'NEVER-SEEN-99')
        admin.click('#demo-form button[type=submit]')
        toast(admin, 'Unregistered RFID tag')
        expect(admin.locator('#admin-events')).to_contain_text('Unknown tag NEVER-SEEN-99', timeout=5000)
        step('unknown tag alert')

        # --- out of stock: product with 0 stock
        inventory.bring_to_front()
        f = inventory.locator('#product-form')
        f.locator('[name=name]').fill('E2E Ghee')
        f.locator('[name=sku]').fill('GHEE-E2E')
        f.locator('[name=category]').fill('Dairy')
        f.locator('[name=price]').fill('550')
        f.locator('[name=stock]').fill('0')
        f.locator('[name=weight_g]').fill('500')
        f.locator('[name=zone]').select_option('dairy')
        f.locator('button[type=submit]').click()
        toast(inventory, 'Product added')
        expect(inventory.locator('#inventory-table-body tr.row-out', has_text='E2E Ghee')).to_have_count(1, timeout=5000)
        step('add product (0 stock highlighted out)')
        admin.bring_to_front()
        expect(admin.locator('#register-product option', has_text='E2E Ghee')).to_have_count(1, timeout=5000)
        admin.fill('#register-uid', 'GHEE-TAG-1')
        pick(admin, '#register-product', 'E2E Ghee')
        admin.click('#register-form button[type=submit]')
        toast(admin, 'registered')
        admin.fill('#demo-uid', 'GHEE-TAG-1')
        admin.click('#demo-form button[type=submit]')
        toast(admin, 'out of stock')
        expect(admin.locator('#admin-events')).to_contain_text('No available stock for E2E Ghee', timeout=5000)
        step('out-of-stock scan refused + alert')

        # --- weight mismatch via fake scale
        expect(admin.locator('#weight-tag')).to_have_text('Weight OK', timeout=6000)
        admin.click('#demo-mismatch')
        expect(admin.locator('#weight-tag')).to_have_text('MISMATCH', timeout=8000)
        expect(admin.locator('#admin-events')).to_contain_text('weight mismatch', timeout=5000)
        customer.bring_to_front()
        expect(customer.locator('#weight-banner')).to_contain_text('Weight mismatch', timeout=5000)
        customer.screenshot(path=str(SHOTS / 'customer-mismatch-1366.png'), full_page=True)
        step('weight mismatch shown on customer + admin alert')

        # --- unregister while in cart is blocked
        admin.bring_to_front()
        admin.fill('#tag-search', '799992525454')
        expect(admin.locator('#tag-rows tr')).to_have_count(1)
        admin.locator('#tag-rows tr', has_text='799992525454').locator('button[data-act=remove]').click()
        toast(admin, 'is in CART-01 right now')
        step('unregister blocked while tag is in a cart')

        # --- checkout with mismatch flag, QR, verify payment
        customer.bring_to_front()
        customer.click('#checkout-btn')
        expect(customer.locator('#checkout-modal')).to_be_visible()
        expect(customer.locator('#payment-amount')).to_have_text('₹115.00')
        expect(customer.locator('#demo-qr-label')).to_be_visible()
        customer.wait_for_function("document.querySelector('#payment-qr').naturalWidth > 100")
        customer.screenshot(path=str(SHOTS / 'customer-qr-1366.png'))
        step('checkout QR rendered (demo-only label)')
        admin.bring_to_front()
        pending = admin.locator('#pending-orders .pending-row')
        expect(pending).to_have_count(1, timeout=5000)
        expect(pending).to_contain_text('Weight mismatch at checkout')
        admin.screenshot(path=str(SHOTS / 'admin-1366.png'), full_page=True)
        pending.locator('.verify-btn').click()
        toast(admin, 'Payment confirmed')
        customer.bring_to_front()
        expect(customer.locator('#payment-done')).to_be_visible(timeout=5000)
        customer.click('#payment-close')
        expect(customer.locator('#bill-total')).to_have_text('₹0.00')
        step('manager verified payment; customer saw confirmation')

        # --- sold tag: re-activate
        admin.bring_to_front()
        row = admin.locator('#tag-rows tr', has_text='799992525454')
        expect(row).to_contain_text('Sold', timeout=5000)
        row.locator('button[data-act=reactivate]').click()
        toast(admin, 'active again')
        expect(row).to_contain_text('Active', timeout=5000)
        step('sold tag re-activated')

        # --- restore follow mode, scan, clear cart from customer
        admin.locator('#demo-follow').check()
        admin.fill('#tag-search', '')
        admin.locator('#demo-chips .chip', has_text='Potato chips').click()
        toast(admin, 'Potato chips added')
        customer.bring_to_front()
        expect(customer.locator('#bill-total')).to_have_text('₹20.00', timeout=5000)
        expect(customer.locator('#weight-banner')).to_contain_text('Weight verified', timeout=8000)
        customer.click('#clear-btn')
        toast(customer, 'Cart cleared')
        expect(customer.locator('#bill-total')).to_have_text('₹0.00')
        step('clear cart restored stock')

        # --- unregister (not in cart)
        admin.bring_to_front()
        admin.locator('#tag-rows tr', has_text='E2E-MANUAL-1').locator('button[data-act=remove]').click()
        toast(admin, 'removed')
        expect(admin.locator('#tag-rows tr', has_text='E2E-MANUAL-1')).to_have_count(0, timeout=5000)
        step('tag unregistered')

        # --- reader + location
        admin.select_option('#location-cart', 'CART-02')
        admin.select_option('#location-zone', 'snacks')
        admin.click('#location-form button[type=submit]')
        toast(admin, 'location updated')
        expect(admin.locator('#admin-map .map-zone[data-zone=snacks] .map-marker')).to_have_count(1, timeout=5000)
        admin.select_option('#reader-cart', 'CART-02')
        admin.click('#reader-form button[type=submit]')
        toast(admin, 'Reader assigned to CART-02')
        admin.select_option('#reader-cart', 'CART-01')
        admin.click('#reader-form button[type=submit]')
        toast(admin, 'Reader assigned to CART-01')
        step('cart location + reader assignment')

        # --- mute toggle
        admin.locator('#mute-toggle').check()
        toast(admin, 'Speaker muted')
        admin.locator('#mute-toggle').uncheck()
        toast(admin, 'Speaker unmuted')
        step('mute toggle')

        # --- inventory: search, restock, edit price
        inventory.bring_to_front()
        inventory.fill('#inventory-search', 'rice')
        expect(inventory.locator('#inventory-table-body tr')).to_have_count(1)
        inventory.fill('#inventory-search', 'GHEE')
        expect(inventory.locator('#inventory-table-body tr')).to_have_count(1)
        inventory.fill('#inventory-search', '')
        pick(inventory, '#restock-product', 'E2E Ghee')
        inventory.fill('#restock-form [name=amount]', '3')
        inventory.click('#restock-form button[type=submit]')
        toast(inventory, 'New stock received')
        ghee = inventory.locator('#inventory-table-body tr', has_text='E2E Ghee')
        expect(ghee).to_contain_text('Low stock', timeout=5000)
        ghee.locator('.edit-btn').click()
        inventory.fill('#edit-price', '525')
        inventory.click('#edit-form button[type=submit]')
        toast(inventory, 'Product updated')
        expect(ghee).to_contain_text('₹525.00', timeout=5000)
        inventory.screenshot(path=str(SHOTS / 'inventory-1366.png'), full_page=True)
        step('inventory search, restock, edit price')
        admin.bring_to_front()
        admin.fill('#demo-uid', 'GHEE-TAG-1')
        admin.click('#demo-form button[type=submit]')
        toast(admin, 'E2E Ghee added')
        step('restocked product scannable again')

        # --- responsive: phone + laptop, no horizontal page scroll
        for label, url in (('customer', '/customer?cart=CART-01'), ('admin', '/admin'), ('inventory', '/inventory')):
            for w, h in ((390, 844), (1366, 768)):
                pg = watch(ctx.new_page(), f'{label}@{w}')
                pg.set_viewport_size({'width': w, 'height': h})
                pg.goto(BASE + url)
                pg.wait_for_timeout(1200)
                overflow = pg.evaluate('document.documentElement.scrollWidth - window.innerWidth')
                if overflow > 1:
                    problems.append(f'{label}@{w}px scrolls horizontally by {overflow}px')
                if w == 390:
                    pg.screenshot(path=str(SHOTS / f'{label}-phone.png'), full_page=True)
                pg.close()
        step('responsive layouts at 390px and 1366px')

        # --- logout
        admin.bring_to_front()
        admin.click('.nav-logout')
        expect(admin).to_have_url(re.compile(r'/login'))
        admin.goto(f'{BASE}/inventory')
        expect(admin).to_have_url(re.compile(r'/login'))
        step('logout')
        browser.close()

    print(f'\n{len(steps)} steps passed.')
    if rejections:
        print(f'{len(rejections)} expected HTTP 4xx rejections from negative tests (browser logs these; not JS errors):')
        for r in rejections:
            print('   ', r)
    if problems:
        print('\nPROBLEMS:')
        for p_ in problems:
            print(' -', p_)
        sys.exit(1)
    print('No console errors, page errors, failed or external requests.')


if __name__ == '__main__':
    main()
