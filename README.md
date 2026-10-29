# flow-to-api

رابط وب **Google Flow** رو به یک **API محلی** تبدیل می‌کنه تا هر برنامه‌ای
(مثلاً یک بات تلگرام) بتونه با یک درخواست HTTP ساده، با مدل‌های تصویری و
ویدیویی گوگل عکس بسازه، عکس رو ادیت کنه یا ویدیو بسازه — با هر مدلی که
اکانت Flow شما در دسترس داره.

چطور کار می‌کنه؟ یک مرورگر Chromium روی سرور با Playwright، Flow رو مثل یک
کاربر عادی باز می‌کنه: پروژه رو باز می‌کنه، عکس رو آپلود می‌کنه، پرامت رو
می‌نویسه، منتظر نتیجه می‌مونه و خروجی رو دانلود می‌کنه. شما فقط با API حرف
می‌زنید، نه با صفحه‌ی وب.

> ⚠️ توجه: این ابزار رابط کاربری وب Flow رو خودکار می‌کنه. با تغییر UI گوگل
> ممکنه بشکنه و نیاز به به‌روزرسانی داشته باشه، و خودکارسازی ممکنه با شرایط
> استفاده‌ی گوگل یا محدودیت‌های اکانت شما در تضاد باشه. با مسئولیت خودتون
> استفاده کنید و برای کارهای حساس، اکانت اصلی‌تون رو به خطر نندازید.

## چی لازم داری

- یک سرور **Ubuntu 22.04 یا 24.04** (حداقل ~۱ گیگ رم؛ با swap هم کار می‌کنه)
- یک اکانت گوگل که به **Google Flow** دسترسی داره (برای بهترین نتیجه،
  اشتراک Google AI Pro؛ نسخه‌ی رایگان هم کردیت روزانه‌ی محدودی داره)
- `sudo` روی سرور

## نصب

```bash
git clone https://github.com/newabolup/flow-to-api.git
cd flow-to-api
bash install.sh
```

نصب‌کننده این کارها رو می‌کنه:

1. پکیج‌های سیستمی (Python، Xvfb، x11vnc، websockify، git و...) رو نصب می‌کنه
2. محیط مجازی پایتون + کتابخانه‌ها (FastAPI، Playwright و...) رو می‌سازه
3. مرورگر Chromium مخصوص Playwright و وابستگی‌های سیستمیش رو نصب می‌کنه
4. `cloudflared` و `noVNC` رو برای مرحله‌ی لاگین آماده می‌کنه
5. دو سرویس systemd رو نصب و فعال می‌کنه: `flowbridge-xvfb` (نمایشگر
   مجازی) و `flowbridge` (خود API)

## لاگین گوگل (فقط یک بار)

سرویس برای استفاده از سشن مرورگر شما، به یک لاگین دستی نیاز داره:

```bash
~/flowbridge/login.sh
```

لینک موقتی که چاپ می‌شه رو روی گوشی یا کامپیوتر خودتون باز کنید، وارد
اکانت گوگل بشید و وارد Flow بشید. بعد لاگین رو ببندید:

```bash
~/flowbridge/stop-login.sh
```

سشن در پوشه‌ی `~/flowbridge/profile` می‌مونه و سرویس از همون استفاده می‌کنه.
برای بررسی وضعیت لاگین:

```bash
curl -X POST http://127.0.0.1:8123/warmup
# {"logged_in": true, ...}
```

اگه سشن از بین رفت (مثلاً گوگل دوباره لاگین خواست)، همین دو دستور رو تکرار
کنید.

## API

سرویس فقط روی **localhost** و پورت **8123** گوش می‌ده و درخواست‌ها رو
**یکی‌یکی** (صف تک‌کاره) انجام می‌ده؛ هر ادیت/ساخت حدود یک دقیقه طول می‌کشه.

### بررسی سلامت

```bash
curl http://127.0.0.1:8123/health
```

### دیدن همه‌ی مدل‌ها

