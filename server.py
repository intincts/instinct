import ast
import json
import os
import subprocess
import sys
import threading
import requests
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
from playwright.sync_api import sync_playwright

app = Flask(__name__, static_folder='.', static_url_path='')
BASE_DIR = Path(__file__).resolve().parent
MAIL_REFRESH_LOCK = threading.Lock()
MAIL_CONFIG_LOCK = threading.Lock()
_config_root = os.environ.get("WELCOME_SCREEN_CONFIG_DIR", "").strip()
MAIL_CONFIG_DIR = (
    Path(_config_root)
    if _config_root
    else Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "WelcomeScreen"
)
MAIL_CONFIG_PATH = MAIL_CONFIG_DIR / "mail_config.json"

ZHIPUAI_API_KEY = os.environ.get("ZHIPUAI_API_KEY", "c925d670d0224059b9af016d2d284c27.0jE0Q5B79BpywXUP")
ZHIPUAI_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MODEL = os.environ.get("ZHIPUAI_MODEL", "glm-4-flash")

IMAP_HOSTS = {
    "qq.com": "imap.qq.com",
    "163.com": "imap.163.com",
    "126.com": "imap.126.com",
    "gmail.com": "imap.gmail.com",
    "outlook.com": "outlook.office365.com",
    "hotmail.com": "outlook.office365.com",
}


def infer_imap_host(address):
    domain = address.rsplit("@", 1)[-1].lower()
    return IMAP_HOSTS.get(domain, f"imap.{domain}")


def normalize_mail_config(host, address, password):
    host = (host or "").strip()
    address = (address or "").strip()
    password = str(password or "")

    # 兼容旧配置中把邮箱地址填到 HOST、把授权码填到 ADDRESS 的情况。
    if "@" in host:
        if address and "@" not in address and not password:
            password = address
        address = address if "@" in address else host
        host = infer_imap_host(address)

    return host, address, password


def write_persisted_mail_config(config):
    with MAIL_CONFIG_LOCK:
        MAIL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        temp_path = MAIL_CONFIG_PATH.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        temp_path.replace(MAIL_CONFIG_PATH)


def load_persisted_mail_config():
    try:
        config = json.loads(MAIL_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return

    host = os.environ.get("JOB_MAIL_IMAP_HOST") or config.get("JOB_MAIL_IMAP_HOST", "")
    address = os.environ.get("JOB_MAIL_ADDRESS") or config.get("JOB_MAIL_ADDRESS", "")
    password = os.environ.get("JOB_MAIL_PASSWORD") or config.get("JOB_MAIL_PASSWORD", "")
    host, address, password = normalize_mail_config(host, address, password)
    normalized = {
        "JOB_MAIL_IMAP_HOST": host,
        "JOB_MAIL_ADDRESS": address,
    }
    if password:
        normalized["JOB_MAIL_PASSWORD"] = password

    for key, value in normalized.items():
        if value:
            os.environ[key] = str(value).strip()
        else:
            os.environ.pop(key, None)

    if normalized != config:
        try:
            write_persisted_mail_config(normalized)
        except OSError:
            pass


load_persisted_mail_config()


# CORS 头
@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response


@app.route('/')
def index():
    return send_from_directory('.', '岗位.html')


@app.route('/api/health')
def health():
    return jsonify({"ok": True, "model": MODEL})


@app.route('/api/mail-config', methods=['GET', 'POST', 'OPTIONS'])
def mail_config():
    if request.method == 'OPTIONS':
        return '', 204

    if request.method == 'GET':
        return jsonify({
            "ok": True,
            "data": {
                "host": os.environ.get("JOB_MAIL_IMAP_HOST", ""),
                "address": os.environ.get("JOB_MAIL_ADDRESS", ""),
                "password": os.environ.get("JOB_MAIL_PASSWORD", ""),
                "passwordConfigured": bool(os.environ.get("JOB_MAIL_PASSWORD", ""))
            }
        })

    data = request.get_json(silent=True) or {}
    host = (data.get('host') or '').strip()
    address = (data.get('address') or '').strip()
    password = str(data.get('password') or '')
    stored_address = os.environ.get("JOB_MAIL_ADDRESS", "").strip()
    existing_password = os.environ.get("JOB_MAIL_PASSWORD", "")
    # 更换邮箱地址时丢弃旧授权码，避免残留授权码被误存到新邮箱。
    if address and stored_address and address.lower() != stored_address.lower():
        existing_password = ""
    if password:
        existing_password = password
    host, address, existing_password = normalize_mail_config(
        host, address, existing_password
    )

    if host and "@" in host:
        return jsonify({
            "ok": False,
            "error": "JOB_MAIL_IMAP_HOST 应填写 imap.qq.com 这类服务器地址"
        }), 400
    if address and "@" not in address:
        return jsonify({
            "ok": False,
            "error": "JOB_MAIL_ADDRESS 应填写邮箱地址；授权码请填到 JOB_MAIL_PASSWORD"
        }), 400

    config = {"JOB_MAIL_IMAP_HOST": host, "JOB_MAIL_ADDRESS": address}
    if existing_password:
        config["JOB_MAIL_PASSWORD"] = existing_password

    try:
        write_persisted_mail_config(config)
    except OSError as e:
        return jsonify({"ok": False, "error": f"保存邮箱配置失败: {e}"}), 500

    for key in ("JOB_MAIL_IMAP_HOST", "JOB_MAIL_ADDRESS", "JOB_MAIL_PASSWORD"):
        value = config.get(key, "")
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)

    return jsonify({
        "ok": True,
        "data": {
            "host": host,
            "address": address,
            "passwordConfigured": bool(existing_password)
        }
    })


