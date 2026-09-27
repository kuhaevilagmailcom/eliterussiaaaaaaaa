(function(){
  var config=window.MGN_SITE_CONFIG||{};
  var botUsername=String(config.botUsername||'mgnvpn_bot').replace(/^@/,'');
  var supportUsername=String(config.supportUsername||botUsername).replace(/^@/,'');
  function applyCatalog(catalog){
    var plans=Array.isArray(catalog&&catalog.plans)?catalog.plans:[];
    var byCode={};
    plans.forEach(function(plan){byCode[String(plan.code)]=plan;});
    document.querySelectorAll('[data-plan-price]').forEach(function(element){
      var plan=byCode[String(element.dataset.planPrice||'')];
      element.textContent=plan?Number(plan.price_rub).toLocaleString('ru-RU')+' ₽':'Открыть в Telegram';
    });
    document.querySelectorAll('[data-device-limit]').forEach(function(element){
      element.textContent=String(Number(catalog&&catalog.max_devices||5));
    });
  }
  fetch('/api/public/catalog',{headers:{Accept:'application/json'}})
    .then(function(response){if(!response.ok)throw new Error('catalog');return response.json();})
    .then(applyCatalog)
    .catch(function(){applyCatalog(null);});

  document.querySelectorAll('[data-bot-link]').forEach(function(link){
    link.href='https://t.me/'+botUsername+'?startapp';
    link.target='_blank';
    link.rel='noopener noreferrer';
  });

  document.querySelectorAll('[data-support-link]').forEach(function(link){
    link.href='https://t.me/'+supportUsername;
    link.target='_blank';
    link.rel='noopener noreferrer';
  });

  var reducedMotion=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if(!reducedMotion&&'IntersectionObserver' in window){
    var observer=new IntersectionObserver(function(entries){
      entries.forEach(function(entry){
        if(entry.isIntersecting){
          entry.target.classList.add('is-visible');
          observer.unobserve(entry.target);
        }
      });
    },{threshold:.12});
    document.querySelectorAll('.reveal').forEach(function(element){observer.observe(element);});
  }else{
    document.querySelectorAll('.reveal').forEach(function(element){element.classList.add('is-visible');});
  }
}());