```bash
curl http://127.0.0.1:8123/models
# {"image_models": ["🍌 Nano Banana Pro", "🍌 Nano Banana 2 Lite", "🍌 Nano Banana 2.1"],
#  "video_models": ["Omni 1.1 Flash", "Veo 3.1 - Lite", "Veo 3.1 - Fast", "Veo 3.1 - Quality"],
#  "cached": false}
```

این endpoint منوی مدل‌های خود Flow رو می‌خونه و دقیقاً همون مدل‌هایی رو
برمی‌گردونه که اکانت شما بهشون دسترسی داره. نتیجه تا یک ساعت کش می‌شه؛
با `?refresh=1` می‌شه دوباره از Flow خوند. توجه: بریج نمی‌تونه مدلی رو
بسازه که Flow به اکانت شما نداده؛ اگه لیست شما فقط ۲–۳ مدل داره، علتش
محدودیت اکانت شماست، نه بریج.

### ادیت عکس

```bash
curl -F "image=@photo.jpg" -F "prompt=Colorize this black-and-white photo" \
  http://127.0.0.1:8123/edit -o edited.jpg
```

پرامت رو انگلیسی بنویسید؛ نتیجه‌ی بهتری می‌گیرید. به‌جای فایل هم می‌شه
فیلد `image_b64` (base64 عکس) فرستاد. با فیلد `model` می‌شه مدل تصویری
رو عوض کرد:

```bash
curl -F "image=@photo.jpg" -F "prompt=Make the apple green" \
  -F "model=Nano Banana Pro" http://127.0.0.1:8123/edit -o edited.jpg
```

### ساخت عکس از روی متن

```bash
curl -X POST http://127.0.0.1:8123/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt": "A cat sitting at a laptop, cozy room"}' -o generated.jpg
```

با فیلد `model` در JSON می‌شه مدل تصویری رو عوض کرد:

```bash
curl -X POST http://127.0.0.1:8123/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt": "A red apple on a wooden table", "model": "Nano Banana 2.1"}' -o generated.jpg
```

هر دو خروجی عکس، فایل JPEG برمی‌گردونن.

### ساخت ویدیو

```bash
# ساخت ویدیو از روی متن (پیش‌فرض: 360p، ۸ ثانیه، x1)
curl -F "prompt=A cat playing piano" \
  http://127.0.0.1:8123/video -o video.mp4

# ادیت ویدیو (فایل mp4 به‌عنوان ingredient)
curl -F "video=@clip.mp4" -F "prompt=Make it sunset" -F "mode=edit" \
  http://127.0.0.1:8123/video -o edited.mp4

# با مدل ویدیویی و کیفیت دلخواه (نام مدل رو از خروجی GET /models بردارید)
curl -F "prompt=A cat playing piano" -F "model=Omni 1.1 Flash" -F "resolution=720p" \
  http://127.0.0.1:8123/video -o video.mp4
```

خروجی ویدیو فایل MP4 برمی‌گردونه. کیفیت `360p` (پیش‌فرض) یا `720p`
قابل انتخابه. برای مدل‌های غیر از Omni، بررسی مبلغ کردیت انجام نمی‌شه
(هزینه‌ی هر مدل فرق می‌کنه) ولی مبلغی که Flow نشون می‌ده در لاگ ثبت می‌شه.

> ⚠️ توجه: در نسخه فعلی رابط وب Flow، باز کردن پنجره تنظیمات ویدیو از
> داخل thread ممکن است ناموفق باشد و job با خطای واضح fail شود؛
> سوییچ مدل ویدیو پیاده شده ولی ساخت ویدیو با همه مدل‌ها هنوز به‌طور
> کامل تست نشده است.

### انتخاب مدل پیش‌فرض برای کل نصب

اگه نمی‌خواید در هر درخواست `model` بفرستید، مدل‌های پیش‌فرض رو در
سرویس systemd عوض کنید:

