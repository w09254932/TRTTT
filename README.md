# TRTTT

لوحة خاصة بالمالك لإنشاء فواتير وروابط دفع عبر EdfaPay، مع إحصائيات، والتخزين في Firestore.

## متغيرات البيئة في Render

| المتغير | القيمة |
| --- | --- |
| `OWNER_NUMBER` | رقم المالك المستخدم في تسجيل الدخول |
| `OWNER_PASSWORD` | كلمة المرور، 10 خانات على الأقل |
| `SECRET_KEY` | نص عشوائي من 32 خانة أو أكثر (زر Generate في Render) |
| `ENCRYPTION_KEY` | نص عشوائي من 32 خانة أو أكثر. لا تغيّره بعد التشغيل وإلا تعذرت قراءة بيانات العملاء المحفوظة |
| `FIREBASE_CREDENTIALS` | محتوى ملف مفتاح حساب الخدمة من Firebase بصيغة JSON |
| `EDFAPAY_API_KEY` | مفتاح API من لوحة EdfaPay |
| `EDFAPAY_WEBHOOK_SECRET` | نفس السر المكتوب في إعداد Webhook في لوحة EdfaPay |
| `EDFAPAY_ENV` | `sandbox` للتجربة أو `production` للتشغيل الفعلي |

اختياري: `STORE_NAME` اسم المتجر الظاهر للعميل، `BASE_URL` رابط الموقع إن استخدمت نطاقاً خاصاً،
`EDFAPAY_BASE_URL` لتغيير عنوان واجهة EdfaPay، `OWNER_PASSWORD_HASH` بديلاً عن كلمة المرور النصية.

إذا نقص متغير مطلوب يتوقف الموقع ويعرض أسماء المتغيرات الناقصة.

## إعداد Render

- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app --workers 2 --threads 4 --timeout 60`
- Health Check Path: `/healthz`

## إعداد EdfaPay

من Configuration ثم Webhook أضف العنوان `https://YOUR-SITE.onrender.com/webhook/edfapay` واكتب السر نفسه
الموجود في `EDFAPAY_WEBHOOK_SECRET`. الفاتورة لا تصبح مدفوعة إلا بإشعار موقّع من EdfaPay.

## الحماية

- الدخول برقم المالك وكلمة المرور فقط، مع قفل 15 دقيقة بعد 5 محاولات خاطئة.
- بيانات العميل ووصف الفاتورة تُشفَّر بـ AES-256-GCM قبل حفظها في Firestore.
- التحقق من توقيع كل إشعار دفع (HMAC-SHA256) ومطابقة المبلغ قبل اعتماد الدفع.
- لا يوجد أي سر داخل المستودع، وكل الأسرار في متغيرات البيئة.
