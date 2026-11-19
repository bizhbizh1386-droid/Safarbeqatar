"""
ربات راهنمای گردشگری یزد
نسخه توسعه‌یافته:
- منوی دو ستونه
- جزئیات جاذبه‌ها + ارسال لوکیشن تلگرام + لینک Google Maps
- نقشه شماتیک مسیرهای گردشگری اطراف یزد
- پیشنهاد مکان‌ها با Google Places API (اختیاری)
- پنج بازی، بازی انفرادی و گروهی، امتیاز و جدول رتبه‌بندی SQLite

نصب:
pip install "python-telegram-bot>=21" requests pillow

اجرا در Linux/macOS:
export BOT_TOKEN="توکن BotFather"
export GOOGLE_MAPS_API_KEY="کلید Google Places API (اختیاری)"
python yazd_travel_bot.py

در Windows PowerShell:
$env:BOT_TOKEN="توکن"
$env:GOOGLE_MAPS_API_KEY="کلید گوگل (اختیاری)"
python yazd_travel_bot.py

برای اطلاعات واقعی امتیازها و عکس‌های گوگل، Places API (New) را در Google Cloud فعال
و Billing را تنظیم کنید. بدون کلید گوگل، لینک جست‌وجوی Google Maps همچنان کار می‌کند.
"""

import asyncio
import io
import html
import re
from pathlib import Path
import logging
import os
import random
import sqlite3
import textwrap
import uuid
from typing import Optional

import requests
from PIL import Image, ImageDraw, ImageFont

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = "yazd_travel.db"
GOOGLE_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()


# عکس جاذبه‌ها: از Wikimedia Commons دریافت و در پوشه images کش می‌شود.
# برای استفاده از عکس‌ها نیازی به Google API Key نیست.
IMAGE_DIR = Path("images")
IMAGE_DIR.mkdir(exist_ok=True)

COMMONS_QUERIES = {
    "bafte": "Yazd historic city old town Iran",
    "jameh": "Jameh Mosque of Yazd Iran",
    "amir": "Amir Chakhmaq complex Yazd",
    "zendan": "Alexander prison Yazd",
    "fire": "Yazd Atash Behram fire temple",
    "dakhme": "Towers of silence Yazd Iran",
    "water": "Yazd water museum",
    "abanbar": "Yazd Ab Anbar water reservoir",
    "dolat": "Dowlat Abad Garden Yazd",
    "qanat": "Zarch Qanat Yazd",
    "shirkuh": "Shirkuh mountain Yazd",
    "desert": "Yazd desert Iran",
    "meybod": "Meybod Narin Castle Iran",
    "chak": "Chak Chak shrine Iran",
    "khara": "Kharanaq village Yazd",
    "zein": "Zein-o-din caravanserai Iran",
    "mehriz": "Pahlavanpur Garden Mehriz",
    "abar": "Abarquh ancient cypress Iran",
    "fahraj": "Fahraj Jame Mosque Yazd",
    "sary": "Saryazd castle Iran",
    "mirror": "Yazd Museum of Mirror and Lighting",
    "heydar": "Yazd historic house",
    "lari": "Lariha House Yazd",
    "termeh": "Yazd Termeh textile",
    "craft": "Yazd pottery handicraft",
    "bazaar": "Yazd Khan Bazaar",
}

def _append_credit(iid, artist, license_name, description_url):
    """Keep image credits in images/CREDITS.md (never shown in Telegram)."""
    try:
        credits = IMAGE_DIR / "CREDITS.md"
        if not credits.exists():
            credits.write_text(
                "# Image credits\n\nImages are from Wikimedia Commons.\n\n",
                encoding="utf-8",
            )
        with credits.open("a", encoding="utf-8") as f:
            f.write(f"- `{iid}.jpg` | Author: {artist} | License: {license_name} | {description_url}\n")
    except OSError:
        logger.exception("Could not write image credits for %s", iid)


def _commons_photo(iid):
    """Download a Commons thumbnail, retaining author/license attribution."""
    IMAGE_DIR.mkdir(exist_ok=True)
    image_path = IMAGE_DIR / f"{iid}.jpg"
    info_path = IMAGE_DIR / f"{iid}.txt"
    if image_path.exists() and image_path.stat().st_size > 5000:
        attribution = info_path.read_text(encoding="utf-8") if info_path.exists() else ""
        return str(image_path), attribution

    search_term = COMMONS_QUERIES.get(iid)
    if not search_term:
        return None, ""
    api = "https://commons.wikimedia.org/w/api.php"
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": search_term, "gsrnamespace": 6, "gsrlimit": 8,
        "prop": "imageinfo", "iiprop": "url|extmetadata",
        "iiurlwidth": 1000,
    }
    headers = {"User-Agent": "YazdTravelBot/1.0 (educational tourism project)"}
    response = requests.get(api, params=params, headers=headers, timeout=20)
    response.raise_for_status()
    pages = response.json().get("query", {}).get("pages", {})
    candidates = sorted(pages.values(), key=lambda p: p.get("index", 999))
    for page in candidates:
        image_info = (page.get("imageinfo") or [{}])[0]
        image_url = image_info.get("thumburl") or image_info.get("url")
        if not image_url:
            continue
        meta = image_info.get("extmetadata", {})
        license_name = html.unescape(meta.get("LicenseShortName", {}).get("value", "License not specified"))
        artist_raw = meta.get("Artist", {}).get("value", "Author not listed")
        artist = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", artist_raw)).split()) or "Author not listed"
        description_url = image_info.get("descriptionurl", "")
        try:
            photo_response = requests.get(image_url, headers=headers, timeout=30)
            photo_response.raise_for_status()
            content_type = photo_response.headers.get("Content-Type", "")
            if not content_type.startswith("image/") or len(photo_response.content) < 5000:
                continue
            image_path.write_bytes(photo_response.content)
            attribution = f"عکس: Wikimedia Commons | پدیدآور: {artist} | مجوز: {license_name}\n{description_url}"
            info_path.write_text(attribution, encoding="utf-8")
            _append_credit(iid, artist, license_name, description_url)
            return str(image_path), attribution
        except requests.RequestException:
            continue
    return None, ""

