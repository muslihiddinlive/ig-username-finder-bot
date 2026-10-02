# IG Username Finder Bot (aiogram 3)

Bo'sh Instagram username qidiruvchi bot. Stars bilan to'lov, **faqat topilgan (tasdiqlangan) username uchun yechiladi**.
DB: Telegram kanal (pinned `snapshot.json`), kerakli joyda RAM.

## Ishga tushirish
1. @BotFather'dan bot oching. Alohida **private kanal** yarating, botni admin qiling (Post / Pin / Delete messages). Kanal ID'si `-100…`.
2. `cp .env.example .env`, to'ldiring (`BOT_TOKEN`, `DB_CHANNEL_ID`, `SUPERADMIN_IDS`, `APIFY_TOKEN`).
3. `pip install -r requirements.txt && set -a && . ./.env && set +a && python main.py`
4. Admin bo'lib `/selftest` yuboring: tekshiruv aniqligini ko'rsatadi. **Bunisiz ishga tushirmang.**

Render free: Web Service, start `python main.py`, env o'zgaruvchilarni kiriting; uxlab qolmasligi uchun `/` ga tashqi ping (masalan cron-job.org).

## Buyruqlar
Admin: `/setprice N`, `/setpackages 10,25,50`, `/grant ID N`, `/stats`, `/selftest`
Superadmin: `/setvipprice N`, `/setvipdays N`, `/addadmin ID`, `/deladmin ID`

## Tekshiruv zanjiri (`CHECKERS`)
- `apify` — Apify aktori (`sync-network/username-availability-checker`), natijasi "tasdiqlangan" hisoblanadi.
- `probe` — oddiy HTTP so'rov, faqat oldindan filtr (band nomlarni Apify'ga yubormaydi). Natijasi hech qachon pul yechmaydi.
- Tavsiya: `CHECKERS=probe,apify`. Oylik Apify sarfi `APIFY_MONTHLY_BUDGET` bilan cheklanadi.

## Cheklovlar
- Kanal DB: bitta instans ishlasin; to'lov har doim darrov snapshotga yoziladi.
- Bir xil bo'sh nomni ikki userga sotib yuborish mumkin (global "sotildi" ro'yxati yo'q).
- Testlar: `python -m pytest tests`

## Render.com'ga deploy (free)
1. Kodni GitHub'ga push qiling (`.env` push qilinmaydi, `.gitignore` da).
2. Render → **New → Blueprint** → reponi tanlang. `render.yaml` o'qiladi.
3. `sync: false` bo'lgan o'zgaruvchilarni qo'lda kiriting: `BOT_TOKEN`, `DB_CHANNEL_ID`, `SUPERADMIN_IDS`, `APIFY_TOKEN` (bAI uchun `AI_*`).
4. Deploy tugagach `https://<nom>.onrender.com/health` ochilib `{"status":"ok"}` qaytarsin.
5. Botda admin bo'lib `/selftest` ni ishga tushiring.

Eslatmalar:
- Render free xizmati 15 daqiqa trafik bo'lmasa uxlaydi, fayl tizimi vaqtincha (har restartda o'chadi). Shuning uchun DB kanalda.
- Oyiga 750 bepul soat **butun akkaunt bo'yicha umumiy**: bitta 24/7 xizmat ~744 soat oladi. Boshqa xizmatlaringiz ham 24/7 ishlasa, soat yetmaydi va hammasi to'xtatiladi.
- Deploy paytida eski va yangi nusxa bir oz birga ishlaydi. Shuning uchun yangi nusxa `STARTUP_GRACE` (20 s) kutib, keyin snapshotni yuklaydi.

## UptimeRobot (uxlatmaslik uchun)
1. uptimerobot.com → **Add New Monitor** → turi **HTTP(s)**.
2. URL: `https://<nom>.onrender.com/health`
3. Interval: **5 minutes** (bepul rejada shu; Render'ning 15 daqiqalik chegarasidan kichik).
4. Saqlang. Alert uchun Telegram/email ulashingiz mumkin.