```bash
sudo systemctl edit flowbridge
# این دو خط رو اضافه کنید:
# [Service]
# Environment=FLOW_IMAGE_MODEL=Nano Banana Pro
# Environment=FLOW_VIDEO_MODEL=Omni 1.1 Flash
sudo systemctl restart flowbridge
```

بدون این تنظیمات، پیش‌فرض‌ها این‌ها هستن: عکس `Nano Banana 2 Lite`،
ویدیو `Omni 1.1 Flash`. درخواست‌هایی که `model` دارن، همیشه بر پیش‌فرض
اولویت دارن. اگه مدلی که خواستید در منوی Flow نباشه، job با یک خطای واضح
(همراه با لیست مدل‌های موجود) fail می‌شه، نه با یک مدل دیگه.

### مثال با پایتون

```python
import httpx

with open("photo.jpg", "rb") as f:
    r = httpx.post(
        "http://127.0.0.1:8123/edit",
        files={"image": ("photo.jpg", f, "image/jpeg")},
        data={"prompt": "Make the wolf in the image happy"},
        timeout=450,
    )
r.raise_for_status()
open("edited.jpg", "wb").write(r.content)

# با مدل دلخواه:
r = httpx.post(
    "http://127.0.0.1:8123/generate",
    json={"prompt": "A red apple on a wooden table", "model": "Nano Banana Pro"},
    timeout=450,
)
```

## امنیت

- پورت 8123 فقط روی localhost بازه؛ بدون احراز هویت، اون رو روی اینترنت
  باز نکنید. اگه دسترسی از راه دور می‌خواید، جلوی اون یک reverse proxy با
  احراز هویت بذارید.
- لینک لاگین موقتیه؛ بعد از لاگین حتماً `stop-login.sh` رو اجرا کنید.
- پوشه‌ی `~/flowbridge/profile` حاوی سشن لاگین‌شده‌ی گوگل شماست؛ اون رو با
  کسی به اشتراک نذارید و هیچ‌وقت توی گیت کامیت نکنید (در `.gitignore` هست).

## عیب‌یابی

- خطای `not_logged_in`: سشن گوگل از بین رفته؛ مرحله‌ی لاگین رو تکرار کنید.
- خطای `image model ... not available in Flow` یا `video model ... not
  available in Flow menu`: اون مدل در منوی Flow اکانت شما نیست؛ با
  `GET /models` لیست واقعی رو ببینید و همون رو بفرستید.
- لاگ سرویس: `sudo journalctl -u flowbridge -f`
- اگه یک job خراب شد، اسکرین‌شات و HTML صفحه موقع خطا در
  `~/flowbridge/debug/` ذخیره می‌شه تا بشه سلکتور خراب رو پیدا کرد.
- گاهی Flow به‌جای یک خروجی، دو خروجی می‌ده (تنظیم تعداد خروجی همیشه
  نمی‌چسبه)؛ بریج همیشه اولی رو برمی‌گردونه، پس مشکلی نیست.
- کردیت Flow: ساخت و ادیت عکس در Flow ممکنه کردیت مصرف کنه؛ میزان مصرف رو
  در حساب Flow خودتون ببینید.

## فایل‌های پروژه

- `bridge.py` — سرویس FastAPI (صف job، مرورگر Playwright، مسیرهای API،
  کشف مدل‌ها از منوی Flow و سوییچ مدل در هر درخواست)
- `install.sh` — نصب‌کننده‌ی کامل روی سرور اوبونتو
- `login.sh` / `stop-login.sh` — لاگین یک‌باره‌ی گوگل از طریق noVNC
- `flowbridge.service` / `flowbridge-xvfb.service` — یونیت‌های systemd
  (قالب با `__USER__` و `__HOME__` که نصب‌کننده پر می‌کنه؛ برای تغییر
  مدل پیش‌فرض، `Environment=FLOW_IMAGE_MODEL` و
  `Environment=FLOW_VIDEO_MODEL` رو به یونیت اضافه کنید)

## لایسنس

MIT
