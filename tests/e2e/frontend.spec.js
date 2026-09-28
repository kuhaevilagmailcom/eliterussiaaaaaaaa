const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '../..');
const read = relative => fs.readFileSync(path.join(root, relative));

async function routeFiles(page, miniApp = false) {
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
      plans: ['30','90','180','365'].map((code, index) => ({code, price_rub: [100,349,599,1200][index]})),
      max_devices: 5,
      extra_device_price_rub: 100
    }});
    if (url.pathname === '/api/miniapp/me') return route.fulfill({ json: {
      user: {first_name:'Тест', username:'test', telegram_id:42},
      subscription: {active:true, plan_name:'1 месяц', until:'2027-01-01T00:00:00+00:00', days_left:30, max_devices:1},
      vpn: {ready:true, ok:true, server:'MGN VPN', subscription_url:'https://mgn.test/sub/token', traffic_used_gb:0, traffic_limit_gb:0, devices:[]},
      plans: [{code:'30',name:'1 месяц',days:30,devices:1,rub:100,stars:63,savings:0}],
      payments: {sbp_enabled:true}, capabilities:{device_list:true,device_removal:false,device_reset:true},
      clients:[
        {name:'Happ',platform:'Android · iOS',redirect_url:'https://mgn.test/client/happ/token',supports_subscription_import:true},
        {name:'INCY',platform:'Android · iOS',redirect_url:'https://mgn.test/client/incy/token',supports_subscription_import:true}
      ], shop:{extra_device_price_rub:100,extra_device_price_stars:63,max_devices:5},
      agreement:{updated:'27 сентября 2026 года',sections:[{heading:'1. Общие положения',paragraphs:['Условия использования MGN VPN.']}]},
      bot_url:'https://t.me/mgnvpn_bot'
    }});
    const files = miniApp ? {
      '/app': ['miniapp/web/index.html','text/html'],
      '/static/app.js': ['miniapp/web/app.js','application/javascript'],
      '/static/styles.css': ['miniapp/web/styles.css','text/css'],
      '/static/assets/lucide.min.js': ['miniapp/web/assets/lucide.min.js','application/javascript']
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
  await expect(page.locator('[data-plan-price="30"]')).toHaveText('100 ₽');
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
  await expect(page.locator('.app-shell')).toBeVisible();
  for (const name of ['plans','profile','bonuses','support']) {
    await page.locator('[data-nav="home"]:visible').first().click();
    await page.locator(`[data-nav="${name}"]:visible`).first().click();
    await expect(page.locator(`[data-page="${name}"]`)).toHaveClass(/active/);
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  expect(errors).toEqual([]);
});


test('Mini App payment sheet stays inside desktop viewport with reduced motion', async ({page}) => {
  const errors=[]; page.on('pageerror', error => errors.push(error.message));
  await page.setViewportSize({width:390,height:844});
  await page.emulateMedia({reducedMotion:'reduce'});
  await routeFiles(page, true);
  await page.goto('http://mgn.test/app');

  await page.locator('[data-nav="plans"]:visible').first().click();
  await page.locator('[data-buy="30"]').click();
  const sheet=page.locator('#paymentSheet');
  await expect(sheet).toBeVisible();

  const box=await sheet.boundingBox();
  expect(box).not.toBeNull();
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.y).toBeGreaterThanOrEqual(0);
  expect(box.x+box.width).toBeLessThanOrEqual(390);
  expect(box.y+box.height).toBeLessThanOrEqual(844);

  const center=box.x+box.width/2;
  expect(Math.abs(center-195)).toBeLessThanOrEqual(3);
  expect(errors).toEqual([]);
});

test('Mini App lets an active user choose Happ or INCY', async ({page}) => {
  await page.setViewportSize({width:390,height:844});
  await routeFiles(page, true);
  await page.goto('http://mgn.test/app');
  await page.locator('#openClientHome').click();
  await expect(page.locator('#clientSheet')).toBeVisible();
  await expect(page.locator('#clientList [data-client]')).toHaveCount(2);
  await expect(page.locator('#clientList')).toContainText('Happ');
  await expect(page.locator('#clientList')).toContainText('INCY');
});
