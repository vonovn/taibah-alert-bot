#!/usr/bin/env python3
"""
بوت مراقبة تويتر - جامعة طيبة
================================
يراقب حسابين رسميين على X (تويتر):
  - @taibahu     (الحساب الرسمي لجامعة طيبة)
  - @TaibahUdar  (عمادة القبول والتسجيل)

ويبحث في تغريداتهم عن كلمات متعلقة بـ "السكن الجامعي"،
وإذا لقى تطابق يرسل تنبيه فوري على تيليجرام.

مصمم للعمل بشكل مستمر (Background Worker) على Render أو أي سيرفر مشابه،
بحيث يفحص الحسابات كل بضع دقائق بدون تدخل منك.
"""

import os
import re
import json
import time
import logging
import threading
import requests
import snscrape.modules.twitter as sntwitter
from http.server import BaseHTTPRequestHandler, HTTPServer

# =========================================================
# الإعدادات (تُقرأ من متغيرات البيئة - Environment Variables)
# لا تكتب التوكن أو الـ Chat ID هنا مباشرة، حطهم في إعدادات
# السيرفر (Render Environment) عشان الأمان.
# =========================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# الحسابات المراقَبة
ACCOUNTS = ["taibahu", "TaibahUdar"]

# الكلمات المفتاحية (ومرادفاتها) المتعلقة بالسكن الجامعي
# استخدمنا صيغ بدون همزات/تشكيل موحدة عشان المطابقة تكون مرنة
KEYWORDS = [
    "السكن الجامعي",
    "سكن الطلاب",
    "سكن الطالبات",
    "سكن الطلبة",
    "الاسكان الجامعي",
    "اسكان الطلاب",
    "اسكان الطالبات",
    "سكن جامعي",
    "السكن الطلابي",
    "السكن",
    "سكن",
]

# كل كم ثانية يفحص السكربت الحسابات (الافتراضي: كل 5 دقائق)
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", 300))

# عدد آخر التغريدات التي يتم سحبها في كل فحص لكل حساب
TWEETS_PER_CHECK = 15

# ملف حفظ آخر تغريدة تمت معالجتها لكل حساب (عشان ما يكرر التنبيهات)
STATE_FILE = os.environ.get("STATE_FILE", "last_seen.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("taibah-bot")


# =========================================================
# دوال مساعدة
# =========================================================

def normalize_arabic(text: str) -> str:
    """توحيد بعض أشكال الحروف العربية عشان تحسين دقة المطابقة
    (مثلاً إ/أ/آ -> ا) بدون التأثير على باقي النص."""
    text = re.sub(r"[إأآا]", "ا", text)
    text = re.sub(r"ى", "ي", text)
    text = re.sub(r"ة", "ه", text)
    return text


def contains_keyword(tweet_text: str) -> str | None:
    """يرجع أول كلمة مفتاحية موجودة في التغريدة، أو None إذا ما فيه تطابق."""
    normalized_tweet = normalize_arabic(tweet_text)
    for kw in KEYWORDS:
        if normalize_arabic(kw) in normalized_tweet:
            return kw
    return None


def load_state() -> dict:
    """تحميل آخر تغريدة تمت معالجتها لكل حساب من ملف الحالة."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"تعذر قراءة ملف الحالة، بدء من جديد: {e}")
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_telegram_alert(account: str, tweet_url: str, tweet_text: str, matched_keyword: str) -> None:
    """إرسال رسالة تنبيه إلى تيليجرام."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN أو TELEGRAM_CHAT_ID غير مضبوطين!")
        return

    message = (
        f"🔔 <b>تنبيه سكن جامعي</b>\n\n"
        f"📌 الحساب: @{account}\n"
        f"🔑 الكلمة المطابقة: {matched_keyword}\n\n"
        f"{tweet_text}\n\n"
        f"🔗 {tweet_url}"
    )

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    try:
        resp = requests.post(api_url, data=payload, timeout=15)
        if resp.status_code == 200:
            log.info(f"✅ تم إرسال تنبيه لتغريدة من @{account}")
        else:
            log.error(f"فشل إرسال تنبيه تيليجرام: {resp.status_code} - {resp.text}")
    except Exception as e:
        log.error(f"خطأ أثناء إرسال تنبيه تيليجرام: {e}")