async def send_item_details(message, iid):
    title, description, section_key, _coords = ITEMS[iid]
    caption = f"{title}\n\n{description}"
    keyboard = item_kb(iid, section_key)
    try:
        photo_path, _attribution = await asyncio.to_thread(_commons_photo, iid)
        if photo_path:
            # بدون منبع، لینک، HTML یا مجوز در پیام تلگرام (اعتبار عکس‌ها در images/CREDITS.md است)
            with open(photo_path, "rb") as photo:
                if len(caption) <= 1024:
                    await message.reply_photo(photo=photo, caption=caption, reply_markup=keyboard)
                    return
                await message.reply_photo(photo=photo, caption=title)
            await message.reply_text(description, reply_markup=keyboard)
            return
    except Exception:
        logger.exception("Could not load Commons image for %s", iid)
    await message.reply_text(caption, reply_markup=keyboard)

# ---------------------------------------------------------------------------
# محتوای راهنمای گردشگری
# هر مطلب: شناسه، عنوان، متن، مختصات (اختیاری)
# ---------------------------------------------------------------------------

SECTIONS = {
    "his": ("🏛 جاذبه‌های تاریخی", [
        ("bafte", "بافت تاریخی یزد",
         "بافت تاریخی یزد در سال ۲۰۱۷ در فهرست میراث جهانی یونسکو ثبت شد. کوچه‌های باریک و خشتی، خانه‌های حیاط‌دار، بادگیرها و آب‌انبارها را پیاده ببینید. صبح زود یا غروب زمان مناسبی برای گشت‌وگذار است.",
         (31.8970, 54.3670)),
        ("jameh", "مسجد جامع یزد",
         "از شاخص‌ترین بناهای شهر با کاشی‌کاری فیروزه‌ای، ایوان بلند و دو مناره چشمگیر. نور صبح و غروب برای عکاسی مناسب است.",
         (31.8976, 54.3676)),
        ("amir", "مجموعه امیرچخماق",
         "میدان، تکیه و مسجدی با نمای متقارن و ردیف طاق‌نماها. غروب و شب زمان خوبی برای قدم‌زدن و عکاسی است.",
         (31.8937, 54.3731)),
        ("zendan", "زندان اسکندر",
         "بنایی کهن با گنبد و چاه در دل بافت قدیم. نام آن پیشینه‌ای تاریخی و افسانه‌ای دارد و اطراف آن غرفه‌های صنایع دستی دیده می‌شود.",
         (31.9010, 54.3660)),
        ("fire", "آتشکده زرتشتیان",
         "از مکان‌های مهم زرتشتیان یزد. بازدیدکنندگان آتش را از پشت شیشه می‌بینند. قوانین و محدودیت‌های بازدید را در محل رعایت کنید.",
         (31.9080, 54.3690)),
        ("dakhme", "دخمه‌های زرتشتیان",
         "دو دخمه تاریخی روی تپه‌های بیرون شهر که امروزه استفاده نمی‌شوند. برای بازدید، مسیر و شرایط دسترسی را از منابع محلی بررسی کنید.",
         (31.8400, 54.3500)),
        ("water", "موزه آب یزد",
         "درباره تاریخ قنات‌ها، مقنی‌ها و ابزار آب‌رسانی یزد. بازدید از آن به درک شیوه زندگی در اقلیم کویری کمک می‌کند.",
         (31.9000, 54.3660)),
        ("abanbar", "آب‌انبارها و یخچال‌ها",
         "آب‌انبارهای بادگیردار و یخچال‌های سنتی نشان می‌دهند مردم کویر چگونه آب را ذخیره و خنک نگه می‌داشتند.",
         None),
    ]),
    "nat": ("🌿 باغ‌ها و طبیعت", [
        ("dolat", "باغ دولت‌آباد",
         "باغ ایرانی دوره زندیه و از باغ‌های ثبت‌شده در مجموعه میراث جهانی باغ ایرانی. بادگیر بلند، جویبارها و درختان سرو آن دیدنی‌اند.",
         (31.9003, 54.3497)),
        ("qanat", "قنات زارچ",
         "قنات زارچ از نمونه‌های شناخته‌شده قنات‌های تاریخی منطقه است. دسترسی به بخش‌های قابل بازدید را پیش از رفتن بررسی کنید.",
         (31.9250, 54.3650)),
        ("shirkuh", "شیرکوه و تفت",
         "تفت در دامنه شیرکوه قرار دارد و باغ‌ها و روستاهای کوهپایه‌ای آن در فصل گرم می‌توانند مقصدی دلپذیر باشند.",
         (31.7400, 54.2100)),
        ("desert", "کویر و تپه‌های ماسه‌ای",
         "کویرنوردی و تماشای آسمان شب تجربه‌ای ویژه است. برای سفر کویری از راهنمای محلی معتبر استفاده کنید و تنها وارد کویر نشوید.",
         None),
    ]),
    "food": ("🍬 خوراک و شیرینی", [
        ("ghotab", "قطاب",
         "شیرینی معروف یزد با مغز بادام یا گردو و پوشش پودر قند؛ از سوغات محبوب شهر است.", None),
        ("baghlava", "باقلوای یزدی",
         "باقلوا با عطر هل و گلاب از شیرینی‌های شناخته‌شده یزد است.", None),
        ("sweets", "شیرینی‌های سنتی",
         "لوز، حاجی‌بادام، نقل و شیرینی‌های خشک از انتخاب‌های محبوب قنادی‌های یزد هستند.", None),
        ("local", "غذاهای محلی",
         "در رستوران‌های سنتی درباره غذای روز و خوراک‌های محلی سؤال کنید. ساعت کار و نیاز به رزرو را پیش از رفتن بررسی کنید.", None),
        ("tea", "چای‌خانه‌ها و کافه‌ها",
         "کافه‌ها و چای‌خانه‌های خانه‌های قدیمی و پشت‌بام‌های بافت تاریخی برای استراحت و تماشای شهر جذاب‌اند.", None),
    ]),
    "stay": ("🛏 اقامت", [
        ("trad", "اقامتگاه‌های سنتی",
         "خانه‌های تاریخی بسیاری به اقامتگاه تبدیل شده‌اند و معمولاً حیاط مرکزی، حوض و پشت‌بام دارند. درباره پله‌ها و امکانات اتاق سؤال کنید.", None),
        ("modern", "هتل‌های مدرن",
         "اگر پارکینگ و دسترسی آسان برایتان مهم است، هتل‌های مدرن خارج از بافت قدیم را هم بررسی کنید.", None),
        ("camp", "اقامت در کویر",
         "کمپ‌ها و اقامتگاه‌های بوم‌گردی اطراف شهر تجربه شب کویر را فراهم می‌کنند. شب‌ها ممکن است هوا سرد شود.", None),
        ("book", "نکته رزرو",
         "در بهار، پاییز و نوروز اقامتگاه‌ها زودتر پر می‌شوند. شرایط لغو و خدمات را پیش از رزرو بررسی کنید.", None),
    ]),
    "near": ("🚗 اطراف یزد", [
        ("meybod", "میبد",
         "نارین‌قلعه، کبوترخانه، یخچال و کاروانسرای شاه‌عباسی از دیدنی‌های میبد هستند. این شهر به سفال و سرامیک هم معروف است.",
         (32.2490, 54.0170)),
        ("chak", "چک‌چک",
         "زیارتگاه زرتشتیان در دل کوه، نزدیک اردکان. برای بازدید، شرایط مسیر و زمان‌های برگزاری آیین‌ها را بررسی کنید.",
         (32.3500, 54.4200)),
        ("khara", "روستای خرانق",
         "روستایی تاریخی با بافت خشتی، کوچه‌های قدیمی و چشم‌اندازهای دیدنی. صبح یا غروب برای بازدید مناسب‌تر است.",
         (32.3450, 54.6650)),
        ("zein", "کاروانسرای زین‌الدین",
         "کاروانسرایی مدور از دوره صفوی در مسیر یزد به کرمان؛ برای توقف در مسیر سفر مناسب است.",
         (31.3790, 54.5310)),
        ("mehriz", "مهریز و باغ پهلوان‌پور",
         "باغ پهلوان‌پور مهریز از باغ‌های ثبت‌شده در مجموعه باغ ایرانی است و آب‌نماها و درختان کهن دارد.",
         (31.5800, 54.4400)),
        ("abar", "ابرکوه",
         "ابرکوه به سرو کهنسال و بناهای تاریخی مانند مسجد جامع و آب‌انبارها شناخته می‌شود.",
         (31.1300, 53.2800)),
        ("fahraj", "فهرج",
         "مسجد جامع فهرج از بناهای کهن منطقه است و برای علاقه‌مندان به معماری اسلامی جذابیت دارد.",
         (31.7700, 54.5700)),
        ("sary", "سریزد",
         "روستایی تاریخی نزدیک یزد با قلعه و کاروانسرا؛ مقصدی مناسب برای گشت نیم‌روزه.",
         (31.5900, 54.4500)),
    ]),
    "shop": ("🛍 خرید و سوغات", [
        ("termeh", "ترمه و زری‌بافی",
         "ترمه و زری‌بافی از صنایع دستی معروف یزدند. برای خرید، کیفیت پارچه و نوع بافت را بررسی کنید.", None),
        ("craft", "سفال و صنایع دستی",
         "سفال، سرامیک و دست‌ساخته‌های هنرمندان محلی را در بازارها و فروشگاه‌های صنایع دستی پیدا می‌کنید.", None),
        ("souv", "سوغات شیرین",
         "قطاب، باقلوا و حاجی‌بادام از سوغات شیرین یزد هستند. تاریخ تولید و بسته‌بندی را بررسی کنید.", None),
        ("bazaar", "بازار قدیمی یزد",
         "بازار قدیمی را می‌توان پیاده گشت. ساعت کاری مغازه‌ها ممکن است متفاوت باشد.", (31.8950, 54.3700)),
    ]),
    "plan": ("🗺 برنامه سفر", [
        ("p1", "برنامه یک‌روزه",
         "صبح: مسجد جامع و بافت قدیم. ظهر: ناهار سنتی. عصر: آتشکده و باغ دولت‌آباد. غروب: امیرچخماق و خرید سوغات.", None),
        ("p2", "برنامه دوروزه",
         "روز اول: جاذبه‌های مرکزی یزد. روز دوم: میبد و دیدنی‌های آن، سپس بازگشت به یزد.", None),
        ("p3", "برنامه سه‌روزه",
         "روز اول و دوم: یزد و میبد. روز سوم: خرانق و چک‌چک یا مهریز و کاروانسرای زین‌الدین.", None),
    ]),
    "cul": ("🎭 فرهنگ و آداب", [
        ("zoro", "زرتشتیان یزد",
         "یزد یکی از مراکز مهم زرتشتیان ایران است. در مکان‌های مذهبی با احترام و مطابق مقررات بازدید کنید.", None),
        ("badgir", "بادگیر و معماری کویری",
         "بادگیرها، حیاط مرکزی، حوض و سرداب از راهکارهای سنتی سازگاری با اقلیم گرم و خشک هستند.", None),
        ("zoor", "زورخانه و ورزش باستانی",
         "زورخانه‌های یزد برنامه‌های ورزش باستانی دارند. زمان اجرا را باید از قبل جویا شوید.", None),
        ("etq", "آداب و پوشش",
         "در اماکن مذهبی پوشش مناسب داشته باشید و پیش از عکاسی از افراد یا فضاهای خصوصی اجازه بگیرید.", None),
    ]),
    "info": ("ℹ️ اطلاعات کاربردی", [
        ("when", "بهترین زمان سفر",
         "بهار و پاییز معمولاً برای گشت‌وگذار مناسب‌ترند. تابستان گرم است و بهتر است صبح زود یا عصر بیرون بروید.", None),
        ("reach", "رسیدن به یزد",
         "یزد فرودگاه، ایستگاه راه‌آهن و پایانه اتوبوس دارد. در فصل‌های شلوغ بلیت را زودتر تهیه کنید.", None),
        ("move", "رفت‌وآمد در شهر",
         "بافت قدیم را بهتر است پیاده بگردید؛ برای مسیرهای دورتر از تاکسی یا سرویس‌های درخواست خودرو استفاده کنید.", None),
        ("health", "سلامت و گرما",
         "آب کافی همراه داشته باشید، از کلاه و ضدآفتاب استفاده کنید و در اوج گرما استراحت کنید.", None),
        ("sos", "شماره‌های اضطراری",
         "پلیس ۱۱۰، اورژانس ۱۱۵ و آتش‌نشانی ۱۲۵.", None),
        ("note", "یادآوری",
         "ساعت کار، قیمت و شرایط بازدید تغییر می‌کنند؛ پیش از مراجعه از منابع رسمی بررسی کنید.", None),
    ]),
    "hist2": ("🏺 موزه‌ها و دیدنی‌های کمترشناخته‌شده", [
        ("mirror", "موزه آینه و روشنایی",
         "موزه‌ای در خانه‌ای تاریخی با آینه‌کاری و اشیای روشنایی؛ مناسب علاقه‌مندان هنر و معماری.", (31.9000, 54.3600)),
        ("heydar", "خانه حیدرزاده",
         "خانه‌ای تاریخی با ارسی‌های رنگی و حیاط مرکزی، نمونه‌ای از معماری خانه‌های قدیمی یزد.", None),
        ("lari", "خانه لاری‌ها",
         "از خانه‌های مشهور تاریخی یزد با حیاط مرکزی و جزئیات معماری سنتی.", (31.9000, 54.3650)),
    ]),
    "practical": ("🧭 نکات عملی سفر", [
        ("money", "پول و پرداخت",
         "برای بازارها و روستاهای اطراف کمی پول نقد همراه داشته باشید.", None),
        ("net", "اینترنت و نقشه آفلاین",
         "در کویر و روستاهای دورافتاده ممکن است اینترنت ضعیف باشد؛ نقشه آفلاین ذخیره کنید.", None),
        ("dress", "پوشش و احترام",
         "در اماکن مذهبی پوشش مناسب داشته باشید و قوانین محلی را رعایت کنید.", None),
        ("photo", "عکاسی",
         "برای عکاسی از ساکنان یا داخل مکان‌های مذهبی اجازه بگیرید.", None),
        ("kids", "سفر با کودکان",
         "برای پیاده‌روی در بافت قدیم آب و کفش راحت همراه داشته باشید.", None),
    ]),
    "eat": ("🍽 پیشنهادهای غذایی بیشتر", [
        ("ash", "آش و غذاهای سنتی",
         "آش‌ها و خوراک‌های محلی را در رستوران‌های سنتی امتحان کنید و غذای روز را بپرسید.", None),
        ("faloodeh", "فالوده و شربت‌های سنتی",
         "فالوده و شربت‌هایی مانند بیدمشک و آلبالو در هوای گرم انتخاب‌های محبوبی هستند.", None),
        ("breakfast", "صبحانه محلی",
         "صبحانه با نان محلی، شیره و مرباهای خانگی در اقامتگاه‌های سنتی تجربه‌ای دلپذیر است.", None),
    ]),
}

