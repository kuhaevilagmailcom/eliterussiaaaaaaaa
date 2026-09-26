(function(){
  var config=window.MGN_SITE_CONFIG||{};
  var botUsername=String(config.botUsername||'mgnvpn_bot').replace(/^@/,'');
  var supportUsername=String(config.supportUsername||botUsername).replace(/^@/,'');
  var maxDevices=Number(config.maxDevices||5);

  document.querySelectorAll('[data-device-limit]').forEach(function(element){
    element.textContent=String(maxDevices);
  });

  document.querySelectorAll('[data-bot-link]').forEach(function(link){
    link.href='https://t.me/'+botUsername+'/mgnvpn';
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