(function(){
  var config=window.MGN_SITE_CONFIG||{};
  var bot=String(config.botUsername||'mgnvpn_bot').replace(/^@/,'');
  var support=String(config.supportUsername||bot).replace(/^@/,'');

  document.querySelectorAll('[data-bot-link]').forEach(function(link){
    link.href='https://t.me/'+bot+'?startapp';
    link.target='_blank'; link.rel='noopener noreferrer';
  });
  document.querySelectorAll('[data-support-link]').forEach(function(link){
    link.href='https://t.me/'+support;
    link.target='_blank'; link.rel='noopener noreferrer';
  });

  function showPrices(catalog){
    var plans={};
    (catalog&&catalog.plans||[]).forEach(function(plan){plans[String(plan.code)]=plan;});
    document.querySelectorAll('[data-plan-price]').forEach(function(node){
      var plan=plans[String(node.dataset.planPrice||'')];
      var price=plan&&plan.price_rub;
      node.textContent=Number.isFinite(Number(price))?Number(price).toLocaleString('ru-RU')+' ₽':'В Telegram';
    });
    document.querySelectorAll('[data-plan-label]').forEach(function(node){
      var plan=plans[String(node.dataset.planLabel||'')];
      var parts=[];
      if(plan&&plan.popular) parts.push('Популярный');
      if(Number(plan&&plan.savings_rub)>0) parts.push('выгода '+Number(plan.savings_rub).toLocaleString('ru-RU')+' ₽');
      node.textContent=parts.length?parts.join(' · '):'Выбрать';
    });
    document.querySelectorAll('[data-extra-device-price]').forEach(function(node){
      var price=catalog&&catalog.extra_device_price_rub;
      node.textContent=Number.isFinite(Number(price))?Number(price).toLocaleString('ru-RU')+' ₽':'в Telegram';
    });
  }
  fetch('/api/public/catalog',{headers:{Accept:'application/json'}})
    .then(function(response){if(!response.ok)throw new Error('catalog');return response.json();})
    .then(showPrices).catch(function(){showPrices(null);});
}());
