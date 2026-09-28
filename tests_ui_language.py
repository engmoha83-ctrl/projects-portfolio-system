"""فحص آليّ: لا حرف عربيّ واحد في الواجهة الإنجليزية."""
import asyncio
import os
import re
from playwright.async_api import async_playwright

AR = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
URL = "http://127.0.0.1:8090/admin-generator/2"

SCAN = """() => {
  const out = [];
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
    acceptNode: n => n.parentElement && n.parentElement.closest('script,style')
      ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT });
  let n; while(n = walk.nextNode()){ const t = n.nodeValue.trim(); if(t) out.push(t); }
  document.querySelectorAll('input,textarea').forEach(e => { if(e.value) out.push(e.value); });
  document.querySelectorAll('option').forEach(e => out.push(e.textContent));
  document.querySelectorAll('[placeholder]').forEach(e => out.push(e.placeholder));
  document.querySelectorAll('[title]').forEach(e => out.push(e.title));
  return out;
}"""


async def main():
    async with async_playwright() as p:
        exe = '/opt/pw-browsers/chromium'
        b = await p.chromium.launch(
            executable_path=exe if os.path.exists(exe) else None)
        ctx = await b.new_context(viewport={'width': 1500, 'height': 950})
        await ctx.add_cookies([{'name': 'super_admin_auth',
                                'value': 'admin_mohamed',
                                'domain': '127.0.0.1', 'path': '/'}])
        pg = await ctx.new_page()
        await pg.goto(URL, wait_until='networkidle')
        await pg.wait_for_timeout(2200)
        bad = []

        async def scan(where):
            for t in await pg.evaluate(SCAN):
                if AR.search(t):
                    bad.append((where, t[:70]))

        await scan('places')
        for tab in ('work', 'scope', 'rules', 'diff'):
            await pg.click(f'.tabs button[data-tab="{tab}"]')
            await pg.wait_for_timeout(900)
            await scan(tab)
        await pg.click('.tabs button[data-tab="places"]')
        await pg.wait_for_timeout(400)
        await pg.click('#btnLevels')
        await pg.wait_for_timeout(900)
        await scan('levels-modal')
        await pg.click('#lvX')
        await pg.wait_for_timeout(300)
        # رسالة خطأ: أكثر موضع تسرّب منه العربية قبلًا
        await pg.evaluate("msg('err','x')")
        await pg.click('#btnPreview')
        await pg.wait_for_timeout(3000)
        await scan('after-preview')

        print('ARABIC STRINGS IN RENDERED UI:', len(bad))
        for w, t in bad[:14]:
            print(f'   [{w}] {t}')
        await pg.screenshot(path='ar_clean.png')
        await b.close()

asyncio.run(main())
