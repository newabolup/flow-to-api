# flow-to-api (v3)

رابط وب **Google Flow** رو به یک **API محلی** تبدیل می‌کنه تا هر برنامه‌ای
(مثلاً یک بات تلگرام) با یک درخواست HTTP ساده بتونه با مدل‌های تصویری و
ویدیویی گوگل عکس بسازه، عکس رو ادیت کنه یا ویدیو بسازه — با هر مدلی که
اکانت Flow شما در دسترس داره.

یک مرورگر Chromium با Playwright روی سرور، Flow رو مثل یک کاربر عادی باز
می‌کنه: پروژه رو باز می‌کنه، عکس رو آپلود می‌کنه، پرامت رو می‌نویسه، منتظر
نتیجه می‌مونه و خروجی رو دانلود می‌کنه. شما فقط با API حرف می‌زنید.

> ⚠️ توجه: این ابزار رابط وب Flow رو خودکار می‌کنه. با تغییر UI گوگل ممکنه
> بشکنه، و خودکارسازی ممکنه با شرایط استفاده‌ی گوگل یا محدودیت اکانت شما در
> تضاد باشه. با مسئولیت خودتون استفاده کنید.

**نسخه‌ی ۳** یک بازنویسی روی همون دستور پخت UI است: تأخیر هر job به‌شدت
کمتر شده، ده‌ها باگ پایداری/امنیتی برطرف شده و کد از یک فایل ۲۱۵۸ خطی به
یک پکیج تست‌شده تبدیل شده. جزئیات کامل: [`IMPROVEMENTS.md`](IMPROVEMENTS.md).

## فهرست

