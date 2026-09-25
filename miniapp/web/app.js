(()=>{
  const tg=window.Telegram?.WebApp;
  if(tg){try{tg.ready();tg.expand();tg.setHeaderColor?.('#08080a');tg.setBackgroundColor?.('#08080a');tg.disableVerticalSwipes?.()}catch(_){}}
  const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
  const state={data:null,page:'home',selected:null,sbpPayment:null,busy:false};
  const apiBase='';
  const haptic=(type='light')=>{try{tg?.HapticFeedback?.impactOccurred(type)}catch(_){}};
  const notify=(type='success')=>{try{tg?.HapticFeedback?.notificationOccurred(type)}catch(_){}};
  function toast(msg){const el=$('#toast');el.textContent=msg;el.classList.add('show');clearTimeout(toast.t);toast.t=setTimeout(()=>el.classList.remove('show'),2400)}
  async function request(path,opts={}){
    const headers={...(opts.headers||{})};
    if(!(opts.body instanceof FormData))headers['Content-Type']='application/json';
    if(tg?.initData)headers['X-Telegram-Init-Data']=tg.initData;
    const ctl=new AbortController(),timer=setTimeout(()=>ctl.abort(),9000);
    let res;
    try{res=await fetch(apiBase+path,{cache:'no-store',...opts,headers,signal:ctl.signal})}
    catch(e){throw new Error(e?.name==='AbortError'?'Сервер отвечает слишком долго':'Нет соединения с сервером')}
    finally{clearTimeout(timer)}
    let data={};try{data=await res.json()}catch(_){}
    if(!res.ok)throw new Error(data.message||'Ошибка сервера');
    return data;
  }
  const fmtDate=(iso)=>{if(!iso)return '—';try{return new Intl.DateTimeFormat('ru-RU',{day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'}).format(new Date(iso))}catch(_){return '—'}};
  const fmtRemain=(sec)=>{sec=Number(sec||0);if(sec<=0)return 'нет активной подписки';if(sec>3000000000)return 'без ограничений';const d=Math.floor(sec/86400);if(d>0)return d+' дн.';const h=Math.max(1,Math.floor(sec/3600));return h+' ч.'};
  function setAvatar(prefix,user){
    const name=(user?.first_name||'M').trim()||'M',initial=name[0].toUpperCase();
    const txt=$('#'+prefix+'Text'),img=$('#'+prefix+'Img');
    if(txt)txt.textContent=initial;
    if(img&&user?.photo_url){img.src=user.photo_url;img.style.display='block';if(txt)txt.style.display='none'}
  }
  function render(){
    const d=state.data;if(!d)return;
    const active=d.subscription.active,ready=d.vpn.ready,ok=d.vpn.ok,url=d.vpn.subscription_url;
    $('#statusDot').classList.toggle('on',active&&ready&&ok);
    $('#powerBtn').classList.toggle('active',active&&ready&&ok&&!!url);
    $('#statusText').textContent=active?(ready?(ok?'VPN готов к подключению':'Сервер временно недоступен'):'Подписка активна'):'VPN выключен';
    $('#heroTitle').textContent=active?(d.subscription.plan||'Подписка активна'):'Подключи MGN VPN';
    $('#heroSub').textContent=active?('Осталось: '+fmtRemain(d.subscription.remaining_seconds)):(d.subscription.trial_available?'Пробный доступ уже ждёт тебя':'Выбери тариф и подключайся');
    $('#serverName').textContent=d.vpn.server||'MGN VPN';
    $('#serverState').textContent=ready?(ok?'онлайн':'недоступен'):'ожидает запуска';
    $('#quickPlan').textContent=active?(d.subscription.plan+' · '+fmtRemain(d.subscription.remaining_seconds)):'Выбрать тариф';
    $('#quickDevices').textContent=(d.vpn.devices?.length||0)+' из '+d.subscription.max_devices;
    $('#quickDiamonds').textContent=d.user.diamonds+' 💎';
    $('#trialCard').hidden=!d.subscription.trial_available;
    $('#serverWaitCard').hidden=!(active&&!ready);
    $('#copyLink').disabled=!url;
    renderPlans();renderDevices();renderProfile();
    setAvatar('avatar',d.user);
    $('#loader').classList.add('hidden');
  }
  function renderPlans(){
    const d=state.data;if(!d)return;
    $('#plans').innerHTML=d.plans.map(p=>{
      const popular=p.code==='30';
      return '<article class="plan '+(popular?'popular':'')+'">'+
        (popular?'<span class="plan-badge">ПОПУЛЯРНЫЙ</span>':'')+
        '<div><h3>'+p.name+'</h3><p>до '+p.devices+' устройств</p><p class="plan-reward">+'+p.diamonds+' 💎 за покупку</p></div>'+
        '<div class="plan-price"><b>'+p.rub+' ₽</b><small>'+p.stars+' ⭐ Stars</small></div>'+
        '<button data-buy="'+p.code+'">Выбрать</button></article>'
    }).join('');
    $$('[data-buy]').forEach(b=>b.onclick=()=>openPayment(b.dataset.buy));
  }
  function renderDevices(){
    const d=state.data;if(!d)return;const list=d.vpn.devices||[];
    $('#deviceCount').textContent=list.length;$('#deviceLimit').textContent=d.subscription.max_devices;
    if(!d.subscription.active){$('#deviceList').innerHTML='<div class="empty">Активируй подписку — здесь появятся подключённые устройства.</div>';return}
    if(!d.vpn.ready){$('#deviceList').innerHTML='<div class="empty">VPN-сервер ещё не подключён. Устройства появятся автоматически после запуска.</div>';return}
    if(!list.length){$('#deviceList').innerHTML='<div class="empty">Пока нет зарегистрированных устройств. Открой персональную ссылку и подключись.</div>';return}
    $('#deviceList').innerHTML=list.map((x,i)=>'<article class="device"><div class="device-icon">📱</div><div class="device-main"><b>'+(x.name||('Устройство '+(i+1)))+'</b><small>'+(x.platform||'Подключено к MGN VPN')+'</small></div><button data-remove="'+encodeURIComponent(x.id||'')+'">×</button></article>').join('');
    $$('[data-remove]').forEach(b=>b.onclick=()=>removeDevice(decodeURIComponent(b.dataset.remove)));
  }
  function renderProfile(){
    const d=state.data;if(!d)return;setAvatar('profileAvatar',d.user);
    $('#profileName').textContent=d.user.first_name||'Пользователь';
    $('#profileUsername').textContent=d.user.username?('@'+d.user.username):('ID '+d.user.id);
    $('#profileDiamonds').textContent=d.user.diamonds;$('#profileRefs').textContent=d.user.referrals;$('#profileDevices').textContent=d.subscription.max_devices;
    $('#profilePlan').textContent=d.subscription.active?(d.subscription.plan||'VPN'):'Нет подписки';
    $('#profileUntil').textContent=d.subscription.plan==='Навсегда'?'Навсегда':fmtDate(d.subscription.until);
    $('#profileServer').textContent=d.vpn.server||'—';
  }
  function go(page){state.page=page;$$('.page').forEach(p=>p.classList.toggle('active',p.dataset.page===page));$$('#bottomNav button').forEach(b=>b.classList.toggle('active',b.dataset.nav===page));window.scrollTo({top:0,behavior:'auto'});haptic();try{page==='home'?tg?.BackButton?.hide():tg?.BackButton?.show()}catch(_){}}
  async function load(silent=false){try{state.data=await request('/api/miniapp/me?_='+Date.now());render()}catch(e){if(!silent)toast(e.message);$('#loader').classList.add('hidden')}}
  function openPayment(code){
    const p=state.data?.plans?.find(x=>x.code===code);if(!p)return;state.selected=p;state.sbpPayment=null;
    $('#sheetTitle').textContent=p.name+' · '+p.rub+' ₽';$('#sheetText').textContent='До '+p.devices+' устройств · +'+p.diamonds+' 💎 после покупки';
    $('#starsPrice').textContent=p.stars+' Stars';$('#sbpPrice').textContent=p.rub+' ₽';
    $('#paySbp').disabled=!state.data.payments.sbp_enabled;$('#checkPayment').hidden=true;
    $('#paymentSheet').hidden=false;$('#sheetBackdrop').hidden=false;document.body.style.overflow='hidden';haptic('medium');
  }
  function closeSheet(){$('#paymentSheet').hidden=true;$('#sheetBackdrop').hidden=true;document.body.style.overflow=''}
  async function payStars(){
    if(state.busy||!state.selected)return;state.busy=true;$('#payStars').disabled=true;
    try{
      const r=await request('/api/miniapp/payment/stars',{method:'POST',body:JSON.stringify({plan_code:state.selected.code})});
      if(tg?.openInvoice){
        tg.openInvoice(r.invoice_url,async status=>{if(status==='paid'){notify();toast('Оплата прошла');closeSheet();setTimeout(()=>load(true),1000)}else if(status==='failed')toast('Оплата не прошла')});
      }else window.location.href=r.invoice_url;
    }catch(e){toast(e.message)}finally{state.busy=false;$('#payStars').disabled=false}
  }
  async function paySbp(){
    if(state.busy||!state.selected)return;state.busy=true;$('#paySbp').disabled=true;
    try{
      const r=await request('/api/miniapp/payment/sbp',{method:'POST',body:JSON.stringify({plan_code:state.selected.code})});
      state.sbpPayment=r.payment_id;$('#checkPayment').hidden=false;
      if(tg?.openLink)tg.openLink(r.pay_url);else window.open(r.pay_url,'_blank');
      toast('После оплаты вернись и нажми «Проверить»');
    }catch(e){toast(e.message)}finally{state.busy=false;$('#paySbp').disabled=!state.data.payments.sbp_enabled}
  }
  async function checkSbp(){
    if(!state.sbpPayment)return;
    $('#checkPayment').disabled=true;
    try{
      const r=await request('/api/miniapp/payment/sbp/'+encodeURIComponent(state.sbpPayment));
      if(r.status==='paid'){notify();toast('Оплата получена');closeSheet();await load(true);go('home')}
      else toast('Оплата пока не подтверждена');
    }catch(e){toast(e.message)}finally{$('#checkPayment').disabled=false}
  }
  async function removeDevice(id){
    if(!id)return;
    let ok=true;
    if(tg?.showConfirm)ok=await new Promise(res=>tg.showConfirm('Отключить это устройство?',res));
    else ok=confirm('Отключить это устройство?');
    if(!ok)return;
    try{await request('/api/miniapp/devices/'+encodeURIComponent(id),{method:'DELETE'});notify();toast('Устройство отключено');await load(true)}catch(e){toast(e.message)}
  }
  async function activateTrial(){
    if(!state.data)return;
    if(state.data.trial_channel_url){try{tg?.openTelegramLink?.(state.data.trial_channel_url)}catch(_){}}
    setTimeout(async()=>{try{await request('/api/miniapp/trial',{method:'POST',body:'{}'});notify();toast('Пробный доступ активирован');await load(true)}catch(e){toast(e.message)}},700);
  }
  async function connect(){
    const d=state.data;if(!d)return;
    if(!d.subscription.active){go('plans');return}
    if(!d.vpn.ready){toast('VPN-серверы ещё не подключены');return}
    if(!d.vpn.subscription_url){toast('Ссылка пока недоступна');return}
    try{tg?.openLink?tg.openLink(d.vpn.subscription_url):window.open(d.vpn.subscription_url,'_blank')}catch(_){window.location.href=d.vpn.subscription_url}
  }
  async function copyLink(){
    const url=state.data?.vpn?.subscription_url;if(!url)return;
    try{await navigator.clipboard.writeText(url);notify();toast('Ссылка скопирована')}catch(_){toast('Не удалось скопировать ссылку')}
  }
  function share(){
    const url=state.data?.user?.referral_url;if(!url)return;
    const share='https://t.me/share/url?url='+encodeURIComponent(url)+'&text='+encodeURIComponent('Подключай MGN VPN');
    try{tg?.openTelegramLink?tg.openTelegramLink(share):window.open(share,'_blank')}catch(_){window.open(share,'_blank')}
  }

  $$('[data-nav]').forEach(b=>b.addEventListener('click',()=>go(b.dataset.nav)));
  $('#avatarBtn').onclick=()=>go('profile');$('#powerBtn').onclick=connect;$('#copyLink').onclick=copyLink;$('#trialBtn').onclick=activateTrial;$('#shareBtn').onclick=share;
  $('#sheetClose').onclick=closeSheet;$('#sheetBackdrop').onclick=closeSheet;$('#payStars').onclick=payStars;$('#paySbp').onclick=paySbp;$('#checkPayment').onclick=checkSbp;
  try{tg?.BackButton?.onClick(()=>{if(!$('#paymentSheet').hidden)closeSheet();else go('home')})}catch(_){}
  document.addEventListener('visibilitychange',()=>{if(!document.hidden&&state.data)load(true)});

  if(!tg?.initData){$('#loader').classList.add('hidden');toast('Открой Mini App из Telegram, чтобы загрузить профиль')}
  else load();
})();
