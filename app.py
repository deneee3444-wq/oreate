import base64
import html
import json
import mimetypes
import os
import random
import re
import string
import time
import urllib.parse
import uuid
import threading
import urllib3

import requests
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA
from flask import Flask, render_template, request, redirect, session, jsonify, Response, send_file, url_for

# Disable SSL warning logs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ====================================================================
#  OREATEBOT CLASS (Copied from oreto.py)
# ====================================================================

class OreateBot:
    """Oreate AI tam otomatik signup + email verify (spamok ile) + resim/video üretme."""

    BASE = "https://www.oreateai.com"
    INBOX_URL = "https://api.spamok.com/v2/EmailBox/{local}"
    MAIL_URL  = "https://api.spamok.com/v2/Email/{local}/{mail_id}"

    FIXED_PASSWORD = "Winci500@"

    COMMON_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/150.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "client-type": "pc",
        "locale": "en-US",
        "Origin": "https://www.oreateai.com",
        "Referer": "https://www.oreateai.com/home/index",
    }

    def __init__(self, email_length: int = 15, http_timeout: int = 20, debug: bool = False):
        self.email_length = email_length
        self.http_timeout = http_timeout
        self.debug = debug
        self.session = requests.Session()
        self.session.verify = False # Bypass SSL certificate validation error
        self.session.headers.update(self.COMMON_HEADERS)
        self.email = ""
        self._init_session()
        self.total_points_used = 0

    def _log(self, *a):
        if self.debug:
            print("[dbg]", *a)

    def _get_cookie_safe(self, name: str) -> str:
        for cookie in self.session.cookies:
            if cookie.name == name:
                return cookie.value
        return ""

    def _init_session(self):
        ts_ms = int(time.time() * 1000)
        ts_hex = hex(ts_ms)[2:]
        rand_hex = "".join(random.choices("0123456789abcdef", k=11))
        bid_val = ts_hex + rand_hex

        for domain in ["www.oreateai.com", ".oreateai.com", "oreateai.com"]:
            try:
                self.session.cookies.clear(domain=domain, name="__bid_n")
            except:
                pass
            self.session.cookies.set("__bid_n", bid_val, domain=domain, path="/")
        try:
            r = self.session.get(
                f"{self.BASE}/home/index",
                timeout=self.http_timeout,
                allow_redirects=True,
            )
            self._log(f"Init session: status={r.status_code}, cookies={list(self.session.cookies.keys())}")
        except Exception as e:
            self._log(f"Init session hatası (önemsiz): {e}")

    # ---------------- email ----------------
    def _random_local(self, n: int) -> str:
        alphabet = string.ascii_lowercase + string.digits
        return "".join(random.SystemRandom().choice(alphabet) for _ in range(n))

    def generate_email(self) -> str:
        return f"{self._random_local(self.email_length)}@spamok.com"

    def generate_password(self) -> str:
        return self.FIXED_PASSWORD

    # ---------------- oreate api ----------------
    def get_ticket(self) -> tuple[str, str]:
        r = self.session.get(f"{self.BASE}/passport/api/getticket",
                             timeout=self.http_timeout)
        r.raise_for_status()
        d = r.json()["data"]
        return d["ticketID"], d["pk"]

    @staticmethod
    def rsa_encrypt(plaintext: str, pub_pem: str) -> str:
        key = RSA.import_key(pub_pem)
        cipher = PKCS1_v1_5.new(key)
        return base64.b64encode(cipher.encrypt(plaintext.encode())).decode()

    @staticmethod
    def _fake_jt() -> str:
        payload = {"k": "0", "t": str(int(time.time() * 1000))}
        return "31$" + base64.b64encode(json.dumps(payload).encode()).decode()

    def signup(self, email: str, password: str) -> dict:
        ticket_id, pk = self.get_ticket()
        enc_pw = self.rsa_encrypt(password, pk)
        body = {
            "fr": "main",
            "email": email,
            "ticketID": ticket_id,
            "password": enc_pw,
            "jt": "" #self._fake_jt(),
        }
        r = self.session.post(
            f"{self.BASE}/passport/api/emailsignupin",
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=self.http_timeout,
        )
        r.raise_for_status()
        return r.json()

    def confirm_email(self, email: str, token_id: str) -> dict:
        referer = f"{self.BASE}/home/index?email={email.replace('@', '%40')}&tokenID={token_id}"
        body = {
            "email": email,
            "tokenID": token_id,
            "plat": "pc",
            "fr": "main",
            "fissionCode": "",
            "inviteCode": "",
            "jt": "" #self._fake_jt(),
        }
        r = self.session.post(
            f"{self.BASE}/passport/api/emailregisterconfirm",
            json=body,
            headers={
                "Content-Type": "application/json",
                "Referer": referer,
            },
            timeout=self.http_timeout,
        )
        r.raise_for_status()
        return r.json()

    # ---------------- spamok ----------------
    def _inbox(self, local: str) -> list[dict]:
        r = requests.get(self.INBOX_URL.format(local=local),
                         timeout=self.http_timeout)
        r.raise_for_status()
        return r.json().get("mails", []) or []

    def _mail_body(self, local: str, mail_id: int) -> str:
        r = requests.get(self.MAIL_URL.format(local=local, mail_id=mail_id),
                         timeout=self.http_timeout)
        r.raise_for_status()
        data = r.json()
        return data.get("messageHtml") or data.get("messagePlain") or ""

    @staticmethod
    def _extract_token(body: str) -> str | None:
        if not body:
            return None
        m = re.search(r"tokenID=([A-Za-z0-9\-]+)", body)
        return m.group(1) if m else None

    def wait_verify_token(self, email: str, timeout: int = 180, interval: float = 2.0) -> str:
        local = email.split("@")[0]
        deadline = time.time() + timeout

        while time.time() < deadline:
            mails = self._inbox(local)
            self._log(f"inbox={len(mails)} mails")

            for m in mails:
                if "oreate" not in m.get("subject", "").lower():
                    continue
                body = self._mail_body(local, m["id"])
                token = self._extract_token(body)
                if token:
                    return token

            time.sleep(interval)

        raise TimeoutError("Verify tokeni bulunamadı")

    def get_user_info(self, referer: str | None = None) -> dict:
        """
        /oreate/user/getuserinfo — pointGrantInfo döner ve sunucu tarafında
        daily refresh bonusunu (+30) tetikler.
        """
        if not referer:
            referer = f"{self.BASE}/home/index"
        r = self.session.get(
            f"{self.BASE}/oreate/user/getuserinfo",
            headers={
                "Accept": "application/json, text/plain, */*",
                "cache-control": "no-cache, no-store",
                "pragma": "no-cache",
                "Referer": referer,
            },
            timeout=self.http_timeout,
        )
        r.raise_for_status()
        data = r.json()
        self._log(f"getuserinfo response: {json.dumps(data, ensure_ascii=False)[:500]}")

        grant = (data.get("data") or {}).get("pointGrantInfo") or {}
        if grant and grant.get("pointGrant"):
            total = grant.get("pointGrant", 0)
            msg = grant.get("pointGrantMsg", "")
            print(f"[bonus] pointGrant={total}  →  {msg}")
        return data

    def trigger_daily_bonus(self, label: str = "", referer: str | None = None) -> None:
        """getuserinfo'yu güvenle çağırır; hata olsa da akışı kesmez."""
        try:
            self.get_user_info(referer=referer)
        except Exception as e:
            prefix = f"[{label}] " if label else ""
            self._log(f"{prefix}getuserinfo hatası (önemsiz): {e}")

    def run(self) -> dict:
        email = self.generate_email()
        password = self.generate_password()
        self.email = email
        print(f"[+] email:    {email}")
        print(f"[+] password: {password}")

        signup_resp = self.signup(email, password)
        self._log(f"signup response: {signup_resp}")

        token_id = self.wait_verify_token(email)
        self._log(f"token: {token_id}")

        confirm_resp = self.confirm_email(email, token_id)
        self._log(f"confirm response: {confirm_resp}")

        doc_id = (confirm_resp.get("data") or {}).get("docId") or ""
        base_path = f"/home/chat/aiImage/{doc_id}" if doc_id else "/home/chat/aiImage"
        landing_referer = (
            f"{self.BASE}{base_path}"
            f"?email={urllib.parse.quote(email)}&tokenID={token_id}"
        )

        try:
            self.session.get(landing_referer, timeout=self.http_timeout, allow_redirects=True)
        except Exception as e:
            self._log(f"landing get hatası (önemsiz): {e}")

        self.trigger_daily_bonus("signup", referer=landing_referer)

        ouss = self._get_cookie_safe("ouss")
        self._log(f"ouss cookie: {ouss}")

        return {
            "email": email,
            "password": password,
            "tokenID": token_id,
            "signup": signup_resp,
            "confirm": confirm_resp,
            "ouss": ouss,
            "isLogin": confirm_resp.get("data", {}).get("isLogin", False),
        }

    def login(self, email: str, password: str) -> dict:
        self._init_session()
        ticket_id, pk = self.get_ticket()
        enc_pw = self.rsa_encrypt(password, pk)
        body = {
            "fr": "GGSEMHTML",
            "email": email,
            "ticketID": ticket_id,
            "password": enc_pw,
            "jt": "" #self._fake_jt(),
        }
        r = self.session.post(
            f"{self.BASE}/passport/api/emaillogin",
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=self.http_timeout,
        )
        r.raise_for_status()
        res = r.json()

        if res.get("status", {}).get("code") == 0:
            self.email = email
            landing_referer = f"{self.BASE}/home/chat/aiImage"
            try:
                self.session.get(landing_referer, timeout=self.http_timeout, allow_redirects=True)
            except Exception:
                pass
            self.trigger_daily_bonus("login", referer=landing_referer)
        return res

    def apply_cookies(self, ouss: str, ouid: str = "", email: str = "") -> None:
        """Cookie ile giriş + daily bonus tetikleyicisi."""
        if not ouss:
            raise ValueError("ouss boş olamaz")
        self.session.cookies.set("ouss", ouss, domain="www.oreateai.com", path="/")
        self.session.cookies.set("ouss", ouss, domain=".oreateai.com", path="/")
        if ouid:
            ouid_val = ouid if (":" in ouid) else f"{ouid}:FG=1"
            self.session.cookies.set("OUID", ouid_val, domain="www.oreateai.com", path="/")
            self.session.cookies.set("OUID", ouid_val, domain=".oreateai.com", path="/")
        if email:
            self.email = email
        landing_referer = f"{self.BASE}/home/chat/aiImage"
        try:
            self.session.get(landing_referer, timeout=self.http_timeout, allow_redirects=True)
        except Exception:
            pass
        self.trigger_daily_bonus("cookie-login", referer=landing_referer)

    def get_credits(self) -> int:
        headers = {
            "cache-control": "no-cache, no-store",
            "pragma": "no-cache",
            "Referer": f"{self.BASE}/home/chat/aiImage",
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
        }
        r = self.session.get(
            f"{self.BASE}/bizapi/point/getrestpoints",
            headers=headers,
            timeout=self.http_timeout,
        )
        r.raise_for_status()
        data = r.json()
        self._log(f"Kredi raw response: {json.dumps(data, ensure_ascii=False)}")
        status_code = data.get("status", {}).get("code", -1)
        if status_code != 0:
            return -1
        return data.get("data", {}).get("restPoint", 0)

    def log_credits(self, label: str = "") -> int:
        try:
            rest = self.get_credits()
            return rest
        except Exception:
            return -1

    def get_image_models(self) -> list:
        r = self.session.get(f"{self.BASE}/oreate/img/getmodelconfig",
                             timeout=self.http_timeout)
        r.raise_for_status()
        return r.json().get("data", {}).get("factory", [])

    def get_video_models(self) -> list:
        r = self.session.get(f"{self.BASE}/oreate/aivideo/getmodelconfigv3",
                             timeout=self.http_timeout)
        r.raise_for_status()
        return r.json().get("data", {}).get("models", [])

    def upload_file(self, file_path: str) -> tuple[str, str, str, int]:
        file_path = os.path.abspath(file_path)
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"Dosya bulunamadı: {file_path}")

        filename = os.path.basename(file_path)
        _, dot_ext = os.path.splitext(filename)
        ext = dot_ext.lstrip(".")
        size = os.path.getsize(file_path)
        upload_key = f"upload-{int(time.time() * 1000)}-0"

        payload = {
            "mFileList": [{"filename": upload_key, "fileExt": ext, "size": size}],
            "source": "aiImage",
        }
        r = self.session.post(
            f"{self.BASE}/oreate/convert/getuploadbostoken",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=self.http_timeout,
        )
        r.raise_for_status()
        resp = r.json()
        status_code = resp.get("status", {}).get("code", -1)
        if status_code != 0 or "data" not in resp:
            err = resp.get("status", {}).get("errMsg", "Bilinmeyen API hatası (Giriş yapılmamış olabilir)")
            raise RuntimeError(f"Oreate API upload token hatası: {err} (code={status_code})")

        key_name = f"{upload_key}.{ext}"
        key_data = resp["data"]["KeyList"][key_name]
        bucket = key_data["bucket"]
        object_path = key_data["objectPath"]
        session_key = key_data["sessionkey"]

        encoded_name = urllib.parse.quote(object_path, safe="")
        init_url = (
            f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o"
            f"?uploadType=resumable&name={encoded_name}"
        )
        content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        init_headers = {
            "Authorization": f"Bearer {session_key}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": content_type,
            "X-Upload-Content-Length": str(size),
            "Origin": "https://www.oreateai.com",
        }
        init_r = requests.post(
            init_url,
            headers=init_headers,
            json={"name": object_path, "contentType": content_type},
            timeout=30,
        )
        init_r.raise_for_status()
        upload_url = init_r.headers.get("Location") or init_r.headers.get("location")

        if not upload_url:
            raise RuntimeError("GCS upload URL alınamadı")

        with open(file_path, "rb") as f:
            file_data = f.read()

        up_headers = {
            "Content-Type": content_type,
            "Content-Length": str(size),
            "Origin": "https://www.oreateai.com",
        }
        up_r = requests.put(upload_url, data=file_data, headers=up_headers, timeout=120)
        up_r.raise_for_status()

        return object_path, filename, ext, size

    def create_chat(self, chat_type: str) -> str:
        r = self.session.post(
            f"{self.BASE}/oreate/create/chat",
            json={"type": chat_type, "docId": ""},
            headers={
                "Content-Type": "application/json",
                "Origin": "https://www.oreateai.com",
            },
            timeout=self.http_timeout,
        )
        r.raise_for_status()
        resp = r.json()
        status_code = resp.get("status", {}).get("code", -1)
        if status_code != 0 or "data" not in resp:
            err = resp.get("status", {}).get("errMsg", "Bilinmeyen API hatası (Giriş yapılmamış olabilir)")
            raise RuntimeError(f"Oreate API chat hatası: {err} (code={status_code})")
        chat_id = resp["data"]["chatId"]
        return chat_id

    def _parse_sse(self, response) -> list[dict]:
        events = []
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            try:
                data = json.loads(line[6:])
                events.append(data)
            except json.JSONDecodeError:
                continue
        return events

    def _build_extra(self) -> dict:
        return {
            "email": self.email,
            "vip": "0",
            "reg_ts": int(time.time()),
            "deviceID": self._get_cookie_safe("OUID"),
            "bid": self._get_cookie_safe("__bid_n"),
            "doc_name": "",
            "module_name": "gpt4o",
        }

    def generate_image(self, prompt: str, chat_id: str,
                        model_name: str = "Google Nano Banana 2 Lite",
                        ratio: str = "1:1", resolution: str = "1K",
                        attachments: list = None) -> list[dict]:
        payload = {
            "jt": "",
            "ua": self.COMMON_HEADERS["User-Agent"],
            "js_env": "h5",
            "extra": self._build_extra(),
            "clientType": "pc",
            "type": "chat",
            "chatType": "aiImage",
            "chatTitle": "Unnamed Session",
            "focusId": chat_id,
            "chatId": chat_id,
            "from": "home",
            "messages": [{"role": "user", "content": prompt, "attachments": attachments or []}],
            "imageConfig": {
                "modelName": model_name,
                "ratio": ratio,
                "resolution": resolution,
            },
            "isFirst": True,
        }

        r = self.session.post(
            f"{self.BASE}/oreate/sse/stream",
            json=payload,
            headers={
                "Accept": "text/event-stream",
                "Content-Type": "application/json",
                "Origin": "https://www.oreateai.com",
                "Referer": f"{self.BASE}/home/chat/aiImage/{chat_id}",
            },
            stream=True,
            timeout=300,
        )
        r.raise_for_status()
        return self._parse_sse(r)

    def generate_video(self, prompt: str, chat_id: str,
                        model_name: str = "Wan 2.7",
                        resolution: str = "720", duration: int = 5,
                        is_audio: bool = True, ai_type: int = 14068,
                        attachments: list = None, mode: str = "image",
                        ref_duration: str | None = None) -> list[dict]:
        video_config = {
            "modelName": model_name,
            "ratio": "",
            "resolution": str(resolution),
            "duration": duration,
            "isAudio": is_audio,
            "aiType": ai_type,
        }

        att_urls = [a["bos_url"] for a in (attachments or [])]

        if mode == "frame_based":
            if len(att_urls) < 2:
                raise ValueError("First & Last Frame modu için 2 resim gerekli (first, last).")
            video_config["scene"] = "frame_based"
            video_config["frameBased"] = {
                "firstFrame": att_urls[0],
                "lastFrame":  att_urls[1],
            }

        elif mode == "reference":
            if not att_urls:
                raise ValueError("Reference modu için en az 1 resim gerekli.")
            video_config["scene"] = "reference"
            video_config["referenceImages"] = [{"image": u} for u in att_urls]
            if ref_duration:
                video_config["refDuration"] = ref_duration

        elif mode == "motion":
            if not att_urls:
                raise ValueError("Motion modu için 1 resim gerekli.")
            video_config["scene"] = "motion"
            video_config["motion"] = {
                "image": att_urls[0],
                "motDuration": duration,
            }
        else:
            video_config["scene"] = "text_or_image"
            if att_urls:
                video_config["textOrImage"] = {"image": att_urls[0]}
            else:
                video_config["textOrImage"] = {"text": prompt}

        payload = {
            "jt": "",
            "ua": self.COMMON_HEADERS["User-Agent"],
            "js_env": "h5",
            "extra": self._build_extra(),
            "clientType": "pc",
            "type": "chat",
            "chatType": "aiVideo",
            "chatTitle": "Unnamed Session",
            "focusId": chat_id,
            "chatId": chat_id,
            "from": "home",
            "messages": [{"role": "user", "content": prompt, "attachments": attachments or []}],
            "videoConfig": video_config,
            "isFirst": True,
        }

        r = self.session.post(
            f"{self.BASE}/oreate/sse/stream",
            json=payload,
            headers={
                "Accept": "text/event-stream",
                "Content-Type": "application/json",
                "Origin": "https://www.oreateai.com",
                "Referer": f"{self.BASE}/home/chat/aiVideo/{chat_id}",
            },
            stream=True,
            timeout=600,
        )
        r.raise_for_status()
        return self._parse_sse(r)

    @staticmethod
    def extract_result_url(events: list[dict]) -> str | None:
        for ev in events:
            if ev.get("event") == "generating":
                result = ev.get("data", {}).get("result", "")
                m_vid = re.search(r'src=["\'](https?://[^"\']+)["\']', result)
                if m_vid:
                    return m_vid.group(1)
                m = re.search(r"!\[.*?\]\((https?://[^\)]+)\)", result)
                if m:
                    return m.group(1)
                if result.startswith("http"):
                    return result.strip()
        return None

