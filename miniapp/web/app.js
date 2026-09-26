(()=>{
  const tg=window.Telegram?.WebApp;
  if(tg){
    try{
      tg.ready();
      tg.expand();
      tg.setHeaderColor?.('#595a5e');
      tg.setBackgroundColor?.('#595a5e');
    }catch(_){}
  }

  const $=(selector,root=document)=>root.querySelector(selector);
  const $$=(selector,root=document)=>[...root.querySelectorAll(selector)];
  const ROOT_PAGES=new Set(['home','plans','devices','profile']);
  const state={
    data:null,
    page:'home',
    previousRoot:'home',
    selectedPlan:null,
    promoCode:'',
    sbpPayment:null,
    busy:false,
  };

  const haptic=(type='light')=>{try{tg?.HapticFeedback?.impactOccurred(type)}catch(_){}};
  const notify=(type='success')=>{try{tg?.HapticFeedback?.notificationOccurred(type)}catch(_){}};

  function icons(){try{window.lucide?.createIcons()}catch(_){}}
  function esc(value=''){
    return String(value).replace(/[&<>"']/g,ch=>({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[ch]));
  }
  function toast(message){
    const el=$('#toast');
    el.textContent=message;
    el.classList.add('show');
    clearTimeout(toast.timer);
    toast.timer=setTimeout(()=>el.classList.remove('show'),2300);
  }
  async function request(path,options={}){
    const headers={...(options.headers||{})};
    if(!(options.body instanceof FormData))headers['Content-Type']='application/json';
    if(tg?.initData)headers['X-Telegram-Init-Data']=tg.initData;
    const controller=new AbortController();
    const timer=setTimeout(()=>controller.abort(),9000);
    try{
      const response=await fetch(path,{cache:'no-store',...options,headers,signal:controller.signal});
      let data={}; try{data=await response.json()}catch(_){}
      if(!response.ok)throw new Error(data.message||('Ошибка '+response.status));
      return data;
    }catch(error){
      if(error?.name==='AbortError')throw new Error('Сервер отвечает слишком долго');
      throw error;
    }finally{
      clearTimeout(timer);
    }
  }
  function fmtDate(iso){
    if(!iso)return '—';
    try{return new Intl.DateTimeFormat('ru-RU',{day:'2-digit',month:'2-digit',year:'numeric'}).format(new Date(iso))}
    catch(_){return '—'}
  }
  function fmtRemain(seconds){
    const sec=Number(seconds||0);
    if(sec<=0)return '—';
    if(sec>3000000000)return 'Навсегда';
    const days=Math.floor(sec/86400);
    if(days>0){
      const mod10=days%10,mod100=days%100;
      const word=(mod10===1&&mod100!==11)?'день':([2,3,4].includes(mod10)&&![12,13,14].includes(mod100)?'дня':'дней');
      return days+' '+word;
    }
    const hours=Math.floor(sec/3600);
    if(hours>0)return hours+' ч.';
    return Math.max(1,Math.floor(sec/60))+' мин.';
  }
  function fmtHistoryDate(iso){
    if(!iso)return '';
    try{return new Intl.DateTimeFormat('ru-RU',{day:'2-digit',month:'2-digit'}).format(new Date(iso))}
    catch(_){return ''}
  }
  function setAvatar(prefix,user){
    const text=$('#'+prefix+'Text');
    const image=$('#'+prefix+'Img');
    const initial=((user?.first_name||'U').trim().charAt(0)||'U').toUpperCase();
    if(text){text.textContent=initial;text.style.display=''}
    if(image){
      image.style.display='none';
      if(user?.photo_url){
        image.onload=()=>{image.style.display='block';if(text)text.style.display='none'};
        image.onerror=()=>{image.style.display='none';if(text)text.style.display=''};
        image.src=user.photo_url;
      }
    }
  }
  function subscriptionNote(d){
    if(!d.subscription.active){
      return d.subscription.trial_available
        ? 'Бесплатный день можно активировать после подписки на канал.'
        : 'Выбери тариф, чтобы снова получить доступ.';
    }
    if(!d.vpn.ready)return 'Подписка активна. VPN-серверы пока готовятся.';
    if(!d.vpn.ok)return 'Подписка сохранена. Сервер временно недоступен.';
    return 'Подписка активна и готова к использованию.';
  }

  function renderHome(){
    const d=state.data;
    const active=!!d.subscription.active;
    const used=(d.vpn.devices||[]).length;
    const limit=Math.max(1,Number(d.subscription.max_devices||1));
    const pct=Math.min(100,Math.round((used/limit)*100));

    $('#headerName').textContent=d.user.first_name||'MGN VPN';
    $('#headerStatus').textContent=active?'Подписка активна':'Нет подписки';

    const status=$('#subscriptionStatus');
    status.classList.toggle('active',active);
    $('b',status).textContent=active?'Активна':'Не активна';

    $('#homePlan').textContent=active?(d.subscription.plan||'MGN VPN'):'Нет подписки';
    $('#homePlanNote').textContent=subscriptionNote(d);
    $('#homeRemaining').textContent=active?fmtRemain(d.subscription.remaining_seconds):'—';
    $('#homeDeviceUsage').textContent=used+' из '+limit;
    $('#deviceProgress').style.width=pct+'%';
    $('b',$('#homeSubscriptionAction')).textContent=active?'Продлить подписку':'Выбрать подписку';

    $('#devicesActionNote').textContent=active?(used+' из '+limit+' активны'):'Нет активной подписки';

    $('#bonusActionNote').textContent='Скидка или бесплатные дни';

    const hasLink=!!d.vpn.subscription_url;
    $('#copySubscriptionHome').disabled=!hasLink;
    $('#linkTitle').textContent=hasLink?'Ваш VPN готов':'Ссылка подключения';
    $('#linkActionNote').textContent=hasLink
      ? 'Скопируйте ссылку и подключитесь на любом устройстве'
      : (active?'Появится после подключения VPN-сервера':'Доступна с активной подпиской');
    $('#linkMasked').textContent=hasLink?'vpn.mgn••••••••':'ссылка пока недоступна';
    $('#vpnLinkCard').classList.toggle('unavailable',!hasLink);

    $('#trialCard').hidden=!d.subscription.trial_available;
    $('#serverWaitCard').hidden=!(active&&!d.vpn.ready);
  }

  function renderPlans(){
    const d=state.data;
    const active=!!d.subscription.active;
    $('#planCurrentName').textContent=active?(d.subscription.plan||'MGN VPN'):'Нет подписки';
    $('#planCurrentUntil').textContent=active
      ? (d.subscription.plan==='Навсегда'?'Без ограничения по сроку':'до '+fmtDate(d.subscription.until))
      : 'Выбери новый тариф';

    const mini=$('#planMiniStatus');
    mini.classList.toggle('active',active);
    mini.textContent=active?'Активна':'Не активна';

    const root=$('#plans');
    root.innerHTML=(d.plans||[]).map(plan=>{
      const featured=plan.code==='30';
      return '<article class="plan-card '+(featured?'featured':'')+'">'+
        (featured?'<span class="plan-label">ПОПУЛЯРНЫЙ</span>':'')+
        '<div class="plan-info"><h3>'+esc(plan.name)+'</h3><p>1 устройство включено</p>'+
        (Number(plan.savings||0)>0?'<em>Выгода '+Number(plan.savings)+' ₽</em>':'')+'</div>'+
        '<div class="plan-price"><b>'+Number(plan.rub||0).toLocaleString('ru-RU')+' ₽</b><small>'+Number(plan.stars||0).toLocaleString('ru-RU')+' Stars</small></div>'+
        '<button type="button" data-buy="'+esc(plan.code)+'">'+(active?'Продлить':'Выбрать')+'</button>'+
      '</article>';
    }).join('');
    $$('[data-buy]',root).forEach(btn=>btn.onclick=()=>openPayment(btn.dataset.buy));
    $('#copySubscriptionPlans').disabled=!d.vpn.subscription_url;
  }

  function deviceIcon(item){
    const value=((item?.platform||'')+' '+(item?.name||item?.device_name||'')).toLowerCase();
    if(/iphone|ios|android|phone/.test(value))return 'smartphone';
    if(/mac|windows|linux|pc|laptop/.test(value))return 'laptop';
    return 'monitor-smartphone';
  }

  function renderDevices(){
    const d=state.data;
    const list=d.vpn.devices||[];
    const limit=Math.max(1,Number(d.subscription.max_devices||1));
    const used=list.length;
    $('#deviceCount').textContent=used;
    $('#deviceLimit').textContent=limit;
    $('#deviceCapacityBar').style.width=Math.min(100,(used/limit)*100)+'%';
    $('#buyDevicePrice').textContent='+1 постоянный слот · '+Number(d.shop.extra_device_price_rub||100)+' ₽';

    const root=$('#deviceList');
    if(!d.subscription.active){
      root.innerHTML='<div class="empty">После активации подписки здесь появятся подключённые устройства.</div>';
    }else if(!d.vpn.ready){
      root.innerHTML='<div class="empty">VPN-сервер ещё не подключён. Лимит устройств уже сохранён.</div>';
    }else if(!list.length){
      root.innerHTML='<div class="empty">Подключённых устройств пока нет. Они появятся после первого подключения к VPN.</div>';
    }else{
      root.innerHTML=list.map((item,index)=>{
        const id=String(item.id||item.device_id||'');
        const name=esc(item.name||item.device_name||('Устройство '+(index+1)));
        const platform=esc(item.platform||item.os||'MGN VPN');
        return '<article class="device">'+
          '<span class="device-symbol"><i data-lucide="'+deviceIcon(item)+'"></i></span>'+
          '<span class="device-copy"><b>'+name+'</b><small>'+platform+'</small></span>'+
          (id?'<button type="button" class="device-remove" aria-label="Отключить устройство" data-remove="'+encodeURIComponent(id)+'"><i data-lucide="trash-2"></i></button>':'')+
        '</article>';
      }).join('');
      $$('[data-remove]',root).forEach(btn=>btn.onclick=()=>removeDevice(decodeURIComponent(btn.dataset.remove)));
    }
  }

  function renderProfile(){
    const d=state.data;
    setAvatar('profileAvatar',d.user);
    $('#profileName').textContent=d.user.first_name||'Пользователь';
    $('#profileUsername').textContent=d.user.username?('@'+d.user.username):'Telegram';
    $('#profileRewards').textContent='+'+Number(d.user.referral_rewards||0);
    $('#profileRefs').textContent=Number(d.user.referrals||0).toLocaleString('ru-RU');
    $('#profileDevices').textContent=Number(d.subscription.max_devices||1);
    $('#profilePlan').textContent=d.subscription.active
      ? (d.subscription.plan+' · '+fmtRemain(d.subscription.remaining_seconds))
      : 'Нет активной подписки';
    $('#profileId').textContent=String(d.user.id||'—');
  }

  function renderReferrals(){
    const d=state.data;
    $('#referralsCount').textContent=Math.min(3,Number(d.user.referrals||0))+' / 3';
    $('#referralsRewards').textContent='+'+Number(d.user.referral_rewards||0);
    $('#referralCode').textContent=d.user.referral_url||'—';
  }

  function renderBonuses(){
    $('#bonusBalance').textContent='MGN VPN';
  }

  function renderClients(){
    const root=$('#clientList');
    root.innerHTML=(state.data.clients||[]).map(client=>
      '<button type="button" data-client="'+esc(client.import_url||client.download_url)+'">'+
      '<span class="icon-box">'+esc(client.icon||'📱')+'</span><span><b>'+esc(client.name)+'</b><small>'+esc(client.platform)+'</small></span>'+
      '<span>'+(client.supports_subscription_import?'Добавить':'Скачать')+'</span></button>'
    ).join('')||'<div class="empty">Сначала активируйте подписку.</div>';
    $$('[data-client]',root).forEach(btn=>btn.onclick=()=>{
      const url=btn.dataset.client;if(!url)return;
      try{tg?.openLink?tg.openLink(url):window.location.href=url}catch(_){window.location.href=url}
    });
  }

  function render(){
    if(!state.data)return;
    setAvatar('avatar',state.data.user);
    renderHome();
    renderPlans();
    renderDevices();
    renderProfile();
    renderReferrals();
    renderBonuses();
    renderClients();
    icons();
    $('#loader').classList.add('hidden');
  }

  function go(page){
    if(!$('.page[data-page="'+page+'"]'))page='home';
    if(ROOT_PAGES.has(page))state.previousRoot=page;
    state.page=page;
    $$('.page').forEach(el=>el.classList.toggle('active',el.dataset.page===page));
    $$('#bottomNav button').forEach(el=>el.classList.toggle('active',el.dataset.nav===page));
    $('#bottomNav').style.display=ROOT_PAGES.has(page)?'grid':'none';
    window.scrollTo({top:0,behavior:'auto'});
    try{
      if(ROOT_PAGES.has(page))tg?.BackButton?.hide();
      else tg?.BackButton?.show();
    }catch(_){}
    haptic();
    icons();
  }

  async function load(silent=false){
    try{
      state.data=await request('/api/miniapp/me?_='+Date.now());
      render();
    }catch(error){
      $('#loader').classList.add('hidden');
      if(!silent)toast(error.message||'Не удалось загрузить данные');
    }
  }

  function showBackdrop(){
    $('#sheetBackdrop').hidden=false;
    document.body.style.overflow='hidden';
  }
  function closeSheets(){
    $('#paymentSheet').hidden=true;
    $('#deviceSheet').hidden=true;
    $('#sheetBackdrop').hidden=true;
    document.body.style.overflow='';
  }

  function openPayment(code){
    const plan=state.data?.plans?.find(x=>x.code===code);
    if(!plan)return;
    closeSheets();
    state.selectedPlan=plan;
    state.promoCode='';
    state.sbpPayment=null;
    $('#sheetTitle').textContent=plan.name+' · '+Number(plan.rub).toLocaleString('ru-RU')+' ₽';
    $('#sheetText').textContent='1 устройство включено. Дополнительный слот — 100 ₽.';
    $('#paymentPromoCode').value='';
    $('#paymentPromoResult').textContent='';
    $('#starsPrice').textContent=Number(plan.stars).toLocaleString('ru-RU')+' Stars';
    $('#sbpPrice').textContent=Number(plan.rub).toLocaleString('ru-RU')+' ₽';
    $('#paySbp').disabled=!state.data.payments.sbp_enabled;
    $('#checkPayment').hidden=true;
    $('#paymentSheet').hidden=false;
    showBackdrop();
    icons();
    haptic('medium');
  }

  function openDeviceSheet(){
    const d=state.data;
    if(!d)return;
    closeSheets();
    const limit=Number(d.subscription.max_devices||1);
    const max=Number(d.shop.max_devices||10);
    $('#deviceSheetPrice').textContent=Number(d.shop.extra_device_price_rub||100)+' ₽';
    $('#deviceSheetBalance').textContent='Оплата через СБП';
    $('#confirmDevicePurchase').disabled=limit>=max;
    $('#confirmDevicePurchase').textContent=limit>=max?'Лимит устройств достигнут':'Купить слот';
    $('#deviceSheet').hidden=false;
    showBackdrop();
    icons();
    haptic('medium');
  }

  async function payStars(){
    if(state.busy||!state.selectedPlan)return;
    state.busy=true; $('#payStars').disabled=true;
    try{
      const result=await request('/api/miniapp/payment/stars',{method:'POST',body:JSON.stringify({plan_code:state.selectedPlan.code,promo_code:state.promoCode})});
      if(tg?.openInvoice){
        tg.openInvoice(result.invoice_url,async status=>{
          if(status==='paid'){notify();toast('Подписка оплачена');closeSheets();setTimeout(()=>load(true),800)}
          else if(status==='failed'){notify('error');toast('Оплата не прошла')}
        });
      }else window.location.href=result.invoice_url;
    }catch(error){toast(error.message)}
    finally{state.busy=false;$('#payStars').disabled=false}
  }

  async function paySbp(){
    if(state.busy||!state.selectedPlan)return;
    state.busy=true; $('#paySbp').disabled=true;
    try{
      const result=await request('/api/miniapp/payment/sbp',{method:'POST',body:JSON.stringify({plan_code:state.selectedPlan.code,promo_code:state.promoCode})});
      state.sbpPayment=result.payment_id;
      $('#checkPayment').hidden=false;
      if(tg?.openLink)tg.openLink(result.pay_url); else window.open(result.pay_url,'_blank');
      toast('После оплаты вернись и нажми «Проверить оплату»');
    }catch(error){toast(error.message)}
    finally{state.busy=false;$('#paySbp').disabled=!state.data?.payments?.sbp_enabled}
  }

  async function checkSbp(){
    if(!state.sbpPayment)return;
    $('#checkPayment').disabled=true;
    try{
      const result=await request('/api/miniapp/payment/sbp/'+encodeURIComponent(state.sbpPayment));
      if(result.status==='paid'){notify();toast('Оплата получена');closeSheets();await load(true);go('home')}
      else toast('Оплата пока не подтверждена');
    }catch(error){toast(error.message)}
    finally{$('#checkPayment').disabled=false}
  }

  async function removeDevice(id){
    let ok=true;
    if(tg?.showConfirm)ok=await new Promise(resolve=>tg.showConfirm('Отключить это устройство?',resolve));
    else ok=window.confirm('Отключить это устройство?');
    if(!ok)return;
    try{
      await request('/api/miniapp/devices/'+encodeURIComponent(id),{method:'DELETE'});
      notify();toast('Устройство отключено');await load(true);
    }catch(error){notify('error');toast(error.message)}
  }

  async function buyExtraDevice(){
    if(state.busy)return;
    state.busy=true; $('#confirmDevicePurchase').disabled=true;
    try{
      const result=await request('/api/miniapp/shop/device',{method:'POST',body:'{}'});
      state.sbpPayment=result.payment_id;
      if(tg?.openLink)tg.openLink(result.pay_url); else window.open(result.pay_url,'_blank');
      closeSheets();toast('После оплаты проверьте платёж в разделе подписки');
      [3000,8000,15000].forEach(delay=>setTimeout(()=>{
        if(state.sbpPayment===result.payment_id)checkSbp();
      },delay));
    }catch(error){notify('error');toast(error.message)}
    finally{state.busy=false;$('#confirmDevicePurchase').disabled=false}
  }

  async function applyPaymentPromo(){
    if(!state.selectedPlan)return;
    const code=$('#paymentPromoCode').value.trim();
    if(!code){state.promoCode='';$('#paymentPromoResult').textContent='';return}
    try{
      const quote=await request('/api/miniapp/promo/quote',{method:'POST',body:JSON.stringify({code,plan_code:state.selectedPlan.code})});
      if(quote.type!=='discount')throw new Error('Этот код даёт бесплатные дни. Активируйте его в профиле.');
      state.promoCode=quote.code;
      $('#paymentPromoResult').textContent='Скидка −'+Number(quote.discount)+' ₽ · итого '+Number(quote.final_price)+' ₽';
      $('#sbpPrice').textContent=Number(quote.final_price)+' ₽';
      $('#starsPrice').textContent='Цена пересчитается сервером';
      notify();
    }catch(error){state.promoCode='';$('#paymentPromoResult').textContent=error.message}
  }

  async function redeemPromo(){
    const code=$('#promoCodePage').value.trim();
    if(!code)return toast('Введите промокод');
    try{
      await request('/api/miniapp/promo/redeem',{method:'POST',body:JSON.stringify({code})});
      $('#promoPageResult').textContent='Промокод активирован. Дни добавлены к подписке.';
      notify();await load(true);
    }catch(error){$('#promoPageResult').textContent=error.message;notify('error')}
  }

  async function activateTrial(){
    const d=state.data;if(!d)return;
    if(d.trial_channel_url){
      try{tg?.openTelegramLink?tg.openTelegramLink(d.trial_channel_url):window.open(d.trial_channel_url,'_blank')}catch(_){}
    }
    setTimeout(async()=>{
      try{await request('/api/miniapp/trial',{method:'POST',body:'{}'});notify();toast('Бесплатный день активирован');await load(true)}
      catch(error){toast(error.message)}
    },700);
  }

  async function copyText(text,success){
    if(!text)return;
    try{
      await navigator.clipboard.writeText(String(text));
      notify();toast(success||'Скопировано');
    }catch(_){
      const area=document.createElement('textarea');
      area.value=String(text); area.style.position='fixed'; area.style.opacity='0';
      document.body.appendChild(area); area.select();
      const ok=document.execCommand('copy'); area.remove();
      if(ok){notify();toast(success||'Скопировано')} else toast('Не удалось скопировать');
    }
  }

  function copySubscription(){
    const url=state.data?.vpn?.subscription_url;
    if(!url){toast(state.data?.subscription?.active?'Ссылка пока недоступна':'Сначала активируй подписку');return}
    copyText(url,'Ссылка VPN скопирована');
  }
  function shareReferral(){
    const url=state.data?.user?.referral_url;if(!url)return;
    const share='https://t.me/share/url?url='+encodeURIComponent(url)+'&text='+encodeURIComponent('Подключай MGN VPN');
    try{tg?.openTelegramLink?tg.openTelegramLink(share):window.open(share,'_blank')}catch(_){window.open(share,'_blank')}
  }
  function openSupport(){
    const url=state.data?.bot_url;if(!url)return;
    try{tg?.openTelegramLink?tg.openTelegramLink(url):window.open(url,'_blank')}catch(_){window.open(url,'_blank')}
  }

  $$('[data-nav]').forEach(btn=>btn.addEventListener('click',()=>go(btn.dataset.nav)));
  $('#copySubscriptionHome').onclick=copySubscription;
  $('#copySubscriptionPlans').onclick=copySubscription;
  $('#trialBtn').onclick=activateTrial;
  $('#buyDevicePage').onclick=openDeviceSheet;
  $('#copyReferral').onclick=()=>copyText(state.data?.user?.referral_url||'','Реферальная ссылка скопирована');
  $('#shareReferral').onclick=shareReferral;
  $('#supportChat').onclick=openSupport;
  $('#copyId').onclick=()=>copyText(state.data?.user?.id||'','Telegram ID скопирован');

  $('#sheetClose').onclick=closeSheets;
  $('#deviceSheetClose').onclick=closeSheets;
  $('#sheetBackdrop').onclick=closeSheets;
  $('#payStars').onclick=payStars;
  $('#paySbp').onclick=paySbp;
  $('#checkPayment').onclick=checkSbp;
  $('#confirmDevicePurchase').onclick=buyExtraDevice;
  $('#applyPaymentPromo').onclick=applyPaymentPromo;
  $('#redeemPromo').onclick=redeemPromo;

  try{
    tg?.BackButton?.onClick(()=>{
      if(!$('#paymentSheet').hidden||!$('#deviceSheet').hidden){closeSheets();return}
      if(!ROOT_PAGES.has(state.page))go(state.previousRoot||'home');
      else go('home');
    });
  }catch(_){}

  document.addEventListener('visibilitychange',()=>{if(!document.hidden&&state.data)load(true)});

  icons();
  if(!tg?.initData){
    $('#loader').classList.add('hidden');
    toast('Открой Mini App внутри Telegram');
  }else{
    load();
  }
})();