ITEMS = {}
for section_key, (_, section_items) in SECTIONS.items():
    for item in section_items:
        iid, title, description, coords = item
        ITEMS[iid] = (title, description, section_key, coords)

# دسته‌های مناسب برای پیشنهاد مکان از Google Places
SUGGESTION_QUERIES = {
    "his": "historic attractions and museums",
    "nat": "gardens and nature attractions",
    "food": "traditional restaurants and sweet shops",
    "stay": "hotels and traditional accommodations",
    "near": "tourist attractions near Yazd",
    "shop": "souvenir shops and traditional markets",
    "hist2": "museums and historic houses",
    "eat": "restaurants and cafes",
}

# مسیرهای شماتیک؛ زمان‌ها تقریبی‌اند و جایگزین مسیریابی زنده نیستند.
ROUTES = [
    ("یزد", "میبد", "حدود ۴۰ تا ۵۰ دقیقه", "حدود ۵۰ کیلومتر"),
    ("یزد", "مهریز", "حدود ۳۰ تا ۴۰ دقیقه", "حدود ۳۵ کیلومتر"),
    ("یزد", "تفت", "حدود ۲۵ تا ۳۵ دقیقه", "حدود ۲۵ کیلومتر"),
    ("یزد", "خرانق", "حدود ۷۵ تا ۹۰ دقیقه", "حدود ۸۵ کیلومتر"),
    ("یزد", "سریزد", "حدود ۴۵ تا ۶۰ دقیقه", "حدود ۵۰ کیلومتر"),
]

