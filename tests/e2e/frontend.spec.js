const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '../..');
const read = relative => fs.readFileSync(path.join(root, relative));

async function routeFiles(page, miniApp = false) {
  if(miniApp){
    page.on('pageerror',error=>console.log('[MINIAPP JS ERROR]',error.stack||error.message));
    page.on('console',message=>{if(message.type()==='error')console.log('[MINIAPP CONSOLE]',message.text())});
  }
  await page.route('https://telegram.org/js/telegram-web-app.js', route => route.fulfill({
    contentType: 'application/javascript',
    body: `window.Telegram={WebApp:{initData:'test-init-data',ready(){},expand(){},
      setHeaderColor(){},setBackgroundColor(){},enableClosingConfirmation(){},
      BackButton:{show(){},hide(){},onClick(){}},HapticFeedback:{impactOccurred(){},notificationOccurred(){}},
      openLink(){},openTelegramLink(){},openInvoice(){}}};`
  }));
  await page.route('http://mgn.test/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/public/catalog') return route.fulfill({ json: {
      plans: ['30','90','180','365'].map((code, index) => ({code, price_rub: [70,170,370,777][index]})),
      max_devices: 5,
      extra_device_price_rub: 50
    }});
    if (url.pathname === '/api/miniapp/admin/gifts') return route.fulfill({json:{
      ok:true,days:30,link:'https://t.me/mgnvpn_bot?start=g_'+('a'.repeat(43)),
      qr_image:'data:image/png;base64,iVBORw0KGgo=',expires_at:'2026-11-12T00:00:00Z'
    }});
    if (url.pathname === '/api/miniapp/gifts/redeem') return route.fulfill({json:{
      ok:true,days:30,label:'1 месяц',subscription_until:'2027-02-01T00:00:00Z'
    }});
    if (url.pathname === '/api/miniapp/promo/redeem') return route.fulfill({json:{
      ok:true,type:'discount',code:'MGN20',value:20
    }});
    if (url.pathname === '/api/miniapp/promo/quote') return route.fulfill({json:{
      code:'MGN20',type:'discount',value:20,original_price:70,discount:14,final_price:56,device_count:2
    }});
    if (url.pathname === '/api/miniapp/admin/overview') return route.fulfill({json:{
      role:'owner',overview:{total:128,active:91,paid_total:74,active_paid:62,sbp_revenue:14800,new_7d:12},
      analytics:{expires_3d:7,rub_month:14800},timeseries:Array.from({length:14},(_,i)=>({date:`2026-09-${String(i+10).padStart(2,'0')}`,users:i%4,rub:i*100,stars:0,payments:i%3})),plans:[{name:'30 дней',users:60},{name:'90 дней',users:31}]
    }});
    if (url.pathname === '/api/miniapp/admin/users') return route.fulfill({json:{users:[{telegram_id:42,first_name:'Тест',username:'test',created_at:'2026-09-01T00:00:00Z',subscription_until:'2027-01-01T00:00:00Z',plan_name:'30 дней',active:1,paid:1,granted:0}],total:1,page:0,page_size:20,pages:1}});
    if (url.pathname === '/api/miniapp/admin/payments') return route.fulfill({json:{summary:{rub_total:14800,stars_total:200,rub_month:14800,stars_month:200},payments:[{method:'СБП',telegram_id:42,plan_code:'30',rub:100,stars:0,status:'paid',created_at:'2026-09-26T00:00:00Z',first_name:'Тест'}]}});
    if (url.pathname === '/api/miniapp/admin/servers') return route.fulfill({json:{servers:['🇳🇱 Нидерланды','🇵🇰 Пакистан','🇹🇷 Турция','🇩🇪 Германия','🇺🇸 США 1','🇫🇮 Финляндия'].map(name=>({name,available:true,latency_ms:100}))}});
    if (url.pathname === '/api/miniapp/me') return route.fulfill({ json: {
      user: {id:42,first_name:'Тест',username:'test',telegram_id:42,referrals:0,referral_rewards:0},
      subscription: {active:true,plan:'1 месяц',until:'2027-01-01T00:00:00+00:00',remaining_seconds:2592000,max_devices:2},
      vpn: {ready:true, ok:true, server:'MGN VPN', subscription_url:'https://mgn.test/sub/token', traffic_used_gb:0, traffic_limit_gb:0, devices:[]},
      plans: [{code:'30',name:'1 месяц',days:30,devices:2,rub:70,stars:44,savings:0}],
      payments: {sbp_enabled:true}, capabilities:{device_list:true,device_removal:false,device_reset:true},
      admin: {enabled:true,role:'owner'},
      clients:[
        {name:'Happ',platform:'Android · iOS',redirect_url:'https://mgn.test/client/happ/token',supports_subscription_import:true},
        {name:'INCY',platform:'Android · iOS',redirect_url:'https://mgn.test/client/incy/token',supports_subscription_import:true}
      ], shop:{extra_device_price_rub:50,extra_device_price_stars:32,max_devices:5},
      agreement:{updated:'27 сентября 2026 года',sections:[{heading:'1. Общие положения',paragraphs:['Условия использования MGN VPN.']}]},
      bot_url:'https://t.me/mgnvpn_bot'
    }});
    const files = miniApp ? {
      '/app': ['miniapp/web/index.html','text/html'],
      '/static/app.js': ['miniapp/web/app.js','application/javascript'],
      '/static/styles.css': ['miniapp/web/styles.css','text/css'],
      '/static/assets/lucide.min.js': ['miniapp/web/assets/lucide.min.js','application/javascript'],
      '/static/assets/Manrope-Variable.ttf': ['miniapp/web/assets/Manrope-Variable.ttf','font/ttf'],
      '/static/assets/mgn-vpn-logo.webp': ['miniapp/web/assets/mgn-vpn-logo.webp','image/webp']
    } : {
      '/': ['miniapp/web/site/index.html','text/html'],
      '/agreement': ['miniapp/web/site/agreement.html','text/html'],
      '/privacy': ['miniapp/web/site/privacy.html','text/html'],
      '/static/site/app.js': ['miniapp/web/site/app.js','application/javascript'],
      '/static/site/config.js': ['miniapp/web/site/config.js','application/javascript'],
      '/static/site/styles.css': ['miniapp/web/site/styles.css','text/css'],
      '/static/assets/mgn-vpn-logo.webp': ['miniapp/web/assets/mgn-vpn-logo.webp','image/webp']
    };
    const match = files[url.pathname];
    if (match) {
      var body=read(match[0]);
      if(!miniApp){
        body=Buffer.from(body.toString()
          .replace('{{AGREEMENT_UPDATED}}','27 сентября 2026 года')
          .replace('{{AGREEMENT_HTML}}','<section class="agreement-part"><h3>1. Общие положения</h3><p>Условия использования MGN VPN.</p></section>')
          .replace('{{PRIVACY_UPDATED}}','27 сентября 2026 года')
          .replace('{{PRIVACY_HTML}}','<section class="agreement-part"><h3>1. Общие положения</h3><p>Политика конфиденциальности MGN VPN.</p></section>'));
      }
      return route.fulfill({body,contentType:match[1]});
    }
    return route.fulfill({status:204,body:''});
  });
}

