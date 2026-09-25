(()=>{
  const tg=window.Telegram?.WebApp;
  if(tg){
    try{
      tg.ready();
      tg.expand();
      tg.setHeaderColor?.('#050506');
      tg.setBackgroundColor?.('#050506');
      tg.disableVerticalSwipes?.();
    }catch(_){}
  }

  const $=(s,r=document)=>r.querySelector(s);
  const $$=(s,r=document)=>[...r.querySelectorAll(s)];
  const state={
    data:null,
    page:'home',
    selectedPlan:null,
    sbpPayment:null,
    busy:false,
  };

  const haptic=(type='light')=>{try{tg?.HapticFeedback?.impactOccurred(type)}catch(_){}};
  const notify=(type='success')=>{try{tg?.HapticFeedback?.notificationOccurred(type)}catch(_){}};

  function icons(){
    try{window.lucide?.createIcons()}catch(_){}
  }

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
    toast.timer=setTimeout(()=>el.classList.remove('show'),2400);
  }

  async function request(path,options={}){
    const headers={...(options.headers||{})};
    if(!(options.body instanceof FormData))headers['Content-Type']='application/json';
    if(tg?.initData)headers['X-Telegram-Init-Data']=tg.initData;

    const controller=new AbortController();
    const timeout=setTimeout(()=>controller.abort(),9000);
    let response;
    try{
      response=await fetch(path,{
        cache:'no-store',
        ...options,
        headers,
        signal:controller.signal,
      });
    }catch(error){
      if(error?.name==='AbortError')throw new Error('Сервер отвечает слишком долго');
      throw new Error('Нет соединения с сервером');
    }finally{
      clearTimeout(timeout);
    }

    let data={};
    try{data=await response.json()}catch(_){}
    if(!response.ok)throw new Error(data.message||('Ошибка '+response.status));
    return data;
  }

  function fmtDate(iso){
    if(!iso)return '—';
    try{
      return new Intl.DateTimeFormat('ru-RU',{
        day:'2-digit',month:'2-digit',year:'numeric'
      }).format(new Date(iso));
    }catch(_){return '—'}
  }

  function fmtRemain(seconds){
    const sec=Number(seconds||0);
    if(sec<=0)return '—';
    if(sec>3000000000)return 'Навсегда';
    const days=Math.floor(sec/86400);
    if(days>=1)return days+' дн.';
    const hours=Math.floor(sec/3600);
    if(hours>=1)return hours+' ч.';
    return Math.max(1,Math.floor(sec/60))+' мин.';
  }

  function setAvatar(prefix,user){
    const name=(user?.first_name||'U').trim()||'U';
    const text=$('#'+prefix+'Text');
    const image=$('#'+prefix+'Img');
    if(text)text.textContent=name.slice(0,1).toUpperCase();
    if(image&&user?.photo_url){
      image.onload=()=>{
        image.style.display='block';
        if(text)text.style.display='none';
      };
      image.onerror=()=>{
        image.style.display='none';
        if(text)text.style.display='';
      };
      image.src=user.photo_url;
    }else if(image){
      image.style.display='none';
      if(text)text.style.display='';
    }
  }

  function subscriptionNote(data){
    if(!data.subscription.active){
      return data.subscription.trial_available
        ? 'Пробный доступ доступен после подписки на канал.'
        : 'Выбери тариф, чтобы снова активировать VPN.';
    }
    if(!data.vpn.ready)return 'Подписка активна. VPN-серверы пока готовятся.';
    if(!data.vpn.ok)return 'Подписка сохранена. Сервер временно не отвечает.';
    return 'Подписка активна и готова к использованию.';
  }

  function renderHome(){
    const d=state.data;
    const active=d.subscription.active;
    const used=d.vpn.devices?.length||0;
    const limit=Math.max(1,Number(d.subscription.max_devices||1));
    const pct=Math.min(100,Math.round(used/limit*100));

    $('#headerName').textContent=d.user.first_name||'MGN VPN';
    $('#headerStatus').textContent=active?(d.subscription.plan||'подписка активна'):'нет подписки';

    const pill=$('#subscriptionStatus');
    pill.classList.toggle('active',active);
    $('b',pill).textContent=active?'Активна':'Не активна';

    $('#homePlan').textContent=active?(d.subscription.plan||'MGN VPN'):'Нет подписки';
    $('#homePlanNote').textContent=subscriptionNote(d);
    $('#homeRemaining').textContent=active?fmtRemain(d.subscription.remaining_seconds):'—';
    $('#homeDeviceUsage').textContent=used+' / '+limit;
    $('#deviceProgress').style.width=pct+'%';

    const action=$('#homeSubscriptionAction');
    $('b',action).textContent=active?'Продлить подписку':'Выбрать подписку';

    $('#devicesActionNote').textContent=active?(used+' из '+limit+' подключено'):'Список появится после активации';
    $('#devicePriceHome').textContent='+1 слот · '+d.shop.extra_device_cost+' 💎';
    $('#referralActionNote').textContent='Приглашено: '+d.user.referrals+' · баланс '+d.user.diamonds+' 💎';

    const copy=$('#copySubscriptionHome');
    copy.disabled=!d.vpn.subscription_url;
    $('#linkActionNote').textContent=d.vpn.subscription_url
      ? 'Скопировать персональную ссылку'
      : (active?'Ссылка появится после запуска сервера':'Доступна с активной подпиской');

    $('#trialCard').hidden=!d.subscription.trial_available;
    $('#serverWaitCard').hidden=!(active&&!d.vpn.ready);
  }

  function renderPlans(){
    const d=state.data;
    const active=d.subscription.active;
    $('#planCurrentName').textContent=active?(d.subscription.plan||'MGN VPN'):'Нет подписки';
    $('#planCurrentUntil').textContent=active
      ? (d.subscription.plan==='Навсегда'?'Без ограничения по сроку':'до '+fmtDate(d.subscription.until))
      : 'Выбери новый тариф';

    const status=$('#planMiniStatus');
    status.classList.toggle('active',active);
    status.textContent=active?'Активна':'Не активна';

    const root=$('#plans');
    root.innerHTML=d.plans.map(plan=>{
      const featured=plan.code==='30';
      return '<article class="plan-card '+(featured?'featured':'')+'">'+
        (featured?'<span class="plan-label">ПОПУЛЯРНЫЙ</span>':'')+
        '<div class="plan-info">'+
          '<h3>'+esc(plan.name)+'</h3>'+
          '<p>До '+Number(plan.devices||1)+' устройств</p>'+
          '<em>+'+Number(plan.diamonds||0)+' 💎 после покупки</em>'+
        '</div>'+
        '<div class="plan-price">'+
          '<b>'+Number(plan.rub||0).toLocaleString('ru-RU')+' ₽</b>'+
          '<small>'+Number(plan.stars||0).toLocaleString('ru-RU')+' Stars</small>'+
        '</div>'+
        '<button data-buy="'+esc(plan.code)+'">'+(active?'Продлить':'Выбрать')+'</button>'+
      '</article>';
    }).join('');

    $$('[data-buy]',root).forEach(button=>{
      button.onclick=()=>openPayment(button.dataset.buy);
    });

    $('#copySubscriptionPlans').disabled=!d.vpn.subscription_url;
    icons();
  }

  function deviceIcon(item){
    const text=((item?.platform||'')+' '+(item?.name||'')).toLowerCase();
    if(text.includes('iphone')||text.includes('ios')||text.includes('android')||text.includes('phone'))return 'smartphone';
    if(text.includes('mac')||text.includes('windows')||text.includes('linux')||text.includes('pc')||text.includes('laptop'))return 'laptop';
    return 'monitor-smartphone';
  }

  function renderDevices(){
    const d=state.data;
    const list=d.vpn.devices||[];
    const limit=Math.max(1,Number(d.subscription.max_devices||1));
    const used=list.length;
    $('#deviceCount').textContent=used;
    $('#deviceLimit').textContent=limit;
    $('#deviceCapacityBar').style.width=Math.min(100,used/limit*100)+'%';
    $('#buyDevicePrice').textContent='+1 постоянный слот · '+d.shop.extra_device_cost+' 💎';

    const root=$('#deviceList');
    if(!d.subscription.active){
      root.innerHTML='<div class="empty">Активной подписки нет. После покупки тарифа здесь появятся подключённые устройства.</div>';
      icons();
      return;
    }
    if(!d.vpn.ready){
      root.innerHTML='<div class="empty">VPN-сервер ещё не подключён. Лимит устройств уже сохранён и появится здесь после запуска.</div>';
      icons();
      return;
    }
    if(!list.length){
      root.innerHTML='<div class="empty">Подключённых устройств пока нет. Они появятся автоматически после первого подключения к VPN.</div>';
      icons();
      return;
    }

    root.innerHTML=list.map((item,index)=>{
      const id=String(item.id||item.device_id||'');
      const name=esc(item.name||item.device_name||('Устройство '+(index+1)));
      const platform=esc(item.platform||item.os||'Подключено к MGN VPN');
      return '<article class="device">'+
        '<span class="device-symbol"><i data-lucide="'+deviceIcon(item)+'"></i></span>'+
        '<span class="device-copy"><b>'+name+'</b><small>'+platform+'</small></span>'+
        (id?'<button class="device-remove" data-remove="'+encodeURIComponent(id)+'" aria-label="Отключить устройство"><i data-lucide="trash-2"></i></button>':'')+
      '</article>';
    }).join('');

    $$('[data-remove]',root).forEach(button=>{
      button.onclick=()=>removeDevice(decodeURIComponent(button.dataset.remove));
    });
    icons();
  }

  function renderProfile(){
    const d=state.data;
    setAvatar('profileAvatar',d.user);
    $('#profileName').textContent=d.user.first_name||'Пользователь';
    $('#profileUsername').textContent=d.user.username?('@'+d.user.username):'Telegram';
    $('#profileDiamonds').textContent=Number(d.user.diamonds||0).toLocaleString('ru-RU');
    $('#profileRefs').textContent=Number(d.user.referrals||0).toLocaleString('ru-RU');
    $('#profilePlan').textContent=d.subscription.active
      ? (d.subscription.plan+' · '+fmtRemain(d.subscription.remaining_seconds))
      : 'Нет активной подписки';
    $('#profileDevices').textContent='Лимит: '+Number(d.subscription.max_devices||1);
    $('#profileId').textContent=String(d.user.id||'—');
  }

  function render(){
    if(!state.data)return;
    setAvatar('avatar',state.data.user);
    renderHome();
    renderPlans();
    renderDevices();
    renderProfile();
    icons();
    $('#loader').classList.add('hidden');
  }

  function go(page){
    if(!['home','plans','devices','profile'].includes(page))page='home';
    state.page=page;
    $$('.page').forEach(el=>el.classList.toggle('active',el.dataset.page===page));
    $$('#bottomNav button').forEach(el=>el.classList.toggle('active',el.dataset.nav===page));
    window.scrollTo({top:0,behavior:'auto'});
    haptic();
    try{page==='home'?tg?.BackButton?.hide():tg?.BackButton?.show()}catch(_){}
    icons();
  }

  async function load(silent=false){
    try{
      state.data=await request('/api/miniapp/me?_='+Date.now());
      render();
    }catch(error){
      $('#loader').classList.add('hidden');
      if(!silent)toast(error.message);
    }
  }

  function showBackdrop(){
    $('#sheetBackdrop').hidden=false;
    document.body.style.overflow='hidden';
  }

  function hideBackdropIfUnused(){
    if($('#paymentSheet').hidden&&$('#deviceSheet').hidden){
      $('#sheetBackdrop').hidden=true;
      document.body.style.overflow='';
    }
  }

  function closePayment(){
    $('#paymentSheet').hidden=true;
    hideBackdropIfUnused();
  }

  function closeDeviceSheet(){
    $('#deviceSheet').hidden=true;
    hideBackdropIfUnused();
  }

  function closeSheets(){
    $('#paymentSheet').hidden=true;
    $('#deviceSheet').hidden=true;
    $('#sheetBackdrop').hidden=true;
    document.body.style.overflow='';
  }

  function openPayment(code){
    const plan=state.data?.plans?.find(item=>item.code===code);
    if(!plan)return;
    closeDeviceSheet();
    state.selectedPlan=plan;
    state.sbpPayment=null;

    $('#sheetTitle').textContent=plan.name+' · '+Number(plan.rub).toLocaleString('ru-RU')+' ₽';
    $('#sheetText').textContent='До '+plan.devices+' устройств · +'+plan.diamonds+' 💎 после оплаты.';
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
    closePayment();
    const atCap=Number(d.subscription.max_devices||1)>=Number(d.shop.max_devices||10);
    $('#deviceSheetPrice').textContent=Number(d.shop.extra_device_cost||0).toLocaleString('ru-RU')+' алмазов';
    $('#deviceSheetBalance').textContent='Баланс: '+Number(d.user.diamonds||0).toLocaleString('ru-RU')+' 💎';
    $('#confirmDevicePurchase').disabled=atCap;
    $('#confirmDevicePurchase').textContent=atCap?'Лимит устройств достигнут':'Купить дополнительный слот';
    $('#deviceSheet').hidden=false;
    showBackdrop();
    icons();
    haptic('medium');
  }

  async function payStars(){
    if(state.busy||!state.selectedPlan)return;
    state.busy=true;
    $('#payStars').disabled=true;
    try{
      const result=await request('/api/miniapp/payment/stars',{
        method:'POST',
        body:JSON.stringify({plan_code:state.selectedPlan.code}),
      });
      if(tg?.openInvoice){
        tg.openInvoice(result.invoice_url,async status=>{
          if(status==='paid'){
            notify();
            toast('Подписка оплачена');
            closePayment();
            setTimeout(()=>load(true),850);
          }else if(status==='failed'){
            notify('error');
            toast('Оплата не прошла');
          }
        });
      }else{
        window.location.href=result.invoice_url;
      }
    }catch(error){
      toast(error.message);
    }finally{
      state.busy=false;
      $('#payStars').disabled=false;
    }
  }

  async function paySbp(){
    if(state.busy||!state.selectedPlan)return;
    state.busy=true;
    $('#paySbp').disabled=true;
    try{
      const result=await request('/api/miniapp/payment/sbp',{
        method:'POST',
        body:JSON.stringify({plan_code:state.selectedPlan.code}),
      });
      state.sbpPayment=result.payment_id;
      $('#checkPayment').hidden=false;
      if(tg?.openLink)tg.openLink(result.pay_url);
      else window.open(result.pay_url,'_blank');
      toast('После оплаты вернись и нажми «Проверить оплату»');
    }catch(error){
      toast(error.message);
    }finally{
      state.busy=false;
      $('#paySbp').disabled=!state.data?.payments?.sbp_enabled;
    }
  }

  async function checkSbp(){
    if(!state.sbpPayment)return;
    $('#checkPayment').disabled=true;
    try{
      const result=await request('/api/miniapp/payment/sbp/'+encodeURIComponent(state.sbpPayment));
      if(result.status==='paid'){
        notify();
        toast('Оплата получена');
        closePayment();
        await load(true);
        go('home');
      }else{
        toast('Оплата пока не подтверждена');
      }
    }catch(error){
      toast(error.message);
    }finally{
      $('#checkPayment').disabled=false;
    }
  }

  async function removeDevice(id){
    if(!id)return;
    let confirmed=true;
    if(tg?.showConfirm){
      confirmed=await new Promise(resolve=>tg.showConfirm('Отключить это устройство?',resolve));
    }else{
      confirmed=window.confirm('Отключить это устройство?');
    }
    if(!confirmed)return;

    try{
      await request('/api/miniapp/devices/'+encodeURIComponent(id),{method:'DELETE'});
      notify();
      toast('Устройство отключено');
      await load(true);
    }catch(error){
      notify('error');
      toast(error.message);
    }
  }

  async function buyExtraDevice(){
    if(state.busy)return;
    state.busy=true;
    const button=$('#confirmDevicePurchase');
    button.disabled=true;
    try{
      const result=await request('/api/miniapp/shop/device',{
        method:'POST',
        body:'{}',
      });
      notify();
      toast('+1 устройство добавлено');
      closeDeviceSheet();
      await load(true);
      go('devices');
      if(result?.max_devices)$('#deviceLimit').textContent=result.max_devices;
    }catch(error){
      notify('error');
      toast(error.message);
    }finally{
      state.busy=false;
      button.disabled=false;
    }
  }

  async function activateTrial(){
    const d=state.data;
    if(!d)return;
    if(d.trial_channel_url){
      try{
        if(tg?.openTelegramLink)tg.openTelegramLink(d.trial_channel_url);
        else window.open(d.trial_channel_url,'_blank');
      }catch(_){}
    }

    setTimeout(async()=>{
      try{
        await request('/api/miniapp/trial',{method:'POST',body:'{}'});
        notify();
        toast('Пробный доступ активирован');
        await load(true);
      }catch(error){
        toast(error.message);
      }
    },800);
  }

  async function copyText(text,success='Скопировано'){
    if(!text)return false;
    try{
      await navigator.clipboard.writeText(text);
      notify();
      toast(success);
      return true;
    }catch(_){
      try{
        const area=document.createElement('textarea');
        area.value=text;
        area.style.position='fixed';
        area.style.opacity='0';
        document.body.appendChild(area);
        area.select();
        const ok=document.execCommand('copy');
        area.remove();
        if(ok){notify();toast(success);return true}
      }catch(__){}
    }
    toast('Не удалось скопировать');
    return false;
  }

  function copySubscription(){
    const url=state.data?.vpn?.subscription_url;
    if(!url){
      toast(state.data?.subscription?.active?'Ссылка пока недоступна':'Сначала активируй подписку');
      return;
    }
    copyText(url,'Ссылка подключения скопирована');
  }

  function shareReferral(){
    const url=state.data?.user?.referral_url;
    if(!url)return;
    const share='https://t.me/share/url?url='+encodeURIComponent(url)+'&text='+encodeURIComponent('Подключай MGN VPN');
    try{
      if(tg?.openTelegramLink)tg.openTelegramLink(share);
      else window.open(share,'_blank');
    }catch(_){
      window.open(share,'_blank');
    }
  }

  function openSupport(){
    const url=state.data?.bot_url;
    if(!url)return;
    try{
      if(tg?.openTelegramLink)tg.openTelegramLink(url);
      else window.open(url,'_blank');
    }catch(_){
      window.open(url,'_blank');
    }
  }

  $$('[data-nav]').forEach(button=>{
    button.addEventListener('click',()=>go(button.dataset.nav));
  });

  $('#buyDeviceHome').onclick=openDeviceSheet;
  $('#buyDevicePage').onclick=openDeviceSheet;
  $('#copySubscriptionHome').onclick=copySubscription;
  $('#copySubscriptionPlans').onclick=copySubscription;
  $('#shareHome').onclick=shareReferral;
  $('#shareProfile').onclick=shareReferral;
  $('#supportBtn').onclick=openSupport;
  $('#trialBtn').onclick=activateTrial;

  $('#sheetClose').onclick=closePayment;
  $('#deviceSheetClose').onclick=closeDeviceSheet;
  $('#sheetBackdrop').onclick=closeSheets;
  $('#payStars').onclick=payStars;
  $('#paySbp').onclick=paySbp;
  $('#checkPayment').onclick=checkSbp;
  $('#confirmDevicePurchase').onclick=buyExtraDevice;
  $('#copyId').onclick=()=>copyText(state.data?.user?.id?String(state.data.user.id):'','Telegram ID скопирован');

  try{
    tg?.BackButton?.onClick(()=>{
      if(!$('#paymentSheet').hidden||!$('#deviceSheet').hidden)closeSheets();
      else if(state.page!=='home')go('home');
    });
  }catch(_){}

  document.addEventListener('visibilitychange',()=>{
    if(!document.hidden&&state.data)load(true);
  });

  icons();
  if(!tg?.initData){
    $('#loader').classList.add('hidden');
    toast('Открой Mini App внутри Telegram');
  }else{
    load();
  }
})();
