'use strict';
const $ = (s) => document.querySelector(s);
const POLL_MS = 2000;
const money = (n) => '₹' + Number(n || 0).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});
const grams = (g) => g === null || g === undefined ? '-' : (Math.abs(g) >= 1000 ? (g/1000).toFixed(2) + ' kg' : Math.round(g) + ' g');
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const iconByZone = {entrance:'↳',produce:'✿',dairy:'♧',pantry:'▥',snacks:'◈',beverages:'◖',care:'✦',home:'⌂',checkout:'↗'};
let toastTimer;
function toast(message,isError=false){const el=$('#toast');if(!el)return;el.textContent=message;el.classList.toggle('error',isError);el.classList.add('visible');clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove('visible'),4500);}
async function api(url, options={}){
  const opts={...options,headers:{...(options.headers||{})}};
  if(opts.method==='POST'){opts.headers['X-SmartMart']='1';}
  if(opts.body && typeof opts.body!=='string'){opts.headers['Content-Type']='application/json';opts.body=JSON.stringify(opts.body);}
  const r=await fetch(url,opts);
  const data=await r.json().catch(()=>({error:'Invalid server response'}));
  if(r.status===401){location.href='/login?next='+encodeURIComponent(location.pathname);throw Error('Please sign in again');}
  if(!r.ok)throw Error(data.error || data.message || `Request failed (${r.status})`);
  return data;
}
function mutate(url, body={}){return api(url,{method:'POST',body});}
function setHTML(el,html){if(el && el.dataset.content!==html){el.innerHTML=html;el.dataset.content=html;}}
function live(ok){const d=$('#live-dot'),t=$('#live-text');if(!d)return;d.classList.toggle('offline',!ok);t.textContent=ok?'LIVE · LOCAL NETWORK':'OFFLINE · RETRYING';}
function poll(fn){const run=()=>fn().then(()=>live(true)).catch(e=>{live(false);console.warn(e.message);});run();setInterval(run,POLL_MS);}
function timeText(d){if(!d)return '';const parsed=new Date(d);return isNaN(parsed)?d:parsed.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});}
function dateText(d){if(!d)return '';const parsed=new Date(d);return isNaN(parsed)?d:parsed.toLocaleString([],{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'});}
function drawMap(container,zones,products,carts,selectedCart=null){
  if(!container)return;
  const pByZone={};
  products.forEach(p => { (pByZone[p.zone] ||= []).push(p.name); });
  const html=zones.map(zone=>{
    const here=carts.filter(c=>c.zone===zone.id);
    const active=selectedCart ? here.some(c=>c.id===selectedCart) : here.length>0;
    const labels=(pByZone[zone.id]||[]).slice(0,2).map(esc).join(' · ') || (zone.id==='checkout'?'Pay & exit':zone.id==='entrance'?'Start here':'Browse section');
    const markers=here.filter(c=>!selectedCart || c.id===selectedCart).map(c=>`<span class="map-marker" title="${esc(c.id)}">🛒 ${esc(selectedCart?'YOU':c.id.replace('CART-',''))}</span>`).join('');
    return `<div class="map-zone ${active?'active':''}" data-zone="${esc(zone.id)}">
      <span class="zone-symbol">${iconByZone[zone.id]||'□'}</span>
      <div class="map-markers">${markers}</div>
      <div><div class="zone-title">${esc(zone.name)}</div><div class="zone-products" title="${esc((pByZone[zone.id]||[]).join(', '))}">${labels}</div></div>
    </div>`;
  }).join('');
  setHTML(container,html);
}
function setSelect(id,options,selected){const e=$(id);if(!e)return;const current=selected ?? e.value;const html=options.map(o=>`<option value="${esc(o.value)}">${esc(o.label)}</option>`).join('');if(e.dataset.html!==html){e.innerHTML=html;e.dataset.html=html;}if(options.some(o=>String(o.value)===String(current)))e.value=current;}
function weightText(w){
  if(!w || w.status==='no_scale')return null;
  if(w.status==='settling')return {cls:'settling',text:'⚖ Weighing… keep the cart still'};
  if(w.status==='mismatch')return {cls:'mismatch',text:`⚠ Weight mismatch – please rescan. Measured ${grams(w.measured)}, expected ${grams(w.expected)}.`};
  return {cls:'ok',text:`✓ Weight verified · ${grams(w.measured)} (expected ${grams(w.expected)})`};
}

/* ------------------------------------------------------------ customer */
const cartId=document.body.dataset.cart;
let latestCustomer=null, openOrder=null;
async function refreshCustomer(){
  const data=await api(`/api/customer/${encodeURIComponent(cartId)}`);
  const c=data.cart;latestCustomer=c;
  $('#your-location').textContent=c.zone_name;
  $('#item-count').textContent=`${c.count} item${c.count===1?'':'s'}`;
  $('#cart-total-top').textContent=money(c.total);
  $('#cart-count-bubble').textContent=c.count;
  $('#subtotal').textContent=money(c.total);
  $('#bill-total').textContent=money(c.total);
  let html;
  if(c.count===0){html='<div class="empty-state"><div class="empty-icon">◌</div><strong>Your cart is waiting</strong><p>Scan an RFID tag to get started.</p></div>';}
  else html=c.summary.map(i=>`<div class="cart-item"><span class="product-emoji">${esc(i.emoji)}</span><div><div class="cart-name">${esc(i.name)}</div><div class="cart-sub">${i.qty} × ${money(i.price)}</div></div><div class="cart-amount">${money(i.qty*i.price)}</div></div>`).join('');
  setHTML($('#cart-items'),html);
  const wb=$('#weight-banner'), wt=weightText(c.weight);
  wb.hidden=!wt; if(wt){wb.className='weight-banner '+wt.cls; wb.textContent=wt.text;}
  drawMap($('#customer-map'),data.zones,data.products,[c],c.id);
  $('#checkout-btn').disabled=c.count===0;
  $('#checkout-btn').innerHTML=c.order?'View payment QR <span>↗</span>':'Checkout &amp; generate QR <span>↗</span>';
  $('#cart-status').textContent=c.order?`Order ${c.order.id} awaiting manager payment verification.`:
    (!data.reader_here?'The RFID reader is assigned to another cart right now.':data.demo?'Demo mode: scans are simulated from the Manager portal.':!data.scanner_ready?'Waiting for RFID reader…':'Ready for RFID scanning');
  if(openOrder && !c.order && !$('#checkout-modal').hidden){$('#payment-done').hidden=false;openOrder=null;}
}
function openModal(payment){
  openOrder=payment.order_id;
  $('#payment-qr').src=payment.qr;$('#payment-amount').textContent=money(payment.amount);$('#payment-order').textContent='Order '+payment.order_id;
  $('#payment-message').textContent=payment.message;$('#demo-qr-label').hidden=payment.configured;$('#payment-done').hidden=true;$('#checkout-modal').hidden=false;
}
function initCustomer(){
  $('#checkout-btn').addEventListener('click',async()=>{try{const out=await mutate(`/api/customer/${encodeURIComponent(cartId)}/checkout`);openModal(out.payment);await refreshCustomer();}catch(e){toast(e.message,true)}});
  $('#clear-btn').addEventListener('click',async()=>{if(!latestCustomer||(!latestCustomer.count&&!latestCustomer.order))return toast('Cart is already empty');
    if(!confirm('Clear this cart? Scanned products will return to available stock and any pending checkout will be cancelled.'))return;
    try{await mutate(`/api/customer/${encodeURIComponent(cartId)}/clear`);$('#checkout-modal').hidden=true;await refreshCustomer();toast('Cart cleared; stock restored.');}catch(e){toast(e.message,true)}});
  const close=()=>{$('#checkout-modal').hidden=true;openOrder=null;};
  $('#close-modal').addEventListener('click',close);
  $('#payment-close').addEventListener('click',close);
  $('#checkout-modal').addEventListener('click',(e)=>{if(e.target===$('#checkout-modal'))close();});
  document.addEventListener('keydown',e=>{if(e.key==='Escape')close();});
  poll(refreshCustomer);
}

/* ------------------------------------------------------------ shared event feed */
const EVENT_ICON={scan:'↳',unknown:'?',out_of_stock:'∅',weight_mismatch:'⚖',weight_ok:'⚖',paid:'✓',register:'+',unregister:'−',reactivate:'↻',registration:'◎',blocked:'⊘',checkout:'₹',clear:'⌫',location:'⌖',reader:'◉',restock:'▲',product:'▦',scale:'⚖'};
const ALERT_TYPES=['unknown','out_of_stock','weight_mismatch','blocked'];
const GOOD_TYPES=['paid','register','weight_ok','reactivate'];
function eventHTML(events,max=12){
  return events.slice(0,max).map(ev=>{
    const c=ALERT_TYPES.includes(ev.type)?'alert':GOOD_TYPES.includes(ev.type)?'good':'';
    const afford=ev.type==='unknown'&&ev.tag_uid?` <button class="mini-use-tag" data-uid="${esc(ev.tag_uid)}" title="Prefill registration field">Register ↗</button>`:'';
    return `<div class="event-row ${c}"><div class="event-icon ${c}">${EVENT_ICON[ev.type]||'•'}</div><div><div class="event-title">${esc(ev.message)}${afford}</div><div class="event-time">${esc(timeText(ev.created_at))} · ${esc(ev.type.replace(/_/g,' '))}</div></div></div>`;
  }).join('') || '<p class="small-muted">No activity recorded yet. Try scanning an RFID tag.</p>';
}

/* ------------------------------------------------------------ admin */
let adminState=null, regTimer=null;
const synced={};  // last server value per control, so polling never overrides what the user is choosing
function changed(key,value){if(synced[key]===value)return false;synced[key]=value;return true;}
const BADGE={ok:'tag-good',demo:'tag-info',off:'tag-muted',error:'tag-bad'};
function renderAdmin(data){
  adminState=data;
  $('#admin-carts').textContent=data.carts.length;
  $('#admin-scans').textContent=data.events.filter(e=>e.type==='scan').length;
  $('#admin-pending').textContent=data.carts.filter(c=>c.order).length;
  $('#admin-unknown').textContent=data.events.filter(e=>ALERT_TYPES.includes(e.type)).length;
  drawMap($('#admin-map'),data.zones,data.products,data.carts);
  setHTML($('#hw-grid'),Object.values(data.hardware).map(h=>`<div class="hw-item"><span class="tag ${BADGE[h.state]||'tag-warning'}">${esc(h.state.toUpperCase())}</span><div><strong>${esc(h.name)}</strong><small>${esc(h.detail)}</small></div></div>`).join(''));
  if(changed('muted',data.muted))$('#mute-toggle').checked=data.muted;
  const rs=data.hardware.rfid;
  $('#reader-status').textContent=data.demo?'Demo mode':rs.state==='ok'?'RFID online':'RFID offline';
  $('#reader-status').className=`tag ${data.demo?'tag-info':rs.state==='ok'?'tag-good':'tag-bad'}`;
  const cartOptions=data.carts.map(c=>({value:c.id,label:c.id}));
  setSelect('#reader-cart',cartOptions,changed('reader',data.reader_cart)?data.reader_cart:null);
  setSelect('#location-cart',cartOptions,null);
  setSelect('#location-zone',data.zones.map(z=>({value:z.id,label:z.name})),null);
  setSelect('#register-product',data.products.map(p=>({value:p.id,label:`${p.emoji} ${p.name} — ${p.sku}`})),null);
  setHTML($('#cart-rows'),data.carts.map(c=>{
    const st=c.order?`<span class="stock-label low">Awaiting payment</span>`:c.weight_flag?`<span class="stock-label out">Weight mismatch</span>`:c.count?`<span class="stock-label ok">Shopping</span>`:'<span class="small-muted">Empty</span>';
    return `<tr><td><a class="link" href="/customer?cart=${encodeURIComponent(c.id)}" target="_blank" rel="noopener">${esc(c.id)}</a>${c.id===data.reader_cart?' <span class="count-chip">reader</span>':''}</td><td>${esc(c.zone_name)}</td><td class="numeric">${c.count}</td><td class="numeric">${money(c.total)}</td><td>${st}</td></tr>`;}).join(''));
  // demo reader
  $('#demo-scanner').hidden=!data.demo;
  if(data.demo){
    const free=data.tags.filter(t=>t.status==='active'&&!t.in_cart).slice(0,14);
    setHTML($('#demo-chips'),free.map(t=>`<button type="button" class="chip" data-uid="${esc(t.uid)}" title="${esc(t.uid)}">${esc(t.emoji)} ${esc(t.product_name)}</button>`).join('')+'<button type="button" class="chip chip-warn" data-uid="UNKNOWN-'+Math.floor(Date.now()/60000)%1000+'">? Unknown tag</button>');
  }
  // scale
  const w=data.weight, wt=$('#weight-tag');
  const wl={ok:['Weight OK','tag-good'],mismatch:['MISMATCH','tag-bad'],settling:['Settling…','tag-warning'],no_scale:['No scale','tag-muted']}[w.status]||[w.status,'tag-warning'];
  wt.textContent=wl[0];wt.className='tag '+wl[1];
  $('#w-measured').textContent=grams(w.measured);$('#w-expected').textContent=grams(w.expected);$('#w-allowed').textContent=grams(w.allowed);
  $('#demo-scale').hidden=!data.demo;
  if(data.demo && changed('follow',data.scale.demo_follow))$('#demo-follow').checked=data.scale.demo_follow;
  renderTags();
  setHTML($('#admin-events'),eventHTML(data.events,14));
  setHTML($('#pending-orders'),data.carts.filter(c=>c.order).map(c=>`<div class="pending-row ${c.order.weight_flag?'flagged':''}"><div><strong>${esc(c.id)} · ${money(c.order.amount)}</strong><small>${esc(c.order.id)} — ${c.count} item(s) · payment not verified</small>${c.order.weight_flag?'<small class="warn-text">⚠ Weight mismatch at checkout – inspect the cart</small>':''}</div><button class="btn btn-outline verify-btn" data-order="${esc(c.order.id)}">Verify payment</button></div>`).join('')||'<p class="small-muted">No payments awaiting verification.</p>');
}
function renderTags(){
  if(!adminState)return;
  const q=$('#tag-search').value.trim().toLowerCase();
  const tags=adminState.tags.filter(t=>!q||[t.uid,t.product_name,t.sku].some(x=>String(x).toLowerCase().includes(q)));
  $('#tag-count').textContent=adminState.tags.length;
  setHTML($('#tag-rows'),tags.map(t=>{
    const status=t.in_cart?`<span class="stock-label low">In ${esc(t.in_cart)}</span>`:t.status==='sold'?'<span class="stock-label out">Sold</span>':'<span class="stock-label ok">Active</span>';
    const re=t.status==='sold'?`<button class="btn btn-outline btn-sm" data-act="reactivate" data-uid="${esc(t.uid)}">Re-activate</button>`:'';
    return `<tr><td class="mono">${esc(t.uid)}</td><td>${esc(t.emoji)} ${esc(t.product_name)}</td><td>${esc(t.sku)}</td><td>${status}</td><td>${esc(dateText(t.registered_at))}</td><td class="actions">${re}<button class="btn btn-ghost btn-sm danger" data-act="remove" data-uid="${esc(t.uid)}">Remove</button></td></tr>`;
  }).join('')||`<tr><td colspan="6" class="small-muted">${q?'No tags match your search.':'No RFID tags registered yet.'}</td></tr>`);
}
async function refreshAdmin(){renderAdmin(await api('/api/admin/state'));}
function regStatus(text,cls=''){const el=$('#scan-register-status');el.hidden=!text;el.className='scan-status '+cls;el.textContent=text||'';}
function stopScanRegister(msg,cls){clearInterval(regTimer);regTimer=null;$('#scan-register-btn').textContent='Scan to register';regStatus(msg,cls);}
async function startScanRegister(){
  if(regTimer){await mutate('/api/admin/scan-register/cancel').catch(()=>{});return stopScanRegister('Scan cancelled.');}
  try{await mutate('/api/admin/scan-register/start');}catch(e){return toast(e.message,true);}
  $('#scan-register-btn').textContent='Cancel scan';
  const hint=adminState&&adminState.demo?' (demo: use “Test scan” above to simulate the reader)':'';
  regStatus('Waiting for a tag on the reader… 30 s'+hint,'waiting');
  regTimer=setInterval(async()=>{
    try{const s=await api('/api/admin/scan-register');
      if(s.uid){$('#register-uid').value=s.uid;stopScanRegister(s.registered_to?`Captured ${s.uid} – already registered to ${s.registered_to}. Remove it first to re-assign.`:`Captured ${s.uid}. Choose the product and press Save tag.`,s.registered_to?'warn':'good');$('#register-product').focus();}
      else if(!s.active)stopScanRegister('No tag scanned within 30 s. Try again.','warn');
      else regStatus(`Waiting for a tag on the reader… ${s.remaining} s${hint}`,'waiting');
    }catch(e){stopScanRegister(e.message,'warn');}
  },700);
}
async function demoScan(uid){
  try{const r=await mutate('/api/admin/demo-scan',{uid});toast(r.message,!r.ok||!!r.duplicate);}catch(err){toast(err.message,true)}
  await refreshAdmin().catch(()=>{});
}
function initAdmin(){
  $('#reader-form').addEventListener('submit',async e=>{e.preventDefault();try{await mutate('/api/admin/reader-cart',{cart_id:$('#reader-cart').value});toast('Reader assigned to '+$('#reader-cart').value);await refreshAdmin();}catch(err){toast(err.message,true)}});
  $('#location-form').addEventListener('submit',async e=>{e.preventDefault();try{await mutate('/api/admin/cart-location',{cart_id:$('#location-cart').value,zone:$('#location-zone').value});toast('Reported cart location updated');await refreshAdmin();}catch(err){toast(err.message,true)}});
  $('#register-form').addEventListener('submit',async e=>{e.preventDefault();try{const r=await mutate('/api/admin/register',{uid:$('#register-uid').value.trim(),product_id:Number($('#register-product').value)});$('#register-uid').value='';regStatus('');toast(r.message);await refreshAdmin();}catch(err){toast(err.message,true)}});
  $('#scan-register-btn').addEventListener('click',startScanRegister);
  $('#tag-search').addEventListener('input',renderTags);
  $('#tag-rows').addEventListener('click',async e=>{const b=e.target.closest('button[data-act]');if(!b)return;const uid=b.dataset.uid;
    if(b.dataset.act==='remove'){if(!confirm(`Remove RFID tag ${uid}? It will no longer bill its product.`))return;
      try{const r=await mutate('/api/admin/unregister',{uid});toast(r.message);}catch(err){toast(err.message,true)}}
    else{if(!confirm(`Re-activate tag ${uid}? Only do this after the physical item is back on the shelf (restock it in Inventory too).`))return;
      try{const r=await mutate('/api/admin/reactivate',{uid});toast(r.message);}catch(err){toast(err.message,true)}}
    await refreshAdmin().catch(()=>{});});
  $('#demo-form').addEventListener('submit',e=>{e.preventDefault();demoScan($('#demo-uid').value.trim());});
  $('#demo-chips').addEventListener('click',e=>{const b=e.target.closest('.chip');if(b){$('#demo-uid').value=b.dataset.uid;demoScan(b.dataset.uid);}});
  $('#admin-events').addEventListener('click',e=>{const b=e.target.closest('.mini-use-tag');if(!b)return;$('#register-uid').value=b.dataset.uid;$('#tags').scrollIntoView({behavior:'smooth'});$('#register-product').focus();toast('Unknown UID copied into the registration form');});
  $('#pending-orders').addEventListener('click',async e=>{const b=e.target.closest('.verify-btn');if(!b)return;const id=b.dataset.order;
    if(!confirm(`ONLY mark ${id} paid if you independently confirmed the payment in your actual merchant account or cash receipt. Has payment been verified?`))return;
    try{await mutate('/api/admin/confirm-payment',{order_id:id,verified:true});toast('Payment confirmed; sale completed and stock finalized');await refreshAdmin();}catch(err){toast(err.message,true)}});
  $('#mute-toggle').addEventListener('change',async e=>{try{const r=await mutate('/api/admin/mute',{muted:e.target.checked});toast(r.muted?'Speaker muted':'Speaker unmuted');}catch(err){toast(err.message,true)}});
  $('#tare-btn').addEventListener('click',async()=>{if(!confirm('Tare now? The cart/basket must be EMPTY.'))return;try{const r=await mutate('/api/admin/scale/tare');toast(r.message);}catch(err){toast(err.message,true)}});
  $('#calibrate-form').addEventListener('submit',async e=>{e.preventDefault();try{const r=await mutate('/api/admin/scale/calibrate',{known_g:Number($('#cal-grams').value)});toast(r.message);}catch(err){toast(err.message,true)}});
  $('#demo-follow').addEventListener('change',async e=>{try{await mutate('/api/admin/scale/demo',{follow:e.target.checked});toast(e.target.checked?'Fake scale follows expected weight':'Fake scale holds its weight');await refreshAdmin();}catch(err){toast(err.message,true)}});
  $('#demo-weight-form').addEventListener('submit',async e=>{e.preventDefault();try{await mutate('/api/admin/scale/demo',{grams:Number($('#demo-grams').value)});toast('Fake weight set');await refreshAdmin();}catch(err){toast(err.message,true)}});
  $('#demo-mismatch').addEventListener('click',async()=>{const exp=(adminState&&adminState.weight.expected)||0;try{await mutate('/api/admin/scale/demo',{grams:exp+500});toast(`Fake weight set to ${grams(exp+500)} – mismatch in ~2 s`);await refreshAdmin();}catch(err){toast(err.message,true)}});
  poll(refreshAdmin);
}

/* ------------------------------------------------------------ inventory */
let inventoryState=null;
function renderInventory(data){
  inventoryState=data;
  const ps=data.products;
  $('#inventory-skus').textContent=ps.length;
  $('#inventory-available').textContent=ps.reduce((s,p)=>s+p.available,0);
  $('#inventory-reserved').textContent=ps.reduce((s,p)=>s+p.reserved,0);
  $('#inventory-low').textContent=ps.filter(p=>p.available<=5).length;
  const filter=$('#inventory-search').value.trim().toLowerCase();
  const showing=ps.filter(p=>[p.name,p.sku,p.category,p.zone].some(x=>String(x).toLowerCase().includes(filter)));
  setHTML($('#inventory-table-body'),showing.map(p=>{
    const s=p.available<=0?'out':p.available<=5?'low':'ok';const label=s==='out'?'Out of stock':s==='low'?'Low stock':'In stock';
    return `<tr class="row-${s}"><td><div class="table-product"><span class="product-emoji">${esc(p.emoji)}</span><div>${esc(p.name)}<small>${esc(p.category)}</small></div></div></td><td>${esc(p.sku)}</td><td>${esc(data.zones.find(z=>z.id===p.zone)?.name||p.zone)}</td><td class="numeric">${money(p.price)}</td><td class="numeric">${p.on_hand}</td><td class="numeric reserved">${p.reserved}</td><td class="numeric positive">${p.available}</td><td class="numeric">${p.active_tags}</td><td class="numeric">${p.weight_g?grams(p.weight_g)+' <small>±'+p.tolerance_pct+'%</small>':'<small>—</small>'}</td><td><span class="stock-label ${s}">${label}</span></td><td><button class="btn btn-ghost btn-sm edit-btn" data-id="${p.id}">Edit</button></td></tr>`;
  }).join('') || '<tr><td colspan="11">No matching products</td></tr>');
  $('#inventory-rowcount').textContent=`Showing ${showing.length} of ${ps.length} products`;
  setSelect('#product-zone',data.zones.map(z=>({value:z.id,label:z.name})),null);
  setSelect('#restock-product',ps.map(p=>({value:p.id,label:`${p.name} (available: ${p.available})`})),null);
  const before=$('#edit-product').value;
  setSelect('#edit-product',ps.map(p=>({value:p.id,label:`${p.emoji} ${p.name} — ${p.sku}`})),null);
  if(!before)fillEdit();
  setHTML($('#inventory-events'),eventHTML(data.events.filter(e=>['scan','clear','paid','restock','out_of_stock','unknown','product','checkout'].includes(e.type)),8));
}
function fillEdit(){
  if(!inventoryState)return;const p=inventoryState.products.find(x=>String(x.id)===$('#edit-product').value);if(!p)return;
  $('#edit-price').value=p.price;$('#edit-weight').value=p.weight_g;$('#edit-tol').value=p.tolerance_pct;
}
async function refreshInventory(){renderInventory(await api('/api/inventory/state'));}
function initInventory(){
  $('#inventory-search').addEventListener('input',()=>{if(inventoryState)renderInventory(inventoryState)});
  $('#product-form').addEventListener('submit',async e=>{e.preventDefault();const data=Object.fromEntries(new FormData(e.target).entries());try{await mutate('/api/inventory/product',data);e.target.reset();toast('Product added to inventory');await refreshInventory();}catch(err){toast(err.message,true)}});
  $('#restock-form').addEventListener('submit',async e=>{e.preventDefault();const f=new FormData(e.target);try{await mutate('/api/inventory/restock',{product_id:Number(f.get('product_id')),amount:Number(f.get('amount'))});toast('New stock received');await refreshInventory();}catch(err){toast(err.message,true)}});
  $('#edit-product').addEventListener('change',fillEdit);
  $('#edit-form').addEventListener('submit',async e=>{e.preventDefault();try{await mutate(`/api/inventory/product/${$('#edit-product').value}`,{price:$('#edit-price').value,weight_g:$('#edit-weight').value,tolerance_pct:$('#edit-tol').value});toast('Product updated');await refreshInventory();}catch(err){toast(err.message,true)}});
  $('#inventory-table-body').addEventListener('click',e=>{const b=e.target.closest('.edit-btn');if(!b)return;$('#edit-product').value=b.dataset.id;fillEdit();$('#edit-panel').scrollIntoView({behavior:'smooth',block:'center'});$('#edit-price').focus({preventScroll:true});});
  poll(refreshInventory);
}

const page=document.body.dataset.page;
if(page==='customer')initCustomer();
if(page==='admin')initAdmin();
if(page==='inventory')initInventory();
