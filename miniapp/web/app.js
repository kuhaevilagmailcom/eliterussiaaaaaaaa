(()=>{
  const tg=window.Telegram?.WebApp;
  if(tg){
    try{
      tg.ready();
      tg.expand();
      tg.setHeaderColor?.('#000000');
      tg.setBackgroundColor?.('#000000');
    }catch(_){}
  }

  const $=(selector,root=document)=>root.querySelector(selector);
  const $$=(selector,root=document)=>[...root.querySelectorAll(selector)];
  const ROOT_PAGES=new Set(['home','plans','profile','bonuses']);
  const state={
    data:null,
    page:'home',
    previousRoot:'home',
    selectedPlan:null,
    selectedDevices:1,
    promoCode:'',
    promoPercent:0,
    sbpPayment:null,
    supportTicketId:null,
    admin:{tab:'overview',loaded:{},userFilter:'all',userPage:0,userPages:1,searchTimer:null},
    busy:false,
  };

  const haptic=(type='light')=>{try{tg?.HapticFeedback?.impactOccurred(type)}catch(_){}};
  const notify=(type='success')=>{try{tg?.HapticFeedback?.notificationOccurred(type)}catch(_){}};

  function icons(){try{window.lucide?.createIcons()}catch(_){}}

  function updateSafeArea(){
    for(const side of ['top','bottom']){
      const inset=Number(tg?.safeAreaInset?.[side]||0)+Number(tg?.contentSafeAreaInset?.[side]||0);
      document.documentElement.style.setProperty('--safe-'+side,'max(env(safe-area-inset-'+side+', 0px), '+inset+'px)');
    }
  }
  updateSafeArea();
  tg?.onEvent?.('safeAreaChanged',updateSafeArea);
  tg?.onEvent?.('contentSafeAreaChanged',updateSafeArea);

  const reduceMotion=()=>window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches===true;

  function animatePage(pageEl,{initial=false}={}){
    if(!pageEl||reduceMotion())return;
    const items=[
      ...pageEl.querySelectorAll(
        ':scope > .page-intro, :scope > article, :scope > .section-title, :scope > .quick-grid, :scope > .page-section-head, :scope > .plans, :scope > .stats-grid, :scope > .menu-list, :scope > .promo-field, :scope > .faq'
      )
    ];
    items.forEach((el,index)=>{
      el.style.setProperty('--motion-index',String(Math.min(index,8)));
      el.classList.remove('motion-in');
    });
    // Force a fresh animation only on actual navigation / first paint.
    void pageEl.offsetWidth;
    items.forEach(el=>el.classList.add('motion-in'));
    if(initial){
      const header=document.querySelector('.topbar');
      header?.classList.remove('topbar-in');
      void header?.offsetWidth;
      header?.classList.add('topbar-in');
      const nav=document.querySelector('#bottomNav');
      nav?.classList.remove('nav-in');
      void nav?.offsetWidth;
      nav?.classList.add('nav-in');
    }
  }

  function pulseElement(el,className='micro-pop'){
    if(!el||reduceMotion())return;
    el.classList.remove(className);
    void el.offsetWidth;
    el.classList.add(className);
    window.setTimeout(()=>el.classList.remove(className),420);
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
    toast.timer=setTimeout(()=>el.classList.remove('show'),2300);
  }
  async function request(path,options={}){
    const headers={...(options.headers||{})};
    if(!(options.body instanceof FormData))headers['Content-Type']='application/json';
    if(tg?.initData)headers['X-Telegram-Init-Data']=tg.initData;
    const controller=new AbortController();
    const timer=setTimeout(()=>controller.abort(),path.includes("devices/reset")?35000:15000);
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
  function fmtTraffic(usedGb,limitGb){
    const used=Math.max(0,Number(usedGb||0));
    const limit=Math.max(0,Number(limitGb||0));
    const format=value=>{
      if(value>0&&value<0.1)return Math.max(1,Math.round(value*1024))+' МБ';
      return value.toLocaleString('ru-RU',{minimumFractionDigits:0,maximumFractionDigits:2})+' ГБ';
    };
    return format(used)+' / '+(limit>0?format(limit):'∞');
  }
  function deviceWord(count){
    const n=Math.max(1,Math.min(5,Number(count||1)));
    return n===1?'устройство':(n>=2&&n<=4?'устройства':'устройств');
  }
  function clampDevices(value){
    return Math.max(1,Math.min(5,Math.round(Number(value)||1)));
  }
  function selectedPlanTotalRub(){
    const plan=state.selectedPlan;
    if(!plan)return 0;
    const extra=Number(state.data?.shop?.extra_device_price_rub||50);
    return Math.max(0,Number(plan.rub||0)+(clampDevices(state.selectedDevices)-1)*extra);
  }
  function rubToStars(amount){
    const rub=Math.max(0,Math.round(Number(amount)||0));
    const xtr=Math.max(1,Number(state.data?.shop?.star_rate_xtr||50));
    const rateRub=Math.max(1,Number(state.data?.shop?.star_rate_rub||80));
    return rub===0?0:Math.ceil(rub*xtr/rateRub);
  }
  function paymentFinalRub(){
    const total=selectedPlanTotalRub();
    const percent=Math.max(0,Math.min(100,Number(state.promoPercent||0)));
    return total-Math.floor(total*percent/100);
  }
  function paymentDeviceHint(){
    const d=state.data;
    const selected=clampDevices(state.selectedDevices);
    const active=!!d?.subscription?.active;
    const current=clampDevices(d?.subscription?.max_devices||1);
    const extra=Number(d?.shop?.extra_device_price_rub||50);
    if(!active){
      if(selected===1)return '1 устройство включено в тариф.';
      return 'Доплата за устройства: +'+((selected-1)*extra).toLocaleString('ru-RU')+' ₽.';
    }
    const delta=selected-current;
    if(delta===0)return 'Лимит устройств останется без изменений.';
    if(delta>0)return 'Добавляем '+delta+' · цена +'+(delta*extra).toLocaleString('ru-RU')+' ₽ относительно текущего лимита.';
    return 'Убираем '+Math.abs(delta)+' · цена −'+(Math.abs(delta)*extra).toLocaleString('ru-RU')+' ₽ относительно текущего лимита.';
  }
  function updatePaymentDeviceUI({hapticTick=false}={}){
    if(!state.selectedPlan)return;
    const selected=clampDevices(state.selectedDevices);
    state.selectedDevices=selected;
    const range=$('#paymentDeviceRange');
    if(range){
      range.value=String(selected);
      range.style.setProperty('--range-progress',((selected-1)/4*100)+'%');
    }
    $('#paymentDeviceCount').textContent=String(selected);
    $('#paymentDeviceWord').textContent=deviceWord(selected);
    const active=!!state.data?.subscription?.active;
    const current=clampDevices(state.data?.subscription?.max_devices||1);
    $('#paymentDeviceCurrent').textContent=active?('Сейчас '+current):'От 1 до 5';
    $('#paymentDeviceHint').textContent=paymentDeviceHint();

    const total=selectedPlanTotalRub();
    const finalRub=paymentFinalRub();
    $('#sbpPrice').textContent=finalRub.toLocaleString('ru-RU')+' ₽';
    $('#starsPrice').textContent=rubToStars(finalRub).toLocaleString('ru-RU')+' Stars';

    if(state.promoPercent>0){
      const discount=total-finalRub;
      $('#paymentPromoResult').textContent=
        'Скидка −'+discount.toLocaleString('ru-RU')+' ₽ · итого '+finalRub.toLocaleString('ru-RU')+' ₽';
    }

    if(hapticTick){
      try{
        if(tg?.HapticFeedback?.selectionChanged)tg.HapticFeedback.selectionChanged();
        else haptic(selected===1||selected===5?'medium':'light');
      }catch(_){}
    }
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
      return 'Выбери тариф или пригласи друзей, чтобы снова получить доступ.';
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
    const headerStatus=$('#headerStatus');
    headerStatus.textContent=active?'Подписка активна':'Нет подписки';
    headerStatus.classList.toggle('active',active);

    const status=$('#subscriptionStatus');
    status.classList.toggle('active',active);
    $('b',status).textContent=active?'Активна':'Не активна';

    $('#homePlan').textContent=active?(d.subscription.plan||'MGN VPN'):'Нет подписки';
    $('#homePlanNote').textContent=subscriptionNote(d);
    $('#homeRemaining').textContent=active?fmtRemain(d.subscription.remaining_seconds):'—';
    $('#homeDeviceUsage').textContent=used+' из '+limit;
    $('#homeTraffic').textContent=active
      ? fmtTraffic(d.vpn.traffic_used_gb,d.vpn.traffic_limit_gb)
      : '—';
    $('#deviceProgress').style.width=pct+'%';
    $('b',$('#homeSubscriptionAction')).textContent=active?'Продлить VPN':'Купить VPN';

    $('#devicesActionNote').textContent=active?(used+' из '+limit+' устройств'):'Профиль и настройки';
    $('#plansActionNote').textContent=active?'Продлить VPN':'Купить VPN';

    $('#bonusActionNote').textContent=active?'Промокод и скидка':'Скидка или бесплатные дни';

    const hasLink=!!d.vpn.subscription_url;
    $('#copySubscriptionHome').disabled=!hasLink;
    $('#copySubscriptionInline').disabled=!hasLink;
    $('#openClientHome').disabled=!hasLink;
    $('#linkTitle').textContent=hasLink?'Ваш VPN готов':'Ссылка подключения';
    $('#linkActionNote').textContent=hasLink
      ? 'Персональная ссылка только для вашего аккаунта'
      : (active?'Появится после подключения VPN-сервера':'Доступна с активной подпиской');
    let masked='mgnvpn.ru/••••••••';
    if(hasLink){
      try{
        const parsed=new URL(d.vpn.subscription_url);
        masked=parsed.hostname+'/••••••••';
      }catch(_){}
    }else masked='ссылка пока недоступна';
    $('#linkMasked').textContent=masked;
    $('#vpnLinkCard').classList.toggle('unavailable',!hasLink);

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
      const featured=Boolean(plan.popular);
      return '<article class="plan-card '+(featured?'featured':'')+'">'+
        (featured?'<span class="plan-label">ПОПУЛЯРНЫЙ</span>':'')+
        '<div class="plan-info"><h3>'+esc(plan.name)+'</h3><p>1 устройство включено</p>'+
        (Number(plan.savings||0)>0?'<em>Выгода '+Number(plan.savings)+' ₽</em>':'')+'</div>'+
        '<div class="plan-price"><b>'+Number(plan.rub||0).toLocaleString('ru-RU')+' ₽</b><small>'+Number(plan.stars||0).toLocaleString('ru-RU')+' Stars</small></div>'+
        '<button type="button" data-buy="'+esc(plan.code)+'">'+(active?'Продлить VPN':'Купить VPN')+'</button>'+
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
    if($('#deviceCount'))$('#deviceCount').textContent=used;
    if($('#deviceLimit'))$('#deviceLimit').textContent=limit;
    if($('#deviceCapacityBar'))$('#deviceCapacityBar').style.width=Math.min(100,(used/limit)*100)+'%';
    if($('#deviceFreeSlots'))$('#deviceFreeSlots').textContent=Math.max(0,limit-used);
    $('#buyDevicePrice').textContent='+1 устройство · '+Number(d.shop.extra_device_price_rub||50)+' ₽';

    const root=$('#deviceList');
    const canRemove=Boolean(d.capabilities?.device_removal);
    const canReset=Boolean(d.capabilities?.device_reset);
    const resetButton=$('#resetDevicesPage');
    if(resetButton)resetButton.hidden=!(d.subscription.active&&canReset&&list.length);
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
          (id&&canRemove?'<button type="button" class="device-remove" aria-label="Отключить устройство" data-remove="'+encodeURIComponent(id)+'"><i data-lucide="trash-2"></i></button>':'')+
        '</article>';
      }).join('');
      $$('[data-remove]',root).forEach(btn=>btn.onclick=()=>removeDevice(decodeURIComponent(btn.dataset.remove)));
    }
  }

  function renderTrafficHistory(){
    const d=state.data;
    const history=d.vpn?.traffic_history||{};
    $('#trafficToday').textContent=fmtTraffic(Number(history.today_gb||0),0).replace(' / ∞','');
    $('#trafficWeek').textContent=fmtTraffic(Number(history.week_gb||0),0).replace(' / ∞','');
    $('#trafficMonth').textContent=fmtTraffic(Number(history.month_gb||0),0).replace(' / ∞','');

    const root=$('#trafficChart');
    if(!root)return;
    const days=Array.isArray(history.days)?history.days.slice(-30):[];
    const max=Math.max(0.001,...days.map(item=>Number(item.gb||0)));
    root.innerHTML=days.map((item,index)=>{
      const value=Math.max(0,Number(item.gb||0));
      const height=Math.max(3,Math.round((value/max)*100));
      const today=index===days.length-1;
      const title=esc((item.date||'')+' · '+fmtTraffic(value,0).replace(' / ∞',''));
      return '<span class="traffic-bar '+(today?'today':'')+'" title="'+title+'"><i style="height:'+height+'%"></i></span>';
    }).join('');
  }

  function renderProfile(){
    const d=state.data;
    setAvatar('profileAvatar',d.user);
    $('#profileName').textContent=d.user.first_name||'Пользователь';
    $('#profileUsername').textContent=d.user.username?('@'+d.user.username):'Telegram';
    $('#profileRewards').textContent='+'+Number(d.user.referral_rewards||0);
    $('#profileRefs').textContent=Number(d.user.referrals||0).toLocaleString('ru-RU');
    const used=(d.vpn.devices||[]).length;
    const limit=Math.max(1,Number(d.subscription.max_devices||1));
    $('#profileDevices').textContent=used+' / '+limit;
    $('#profileTraffic').textContent=d.subscription.active
      ? fmtTraffic(d.vpn.traffic_used_gb,d.vpn.traffic_limit_gb)
      : '—';
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
    if(!root)return;
    const clients=(state.data.clients||[]).filter(item=>['happ','incy'].includes(String(item.name||'').toLowerCase()));
    if(!clients.length){
      root.innerHTML='<div class="empty">Клиенты станут доступны после активации подписки.</div>';
      return;
    }
    root.innerHTML=clients.map(client=>{
      const target=client.redirect_url||client.import_url||client.download_url||'';
      return '<button type="button" data-client="'+esc(target)+'">'+
        '<span class="icon-box"><i data-lucide="shield-check"></i></span>'+
        '<span><b>'+esc(client.name||'VPN-клиент')+'</b><small>'+esc(client.platform||'VPN-клиент')+'</small></span>'+
        '<span>'+(client.supports_subscription_import?'Добавить':'Установить')+'</span></button>';
    }).join('');
    $$('[data-client]',root).forEach(button=>button.onclick=()=>openClientUrl(button.dataset.client||''));
  }

  function renderAgreement(){
    const root=$('#agreementContent');
    const agreement=state.data?.agreement;
    if(!root||!agreement)return;
    root.innerHTML=(agreement.sections||[]).map(section=>
      '<h3>'+esc(section.heading||'')+'</h3>'+
      (section.paragraphs||[]).map(value=>'<p>'+esc(value)+'</p>').join('')
    ).join('')+'<p><small>Редакция от '+esc(agreement.updated||'')+'.</small></p>';
  }

  function openClientUrl(url){
    if(!url)return;
    try{
      if(/^https?:/i.test(url)&&tg?.openLink)tg.openLink(url);
      else window.location.href=url;
    }catch(_){window.location.href=url}
  }


  let initialMotionDone=false;
  function render(){
    if(!state.data)return;
    $('.app-shell').inert=false;
    $('.app-shell').hidden=false;
    $('#appError').hidden=true;
    setAvatar('avatar',state.data.user);
    renderHome();
    renderPlans();
    try{state.sbpPayment=localStorage.getItem('mgn-payment-'+state.data.user.id)||state.sbpPayment}catch(_){}
    $('#pendingPaymentCheck').hidden=!state.sbpPayment;
    renderDevices();
    renderProfile();
    renderTrafficHistory();
    renderReferrals();
    renderBonuses();
    renderClients();
    renderAgreement();
    const adminEnabled=!!state.data?.admin?.enabled;
    $('#adminEntry').hidden=!adminEnabled;
    $('#adminRole').textContent=String(state.data?.admin?.role||'admin').toUpperCase();
    icons();
    const loader=$('#loader');
    loader.classList.add('hidden');
    if(!initialMotionDone){
      initialMotionDone=true;
      requestAnimationFrame(()=>animatePage($('.page.active'),{initial:true}));
    }
  }

  const adminNumber=value=>Number(value||0).toLocaleString('ru-RU');
  const adminMoney=value=>adminNumber(value)+' ₽';
  const adminShortDate=value=>{
    try{return new Intl.DateTimeFormat('ru-RU',{day:'2-digit',month:'2-digit'}).format(new Date(value))}catch(_){return '—'}
  };

  function renderAdminOverview(data){
    const o=data.overview||{},a=data.analytics||{},series=data.timeseries||[],plans=data.plans||[];
    $('#adminUsersTotal').textContent=adminNumber(o.total);
    $('#adminUsersNew').textContent='+'+adminNumber(o.new_7d)+' за 7 дней';
    $('#adminActiveTotal').textContent=adminNumber(o.active);
    $('#adminExpires').textContent=adminNumber(a.expires_3d)+' истекают за 3 дня';
    $('#adminPaidTotal').textContent=adminNumber(o.paid_total);
    $('#adminPaidActive').textContent=adminNumber(o.active_paid)+' активны';
    $('#adminRevenue').textContent=adminMoney(o.sbp_revenue);
    $('#adminRevenueMonth').textContent=adminMoney(a.rub_month)+' за 30 дней';
    const max=Math.max(1,...series.map(item=>Number(item.users||0)));
    $('#adminChart').innerHTML=series.map(item=>
      '<i class="admin-bar" style="--bar-height:'+Math.max(3,Math.round(Number(item.users||0)/max*100))+'%" data-value="'+adminNumber(item.users)+'" title="'+esc(item.date)+': '+adminNumber(item.users)+'"></i>'
    ).join('');
    $('#adminChartTotal').textContent='+'+adminNumber(series.reduce((sum,item)=>sum+Number(item.users||0),0));
    $('#adminChartStart').textContent=series.length?adminShortDate(series[0].date):'—';
    $('#adminChartEnd').textContent=series.length?adminShortDate(series[series.length-1].date):'—';
    const planMax=Math.max(1,...plans.map(item=>Number(item.users||0)));
    $('#adminBreakdown').innerHTML=plans.length?plans.map(item=>
      '<div class="admin-breakdown-row"><b>'+esc(item.name)+'</b><span>'+adminNumber(item.users)+'</span><i style="--fill:'+Math.round(Number(item.users||0)/planMax*100)+'%"></i></div>'
    ).join(''):'<p class="admin-empty">Активных подписок пока нет</p>';
  }

  async function loadAdminOverview(force=false){
    if(state.admin.loaded.overview&&!force)return;
    try{
      renderAdminOverview(await request('/api/miniapp/admin/overview?_='+Date.now()));
      state.admin.loaded.overview=true;
    }catch(error){toast(error.message);if(error.message==='Доступ запрещён')go('home')}
  }

  function renderAdminUsers(data){
    state.admin.userPages=Number(data.pages||1);
    state.admin.userPage=Number(data.page||0);
    $('#adminUsersCaption').textContent=adminNumber(data.total)+' в выборке';
    $('#adminUsersPage').textContent=(state.admin.userPage+1)+' / '+state.admin.userPages;
    $('#adminUsersPrev').disabled=state.admin.userPage<=0;
    $('#adminUsersNext').disabled=state.admin.userPage>=state.admin.userPages-1;
    $('#adminUsersTable').innerHTML=(data.users||[]).length?(data.users||[]).map(user=>{
      const name=user.first_name||user.username||('ID '+user.telegram_id);
      const handle=user.username?'@'+user.username:'ID '+user.telegram_id;
      const tags=(user.paid?'<span class="admin-tag paid">ОПЛАТИЛ</span>':'')+(user.granted?'<span class="admin-tag granted">ВЫДАНО</span>':'')+(user.active?'<span class="admin-tag online">АКТИВНА</span>':'<span class="admin-tag offline">НЕТ VPN</span>');
      return '<div class="admin-row"><div class="admin-row-main"><b>'+esc(name)+'</b><small>'+esc(handle)+' · '+esc(String(user.telegram_id))+'</small><div class="admin-tags">'+tags+'</div></div><div class="admin-row-side"><b>'+esc(user.plan_name||'Без тарифа')+'</b><small>'+(user.active?'до '+fmtDate(user.subscription_until):'с '+fmtDate(user.created_at))+'</small></div></div>';
    }).join(''):'<p class="admin-empty">Ничего не найдено</p>';
    icons();
  }

  async function loadAdminUsers(page=state.admin.userPage){
    const q=$('#adminUserSearch').value.trim();
    try{
      const url='/api/miniapp/admin/users?status='+encodeURIComponent(state.admin.userFilter)+'&page='+Math.max(0,page)+'&q='+encodeURIComponent(q)+'&_='+Date.now();
      renderAdminUsers(await request(url));
      state.admin.loaded.users=true;
    }catch(error){toast(error.message)}
  }

  function renderAdminPayments(data){
    const s=data.summary||{};
    $('#adminRubTotal').textContent=adminMoney(s.rub_total);
    $('#adminRubMonth').textContent=adminMoney(s.rub_month)+' за 30 дней';
    $('#adminStarsTotal').textContent=adminNumber(s.stars_total)+' ★';
    $('#adminStarsMonth').textContent=adminNumber(s.stars_month)+' ★ за 30 дней';
    const rows=data.payments||[];
    $('#adminPaymentsTable').innerHTML=rows.length?rows.map(item=>{
      const name=item.first_name||(item.username?'@'+item.username:'ID '+item.telegram_id);
      const amount=item.method==='СБП'?adminMoney(item.rub):adminNumber(item.stars)+' ★';
      const paid=item.status==='paid';
      return '<div class="admin-row"><div class="admin-row-main"><b>'+esc(name)+'</b><small>'+esc(item.method)+' · '+esc(item.plan_code||'—')+' · '+adminShortDate(item.created_at)+'</small></div><div class="admin-row-side"><b>'+amount+'</b><span class="admin-tag '+(paid?'paid':'granted')+'">'+(paid?'ОПЛАЧЕНО':'ОЖИДАЕТ')+'</span></div></div>';
    }).join(''):'<p class="admin-empty">Платежей пока нет</p>';
  }

  async function loadAdminPayments(force=false){
    if(state.admin.loaded.payments&&!force)return;
    try{renderAdminPayments(await request('/api/miniapp/admin/payments?_='+Date.now()));state.admin.loaded.payments=true}catch(error){toast(error.message)}
  }

  function renderAdminServers(data){
    const rows=data.servers||[];
    $('#adminServers').innerHTML=rows.length?rows.map(item=>
      '<div class="admin-server '+(item.available?'online':'offline')+'"><i class="admin-server-dot"></i><b>'+esc(item.name)+'</b><span>'+(item.latency_ms===null?'—':adminNumber(item.latency_ms)+' мс')+'</span></div>'
    ).join(''):'<p class="admin-empty">Серверы не найдены</p>';
  }

  async function loadAdminServers(force=false){
    if(state.admin.loaded.servers&&!force)return;
    $('#adminServers').innerHTML='<p class="admin-empty">Проверяем шесть узлов H1Cloud…</p>';
    try{renderAdminServers(await request('/api/miniapp/admin/servers?_='+Date.now()));state.admin.loaded.servers=true}catch(error){$('#adminServers').innerHTML='<p class="admin-empty">'+esc(error.message)+'</p>'}
  }

  function openAdminTab(tab){
    if(!['overview','users','payments','servers'].includes(tab))tab='overview';
    state.admin.tab=tab;
    $$('[data-admin-tab]').forEach(el=>el.classList.toggle('active',el.dataset.adminTab===tab));
    $$('[data-admin-panel]').forEach(el=>el.classList.toggle('active',el.dataset.adminPanel===tab));
    if(tab==='overview')loadAdminOverview();
    if(tab==='users')loadAdminUsers();
    if(tab==='payments')loadAdminPayments();
    if(tab==='servers')loadAdminServers();
    icons();haptic();
  }

  function go(page){
    if(page==='admin'&&!state.data?.admin?.enabled){toast('Доступ запрещён');page='home'}
    if(!$('.page[data-page="'+page+'"]'))page='home';
    if(page===state.page&&$('.page[data-page="'+page+'"]')?.classList.contains('active'))return;
    if(ROOT_PAGES.has(page))state.previousRoot=page;
    state.page=page;
    let activePage=null;
    $$('.page').forEach(el=>{
      const active=el.dataset.page===page;
      el.classList.toggle('active',active);
      if(active)activePage=el;
    });
    $$('#bottomNav button').forEach(el=>{
      const active=el.dataset.nav===page;
      el.classList.toggle('active',active);
      if(active)pulseElement(el,'nav-pop');
    });
    $('#bottomNav').style.display=ROOT_PAGES.has(page)?'grid':'none';
    window.scrollTo({top:0,behavior:'auto'});
    try{
      if(ROOT_PAGES.has(page))tg?.BackButton?.hide();
      else tg?.BackButton?.show();
    }catch(_){}
    haptic();
    icons();
    if(page==='support')loadSupport();
    if(page==='admin')openAdminTab(state.admin.tab||'overview');
    requestAnimationFrame(()=>animatePage(activePage));
  }

  async function load(silent=false){
    try{
      state.data=await request('/api/miniapp/me?_='+Date.now());
      render();
    }catch(error){
      $('#loader').classList.add('hidden');
      if(!state.data)showLoadError(error.message||'Не удалось загрузить данные');
      else if(!silent)toast(error.message||'Не удалось загрузить данные');
    }
  }

  let sheetReturnFocus=null;
  function showBackdrop(){
    sheetReturnFocus=document.activeElement;
    $('#sheetBackdrop').hidden=false;
    document.body.style.overflow='hidden';
    requestAnimationFrame(()=>document.querySelector('.sheet:not([hidden]) button:not([hidden])')?.focus());
  }
  function closeSheets(){
    $('#paymentSheet').hidden=true;
    $('#deviceSheet').hidden=true;
    $('#clientSheet').hidden=true;
    $('#sheetBackdrop').hidden=true;
    document.body.style.overflow='';
    sheetReturnFocus?.focus();
  }

  document.addEventListener('keydown',event=>{
    const sheet=document.querySelector('.sheet:not([hidden])');
    if(!sheet)return;
    if(event.key==='Escape'){closeSheets();return}
    if(event.key!=='Tab')return;
    const focusables=[...sheet.querySelectorAll('button:not(:disabled),input,a[href]')].filter(el=>el.getClientRects().length);
    const first=focusables[0],last=focusables[focusables.length-1];
    if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus()}
    else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus()}
  });

  function openPayment(code){
    const plan=state.data?.plans?.find(x=>x.code===code);
    if(!plan)return;
    closeSheets();
    state.selectedPlan=plan;
    state.promoCode='';
    state.promoPercent=0;
    const active=!!state.data?.subscription?.active;
    state.selectedDevices=active
      ? clampDevices(state.data?.subscription?.max_devices||1)
      : 1;

    $('#sheetTitle').textContent=(active?'Продление · ':'')+plan.name;
    const savings=Number(plan.savings||0);
    $('#sheetText').textContent=
      (active
        ? 'Выберите срок и количество устройств. Новые дни прибавятся к текущей подписке.'
        : 'Выберите количество устройств и способ оплаты.')+
      (savings>0?(' Выгода тарифа '+savings.toLocaleString('ru-RU')+' ₽.'):'');
    const savedCode=readSavedPromo();
    $('#paymentPromoCode').value=savedCode;
    $('#paymentPromoResult').textContent='';
    $('#paymentSheet').classList.remove('payment-pending');
    $('#checkPayment').hidden=true;
    $('#payStars').hidden=false;
    $('#paySbp').hidden=false;
    $('#paySbp').disabled=!state.data.payments.sbp_enabled;
    updatePaymentDeviceUI();
    $('#paymentSheet').hidden=false;
    showBackdrop();
    icons();
    haptic('medium');
    if(savedCode)applyPaymentPromo();
  }

  function openDeviceSheet(){
    const d=state.data;
    if(!d)return;
    closeSheets();
    const limit=Number(d.subscription.max_devices||1);
    const max=Number(d.shop.max_devices||5);
    $('#deviceSheetPrice').textContent=Number(d.shop.extra_device_price_rub||50)+' ₽';
    $('#deviceSheetBalance').textContent='Оплата через СБП';
    $('#confirmDevicePurchase').disabled=limit>=max||!d.payments.sbp_enabled;
    $('#confirmDevicePurchase').textContent=limit>=max?'Лимит устройств достигнут':'Добавить устройство';
    $('#deviceSheet').hidden=false;
    showBackdrop();
    icons();
    haptic('medium');
  }

  function openClientSheet(){
    if(!state.data?.vpn?.subscription_url){
      toast(state.data?.subscription?.active?'Клиенты пока недоступны':'Сначала активируй подписку');
      return;
    }
    closeSheets();
    renderClients();
    $('#clientSheet').hidden=false;
    showBackdrop();
    icons();
    haptic('medium');
  }

  function rememberPayment(id){
    state.sbpPayment=id;
    try{
      const key='mgn-payment-'+state.data.user.id;
      if(id)localStorage.setItem(key,id);else localStorage.removeItem(key);
    }catch(_){}
    $('#pendingPaymentCheck').hidden=!id;
  }

  function promoStorageKey(){
    return 'mgn-promo-'+String(state.data?.user?.id||state.data?.user?.telegram_id||'user');
  }
  function readSavedPromo(){
    try{return String(localStorage.getItem(promoStorageKey())||'').trim()}catch(_){return ''}
  }
  function rememberPromo(code){
    try{
      const key=promoStorageKey();
      if(code)localStorage.setItem(key,String(code).trim().toUpperCase());
      else localStorage.removeItem(key);
    }catch(_){}
  }

  async function payStars(){
    if(state.busy||!state.selectedPlan)return;
    state.busy=true; $('#payStars').disabled=true;
    try{
      const result=await request('/api/miniapp/payment/stars',{method:'POST',body:JSON.stringify({plan_code:state.selectedPlan.code,promo_code:state.promoCode,device_count:state.selectedDevices})});
      if(result.granted){notify();toast('Подписка активирована');closeSheets();await load(true);go('home');return}
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
      const result=await request('/api/miniapp/payment/sbp',{method:'POST',body:JSON.stringify({plan_code:state.selectedPlan.code,promo_code:state.promoCode,device_count:state.selectedDevices})});
      if(result.granted){notify();toast('Подписка активирована');closeSheets();await load(true);go('home');return}
      rememberPayment(result.payment_id);
      $('#paymentSheet').classList.add('payment-pending');
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
      if(result.status==='paid'){rememberPayment(null);notify();toast('Оплата получена');closeSheets();await load(true);go('home')}
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

  async function resetDevices(){
    let ok=true;
    const message='Сбросить все устройства? Старые VPN-конфигурации перестанут работать. Персональная ссылка MGN VPN останется прежней.';
    if(tg?.showConfirm)ok=await new Promise(resolve=>tg.showConfirm(message,resolve));
    else ok=window.confirm(message);
    if(!ok)return;
    try{
      await request('/api/miniapp/devices/reset',{method:'POST',body:'{}'});
      notify();toast('Все устройства сброшены');await load(true);
    }catch(error){notify('error');toast(error.message)}
  }

  async function buyExtraDevice(){
    if(state.busy)return;
    state.busy=true; $('#confirmDevicePurchase').disabled=true;
    try{
      const result=await request('/api/miniapp/shop/device',{method:'POST',body:'{}'});
      rememberPayment(result.payment_id);
      if(tg?.openLink)tg.openLink(result.pay_url); else window.open(result.pay_url,'_blank');
      closeSheets();
      go('profile');
      toast('После оплаты вернись в приложение — платёж проверится автоматически');
      [3000,8000,15000].forEach(delay=>setTimeout(()=>{
        if(state.sbpPayment===result.payment_id)checkSbp();
      },delay));
    }catch(error){notify('error');toast(error.message)}
    finally{state.busy=false;$('#confirmDevicePurchase').disabled=false}
  }

  async function applyPaymentPromo(){
    if(!state.selectedPlan)return;
    const code=$('#paymentPromoCode').value.trim();
    if(!code){
      state.promoCode='';
      state.promoPercent=0;
      rememberPromo('');
      $('#paymentPromoResult').textContent='';
      updatePaymentDeviceUI();
      return;
    }
    try{
      const quote=await request('/api/miniapp/promo/quote',{
        method:'POST',
        body:JSON.stringify({
          code,
          plan_code:state.selectedPlan.code,
          device_count:state.selectedDevices
        })
      });
      if(quote.type!=='discount')throw new Error('Этот код даёт бесплатные дни. Активируйте его в профиле.');
      state.promoCode=quote.code;
      state.promoPercent=Number(quote.value||0);
      rememberPromo(quote.code);
      updatePaymentDeviceUI();
      notify();
    }catch(error){
      state.promoCode='';
      state.promoPercent=0;
      rememberPromo('');
      $('#paymentPromoResult').textContent=error.message;
      updatePaymentDeviceUI();
    }
  }

  async function redeemPromo(){
    const code=$('#promoCodePage').value.trim();
    if(!code)return toast('Введите промокод');
    try{
      const result=await request('/api/miniapp/promo/redeem',{method:'POST',body:JSON.stringify({code})});
      if(result.type==='discount'){
        rememberPromo(result.code||code);
        $('#promoPageResult').textContent='Скидка '+Number(result.value||0)+'% сохранена и применится при покупке тарифа.';
        toast('Промокод сохранён');
      }else{
        rememberPromo('');
        $('#promoPageResult').textContent='Промокод активирован. Дни добавлены к подписке.';
        await load(true);
      }
      notify();
    }catch(error){$('#promoPageResult').textContent=error.message;notify('error')}
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
  async function loadSupport(){
    const list=$('#supportTickets');
    if(!list)return;
    try{
      const result=await request('/api/miniapp/support?page=0&_='+Date.now());
      const tickets=result.tickets||[];
      list.innerHTML=tickets.length?tickets.map(ticket=>
        '<button type="button" data-support-id="'+Number(ticket.id)+'"><span class="icon-box"><i data-lucide="message-square"></i></span><span><b>Обращение #'+Number(ticket.id)+'</b><small>'+esc(ticket.preview||'Без текста')+'</small></span><em>'+esc(ticket.status==='closed'?'Закрыто':'Открыто')+'</em></button>'
      ).join(''):'<p class="promo-hint">Обращений пока нет.</p>';
      icons();
      if(state.supportTicketId)await openSupportThread(state.supportTicketId);
    }catch(error){list.innerHTML='<p class="promo-hint">'+esc(error.message||'Не удалось загрузить обращения')+'</p>'}
  }

  async function openSupportThread(ticketId){
    try{
      const result=await request('/api/miniapp/support/'+Number(ticketId)+'?_='+Date.now());
      const ticket=result.ticket;
      state.supportTicketId=Number(ticket.id);
      const thread=$('#supportThread');
      thread.hidden=false;
      thread.innerHTML='<h3>Обращение #'+state.supportTicketId+'</h3>'+(ticket.messages||[]).map(item=>
        '<p><b>'+(item.sender_type==='admin'?'Поддержка':'Вы')+':</b> '+esc(item.text||(item.message_type==='photo'?'Фото':'Видео'))+'</p>'
      ).join('');
      const closed=ticket.status==='closed';
      $('#supportMessage').disabled=closed;
      $('#supportSubmit').hidden=closed;
      $('#supportSubmit').textContent='Отправить сообщение';
      $('#supportClose').hidden=closed;
      $('#supportResult').textContent=closed?'Обращение закрыто. Создать новое можно после возврата к списку.':'';
    }catch(error){toast(error.message||'Не удалось открыть обращение')}
  }

  function resetSupportComposer(){
    state.supportTicketId=null;
    $('#supportThread').hidden=true;
    $('#supportMessage').disabled=false;
    $('#supportSubmit').hidden=false;
    $('#supportSubmit').textContent='Создать обращение';
    $('#supportClose').hidden=true;
    $('#supportResult').textContent='';
  }

  async function closeSupport(){
    if(!state.supportTicketId||state.busy)return;
    state.busy=true;
    try{
      await request('/api/miniapp/support/'+state.supportTicketId+'/close',{method:'POST',body:'{}'});
      notify();
      await openSupportThread(state.supportTicketId);
      await loadSupport();
    }catch(error){notify('error');toast(error.message)}finally{state.busy=false}
  }

  async function submitSupport(){
    if(state.busy)return;
    const field=$('#supportMessage');
    const resultEl=$('#supportResult');
    const message=String(field?.value||'').trim();
    if(!message){
      if(resultEl)resultEl.textContent='Напишите сообщение';
      field?.focus();
      return;
    }
    state.busy=true;
    const button=$('#supportSubmit');
    if(button)button.disabled=true;
    try{
      const endpoint=state.supportTicketId
        ? '/api/miniapp/support/'+state.supportTicketId+'/messages'
        : '/api/miniapp/support';
      const result=await request(endpoint,{
        method:'POST',
        body:JSON.stringify({message})
      });
      if(field)field.value='';
      const counter=$('#supportCounter');if(counter)counter.textContent='0';
      state.supportTicketId=Number(result.ticket?.id||state.supportTicketId||0);
      if(resultEl)resultEl.textContent='Сообщение сохранено. Ответ придёт в Telegram.';
      notify();
      toast('Сообщение отправлено');
      await loadSupport();
    }catch(error){
      if(resultEl)resultEl.textContent=error.message||'Не удалось отправить обращение';
      notify('error');
    }finally{
      state.busy=false;
      if(button)button.disabled=false;
    }
  }

  function shareReferral(){
    const url=state.data?.user?.referral_url;if(!url)return;
    const share='https://t.me/share/url?url='+encodeURIComponent(url)+'&text='+encodeURIComponent('Подключай MGN VPN');
    try{tg?.openTelegramLink?tg.openTelegramLink(share):window.open(share,'_blank')}catch(_){window.open(share,'_blank')}
  }
  $$('[data-nav]').forEach(btn=>btn.addEventListener('click',()=>go(btn.dataset.nav)));
  $('#copySubscriptionHome').onclick=event=>{pulseElement(event.currentTarget);copySubscription()};
  $('#copySubscriptionInline').onclick=event=>{pulseElement(event.currentTarget);copySubscription()};
  $('#openClientHome').onclick=event=>{pulseElement(event.currentTarget);openClientSheet()};
  $('#copySubscriptionPlans').onclick=copySubscription;
  $('#buyDevicePage').onclick=openDeviceSheet;
  $('#resetDevicesPage').onclick=resetDevices;
  $('#copyReferral').onclick=()=>copyText(state.data?.user?.referral_url||'','Реферальная ссылка скопирована');
  $('#shareReferral').onclick=shareReferral;
  $('#copyId').onclick=()=>copyText(state.data?.user?.id||'','Telegram ID скопирован');

  $('#sheetClose').onclick=closeSheets;
  $('#deviceSheetClose').onclick=closeSheets;
  $('#clientSheetClose').onclick=closeSheets;
  $('#sheetBackdrop').onclick=closeSheets;
  $('#paymentDeviceRange').addEventListener('input',event=>{
    const next=clampDevices(event.target.value);
    if(next===state.selectedDevices)return;
    state.selectedDevices=next;
    updatePaymentDeviceUI({hapticTick:true});
  });
  $('#payStars').onclick=payStars;
  $('#paySbp').onclick=paySbp;
  $('#checkPayment').onclick=checkSbp;
  $('#pendingPaymentCheck').onclick=checkSbp;
  $('#confirmDevicePurchase').onclick=buyExtraDevice;
  $('#applyPaymentPromo').onclick=applyPaymentPromo;
  $('#redeemPromo').onclick=redeemPromo;
  $('#supportSubmit').onclick=submitSupport;
  $('#supportNew').onclick=resetSupportComposer;
  $('#supportClose').onclick=closeSupport;
  $('#supportTelegramMedia').onclick=()=>{
    const url=state.data?.bot_url||'https://t.me/mgnvpn_bot';
    try{tg?.openTelegramLink?tg.openTelegramLink(url):window.open(url,'_blank')}catch(_){window.open(url,'_blank')}
  };
  $('#supportTickets').addEventListener('click',event=>{
    const button=event.target.closest('[data-support-id]');
    if(button)openSupportThread(Number(button.dataset.supportId));
  });
  $('#supportMessage').addEventListener('input',event=>{
    const counter=$('#supportCounter');
    if(counter)counter.textContent=String(event.target.value.length);
    const result=$('#supportResult');
    if(result)result.textContent='';
  });
  $$('[data-admin-tab]').forEach(btn=>btn.addEventListener('click',()=>openAdminTab(btn.dataset.adminTab)));
  $('#adminUserFilters').addEventListener('click',event=>{
    const button=event.target.closest('[data-user-filter]');if(!button)return;
    state.admin.userFilter=button.dataset.userFilter;state.admin.userPage=0;
    $$('[data-user-filter]').forEach(el=>el.classList.toggle('active',el===button));
    loadAdminUsers(0);
  });
  $('#adminUserSearch').addEventListener('input',()=>{
    clearTimeout(state.admin.searchTimer);state.admin.userPage=0;
    state.admin.searchTimer=setTimeout(()=>loadAdminUsers(0),280);
  });
  $('#adminUsersPrev').onclick=()=>loadAdminUsers(state.admin.userPage-1);
  $('#adminUsersNext').onclick=()=>loadAdminUsers(state.admin.userPage+1);
  $('#adminServersRefresh').onclick=()=>loadAdminServers(true);

  document.addEventListener('pointerdown',event=>{
    const el=event.target.closest('button,[data-nav],.plan-card');
    if(!el||el.disabled||reduceMotion())return;
    el.classList.add('pressing');
  },{passive:true});
  const clearPress=event=>{
    const el=event.target.closest?.('button,[data-nav],.plan-card');
    el?.classList.remove('pressing');
  };
  document.addEventListener('pointerup',clearPress,{passive:true});
  document.addEventListener('pointercancel',clearPress,{passive:true});

  try{
    tg?.BackButton?.onClick(()=>{
      if(!$('#paymentSheet').hidden||!$('#deviceSheet').hidden||!$('#clientSheet').hidden){closeSheets();return}
      if(!ROOT_PAGES.has(state.page))go(state.previousRoot||'home');
      else go('home');
    });
  }catch(_){}

  document.addEventListener('visibilitychange',()=>{if(!document.hidden&&state.data)load(true)});

  function showLoadError(message){
    $('.app-shell').inert=true;
    $('.app-shell').hidden=true;
    $('#appError').hidden=false;
    $('#appErrorText').textContent=message;
    $('#retryLoad').hidden=!tg?.initData;
  }
  $('#retryLoad').onclick=()=>load();
  icons();
  if(!tg?.initData){
    $('#loader').classList.add('hidden');
    showLoadError('Открой Mini App внутри Telegram');
  }else{
    load();
  }
})();