@app.route('/api/parse', methods=['POST', 'OPTIONS'])
def parse():
    if request.method == 'OPTIONS':
        return '', 204
    data = request.get_json(silent=True)
    if not data or 'url' not in data:
        return jsonify({"ok": False, "error": "缺少 url 参数"}), 400
    url = data['url'].strip()
    if not url:
        return jsonify({"ok": False, "error": "url 不能为空"}), 400

    try:
        page_text = fetch_page_text(url)
    except Exception as e:
        return jsonify({"ok": False, "error": f"抓取页面失败: {str(e)}"}), 500

    try:
        info = extract_info_with_ai(page_text)
    except Exception as e:
        return jsonify({"ok": False, "error": f"AI 提取失败: {str(e)}"}), 500

    return jsonify({"ok": True, "data": info})


@app.route('/api/refresh-mail', methods=['POST', 'OPTIONS'])
def refresh_mail():
    if request.method == 'OPTIONS':
        return '', 204

    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    count = normalize_fetch_count(data.get('count'))

    if not url:
        missing = [
            name for name in ("JOB_MAIL_IMAP_HOST", "JOB_MAIL_ADDRESS", "JOB_MAIL_PASSWORD")
            if not os.environ.get(name, "").strip()
        ]
        if missing:
            return jsonify({
                "ok": False,
                "error": "请先完整填写邮箱服务器地址、邮箱地址和授权码后再刷新"
            }), 400

    try:
        result = run_mail_refresh(url, count=count)
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "邮箱刷新超时，请稍后重试"}), 504
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({
        "ok": True,
        "data": result["updates"],
        "message": result.get("message", "")
    })


# test.py 的 --count 只接受 50/100/300，这里保持一致的取值范围。
def normalize_fetch_count(value):
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if count in (50, 100, 300) else None


def run_mail_refresh(url='', count=None):
    command = [sys.executable, str(BASE_DIR / 'test.py'), '--json', '--no-input']
    if url:
        command.extend(['--url', url])
    if count:
        command.extend(['--count', str(count)])

    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'

    with MAIL_REFRESH_LOCK:
        completed = subprocess.run(
            command,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            env=env,
            timeout=300
        )

    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout or '').strip()
        raise RuntimeError(f"test.py 运行失败: {details[-800:]}")

    payload = parse_test_output(completed.stdout)
    updates = normalize_test_payload(payload)
    message = payload.get('message', '') if isinstance(payload, dict) else ''
    return {"updates": updates, "message": message}


def parse_test_output(output):
    text = (output or '').strip()
    if not text:
        raise ValueError("test.py 没有输出数据")

    candidates = [text]
    if text.startswith('```'):
        lines = text.splitlines()
        candidates.append('\n'.join(lines[1:-1]).strip())

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (TypeError, ValueError):
            pass
        try:
            return ast.literal_eval(candidate)
        except (SyntaxError, ValueError):
            pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in '[{':
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
            return value
        except ValueError:
            continue

    raise ValueError("无法解析 test.py 输出，请让脚本输出 JSON")


def normalize_test_payload(payload):
    if isinstance(payload, dict):
        if isinstance(payload.get('updates'), list):
            payload = payload['updates']
        elif isinstance(payload.get('data'), list):
            payload = payload['data']
        else:
            payload = [payload]

    if not isinstance(payload, list):
        raise ValueError("test.py 输出必须是 JSON 对象或数组")

    aliases = {
        'company': ('company', 'companyName', '公司名称', '公司'),
        'position': ('position', 'jobTitle', '职位名称', '职位', '岗位'),
        'assessLink': (
            'assessLink', 'assessmentLink', 'assessmentUrl', 'testLink',
            '测评链接', '笔试链接'
        ),
        'deadline': (
            'deadline', 'assessmentDeadline', '测评截止时间', '测评截止',
            '截止时间'
        ),
        'email': (
            'emailAccount', 'email', 'mail', 'emailInfo', '邮件账号',
            '测评账号', '邮箱账号', '邮件', '邮箱'
        ),
        'passInfo': (
            'passInfo', 'pass', 'passport', '通行证', '通行证信息'
        )
    }

    updates = []
    for item in payload:
        if not isinstance(item, dict):
            continue

        record = {}
        for field, names in aliases.items():
            value = ''
            for name in names:
                if name in item and item[name] not in (None, ''):
                    value = item[name]
                    break
            record[field] = stringify_value(value)

        if record['company']:
            updates.append(record)

    return updates


def stringify_value(value):
    if value is None:
        return ''
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def fetch_page_text(url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)
        page_text = page.inner_text("body")
        browser.close()
    page_text = " ".join(page_text.split())
    if len(page_text) > 3000:
        page_text = page_text[:3000]
    return page_text


def extract_info_with_ai(page_text):
    messages = [
        {"role": "system",
         "content": "你是招聘信息提取助手，只输出JSON，不要任何多余文字。字段：company, position, salary, location。找不到填空字符串。"},
        {"role": "user", "content": f"网页文本：{page_text}"}
    ]
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0
    }
    headers = {
        "Authorization": f"Bearer {ZHIPUAI_API_KEY}",
        "Content-Type": "application/json"
    }
    resp = requests.post(ZHIPUAI_URL, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"]
    # 清理可能的代码块
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    info = json.loads(raw)
    # 确保字段存在
    for k in ['company', 'position', 'salary', 'location']:
        info.setdefault(k, '')
    return info


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