# ====================================================================
#  POINT COST CALCULATION HELPERS
# ====================================================================

def find_image_point_cost(models: list, model_name: str, resolution: str) -> int:
    for factory in models:
        for m in factory.get("models", []):
            if m["modelName"] == model_name:
                for pc in m.get("pointCost", []):
                    if pc.get("resolution", "").lower() == resolution.lower():
                        return pc["point"]
                costs = m.get("pointCost", [])
                if costs:
                    return costs[0]["point"]
    return 0

def find_video_point_cost(models: list, model_name: str, resolution: str,
                          duration: int, is_audio: bool,
                          mode: str = "image",
                          ref_duration: str | None = None
                          ) -> tuple[int, int]:
    for m in models:
        if m["modelName"] != model_name:
            continue

        if mode == "motion":
            for pc in m.get("pointCostMotion", []):
                if (str(pc.get("resolution")).lower() == str(resolution).lower()
                        and pc.get("motDuration") == duration):
                    return pc["point"], pc["aiType"]
            return 0, 0

        if mode == "reference":
            costs = m.get("pointCostReference", [])
            if ref_duration:
                for pc in costs:
                    if (str(pc.get("resolution")).lower() == str(resolution).lower()
                            and pc.get("duration") == duration
                            and pc.get("refDuration") == ref_duration):
                        return pc["point"], pc["aiType"]
            for pc in costs:
                if (str(pc.get("resolution")).lower() == str(resolution).lower()
                        and pc.get("duration") == duration):
                    return pc["point"], pc["aiType"]
            return 0, 0

        costs = m.get("pointCostImage", [])
        for pc in costs:
            if (str(pc.get("resolution")).lower() == str(resolution).lower()
                    and pc.get("duration") == duration
                    and pc.get("audio", False) == is_audio):
                return pc["point"], pc["aiType"]
        for pc in costs:
            if (str(pc.get("resolution")).lower() == str(resolution).lower()
                    and pc.get("duration") == duration):
                return pc["point"], pc["aiType"]
        if costs:
            return costs[0]["point"], costs[0]["aiType"]
    return 0, 0

