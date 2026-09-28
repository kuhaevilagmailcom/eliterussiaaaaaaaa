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

  var catalogCache=null;

  function showPrices(catalog){
    catalogCache=catalog||null;
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

    var select=document.getElementById('renew-plan');
    if(select){
      var current=select.value;
      select.innerHTML='';
      (catalog&&catalog.plans||[]).forEach(function(plan){
        var option=document.createElement('option');
        option.value=String(plan.code);
        option.textContent=String(plan.name)+' — '+Number(plan.price_rub).toLocaleString('ru-RU')+' ₽';
        select.appendChild(option);
      });
      if(current&&plans[current])select.value=current;
      if(!select.options.length){
        var empty=document.createElement('option');
        empty.value='';
        empty.textContent='Тарифы временно недоступны';
        select.appendChild(empty);
      }
    }
  }

  function paymentMessage(text,type){
    var node=document.getElementById('renew-status');
    if(!node)return;
    node.textContent=text||'';
    node.dataset.type=type||'';
  }

  function checkPayment(paymentId,attempt){
    if(!paymentId||attempt>100)return;
    window.setTimeout(function(){
      fetch('/api/public/payment/'+encodeURIComponent(paymentId),{headers:{Accept:'application/json'}})
        .then(function(response){
          return response.json().catch(function(){return {};}).then(function(body){
            if(!response.ok)throw new Error(body.message||'Не удалось проверить оплату');
            return body;
          });
        })
        .then(function(body){
          if(String(body.status||'').toLowerCase()==='paid'){
            paymentMessage('Оплата подтверждена. Подписка активирована ✅','success');
            return;
          }
          paymentMessage('Ожидаем подтверждение оплаты…','pending');
          checkPayment(paymentId,attempt+1);
        })
        .catch(function(){checkPayment(paymentId,attempt+1);});
    },3000);
  }

  var paymentForm=document.getElementById('id-payment-form');
  if(paymentForm){
    paymentForm.addEventListener('submit',function(event){
      event.preventDefault();
      var input=document.getElementById('renew-user-id');
      var select=document.getElementById('renew-plan');
      var button=document.getElementById('renew-submit');
      var userId=String(input&&input.value||'').trim();
      var planCode=String(select&&select.value||'').trim();
      if(!/^\d{5,19}$/.test(userId)){
        paymentMessage('Введите корректный Telegram ID.','error');
        return;
      }
      if(!planCode){
        paymentMessage('Выберите тариф.','error');
        return;
      }

      button.disabled=true;
      paymentMessage('Создаём оплату…','pending');
      fetch('/api/public/payment',{
        method:'POST',
        headers:{'Content-Type':'application/json',Accept:'application/json'},
        body:JSON.stringify({user_id:userId,plan_code:planCode})
      })
        .then(function(response){
          return response.json().catch(function(){return {};}).then(function(body){
            if(!response.ok)throw new Error(body.message||'Не удалось создать оплату');
            return body;
          });
        })
        .then(function(body){
          paymentMessage('Платёж создан. После оплаты подписка включится автоматически.','pending');
          if(body.pay_url)window.open(String(body.pay_url),'_blank','noopener,noreferrer');
          checkPayment(String(body.payment_id||''),0);
        })
        .catch(function(error){
          paymentMessage(error.message||'Не удалось создать оплату.','error');
        })
        .finally(function(){button.disabled=false;});
    });
  }
  fetch('/api/public/catalog',{headers:{Accept:'application/json'}})
    .then(function(response){if(!response.ok)throw new Error('catalog');return response.json();})
    .then(showPrices).catch(function(){showPrices(null);});
}());
