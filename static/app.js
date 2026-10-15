'use strict';
const $ = (s) => document.querySelector(s);
const money = (n) => '₹' + Number(n || 0).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const iconByZone = {entrance:'↳',produce:'✿',dairy:'♧',pantry:'▥',snacks:'◈',beverages:'◖',care:'✦',home:'⌂',checkout:'↗'};
let toastTimer;
function toast(message,isError=false){const el=$('#toast');if(!el)return;el.textContent=message;el.classList.toggle('error',isError);el.classList.add('visible');clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove('visible'),4500);}
async function api(url, options={}){
  const opts={...options};
  if(opts.body && typeof opts.body!=='string'){opts.headers={...(opts.headers||{}),'Content-Type':'application/json'};opts.body=JSON.stringify(opts.body);}
  const r=await fetch(url,opts);
  const data=await r.json().catch(()=>({error:'Invalid server response'}));
  if(!r.ok)throw Error(data.error || data.message || `Request failed (${r.status})`);
  return data;
}
function mutate(url, body={}){return api(url,{method:'POST',body});}
function timeText(d){if(!d)return '';const parsed=new Date(d);return isNaN(parsed)?d:parsed.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});}
function drawMap(container,zones,products,carts,selectedCart=null){
  if(!container)return;
  const pByZone={};
  products.forEach(p => { (pByZone[p.zone] ||= []).push(p.name); });
  const html=zones.map(zone=>{
    const here=carts.filter(c=>c.zone===zone.id);
    const active=selectedCart ? here.some(c=>c.id===selectedCart) : here.length>0;
    const labels=(pByZone[zone.id]||[]).slice(0,2).map(esc).join(' · ') || (zone.id==='checkout'?'Pay & exit':zone.id==='entrance'?'Start here':'Browse section');
    let markers='';
    if(here.length){markers=here.filter(c=>!selectedCart || c.id===selectedCart).map(c=>`<span class="map-marker" title="${esc(c.id)}">🛒 ${esc(selectedCart?'YOU':c.id.replace('CART-',''))}</span>`).join('');}
    return `<div class="map-zone ${active?'active':''}" data-zone="${esc(zone.id)}">
      <span class="zone-symbol">${iconByZone[zone.id]||'□'}</span>
      <div class="map-markers">${markers}</div>
      <div><div class="zone-title">${esc(zone.name)}</div><div class="zone-products" title="${esc((pByZone[zone.id]||[]).join(', '))}">${labels}</div></div>
    </div>`;
  }).join('');
  if(container.dataset.content!==html){container.innerHTML=html;container.dataset.content=html;}
}
function setSelect(id,options,selected){const e=$(id);if(!e)return;const current=selected ?? e.value;let html=options.map(o=>`<option value="${esc(o.value)}">${esc(o.label)}</option>`).join('');if(e.dataset.html!==html){e.innerHTML=html;e.dataset.html=html;}if(options.some(o=>String(o.value)===String(current)))e.value=current;}
const cartId=document.body.dataset.cart;
let latestCustomer=null;
async function refreshCustomer(){
  const data=await api(`/api/customer/${encodeURIComponent(cartId)}`);
  const c=data.cart;latestCustomer=c;
  $('#your-location').textContent=c.zone_name;
  $('#item-count').textContent=`${c.count} item${c.count===1?'':'s'}`;
  $('#cart-total-top').textContent=money(c.total);
  $('#cart-count-bubble').textContent=c.count;
  $('#subtotal').textContent=money(c.total);
  $('#bill-total').textContent=money(c.total);
  const el=$('#cart-items');
  let html;
  if(c.count===0){html='<div class="empty-state"><div class="empty-icon">◌</div><strong>Your cart is waiting</strong><p>Scan an RFID tag to get started.</p></div>';}
  else html=c.summary.map(i=>`<div class="cart-item"><span class="product-emoji">${esc(i.emoji)}</span><div><div class="cart-name">${esc(i.name)}</div><div class="cart-sub">${i.qty} × ${money(i.price)}</div></div><div class="cart-amount">${money(i.qty*i.price)}</div></div>`).join('');
  if(el.dataset.content!==html){el.innerHTML=html;el.dataset.content=html;}
  drawMap($('#customer-map'),data.zones,data.products,[c],c.id);
  $('#checkout-btn').disabled=c.count===0;
  $('#checkout-btn').textContent=c.order?'View payment QR  ↗':'Checkout & generate QR  ↗';
  $('#cart-status').textContent=c.order?`Order ${c.order.id} awaiting payment verification.`:(!data.scanner_ready?'Waiting for RFID reader. Reader controlled from Manager portal.':'Ready for RFID scanning');
}
function openModal(payment){$('#payment-qr').src=payment.qr;$('#payment-amount').textContent=money(payment.amount);$('#payment-order').textContent=payment.order_id;$('#payment-message').textContent=payment.message;$('#checkout-modal').hidden=false;}
function initCustomer(){
  $('#checkout-btn').addEventListener('click',async()=>{try{const out=await mutate(`/api/customer/${encodeURIComponent(cartId)}/checkout`);openModal(out.payment);await refreshCustomer();}catch(e){toast(e.message,true)}});
  $('#clear-btn').addEventListener('click',async()=>{if(!latestCustomer||(!latestCustomer.count&&!latestCustomer.order))return;
    if(!confirm('Clear this cart? Scanned products will return to available stock and any pending checkout will be cancelled.'))return;
    try{await mutate(`/api/customer/${encodeURIComponent(cartId)}/clear`);$('#checkout-modal').hidden=true;await refreshCustomer();toast('Cart cleared; stock restored.');}catch(e){toast(e.message,true)}});
  $('#close-modal').addEventListener('click',()=>$('#checkout-modal').hidden=true);
  $('#payment-close').addEventListener('click',()=>$('#checkout-modal').hidden=true);
  $('#checkout-modal').addEventListener('click',(e)=>{if(e.target===$('#checkout-modal'))$('#checkout-modal').hidden=true;});
  refreshCustomer().catch(e=>toast(e.message,true));
  setInterval(()=>refreshCustomer().catch(()=>{}),2000);
}
let adminState=null;
function eventHTML(events,max=9){
  return events.slice(0,max).map(ev=>{
    const badge=ev.type==='unknown'?'!':ev.type==='scan'?'↳':ev.type==='paid'?'✓':ev.type==='location'?'⌖':'•';
    const c=['unknown','paid'].includes(ev.type)?ev.type:'';
    const afford=ev.type==='unknown'?` <button class="mini-use-tag" data-uid="${esc(ev.tag_uid)}" title="Prefill registration field">Use UID ↗</button>`:'';
    return `<div class="event-row"><div class="event-icon ${c}">${badge}</div><div><div class="event-title">${esc(ev.message)}${afford}</div><div class="event-time">${esc(timeText(ev.created_at))} · ${esc(ev.type.replace(/_/g,' '))}</div></div></div>`;
  }).join('') || '<p class="small-muted">No activity recorded yet. Try scanning an RFID tag.</p>';
}
function renderAdmin(data){
  adminState=data;
  $('#admin-carts').textContent=data.carts.length;
  $('#admin-scans').textContent=data.events.filter(e=>e.type==='scan').length;
  $('#admin-pending').textContent=data.carts.filter(c=>c.order).length;
  $('#admin-unknown').textContent=data.events.filter(e=>e.type==='unknown').length;
  drawMap($('#admin-map'),data.zones,data.products,data.carts);
  $('#reader-status').textContent=data.demo?'Demo mode':data.scanner.connected?'RFID online':'RFID offline';
  $('#reader-status').className=`tag ${data.scanner.connected?'tag-good':'tag-warning'}`;
  const cartOptions=data.carts.map(c=>({value:c.id,label:c.id}));
  setSelect('#reader-cart',cartOptions,data.reader_cart);
  setSelect('#location-cart',cartOptions,null);
  const zs=data.zones.map(z=>({value:z.id,label:z.name}));
  setSelect('#location-zone',zs,null);
  const prodOpts=data.products.map(p=>({value:p.id,label:`${p.name} — ${p.sku}`}));
  setSelect('#register-product',prodOpts,null);
  $('#demo-scanner').hidden=!data.demo;
  const tags=data.tags.slice(0,8).map(t=>`<div class="tag-row"><span class="tag-uid">${esc(t.uid)}</span><span class="tag-product">${esc(t.product_name)}</span>${t.status==='sold'?'<span class="tag-sold">SOLD</span>':''}</div>`).join('');
  $('#tag-list').innerHTML=tags||'<p class="small-muted">No RFID tags registered yet.</p>';
  $('#admin-events').innerHTML=eventHTML(data.events,10);
  const pending=data.carts.filter(c=>c.order).map(c=>`<div class="pending-row"><div><strong>${esc(c.id)} · ${money(c.order.amount)}</strong><small>${esc(c.order.id)} — Payment not verified</small></div><button class="btn btn-outline verify-btn" data-order="${esc(c.order.id)}">Verify payment</button></div>`).join('');
  $('#pending-orders').innerHTML=pending||'<p class="small-muted">No payments awaiting verification.</p>';
}
async function refreshAdmin(){renderAdmin(await api('/api/admin/state'));}
function initAdmin(){
  $('#reader-form').addEventListener('submit',async e=>{e.preventDefault();try{await mutate('/api/admin/reader-cart',{cart_id:$('#reader-cart').value});toast('Reader assigned to '+$('#reader-cart').value);await refreshAdmin();}catch(err){toast(err.message,true)}});
  $('#location-form').addEventListener('submit',async e=>{e.preventDefault();try{await mutate('/api/admin/cart-location',{cart_id:$('#location-cart').value,zone:$('#location-zone').value});toast('Reported cart location updated');await refreshAdmin();}catch(err){toast(err.message,true)}});
  $('#register-form').addEventListener('submit',async e=>{e.preventDefault();try{await mutate('/api/admin/register',{uid:$('#register-uid').value.trim(),product_id:Number($('#register-product').value)});$('#register-uid').value='';toast('RFID tag registered');await refreshAdmin();}catch(err){toast(err.message,true)}});
  $('#unregister-form').addEventListener('submit',async e=>{e.preventDefault();const uid=$('#unregister-uid').value.trim();if(!confirm(`Unregister RFID tag ${uid}?`))return;try{await mutate('/api/admin/unregister',{uid});$('#unregister-uid').value='';toast('RFID tag unregistered');await refreshAdmin();}catch(err){toast(err.message,true)}});
  $('#demo-form').addEventListener('submit',async e=>{e.preventDefault();try{const r=await mutate('/api/admin/demo-scan',{uid:$('#demo-uid').value.trim()});toast(r.message);await refreshAdmin();}catch(err){toast(err.message,true);await refreshAdmin()}});
  $('#admin-events').addEventListener('click',e=>{const b=e.target.closest('.mini-use-tag');if(!b)return;$('#register-uid').value=b.dataset.uid;$('#register-uid').focus();toast('Unknown UID copied into registration field');});
  $('#pending-orders').addEventListener('click',async e=>{const b=e.target.closest('.verify-btn');if(!b)return;const id=b.dataset.order;
    if(!confirm(`ONLY mark ${id} paid if you independently confirmed the payment in your actual merchant account or cash receipt. Has payment been verified?`))return;
    try{await mutate('/api/admin/confirm-payment',{order_id:id,verified:true});toast('Payment confirmed; sale completed and stock finalized');await refreshAdmin();}catch(err){toast(err.message,true)}
  });
  refreshAdmin().catch(e=>toast(e.message,true));setInterval(()=>refreshAdmin().catch(()=>{}),2500);
}
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
  $('#inventory-table-body').innerHTML=showing.map(p=>{
    const s=p.available===0?'out':p.available<=5?'low':'ok';const label=s==='out'?'Out of stock':s==='low'?'Low stock':'In stock';
    return `<tr><td><div class="table-product"><span class="product-emoji">${esc(p.emoji)}</span><div>${esc(p.name)}<small>${esc(p.category)}</small></div></div></td><td>${esc(p.sku)}</td><td>${esc(data.zones.find(z=>z.id===p.zone)?.name||p.zone)}</td><td class="numeric">${money(p.price)}</td><td class="numeric">${p.on_hand}</td><td class="numeric reserved">${p.reserved}</td><td class="numeric positive">${p.available}</td><td><span class="stock-label ${s}">${label}</span></td></tr>`;
  }).join('') || '<tr><td colspan="8">No matching products</td></tr>';
  $('#inventory-rowcount').textContent=`Showing ${showing.length} of ${ps.length} products`;
  setSelect('#product-zone',data.zones.map(z=>({value:z.id,label:z.name})),null);
  setSelect('#restock-product',ps.map(p=>({value:p.id,label:`${p.name} (available: ${p.available})`})),null);
  $('#inventory-events').innerHTML=eventHTML(data.events.filter(e=>['scan','clear','paid','restock','out_of_stock','unknown'].includes(e.type)),6);
}
async function refreshInventory(){renderInventory(await api('/api/inventory/state'));}
function initInventory(){
  $('#inventory-search').addEventListener('input',()=>{if(inventoryState)renderInventory(inventoryState)});
  $('#product-form').addEventListener('submit',async e=>{e.preventDefault();const form=new FormData(e.target);const data=Object.fromEntries(form.entries());try{await mutate('/api/inventory/product',data);e.target.reset();toast('Product added to inventory');await refreshInventory();}catch(err){toast(err.message,true)}});
  $('#restock-form').addEventListener('submit',async e=>{e.preventDefault();const f=new FormData(e.target);try{await mutate('/api/inventory/restock',{product_id:Number(f.get('product_id')),amount:Number(f.get('amount'))});toast('New stock received');await refreshInventory();}catch(err){toast(err.message,true)}});
  refreshInventory().catch(e=>toast(e.message,true));setInterval(()=>refreshInventory().catch(()=>{}),2500);
}
const page=document.body.dataset.page;
if(page==='customer')initCustomer();
if(page==='admin')initAdmin();
if(page==='inventory')initInventory();