for (const viewport of [
  {width:320,height:700},{width:375,height:812},{width:390,height:844},
  {width:430,height:932},{width:768,height:1024},{width:1440,height:900}
]) test(`public site ${viewport.width}px`, async ({page}) => {
  const errors=[]; page.on('pageerror', error => errors.push(error.message));
  await page.setViewportSize(viewport); await routeFiles(page); await page.goto('http://mgn.test/');
  await expect(page.locator('[data-plan-price="30"]')).toHaveText('70 ₽');
  await expect(page.locator('.logo img')).toBeVisible();
  await expect(page.locator('main .agreement-part')).toHaveCount(0);
  await expect(page.locator('footer a[href="/agreement"]')).toBeVisible();
  await expect(page.locator('[data-bot-link]').first()).toHaveAttribute('href', /t\.me\/mgnvpn_bot/);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  expect(errors).toEqual([]);
});

test('legal pages are separate from the landing page', async ({page}) => {
  await page.setViewportSize({width:390,height:844}); await routeFiles(page);
  await page.goto('http://mgn.test/agreement');
  await expect(page.locator('h1')).toContainText('Пользовательское');
  await expect(page.locator('.agreement-part')).toHaveCount(1);
  await page.goto('http://mgn.test/privacy');
  await expect(page.locator('h1')).toContainText('Политика');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
});