# ---------------------------------------------------------------------------
# پایگاه داده
# ---------------------------------------------------------------------------

def db_connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scores (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                points INTEGER NOT NULL DEFAULT 0,
                games INTEGER NOT NULL DEFAULT 0,
                correct INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS game_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                game_type TEXT NOT NULL,
                is_correct INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


def record_answer(user, game_type: str, is_correct: bool, points: int):
    display_name = user.full_name or user.username or str(user.id)
    with db_connect() as conn:
        conn.execute("""
            INSERT INTO scores(user_id, username, display_name, points, games, correct)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                display_name=excluded.display_name,
                points=scores.points + excluded.points,
                games=scores.games + 1,
                correct=scores.correct + excluded.correct
        """, (user.id, user.username, display_name, points, int(is_correct)))
        conn.execute(
            "INSERT INTO game_history(user_id, game_type, is_correct) VALUES (?, ?, ?)",
            (user.id, game_type, int(is_correct)),
        )
        conn.commit()


def get_leaderboard(limit=10):
    with db_connect() as conn:
        return conn.execute("""
            SELECT display_name, points, games, correct
            FROM scores ORDER BY points DESC, correct DESC LIMIT ?
        """, (limit,)).fetchall()


# ---------------------------------------------------------------------------
# صفحه‌کلیدها و ابزارهای عمومی
# ---------------------------------------------------------------------------

WELCOME = (
    "سلام! به راهنمای گردشگری یزد خوش آمدید 🏜️\n\n"
    "از منوی زیر یک بخش را انتخاب کنید. برای جست‌وجو هم می‌توانید بنویسید:\n"
    "/search قطاب"
)

GAME_NAMES = {
    "quiz": "🧠 مسابقه اطلاعات یزد",
    "photo": "📸 حدس مکان از روی عکس",
    "food": "🍬 حدس خوراکی یزدی",
    "tf": "✅ درست یا غلط",
    "mix": "🎮 بازی ترکیبی",
}

MAIN_ORDER = list(SECTIONS.keys())


def main_kb():
    rows = []
    keys = MAIN_ORDER[:]
    for i in range(0, len(keys), 2):
        row = [
            InlineKeyboardButton(SECTIONS[keys[j]][0], callback_data=f"sec:{keys[j]}")
            for j in range(i, min(i + 2, len(keys)))
        ]
        rows.append(row)
    rows.append([
        InlineKeyboardButton("🗺 نقشه مسیرها", callback_data="routes"),
        InlineKeyboardButton("🎮 بازی و سرگرمی", callback_data="games"),
    ])
    return InlineKeyboardMarkup(rows)


def section_kb(key):
    rows = []
    for iid, title, _, _ in SECTIONS[key][1]:
        rows.append([InlineKeyboardButton(title, callback_data=f"item:{iid}")])

    if key in SUGGESTION_QUERIES:
        rows.append([InlineKeyboardButton("⭐ پیشنهاد مکان‌ها و کسب‌وکارها", callback_data=f"sug:{key}")])

    rows.append([InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def item_kb(iid, section_key):
    rows = []
    coords = ITEMS[iid][3]
    if coords:
        rows.append([
            InlineKeyboardButton("📍 نمایش موقعیت در تلگرام", callback_data=f"loc:{iid}"),
            InlineKeyboardButton("🗺 گوگل‌مپ", url=google_maps_url(iid)),
        ])
    rows.extend([
        [InlineKeyboardButton("↩️ بازگشت به این بخش", callback_data=f"sec:{section_key}")],
        [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")],
    ])
    return InlineKeyboardMarkup(rows)


def google_maps_url(iid):
    title, _, _, coords = ITEMS[iid]
    if coords:
        query = f"{coords[0]},{coords[1]}"
    else:
        query = f"{title} Yazd Iran"
    return "https://www.google.com/maps/search/?api=1&query=" + requests.utils.quote(query)


def game_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 بازی ترکیبی", callback_data="play:mix")],
        [
            InlineKeyboardButton("🧠 اطلاعات یزد", callback_data="play:quiz"),
            InlineKeyboardButton("📸 حدس مکان", callback_data="play:photo"),
        ],
        [
            InlineKeyboardButton("🍬 حدس خوراکی", callback_data="play:food"),
            InlineKeyboardButton("✅ درست یا غلط", callback_data="play:tf"),
        ],
        [InlineKeyboardButton("🏆 جدول برترین‌ها", callback_data="leader")],
        [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")],
    ])


def routes_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗺 بازکردن مسیر یزد تا میبد", url="https://www.google.com/maps/dir/?api=1&origin=Yazd,Iran&destination=Meybod,Iran&travelmode=driving")],
        [InlineKeyboardButton("🗺 بازکردن مسیر یزد تا مهریز", url="https://www.google.com/maps/dir/?api=1&origin=Yazd,Iran&destination=Mehriz,Iran&travelmode=driving")],
        [InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")],
    ])


# ---------------------------------------------------------------------------
# نقشه شماتیک مسیرها (تصویر قابل ارسال در تلگرام)
# ---------------------------------------------------------------------------

def load_font(size, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def create_route_map():
    # نقشه شماتیک است، نه نقشه جغرافیایی دقیق.
    width, height = 1200, 1000
    img = Image.new("RGB", (width, height), "#f4ead7")
    draw = ImageDraw.Draw(img)
    title_font = load_font(42, True)
    city_font = load_font(29, True)
    small_font = load_font(19)

    # پس‌زمینه و بافت ساده
    draw.rounded_rectangle((35, 35, width - 35, height - 35), radius=35,
                           fill="#fffaf0", outline="#b78b55", width=5)
    draw.text((width // 2, 75), "نقشه شماتیک مسیرهای گردشگری یزد",
              fill="#563b27", font=title_font, anchor="mm")

    center = (600, 420)
    cities = {
        "یزد": (600, 420),
        "میبد": (850, 230),
        "مهریز": (800, 650),
        "تفت": (340, 620),
        "خرانق": (1020, 420),
        "سریزد": (340, 250),
    }

    # خطوط نقطه‌چین
    for name, point in cities.items():
        if name == "یزد":
            continue
        x1, y1 = center
        x2, y2 = point
        dx, dy = x2 - x1, y2 - y1
        length = max(1, (dx * dx + dy * dy) ** 0.5)
        steps = int(length / 18)
        for n in range(steps):
            if n % 2 == 0:
                a = n / steps
                b = min(1, (n + 1) / steps)
                p1 = (int(x1 + dx * a), int(y1 + dy * a))
                p2 = (int(x1 + dx * b), int(y1 + dy * b))
                draw.line((p1, p2), fill="#9b7044", width=7)

    # شهرها
    for name, (x, y) in cities.items():
        radius = 21 if name == "یزد" else 16
        color = "#b34d32" if name == "یزد" else "#277b76"
        draw.ellipse((x-radius, y-radius, x+radius, y+radius),
                     fill=color, outline="white", width=5)
        draw.text((x, y - 42), name, fill="#3f3025", font=city_font, anchor="mm")

    # توضیحات مسیرها در کادرهای جدا
    notes = [
        "یزد ← میبد: حدود ۴۰ تا ۵۰ دقیقه | حدود ۵۰ کیلومتر",
        "یزد ← مهریز: حدود ۳۰ تا ۴۰ دقیقه | حدود ۳۵ کیلومتر",
        "یزد ← تفت: حدود ۲۵ تا ۳۵ دقیقه | حدود ۲۵ کیلومتر",
        "یزد ← خرانق: حدود ۷۵ تا ۹۰ دقیقه | حدود ۸۵ کیلومتر",
        "یزد ← سریزد: حدود ۴۵ تا ۶۰ دقیقه | حدود ۵۰ کیلومتر",
    ]
    y = 790
    for note in notes:
        draw.text((width // 2, y), note, fill="#57432f", font=small_font, anchor="mm")
        y += 30

    draw.text((width // 2, 960),
              "زمان و مسافت تقریبی‌اند؛ برای مسیر زنده، دکمه‌های مسیریابی را بزنید.",
              fill="#805f43", font=small_font, anchor="mm")
    output = io.BytesIO()
    output.name = "yazd_routes.png"
    img.save(output, format="PNG")
    output.seek(0)
    return output


# ---------------------------------------------------------------------------
# Google Places API (اختیاری)
# ---------------------------------------------------------------------------

def google_places_search(query, max_results=5):
    if not GOOGLE_API_KEY:
        return []

    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_API_KEY,
        "X-Goog-FieldMask": (
            "places.id,places.displayName,places.formattedAddress,"
            "places.rating,places.userRatingCount,places.googleMapsUri,places.photos"
        ),
    }
    body = {
        "textQuery": query + " in Yazd, Iran",
        "languageCode": "fa",
        "maxResultCount": max_results,
    }
    response = requests.post(url, headers=headers, json=body, timeout=20)
    response.raise_for_status()
    return response.json().get("places", [])


def google_photo_bytes(photo_name):
    if not GOOGLE_API_KEY or not photo_name:
        return None
    url = f"https://places.googleapis.com/v1/{photo_name}/media"
    response = requests.get(
        url,
        params={"maxWidthPx": 900, "key": GOOGLE_API_KEY},
        timeout=20,
    )
    response.raise_for_status()
    return io.BytesIO(response.content)


async def send_suggestions(chat, section_key):
    query = SUGGESTION_QUERIES.get(section_key)
    if not query:
        await chat.send_message("برای این بخش پیشنهاد مکان تعریف نشده است.")
        return

    if not GOOGLE_API_KEY:
        url = "https://www.google.com/maps/search/?api=1&query=" + requests.utils.quote(query + " Yazd Iran")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔎 جست‌وجوی این پیشنهادها در گوگل‌مپ", url=url)]
        ])
        await chat.send_message(
            "برای نمایش امتیاز، تعداد نظر و عکس واقعی مکان‌ها باید کلید Google Places API تنظیم شود.\n"
            "فعلاً می‌توانید پیشنهادهای این دسته را در گوگل‌مپ ببینید.",
            reply_markup=keyboard,
        )
        return

    try:
        places = await asyncio.to_thread(google_places_search, query, 5)
    except Exception:
        logger.exception("Google Places request failed")
        await chat.send_message(
            "دریافت پیشنهادها از گوگل فعلاً ممکن نشد. لطفاً کمی بعد دوباره امتحان کنید."
        )
        return

    if not places:
        await chat.send_message("برای این دسته نتیجه‌ای پیدا نشد.")
        return

    await chat.send_message("⭐ پیشنهادهای مکان برای این دسته:")
    for place in places:
        name = place.get("displayName", {}).get("text", "مکان پیشنهادی")
        address = place.get("formattedAddress", "نشانی ثبت نشده")
        rating = place.get("rating")
        count = place.get("userRatingCount")
        maps_url = place.get("googleMapsUri") or (
            "https://www.google.com/maps/search/?api=1&query=" +
            requests.utils.quote(name + " Yazd")
        )
        rating_line = f"⭐ امتیاز: {rating}/5" if rating is not None else "⭐ امتیاز ثبت نشده"
        count_line = f" | {count} نظر" if count is not None else ""
        caption = f"📍 {name}\n{rating_line}{count_line}\n{address}"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗺 مشاهده در گوگل‌مپ", url=maps_url)]
        ])

        photos = place.get("photos") or []
        sent_photo = False
        if photos:
            try:
                photo = await asyncio.to_thread(google_photo_bytes, photos[0].get("name"))
                if photo:
                    await chat.send_photo(photo=photo, caption=caption[:1024], reply_markup=keyboard)
                    sent_photo = True
            except Exception:
                logger.exception("Could not fetch Google place photo")
        if not sent_photo:
            await chat.send_message(caption, reply_markup=keyboard)


# ---------------------------------------------------------------------------
# بازی‌ها
# ---------------------------------------------------------------------------

QUESTION_BANK = {
    "quiz": [
        ("یزد به چه معماری سنتی مشهوری شناخته می‌شود؟",
         ["بادگیرها", "خانه‌های چوبی", "کلبه‌های برفی", "برج‌های شیشه‌ای"], 0,
         "بادگیرها از نمادهای معماری سازگار با اقلیم گرم و خشک یزد هستند."),
        ("کدام بنای یزد به کاشی‌کاری فیروزه‌ای و مناره‌های بلند معروف است؟",
         ["مسجد جامع یزد", "باغ فین", "سی‌وسه‌پل", "ارگ بم"], 0,
         "مسجد جامع یزد از شاخص‌ترین بناهای تاریخی شهر است."),
        ("کدام شهر نزدیک یزد به نارین‌قلعه معروف است؟",
         ["میبد", "رشت", "بوشهر", "سنندج"], 0,
         "نارین‌قلعه از آثار تاریخی شناخته‌شده میبد است."),
        ("کدام باغ در یزد به بادگیر بلند خود معروف است؟",
         ["باغ دولت‌آباد", "باغ ارم", "باغ شازده", "باغ فین"], 0,
         "باغ دولت‌آباد از باغ‌های تاریخی مشهور یزد است."),
    ],
    "food": [
        ("قطاب یزدی معمولاً با چه موادی تهیه می‌شود؟",
         ["مغز بادام یا گردو", "ماهی", "سیب‌زمینی", "ذرت"], 0,
         "قطاب معمولاً با مغز بادام یا گردو و پوشش پودر قند تهیه می‌شود."),
        ("کدام مورد از شیرینی‌های معروف یزد است؟",
         ["حاجی‌بادام", "باقلوا استانبولی", "گز اصفهان", "کلمپه کرمان"], 0,
         "حاجی‌بادام از شیرینی‌های شناخته‌شده یزد است."),
        ("در تهیه عطر باقلوای یزدی معمولاً از چه چیزی استفاده می‌شود؟",
         ["هل و گلاب", "سرکه", "فلفل", "ریحان"], 0,
         "هل و گلاب از عطرهای رایج شیرینی‌های سنتی یزد هستند."),
    ],
    "tf": [
        ("بافت تاریخی یزد در فهرست میراث جهانی یونسکو ثبت شده است.", ["درست", "غلط"], 0,
         "بافت تاریخی یزد در سال ۲۰۱۷ در فهرست میراث جهانی ثبت شد."),
        ("میبد به سفال و سرامیک هم شناخته می‌شود.", ["درست", "غلط"], 0,
         "میبد از مراکز شناخته‌شده سفال و سرامیک منطقه است."),
        ("بادگیرها برای سازگاری با آب‌وهوای گرم و خشک کاربرد داشته‌اند.", ["درست", "غلط"], 0,
         "بادگیرها به تهویه و خنک‌سازی طبیعی ساختمان کمک می‌کردند."),
        ("باغ دولت‌آباد در شهر شیراز قرار دارد.", ["درست", "غلط"], 1,
         "باغ دولت‌آباد در یزد قرار دارد."),
    ],
    "photo": [
        ("حدس بزن این مکان کدام است؟\n\nراهنما: مسجدی با ایوان بلند و مناره‌های شاخص در بافت تاریخی یزد.",
         ["مسجد جامع یزد", "باغ دولت‌آباد", "نارین‌قلعه", "کاروانسرای زین‌الدین"], 0,
         "پاسخ: مسجد جامع یزد. برای نسخه تصویری این بازی، کلید Google Places API را تنظیم کنید تا عکس مکان‌ها دریافت شود."),
        ("حدس بزن این مکان کدام است؟\n\nراهنما: باغی تاریخی با بادگیر بلند و جوی‌های آب.",
         ["باغ دولت‌آباد", "امیرچخماق", "دخمه‌ها", "چک‌چک"], 0,
         "پاسخ: باغ دولت‌آباد. در حالت دارای کلید گوگل، می‌توان عکس مکان را هم به سؤال پیوست کرد."),
        ("حدس بزن این مکان کدام است؟\n\nراهنما: شهری نزدیک یزد که نارین‌قلعه در آن قرار دارد.",
         ["میبد", "مهریز", "ابرکوه", "تفت"], 0,
         "پاسخ: میبد."),
    ],
}

# وضعیت دورهای فعال در حافظه است؛ امتیازها در SQLite ماندگارند.
ACTIVE_ROUNDS = {}


def build_question(game_type):
    if game_type == "mix":
        game_type = random.choice(["quiz", "photo", "food", "tf"])
    question, options, answer, explanation = random.choice(QUESTION_BANK[game_type])
    return game_type, question, options, answer, explanation


async def start_game(chat, game_type):
    actual_type, question, options, answer, explanation = build_question(game_type)
    round_id = uuid.uuid4().hex[:8]
    ACTIVE_ROUNDS[round_id] = {
        "chat_id": chat.id,
        "type": actual_type,
        "answer": answer,
        "explanation": explanation,
        "answered": False,
        "players": set(),
    }

    labels = {
        "quiz": "🧠 مسابقه اطلاعات یزد",
        "photo": "📸 حدس مکان",
        "food": "🍬 حدس خوراکی یزدی",
        "tf": "✅ درست یا غلط",
    }
    keyboard = []
    for index, option in enumerate(options):
        keyboard.append([
            InlineKeyboardButton(option, callback_data=f"ans:{round_id}:{index}")
        ])
    keyboard.append([
        InlineKeyboardButton("🏆 جدول امتیازها", callback_data="leader"),
        InlineKeyboardButton("🔄 سؤال بعدی", callback_data=f"next:{game_type}"),
    ])

    # در بازی حدس مکان، در صورت تنظیم کلید Google Places عکس واقعی مکان ارسال می‌شود.
    if actual_type == "photo" and GOOGLE_API_KEY:
        if "مسجدی" in question:
            photo_query = "Jameh Mosque Yazd"
        elif "باغی" in question:
            photo_query = "Dowlat Abad Garden Yazd"
        else:
            photo_query = "Narin Qaleh Meybod"
        try:
            photo_places = await asyncio.to_thread(google_places_search, photo_query, 1)
            if photo_places and photo_places[0].get("photos"):
                photo_data = await asyncio.to_thread(
                    google_photo_bytes, photo_places[0]["photos"][0].get("name")
                )
                if photo_data:
                    await chat.send_photo(
                        photo=photo_data,
                        caption="📸 به عکس نگاه کن و مکان را حدس بزن!",
                    )
        except Exception:
            logger.exception("Photo quiz image could not be loaded")

    await chat.send_message(
        f"{labels.get(actual_type, '🎮 بازی')}\n\n{question}\n\n"
        "برای بازی گروهی، اعضای گروه می‌توانند پاسخ دهند؛ اولین پاسخ درست امتیاز می‌گیرد.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_leaderboard(chat):
    rows = get_leaderboard(10)
    if not rows:
        await chat.send_message("هنوز امتیازی ثبت نشده است. اولین نفری باش که بازی می‌کند! 🎮")
        return
    lines = ["🏆 جدول برترین بازیکنان ربات یزد\n"]
    medals = ["🥇", "🥈", "🥉"]
    for index, (name, points, games, correct) in enumerate(rows, start=1):
        medal = medals[index - 1] if index <= 3 else f"{index}."
        lines.append(f"{medal} {name} — {points} امتیاز | {correct} پاسخ درست از {games} بازی")
    await chat.send_message("\n".join(lines))


# ---------------------------------------------------------------------------
# دستورها
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(WELCOME, reply_markup=main_kb())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "دستورهای ربات:\n"
        "/start — منوی اصلی\n"
        "/help — راهنما\n"
        "/search کلمه — جست‌وجو\n"
        "/random — پیشنهاد تصادفی\n"
        "/map شناسه — ارسال موقعیت (مثلاً /map jameh)\n"
        "/routes — نقشه مسیرهای گردشگری\n"
        "/games — بازی و سرگرمی\n"
        "/leaderboard — جدول امتیازها\n"
        "/sos — شماره‌های اضطراری\n"
        "/about — درباره ربات"
    )


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total = sum(len(section[1]) for section in SECTIONS.values())
    await update.effective_message.reply_text(
        f"این ربات {len(SECTIONS)} بخش و {total} مطلب گردشگری دارد.\n"
        "امکانات: نقشه، موقعیت تلگرام، لینک گوگل‌مپ، پیشنهاد مکان و بازی‌های امتیازی."
    )


async def sos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🚨 شماره‌های اضطراری ایران:\nپلیس: ۱۱۰\nاورژانس: ۱۱۵\nآتش‌نشانی: ۱۲۵"
    )


async def random_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    iid = random.choice(list(ITEMS))
    title, description, section_key, _ = ITEMS[iid]
    await update.effective_message.reply_text(
        f"🎲 پیشنهاد تصادفی:\n\n{title}\n\n{description}",
        reply_markup=item_kb(iid, section_key),
    )


def search_items(query):
    query = query.strip()
    if not query:
        return []
    return [
        (iid, value[0], value[2])
        for iid, value in ITEMS.items()
        if query in value[0] or query in value[1]
    ][:10]


async def send_search_results(message, query):
    results = search_items(query)
    if not results:
        await message.reply_text(
            "چیزی پیدا نشد. کلمه دیگری امتحان کنید.",
            reply_markup=main_kb(),
        )
        return
    keyboard = [
        [InlineKeyboardButton(title, callback_data=f"item:{iid}")]
        for iid, title, _ in results
    ]
    keyboard.append([InlineKeyboardButton("🏠 منوی اصلی", callback_data="home")])
    await message.reply_text(
        f"نتایج جست‌وجوی «{query}»:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query:
        await update.effective_message.reply_text("مثال: /search باغ")
        return
    await send_search_results(update.effective_message, query)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message and update.effective_message.text:
        await send_search_results(update.effective_message, update.effective_message.text)


async def map_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0] not in ITEMS:
        await update.effective_message.reply_text("مثال: /map jameh")
        return
    iid = context.args[0]
    coords = ITEMS[iid][3]
    if not coords:
        await update.effective_message.reply_text("برای این مطلب مختصات ثبت نشده است.")
        return
    await update.effective_message.reply_location(latitude=coords[0], longitude=coords[1])


async def routes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_routes(update.effective_message)


async def send_routes(message):
    image = create_route_map()
    await message.reply_photo(
        photo=image,
        caption=(
            "🗺 نقشه شماتیک مسیرهای گردشگری اطراف یزد\n"
            "زمان‌ها و فاصله‌ها تقریبی‌اند. برای ترافیک و مسیر زنده، لینک گوگل‌مپ را باز کنید."
        ),
        reply_markup=routes_kb(),
    )


async def games_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🎮 به بخش بازی و سرگرمی خوش آمدید!\n"
        "می‌توانید انفرادی بازی کنید یا در گروه با دوستان رقابت کنید.",
        reply_markup=game_menu_kb(),
    )