def check_account(account: str, state: dict) -> None:
    """يفحص آخر تغريدات حساب معيّن ويبحث عن كلمات مفتاحية."""
    log.info(f"جاري فحص حساب @{account} ...")

    scraper = sntwitter.TwitterUserScraper(account)
    last_seen_id = state.get(account)
    new_last_seen_id = last_seen_id

    tweets_collected = []
    try:
        for i, tweet in enumerate(scraper.get_items()):
            if i >= TWEETS_PER_CHECK:
                break
            # إذا وصلنا لتغريدة سبق أن عالجناها، نوقف (كل الجديد بعدها اتفحص)
            if last_seen_id and tweet.id <= last_seen_id:
                break
            tweets_collected.append(tweet)
    except Exception as e:
        log.error(f"خطأ أثناء سحب تغريدات @{account}: {e}")
        return

    if not tweets_collected:
        log.info(f"لا توجد تغريدات جديدة لدى @{account}")
        return

    # نعالج من الأقدم للأحدث عشان ترتيب التنبيهات يكون منطقي
    for tweet in reversed(tweets_collected):
        matched = contains_keyword(tweet.rawContent)
        if matched:
            send_telegram_alert(account, tweet.url, tweet.rawContent, matched)
        if new_last_seen_id is None or tweet.id > new_last_seen_id:
            new_last_seen_id = tweet.id

    state[account] = new_last_seen_id
    save_state(state)


def main_loop() -> None:
    log.info("🚀 بدء تشغيل بوت مراقبة تويتر - جامعة طيبة")
    log.info(f"الحسابات المراقَبة: {', '.join(ACCOUNTS)}")
    log.info(f"الفحص كل {CHECK_INTERVAL_SECONDS} ثانية")

    state = load_state()

    # أول تشغيل: نسجل آخر تغريدة موجودة حالياً بدون إرسال تنبيهات
    # (عشان ما ترسل لك سيل تنبيهات عن كل تغريدات الحساب القديمة)
    if not state:
        log.info("أول تشغيل، جاري تسجيل آخر تغريدة حالية بدون إرسال تنبيهات...")
        for account in ACCOUNTS:
            try:
                scraper = sntwitter.TwitterUserScraper(account)
                for tweet in scraper.get_items():
                    state[account] = tweet.id
                    break
            except Exception as e:
                log.error(f"تعذر تهيئة الحالة الأولية لـ @{account}: {e}")
        save_state(state)
        log.info("تم ضبط نقطة البداية. من الآن فصاعداً سيتم إرسال تنبيهات للتغريدات الجديدة فقط.")

    while True:
        for account in ACCOUNTS:
            try:
                check_account(account, state)
            except Exception as e:
                log.error(f"خطأ عام أثناء فحص @{account}: {e}")
            time.sleep(5)  # فاصل بسيط بين الحسابات

        log.info(f"⏳ انتظار {CHECK_INTERVAL_SECONDS} ثانية للفحص التالي...")
        time.sleep(CHECK_INTERVAL_SECONDS)


class _HealthHandler(BaseHTTPRequestHandler):
    """خادم HTTP بسيط جداً، وظيفته الوحيدة الرد بـ200 عشان Render
    يتأكد إن الخدمة حية (health check). لا علاقة له بمنطق المراقبة."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Taibah Twitter Alert Bot is running.".encode("utf-8"))

    def log_message(self, format, *args):
        pass  # تجاهل لوقات HTTP الافتراضية عشان ما تشوش على لوقات البوت


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    log.info(f"🌐 خادم فحص الصحة يعمل على المنفذ {port}")
    server.serve_forever()


if __name__ == "__main__":
    # نشغّل خادم الصحة في خيط منفصل (مطلوب لـ Render Web Service)
    # ومنطق المراقبة الفعلي يشتغل في الخيط الرئيسي
    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()

    main_loop()