- [چی لازم داری](#چی-لازم-داری)
- [نصب](#نصب)
- [لاگین گوگل (یک بار)](#لاگین-گوگل-یک-بار)
- [بررسی سلامت بدون مصرف کردیت](#بررسی-سلامت-بدون-مصرف-کردیت)
- [API](#api)
- [تنظیمات](#تنظیمات)
- [ساختار پروژه](#ساختار-پروژه)
- [تست‌ها](#تست‌ها)
- [عیب‌یابی](#عیب‌یابی)
- [امنیت](#امنیت)
- [محدودیت‌های شناخته‌شده](#محدودیت‌های-شناخته‌شده)

## چی لازم داری

- سرور **Ubuntu 22.04 یا 24.04** (حداقل ~۱ گیگ رم؛ با swap هم کار می‌کنه)
- اکانت گوگل با دسترسی به **Google Flow** (Google AI Pro برای بهترین نتیجه)
- `sudo` روی سرور

## نصب

```bash
git clone https://github.com/newabolup/flow-to-api.git
cd flow-to-api
bash install.sh                 # نصب معمول
bash install.sh --with-api-key  # + کلید API اجباری (توصیه‌شده اگر فقط localhost نیست)
```

نصب‌کننده این کارها رو می‌کنه:

1. پکیج‌های سیستمی (Python، Xvfb، x11vnc، websockify و…)
2. محیط مجازی + وابستگی‌های پین‌شده از `requirements.txt`
3. Chromium مخصوص Playwright + وابستگی‌های سیستمی‌اش
4. `cloudflared` و noVNC برای مرحله‌ی لاگین
5. دو سرویس systemd: `flowbridge-xvfb` (نمایشگر مجازی) و `flowbridge` (API)

نصب دوباره‌پذیر است (re-run امن) و پروفایل لاگین‌شده و `state.json` را دست
نمی‌زند.

## لاگین گوگل (یک بار)

```bash
~/flowbridge/login.sh      # لینک + رمز VNC را چاپ می‌کند
# روی گوشی/لپ‌تاپ خودت باز کن، وارد گوگل و Flow شو
~/flowbridge/stop-login.sh
```

سشن در `~/flowbridge/profile` می‌مونه و سرویس از همون استفاده می‌کند.
وضعیت لاگین:

```bash
curl -s localhost:8123/health
curl -s -X POST localhost:8123/warmup   # {"logged_in": true, ...}
```

## بررسی سلامت بدون مصرف کردیت

بعد از هر تغییر (یا هر بار که احساس کردی Flow عوض شده) این را اجرا کن:

```bash
~/flowbridge/venv/bin/python ~/flowbridge/tools/check_ui.py --models
```

این ابزار مرورگر را با همان پروفایل باز می‌کند، لاگین، پروژه، prompt box،
وضعیت Agent و منوی مدل‌ها را می‌خواند، اسکرین‌شات/گزارش می‌دهد و **هیچ
پرامتی نمی‌فرستد و هیچ کردیتی مصرف نمی‌کند**. خروجی صفر یعنی لایه‌ی UI
سالم است؛ خروجی ۱ یعنی یک سلکتور جابه‌جا شده و باید اصلاح شود.

## API

سرویس روی **localhost:8123** گوش می‌دهد و درخواست‌ها را **یکی‌یکی** انجام
می‌دهد (یک مرورگر، یک job در لحظه؛ صف دارد و ظرفیتش محدود است).

| متد | مسیر | توضیح |
| --- | --- | --- |
| GET | `/health` | وضعیت سرویس/صف/job در حال اجرا (همیشه جواب می‌دهد، حتی وسط job) |
| GET | `/models` | لیست مدل‌های تصویری/ویدیویی این اکانت (`?refresh=1` برای اسکرپ تازه) |
| GET | `/stats` | تنظیمات + صف + وضعیت مرورگر |
| POST | `/warmup` | آماده‌سازی مرورگر و گزارش وضعیت لاگین |
| POST | `/generate` | ساخت عکس از متن (JSON) |
| POST | `/edit` | ادیت عکس (multipart: `image` یا `image_b64`) |
| POST | `/video` | ساخت/ادیت ویدیو (multipart) |
| GET | `/jobs/{id}` | وضعیت یک job |
| POST | `/diagnose-model` | دیباگ UI (اسکرین‌شات + وضعیت، بدون تولید) |

همه‌ی پاسخ‌های تصویری/ویدیویی دو هدر اضافه دارند:
`X-Flowbridge-Ms` (زمان کل) و `X-Flowbridge-Model` (مدل استفاده‌شده).

### ساخت عکس از متن

```bash
curl -X POST localhost:8123/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt": "A cat sitting at a laptop, cozy room"}' -o generated.jpg

# با مدل دلخواه (نام دقیق از GET /models)
curl -X POST localhost:8123/generate -H "Content-Type: application/json" \
  -d '{"prompt": "A red apple on a wooden table", "model": "Nano Banana 2.1"}' -o out.jpg
```

### ادیت عکس

```bash
curl -F "image=@photo.jpg" -F "prompt=Colorize this black-and-white photo" \
  localhost:8123/edit -o edited.jpg

curl -F "image=@photo.jpg" -F "prompt=Make the apple green" -F "model=Nano Banana Pro" \
  localhost:8123/edit -o edited.jpg
```

پرامت را انگلیسی بنویسید؛ نتیجه بهتر است. به‌جای فایل می‌شود `image_b64`
فرستاد (base64 خالص یا `data:` URI).

### ویدیو

```bash
# ساخت ویدیو از متن (پیش‌فرض: 360p، ۸ ثانیه، x1)
curl -F "prompt=A cat playing piano" localhost:8123/video -o video.mp4

# ادیت ویدیو
curl -F "video=@clip.mp4" -F "prompt=Make it sunset" -F "mode=edit" \
  localhost:8123/video -o edited.mp4

# مدل و کیفیت دلخواه
curl -F "prompt=A cat playing piano" -F "model=Omni 1.1 Flash" -F "resolution=720p" \
  localhost:8123/video -o video.mp4
```

خروجی با **magic bytes** تشخیص داده می‌شود: اگر Flow به‌جای ویدیو عکس
برگرداند، درخواست با خطای واضح رد می‌شود، نه با فایل اشتباه.

### اگر کلید API فعال باشد

```bash
curl -H "X-API-Key: $KEY" ...        # یا: -H "Authorization: Bearer $KEY"
```

`/health` همیشه باز است تا مانیتورینگ کار کند.

### مثال پایتون

```python
import httpx

r = httpx.post(
    "http://127.0.0.1:8123/edit",
    files={"image": ("photo.jpg", open("photo.jpg", "rb"), "image/jpeg")},
    data={"prompt": "Make the wolf in the image happy"},
    timeout=450,
)
r.raise_for_status()
print("took", r.headers["X-Flowbridge-Ms"], "ms")
open("edited.jpg", "wb").write(r.content)
```

### کدهای خطا

| کد | معنی |
| --- | --- |
| 400 | ورودی ناقص (پرامت/عکس/حالت نامعتبر) |
| 401 | کلید API نامعتبر |
| 413 | آپلود بزرگ‌تر از حد مجاز |
| 415 | فرمت فایل ناشناخته |
| 429 | صف پر است؛ بعداً تلاش کنید |
| 502 | job شکست خورد (پیام دقیق در `detail`) |
| 503 | `not_logged_in` — سشن گوگل از بین رفته؛ لاگین را تکرار کنید |
| 504 | تایم‌اوت job |

## تنظیمات

همه از طریق متغیر محیطی. ساده‌ترین راه: فایل `/etc/flowbridge.env`
(یونیت خودش آن را می‌خواند) و بعد `sudo systemctl restart flowbridge`.

```bash
sudo tee /etc/flowbridge.env >/dev/null <<'EOF'
FLOWBRIDGE_API_KEY=change-me
FLOW_IMAGE_MODEL=Nano Banana 2 Lite
FLOW_VIDEO_MODEL=Omni 1.1 Flash
FLOWBRIDGE_IDLE_RELEASE_S=1800
EOF
```

| متغیر | پیش‌فرض | کار |
| --- | --- | --- |
| `FLOWBRIDGE_HOME` | `~/flowbridge` | مسیر پروفایل/state/debug |
| `FLOW_IMAGE_MODEL` / `FLOW_VIDEO_MODEL` | `Nano Banana 2 Lite` / `Omni 1.1 Flash` | مدل پیش‌فرض |
| `FLOWBRIDGE_API_KEY` | خالی | اگر پر شود، همه‌ی endpointها کلید می‌خواهند |
| `FLOWBRIDGE_MAX_QUEUE` | `32` | حداکثر job در صف (بیشتر = 429) |
| `FLOWBRIDGE_IMAGE_TIMEOUT` / `FLOWBRIDGE_VIDEO_TIMEOUT` | `420` / `900` | سقف زمان هر job |
| `FLOWBRIDGE_RESULT_TIMEOUT` / `FLOWBRIDGE_VIDEO_RESULT_TIMEOUT` | `300` / `600` | انتظار نتیجه از Flow |
| `FLOWBRIDGE_IDLE_RELEASE_S` | `900` | بعد از این مدت بی‌کاری، مرورگر آزاد می‌شود (۰ = هرگز) |
| `FLOWBRIDGE_WARM_ON_START` | `1` | آماده‌سازی در پس‌زمینه موقع بالا آمدن سرویس |
| `FLOWBRIDGE_MODELS_TTL_S` | `3600` | عمر کش لیست مدل‌ها |
| `FLOWBRIDGE_MAX_UPLOAD_MB` | `64` | سقف حجم آپلود |
| `FLOWBRIDGE_HEADLESS` | خودکار | `1`/`0` برای اجبار headless |
| `FLOWBRIDGE_DEBUG` | `1` | ذخیره‌ی اسکرین‌شات/HTML هنگام خطا در `debug/` |
| `FLOWBRIDGE_LOG_LEVEL` | `INFO` | سطح لاگ |

## ساختار پروژه

```
bridge.py                 نقطه‌ی ورود سازگار (uvicorn bridge:app)
flowbridge/
  config.py               تنظیمات از env (بدون گلوبال)
  state.py                state.json اتمیک (project url، کش مدل‌ها)
  driver.py               تنها لایه‌ای که با Playwright حرف می‌زند
  js.py                   کد درون‌صفحه‌ای: probe و predicateهای انتظار
  ui.py                   دستور پخت UI: سلکتورها، تنظیمات، پرامت، نتیجه، دانلود
  image_flow.py           جریان generate/edit عکس
  video_flow.py           جریان ساخت/ادیت ویدیو
  discovery.py            خواندن منوی مدل‌ها (کش RAM + دیسک)
  runtime.py              مالکیت مرورگر (پروفایل، آزادسازی بی‌کاری)
  jobs.py                 صف job با نتیجه‌ی event-driven
  api.py                  مسیرهای FastAPI
  diag.py                 دیباگ اختیاری
tools/check_ui.py         سلامت‌سنجی UI بدون کردیت
tests/                    ۶۱ تست (unit + fake driver + JS واقعی در Chromium)
```

## تست‌ها

```bash
pip install -r requirements-dev.txt
pytest            # ~۸ ثانیه، بدون نیاز به اکانت گوگل
ruff check .
```

تست‌ها سه لایه دارند:

1. **واحد**: sniff فایل، state، صف job، تنظیمات.
2. **جریان کامل** با یک driver جعلی (`tests/fake.py`): کل مسیر job تا bytes.
3. **مرورگر واقعی**: `tests/test_browser_js.py` کد درون‌صفحه‌ای (`js.py`) و
   `PageDriver` را روی یک DOM شبیه Flow در Chromium هدلس اجرا می‌کند — یعنی
   probe و predicateها واقعاً در مرورگر تست می‌شوند.

## عیب‌یابی

- خطای `not_logged_in`: سشن از بین رفته؛ `login.sh` / `stop-login.sh` را تکرار کن.
- `image model ... not available in Flow`: با `GET /models` لیست واقعی را ببین.
- لاگ: `sudo journalctl -u flowbridge -f`
- اسکرین‌شات/HTML خطا: `~/flowbridge/debug/`
- اگر job در مرحله‌ای گیر می‌کند: `check_ui.py --json` را اجرا کن تا ببینی
  کدام بخش UI عوض شده است.
- اگر مرورگر بالا نمی‌آید: قفل پروفایل خودکار پاک می‌شود؛ اگر باز هم نشد،
  مطمئن شو هیچ پروسه‌ی دیگری (مثلاً login.sh) با همان پروفایل باز نیست.

## امنیت

- پورت 8123 فقط روی localhost است. اگر از راه دور لازم داری، reverse proxy
  با احراز هویت بگذار و `FLOWBRIDGE_API_KEY` را فعال کن.
- `~/flowbridge/profile` سشن گوگل شماست؛ در git نگذار و با کسی به اشتراک نگذار.
- لینک لاگین (`login.sh`) یک تونل عمومی است، ولی حالا با **رمز تصادفی VNC**
  محافظت می‌شود و websockify فقط روی localhost گوش می‌دهد. بعد از لاگین حتماً
  `stop-login.sh`.

## محدودیت‌های شناخته‌شده

- **یک job در لحظه**: یک اکانت + یک مرورگر. صف، ترتیب را حفظ می‌کند.
  موازی‌سازی واقعی نیازمند بازنویسی لایه‌ی UI روی API async Playwright است
  (چون API سینک به ترد خودش قفل می‌شود) — جزئیات در `IMPROVEMENTS.md`.
- **سوییچ مدل ویدیو** پیاده شده اما با همه‌ی مدل‌های غیر از Omni روی اکانت
  واقعی تست نشده است.
- با تغییر UI گوگل، سلکتورها می‌شکنند؛ `check_ui.py` سریع‌ترین راه دیدن
  محل شکستن است.

## لایسنس

MIT