async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_leaderboard(update.effective_message)


# ---------------------------------------------------------------------------
# مدیریت دکمه‌ها
# ---------------------------------------------------------------------------

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, _, key = query.data.partition(":")

    if action == "home":
        await query.edit_message_text(WELCOME, reply_markup=main_kb())

    elif action == "sec" and key in SECTIONS:
        await query.edit_message_text(SECTIONS[key][0], reply_markup=section_kb(key))

    elif action == "item" and key in ITEMS:
        # جزئیات و عکس جاذبه در یک پیام جداگانه ارسال می‌شود.
        await send_item_details(query.message, key)

    elif action == "loc" and key in ITEMS:
        coords = ITEMS[key][3]
        if coords:
            await query.message.reply_location(latitude=coords[0], longitude=coords[1])

    elif action == "sug" and key in SUGGESTION_QUERIES:
        await send_suggestions(query.message.chat, key)

    elif action == "routes":
        await send_routes(query.message)

    elif action == "games":
        await query.edit_message_text(
            "🎮 بخش بازی و سرگرمی\nیک بازی انتخاب کن:",
            reply_markup=game_menu_kb(),
        )

    elif action == "play":
        game_type = key if key in ("mix", "quiz", "photo", "food", "tf") else "mix"
        await start_game(query.message.chat, game_type)

    elif action == "next":
        game_type = key if key in ("mix", "quiz", "photo", "food", "tf") else "mix"
        await start_game(query.message.chat, game_type)

    elif action == "leader":
        await show_leaderboard(query.message.chat)

    elif action == "ans":
        try:
            round_id, answer_text = key.split(":")
            selected = int(answer_text)
        except (ValueError, TypeError):
            return

        game = ACTIVE_ROUNDS.get(round_id)
        if not game or game["chat_id"] != query.message.chat_id:
            await query.message.reply_text("این دور بازی منقضی شده است. یک دور تازه شروع کنید.")
            return
        if game["answered"]:
            await query.message.reply_text("این سؤال قبلاً پاسخ داده شده است.")
            return

        user = query.from_user
        game["players"].add(user.id)
        is_correct = selected == game["answer"]

        if is_correct:
            game["answered"] = True
            points = 10
            record_answer(user, game["type"], True, points)
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                f"🎉 آفرین {user.full_name}!\n"
                f"پاسخ درست بود و {points} امتیاز گرفتی.\n\n"
                f"💡 {game['explanation']}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 سؤال بعدی", callback_data="next:mix")],
                    [InlineKeyboardButton("🏆 جدول برترین‌ها", callback_data="leader")],
                ]),
            )
        else:
            # پاسخ اشتباه ثبت می‌شود، اما امتیاز منفی ندارد.
            record_answer(user, game["type"], False, 0)
            await query.message.reply_text(f"{user.first_name}، این گزینه درست نیست؛ دوباره تلاش کن!")

    else:
        await query.answer("این گزینه شناخته نشد.", show_alert=True)


# ---------------------------------------------------------------------------
# راه‌اندازی
# ---------------------------------------------------------------------------

def main():
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "BOT_TOKEN تنظیم نشده است.\n"
            "ابتدا توکن ربات را در متغیر محیطی BOT_TOKEN قرار دهید."
        )

    init_db()
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("about", about))
    app.add_handler(CommandHandler("sos", sos))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("random", random_item))
    app.add_handler(CommandHandler("map", map_cmd))
    app.add_handler(CommandHandler("routes", routes_cmd))
    app.add_handler(CommandHandler("games", games_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))

    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info("Yazd travel bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