# ====================================================================
#  FLASK WEB APPLICATION
# ====================================================================

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'oreto-studio-glass-secret-2026')
PANEL_PASSWORD = os.environ.get('PANEL_PASSWORD', '123')

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Global memory states
bot = None
bot_lock = threading.Lock()

tasks = {}
tasks_lock = threading.Lock()

gallery_items = {}
gallery_lock = threading.Lock()

saved_prompts = {}
prompts_lock = threading.Lock()

def get_bot():
    global bot
    if bot is None:
        bot = OreateBot(debug=False)
        try:
            print("[+] Otomatik ilk kayıt başlatılıyor (80 kredi için)...")
            res = bot.run()
            time.sleep(1.0)
            bot.trigger_daily_bonus("signup_extra")
            print(f"[+] Otomatik ilk kayıt başarılı: {res.get('email')}")
        except Exception as e:
            print(f"[-] Otomatik ilk kayıt hatası: {e}")
    return bot

# ----------------- UI / AUTH ROUTES -----------------

@app.route('/')
def index():
    show_login = not session.get('logged_in', False)
    error = request.args.get('error')
    return render_template('index.html', show_login=show_login, error=error)

@app.route('/login', methods=['POST'])
def login():
    password = request.form.get('password')
    if password == PANEL_PASSWORD:
        session['logged_in'] = True
        return redirect(url_for('index'))
    return redirect(url_for('index', error='1'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# ----------------- ACCOUNT MANAGEMENT -----------------

@app.route('/api/signup', methods=['POST'])
def api_signup():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        with bot_lock:
            b = get_bot()
            res = b.run()
            # Wait 1.0s and re-trigger daily checkin to ensure point settlement
            time.sleep(1.0)
            b.trigger_daily_bonus("signup_extra")
            time.sleep(0.5)
            credits = b.get_credits()
        return jsonify({
            "email": res.get("email"),
            "password": res.get("password"),
            "ouss": res.get("ouss"),
            "credits": credits
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/login-oreate', methods=['POST'])
def api_login_oreate():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json or {}
    email = data.get('email')
    password = data.get('password')
    if not email or not password:
        return jsonify({"error": "E-posta ve şifre gereklidir"}), 400
    try:
        with bot_lock:
            b = get_bot()
            res = b.login(email, password)
            code = res.get("status", {}).get("code", -1)
            if code == 0:
                credits = b.get_credits()
                return jsonify({"ok": True, "credits": credits})
            else:
                err = res.get("status", {}).get("errMsg", "Giriş başarısız")
                return jsonify({"error": err}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/cookie-login', methods=['POST'])
def api_cookie_login():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json or {}
    ouss = data.get('ouss')
    ouid = data.get('ouid', '')
    email = data.get('email', '')
    if not ouss:
        return jsonify({"error": "ouss çerezi gereklidir"}), 400
    try:
        with bot_lock:
            b = get_bot()
            b.apply_cookies(ouss, ouid, email)
            credits = b.get_credits()
        return jsonify({"ok": True, "credits": credits})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/daily-bonus', methods=['POST'])
def api_daily_bonus():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        with bot_lock:
            b = get_bot()
            before = b.get_credits()
            b.trigger_daily_bonus("manual")
            after = b.get_credits()
            diff = after - before if (after >= 0 and before >= 0) else 0
        return jsonify({
            "ok": True, 
            "message": f"+{diff} kredi eklendi" if diff > 0 else "Ek kredi verilmedi veya zaten alındı.",
            "credits": after
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/credits', methods=['GET'])
def api_credits():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        with bot_lock:
            b = get_bot()
            credits = b.get_credits()
        return jsonify({"credits": credits})
    except Exception as e:
        return jsonify({"credits": -1, "error": str(e)})

# ----------------- MODELS -----------------

@app.route('/api/models', methods=['GET'])
def api_models():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        with bot_lock:
            b = get_bot()
            img_factories = b.get_image_models()
            vid_models = b.get_video_models()
        
        # Flatten image models for client usage
        img_models = []
        for factory in img_factories:
            factory_name = factory.get("modelFactoryName", "Diğer")
            factory_icon = factory.get("modelIcon", "")
            for model in factory.get("models", []):
                model_copied = model.copy()
                model_copied["factoryName"] = factory_name
                # If modelIcon is empty, use factory's icon
                if not model_copied.get("modelIcon"):
                    model_copied["modelIcon"] = factory_icon
                img_models.append(model_copied)

        return jsonify({
            "image_models": img_models,
            "video_models": vid_models
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ----------------- GENERATION TASK PIPELINE -----------------

def background_generate(task_id, mode, prompt, model, resolution, ratio, duration, audio, ai_type, scene, file_paths):
    try:
        def update_log(msg):
            with tasks_lock:
                if task_id in tasks:
                    tasks[task_id]['logs'].append(msg)
                    tasks[task_id]['status'] = 'İşleniyor'

        with bot_lock:
            b = get_bot()
            if not b._get_cookie_safe("ouss"):
                raise RuntimeError("Oreate oturumu bulunamadı. Lütfen sağ üstteki 'Yönetim' menüsünden 'Otomatik Yeni Hesap Aç' veya 'Giriş Yap' seçeneğini kullanarak giriş yapın.")

        attachments = []
        # Upload attachments one by one
        for i, fp in enumerate(file_paths):
            if not os.path.exists(fp):
                continue
            update_log(f"Dosya Oreate sunucularına yükleniyor ({i+1}/{len(file_paths)})...")
            with bot_lock:
                bos_url, ftitle, fext, fsize = b.upload_file(fp)
            attachments.append({
                "bos_url": bos_url, "doc_title": ftitle,
                "doc_type": fext, "size": fsize,
                "bosUrl": bos_url, "flag": "upload",
                "type": "file", "status": 1,
            })
            update_log(f"Dosya yüklendi: {ftitle}")

        if mode == 'image':
            update_log("Görüntü oturumu (chat) oluşturuluyor...")
            with bot_lock:
                chat_id = b.create_chat("aiImage")
            update_log("Görsel üretim isteği gönderildi. Sunucu bekleniyor...")
            with bot_lock:
                events = b.generate_image(
                    prompt, chat_id,
                    model_name=model,
                    ratio=ratio,
                    resolution=resolution,
                    attachments=attachments
                )
            result_url = OreateBot.extract_result_url(events)
        else:
            update_log("Video oturumu (chat) oluşturuluyor...")
            with bot_lock:
                chat_id = b.create_chat("aiVideo")
            update_log("Video üretim isteği gönderildi. Sunucu bekleniyor...")
            
            # map UI scene mode to api mode
            api_mode = 'image'
            if scene == 'frame_based':
                api_mode = 'frame_based'
            elif scene == 'reference':
                api_mode = 'reference'
            elif scene == 'motion':
                api_mode = 'motion'

            with bot_lock:
                events = b.generate_video(
                    prompt, chat_id,
                    model_name=model,
                    resolution=resolution,
                    duration=duration,
                    is_audio=audio,
                    ai_type=ai_type,
                    attachments=attachments,
                    mode=api_mode
                )
            result_url = OreateBot.extract_result_url(events)

        if result_url:
            update_log("Üretim başarıyla tamamlandı!")
            with tasks_lock:
                if task_id in tasks:
                    tasks[task_id]['status'] = 'Tamamlandı'
                    tasks[task_id]['result_url'] = result_url
        else:
            err_msg = "Üretim başarısız oldu veya zaman aşımına uğradı."
            for ev in events:
                if ev.get("event") == "generating" and ev.get("data", {}).get("errMsg"):
                    err_msg = ev.get("data", {}).get("errMsg")
                    break
                elif ev.get("event") == "error":
                    err_msg = str(ev.get("data", "Sunucu hatası"))
                    break
            
            update_log(f"Hata: {err_msg}")
            with tasks_lock:
                if task_id in tasks:
                    tasks[task_id]['status'] = 'Hata'
    
    except Exception as e:
        with tasks_lock:
            if task_id in tasks:
                tasks[task_id]['status'] = 'Hata'
                tasks[task_id]['logs'].append(f"İşlem hatası: {str(e)}")
    finally:
        # Clean local temporary uploads
        for fp in file_paths:
            try:
                if os.path.exists(fp):
                    os.remove(fp)
            except Exception:
                pass

@app.route('/start_task', methods=['POST'])
def start_task():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401
    
    mode = request.form.get('mode')
    prompt = request.form.get('prompt', '').strip()
    model = request.form.get('model')

    if not prompt:
        return jsonify({"error": "Prompt boş bırakılamaz"}), 400
    if not model:
        return jsonify({"error": "Model seçilmelidir"}), 400

    task_id = str(uuid.uuid4())[:8]
    local_saved_paths = []

    # Handle Uploaded files
    uploaded_files = request.files.getlist('images') or request.files.getlist('image') or []
    end_image = request.files.get('end_image')
    if end_image:
        uploaded_files.append(end_image)

    for f in uploaded_files:
        if f and f.filename:
            safe_name = f"{uuid.uuid4().hex}_{f.filename}"
            save_path = os.path.join(UPLOAD_FOLDER, safe_name)
            f.save(save_path)
            local_saved_paths.append(save_path)

    # Read configuration params
    resolution = request.form.get('resolution', '720')
    ratio = request.form.get('ratio', '1:1')
    duration_str = request.form.get('duration', '5')
    duration = int(duration_str) if (duration_str and duration_str.isdigit()) else 5
    audio = request.form.get('audio') == 'true'
    ai_type_str = request.form.get('ai_type', '0')
    ai_type = int(ai_type_str) if (ai_type_str and ai_type_str.isdigit()) else 0
    scene = request.form.get('scene', 'text_or_image')

    with tasks_lock:
        tasks[task_id] = {
            "id": task_id,
            "status": "Beklemede",
            "logs": ["Görev sıraya alındı, işlem başlatılıyor..."],
            "result_url": None,
            "result_type": mode,
            "prompt": prompt,
            "model": model,
            "mode": mode,
            "created_at": time.time()
        }

    # Start generation thread
    threading.Thread(
        target=background_generate,
        args=(task_id, mode, prompt, model, resolution, ratio, duration, audio, ai_type, scene, local_saved_paths),
        daemon=True
    ).start()

    return jsonify({"task_id": task_id})

@app.route('/task_status/<task_id>', methods=['GET'])
def task_status(task_id):
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({"error": "Görev bulunamadı"}), 404
    return jsonify(task)

@app.route('/get_tasks', methods=['GET'])
def get_tasks():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401
    with tasks_lock:
        return jsonify(tasks)

# ----------------- GALLERY STORAGE -----------------

@app.route('/get_gallery', methods=['GET'])
def get_gallery():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401
    with gallery_lock:
        items = list(gallery_items.values())
        # Sort by timestamp descending
        items.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
    return jsonify(items)

@app.route('/gallery_add', methods=['POST'])
def gallery_add():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json or {}
    item_id = data.get('id')
    if not item_id:
        return jsonify({"error": "ID gereklidir"}), 400
    with gallery_lock:
        gallery_items[item_id] = data
    return jsonify({"ok": True})

@app.route('/delete_gallery/<id>', methods=['DELETE'])
def delete_gallery(id):
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401
    with gallery_lock:
        gallery_items.pop(id, None)
    return jsonify({"ok": True})

@app.route('/clear_gallery', methods=['DELETE'])
def clear_gallery():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401
    with gallery_lock:
        gallery_items.clear()
    return jsonify({"ok": True})

# ----------------- PROMPT LIBRARY -----------------

@app.route('/get_prompts', methods=['GET'])
def get_prompts():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401
    with prompts_lock:
        return jsonify(list(saved_prompts.values()))

@app.route('/save_prompt', methods=['POST'])
def save_prompt():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json or {}
    text = data.get('text', '').strip()
    if not text:
        return jsonify({"error": "Prompt metni boş olamaz"}), 400
    p_id = str(uuid.uuid4())[:8]
    with prompts_lock:
        saved_prompts[p_id] = {"id": p_id, "text": text}
    return jsonify(saved_prompts[p_id])

@app.route('/delete_prompt/<id>', methods=['DELETE'])
def delete_prompt(id):
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401
    with prompts_lock:
        saved_prompts.pop(id, None)
    return jsonify({"ok": True})

# ----------------- PROXIES FOR GCS / CDN DOWNLOADS & CORS -----------------

@app.route('/proxy_image')
def proxy_image():
    url = request.args.get('url')
    if not url:
        return "URL missing", 400
    try:
        r = requests.get(url, stream=True, timeout=15, verify=False)
        r.raise_for_status()
        mime = r.headers.get('content-type', 'image/png')
        return Response(r.content, mimetype=mime)
    except Exception as e:
        return str(e), 500

@app.route('/proxy_video')
def proxy_video():
    url = request.args.get('url')
    dl = request.args.get('dl')
    if not url:
        return "URL missing", 400
    try:
        r = requests.get(url, stream=True, timeout=30, verify=False)
        r.raise_for_status()
        headers = {}
        if dl == '1':
            headers["Content-Disposition"] = "attachment; filename=orete_studio_video.mp4"
        return Response(r.content, mimetype="video/mp4", headers=headers)
    except Exception as e:
        return str(e), 500

# ----------------- RUN SERVER -----------------

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