test('Mini App loads and navigates core screens', async ({page}) => {
  const errors=[]; page.on('pageerror', error => errors.push(error.message));
  await page.setViewportSize({width:390,height:844}); await routeFiles(page, true);
  await page.goto('http://mgn.test/app');
  await expect(page.locator('#loader')).toHaveClass(/hidden/,{timeout:3500});
  await expect(page.locator('.app-shell')).toBeVisible();
  await page.waitForTimeout(500);
  await expect(page.locator('#bottomNav button')).toHaveCount(4);
  if (process.env.MGN_APP_SCREENSHOT_DIR) {
    await page.screenshot({path:path.join(process.env.MGN_APP_SCREENSHOT_DIR,'home.png')});
  }
  for (const name of ['plans','profile','bonuses','support']) {
    await page.locator('[data-nav="home"]:visible').first().click();
    await page.locator(`[data-nav="${name}"]:visible`).first().click();
    await expect(page.locator(`[data-page="${name}"]`)).toHaveClass(/active/);
    await page.waitForTimeout(500);
    if (process.env.MGN_APP_SCREENSHOT_DIR) {
      await page.screenshot({path:path.join(process.env.MGN_APP_SCREENSHOT_DIR,`${name}.png`)});
    }
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  expect(errors).toEqual([]);
});


test('Mini App checkout fills the viewport and keeps payment actions visible', async ({page}) => {
  const errors=[]; page.on('pageerror', error => errors.push(error.message));
  await page.emulateMedia({reducedMotion:'reduce'});
  await routeFiles(page, true);
  for (const viewport of [{width:320,height:700},{width:390,height:844}]) {
    await page.setViewportSize(viewport);
    await page.goto('http://mgn.test/app');
  await expect(page.locator('#loader')).toHaveClass(/hidden/,{timeout:3500});

    await page.locator('[data-nav="plans"]:visible').first().click();
    await page.locator('[data-buy="30"]').click();
    const sheet=page.locator('#paymentSheet');
    await expect(sheet).toBeVisible();

    const box=await sheet.boundingBox();
    expect(box).not.toBeNull();
    expect(box.x).toBe(0);
    expect(box.y).toBe(0);
    expect(box.width).toBe(viewport.width);
    expect(box.height).toBe(viewport.height);

    const center=box.x+box.width/2;
    expect(Math.abs(center-viewport.width/2)).toBeLessThanOrEqual(3);
    for (const selector of ['#payStars','#paySbp']) {
      const action=page.locator(selector);
      await expect(action).toBeVisible();
      const actionBox=await action.boundingBox();
      expect(actionBox.y+actionBox.height).toBeLessThanOrEqual(viewport.height);
    }
    const promoBox=await page.locator('#applyPaymentPromo').boundingBox();
    const firstPaymentBox=await page.locator('#payStars').boundingBox();
    expect(firstPaymentBox.y-(promoBox.y+promoBox.height)).toBeLessThanOrEqual(80);
    await expect(page.locator('#payStars b')).toHaveText('Telegram Stars');
    await expect(page.locator('#paymentSheet')).toHaveCSS('font-family',/Manrope/);

    await page.evaluate(() => {
      document.querySelector('#paymentSheet').classList.add('payment-pending');
      document.querySelector('#checkPayment').hidden=false;
    });
    await expect(page.locator('#checkPayment')).toBeVisible();
    const checkBox=await page.locator('#checkPayment').boundingBox();
    expect(checkBox.y+checkBox.height).toBeLessThanOrEqual(viewport.height);

    if (process.env.MGN_CHECKOUT_SCREENSHOT && viewport.width===390) {
      await page.evaluate(() => {
        document.querySelector('#paymentSheet').classList.remove('payment-pending');
        document.querySelector('#checkPayment').hidden=true;
      });
      await page.screenshot({path:process.env.MGN_CHECKOUT_SCREENSHOT});
    }
  }
  expect(errors).toEqual([]);
});

test('Mini App lets an active user choose Happ or INCY', async ({page}) => {
  await page.setViewportSize({width:390,height:844});
  await routeFiles(page, true);
  await page.goto('http://mgn.test/app');
  await expect(page.locator('#loader')).toHaveClass(/hidden/,{timeout:3500});
  await page.locator('#openClientHome').click();
  await expect(page.locator('#clientSheet')).toBeVisible();
  await expect(page.locator('#clientList [data-client]')).toHaveCount(2);
  await expect(page.locator('#clientList')).toContainText('Happ');
  await expect(page.locator('#clientList')).toContainText('INCY');
});

test('Mini App saves a discount promo from Bonuses and carries it into checkout', async ({page}) => {
  await page.setViewportSize({width:390,height:844});
  await routeFiles(page, true);
  await page.goto('http://mgn.test/app');
  await expect(page.locator('#loader')).toHaveClass(/hidden/,{timeout:3500});

  await page.locator('#bottomNav [data-nav="bonuses"]').click();
  await page.locator('#promoCodePage').fill('mgn20');
  await page.locator('#redeemPromo').click();
  await expect(page.locator('#promoPageResult')).toContainText('Скидка 20% сохранена');

  await page.locator('#bottomNav [data-nav="plans"]').click();
  await page.locator('[data-buy="30"]').click();
  await expect(page.locator('#paymentPromoCode')).toHaveValue('MGN20');
  await expect(page.locator('#paymentPromoResult')).toContainText('итого 56 ₽');

  await page.locator('#sheetClose').click();
  await page.locator('#bottomNav [data-nav="home"]').click();
  const icon=page.locator('#copySubscriptionHome svg');
  const label=page.locator('#copySubscriptionHome span');
  await expect(icon).toBeVisible();
  const iconRect=await icon.boundingBox();
  const labelRect=await label.boundingBox();
  expect(labelRect.x-(iconRect.x+iconRect.width)).toBeGreaterThanOrEqual(5);
});

test('Mini App admin dashboard is available to an owner and stays compact', async ({page}) => {
  const errors=[]; page.on('pageerror', error => errors.push(error.message));
  await page.setViewportSize({width:390,height:844}); await routeFiles(page, true); await page.goto('http://mgn.test/app');
  await expect(page.locator('#loader')).toHaveClass(/hidden/,{timeout:3500});
  await page.locator('[data-nav="profile"]').first().click();
  await expect(page.locator('#adminEntry')).toBeVisible();
  await page.locator('#adminEntry').click();
  await expect(page.locator('[data-page="admin"]')).toHaveClass(/active/);
  await expect(page.locator('#adminUsersTotal')).toHaveText('128');
  await page.locator('[data-admin-tab="users"]').click();
  await expect(page.locator('#adminUsersTable')).toContainText('Тест');
  await page.locator('[data-admin-tab="servers"]').click();
  await expect(page.locator('#adminServers')).toContainText('Нидерланды');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  expect(errors).toEqual([]);
});


test('MGN checkout includes 2 devices and charges 50 RUB for each extra slot', async ({page}) => {
  await page.setViewportSize({width:390,height:844});
  await routeFiles(page,true);
  await page.goto('http://mgn.test/app');
  await expect(page.locator('#loader')).toHaveClass(/hidden/,{timeout:3500});
  await page.locator('[data-nav="plans"]:visible').first().click();
  await page.locator('[data-buy="30"]').click();
  const range=page.locator('#paymentDeviceRange');
  await expect(range).toHaveAttribute('min','2');
  await expect(range).toHaveAttribute('max','5');
  await expect(range).toHaveValue('2');
  await expect(page.locator('#sbpPrice')).toHaveText('70 ₽');

  await range.evaluate(input=>{
    input.value='5';
    input.dispatchEvent(new Event('input',{bubbles:true}));
  });
  await expect(page.locator('#sbpPrice')).toHaveText('220 ₽');
  await expect(page.locator('#paymentDeviceCount')).toHaveText('5');
  await expect(page.locator('#starsPrice')).toHaveText('138 Stars');
});


test('Admin can generate a gift QR and user can redeem it from Bonus scanner', async ({page})=>{
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.setViewportSize({width:390,height:844});
  await routeFiles(page,true);
  await page.goto('http://mgn.test/app');
  await expect(page.locator('#loader')).toHaveClass(/hidden/,{timeout:3500});
  await page.locator('#bottomNav [data-nav="profile"]').click();
  await page.locator('#adminEntry').click();
  await page.locator('[data-admin-tab="gifts"]').click();
  await page.locator('[data-gift-days="30"]').click();
  await expect(page.locator('#adminGiftQrImage')).toHaveAttribute('src',/^data:image\/png;base64,/);
  await expect(page.locator('#adminGiftLink')).toHaveAttribute('href',/t\.me\/mgnvpn_bot\?start=g_/);

  await page.evaluate(()=>{
    window.Telegram.WebApp.showScanQrPopup=(options,callback)=>{
      callback('https://t.me/mgnvpn_bot?start=g_'+('a'.repeat(43)));
    };
    window.Telegram.WebApp.closeScanQrPopup=()=>{};
  });
  await page.locator('#bottomNav button[data-nav="bonuses"]').evaluate(el=>{
    // Inner admin page has hidden bottom bar; use existing navigation event.
    el.click();
  });
  await page.waitForTimeout(200);
  await page.locator('#scanGiftQr').click();
  await expect(page.locator('#giftRedeemResult')).toContainText('Вы получили подарок MGN VPN');
  expect(errors).toEqual([]);
});

test('Mini App servers shows H1 Turkey and Germany, not sold Poland or USA2', async ({page})=>{
  await page.setViewportSize({width:390,height:844});
  await routeFiles(page,true);
  await page.goto('http://mgn.test/app');
  await expect(page.locator('#loader')).toHaveClass(/hidden/);
  await page.locator('#bottomNav [data-nav="profile"]').click();
  await page.locator('#adminEntry').click();
  await page.locator('[data-admin-tab="servers"]').click();
  const servers=page.locator('#adminServers');
  await expect(servers).toContainText('Турция');
  await expect(servers).toContainText('Германия');
  await expect(servers).not.toContainText('Польша');
  await expect(servers).not.toContainText('США 2');
  await expect(servers.locator('.admin-server')).toHaveCount(6);
});
