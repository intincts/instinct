import argparse
import email
import imaplib
import json
import os
import re
from datetime import datetime, timedelta
from email.header import decode_header
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright


ZHIPUAI_API_KEY = os.environ.get("ZHIPUAI_API_KEY", "c925d670d0224059b9af016d2d284c27.0jE0Q5B79BpywXUP")
ZHIPUAI_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MODEL = os.environ.get("ZHIPUAI_MODEL", "glm-4-flash")

FIELD_NAMES = (
    "company, position, assessLink, deadline, emailAccount, passInfo, "
    "字段找不到时使用空字符串"
)

ASSESSMENT_MAIL_KEYWORDS = re.compile(
    r"测评|在线考试|线上考试|笔试|能力测试|人才测评|"
    r"测试链接|登录账号|通行证|assessment|test invitation",
    re.IGNORECASE,
)


class MailboxNotConfigured(Exception):
    pass


def persisted_mail_setting(name):
    value = os.environ.get(name, "").strip()
    if value:
        return value

    config_root = os.environ.get("WELCOME_SCREEN_CONFIG_DIR", "").strip()
    config_dir = (
        Path(config_root)
        if config_root
        else Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "WelcomeScreen"
    )
    try:
        config = json.loads((config_dir / "mail_config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ""
    return str(config.get(name) or "").strip()


def fetch_page_text(url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)
        page_text = page.inner_text("body")
        context.close()
        browser.close()

    return " ".join(page_text.split())[:12000]


def call_ai(user_content, system_content):
    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {ZHIPUAI_API_KEY}",
        "Content-Type": "application/json",
    }

    response = requests.post(
        ZHIPUAI_URL,
        headers=headers,
        json=payload,
        timeout=90,
    )
    response.raise_for_status()
    raw = response.json()["choices"][0]["message"]["content"].strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


def extract_page_info(page_text):
    system_content = (
        "你是招聘信息提取助手，只输出JSON对象，不要输出任何多余文字。"
        f"字段：{FIELD_NAMES}。测评截止时间尽量格式化为 YYYY-MM-DD HH:mm。"
    )
    return call_ai(f"网页文本：{page_text}", system_content)


def extract_mail_info(mail_text):
    system_content = (
        "你是招聘测评邮件信息提取助手，只输出JSON数组，不要输出任何多余文字。"
        "数组中的每一项代表一封测评邮件，"
        f"字段：{FIELD_NAMES}。"
        "company 填公司名称，规则可宽松："
        "公司名与岗位连写时（如“英科 - 国际销售（上海）”）取公司名（如“英科”）；"
        "其余情况按邮件原文保留公司全称（如“中国航发南方工业有限公司”），不要删减或改写；"
        "找不到明确名称时，可结合主题、HR签名等线索给出最可能的公司名，确实没有才留空；"
        "position 填对应的岗位名称（例如“国际销售（上海）”），没有则留空；"
        "deadline 必须来自邮件原文：邮件写“3个工作日内”就填“3个工作日内”，"
        "写具体日期时间就按原文填写；邮件没有明确写截止时间就填空字符串，"
        "严禁推算、编造或猜测日期。"
        "emailAccount 填写邮件中提供的测评登录账号（如 15907551840@163.com），"
        "passInfo 填写邮件中提供的测评登录密码或通行证（如 627169），没有则留空。"
    )
    return call_ai(f"邮件正文：{mail_text}", system_content)


def detect_assessment_messages(messages):
    candidates = [
        message for message in messages
        if ASSESSMENT_MAIL_KEYWORDS.search(
            f"{message.get('subject', '')} {message.get('body', '')}"
        )
    ]
    if not candidates:
        return []

    payload = [
        {
            "subject": message.get("subject", ""),
            "body": message.get("body", "")[:1800],
        }
        for message in candidates[:20]
    ]
    system_content = (
        "你是招聘邮件筛选器。只判断哪些邮件是在通知候选人参加测评、"
        "笔试或线上考试，不解析具体字段。只输出JSON数组，数组元素是"
        "候选邮件的0开始序号，不要输出解释。"
    )
    try:
        result = call_ai(
            json.dumps(payload, ensure_ascii=False),
            system_content,
        )
        if isinstance(result, dict):
            result = result.get("indexes") or result.get("assessmentEmails") or []
        selected = []
        for index in result if isinstance(result, list) else []:
            if isinstance(index, str) and index.isdigit():
                index = int(index)
            if isinstance(index, int) and 0 <= index < len(candidates):
                selected.append(candidates[index])
        return selected
    except Exception:
        return candidates


def normalize_records(payload):
    if isinstance(payload, dict):
        if isinstance(payload.get("updates"), list):
            payload = payload["updates"]
        elif isinstance(payload.get("data"), list):
            payload = payload["data"]
        else:
            payload = [payload]

    if not isinstance(payload, list):
        return []

    records = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        record = {
            "company": text_value(item.get("company") or item.get("公司名称") or item.get("公司")),
            "position": text_value(item.get("position") or item.get("jobTitle") or item.get("职位")),
            "assessLink": text_value(
                item.get("assessLink")
                or item.get("assessmentLink")
                or item.get("测评链接")
                or item.get("笔试链接")
            ),
            "deadline": text_value(
                item.get("deadline")
                or item.get("assessmentDeadline")
                or item.get("测评截止时间")
                or item.get("截止时间")
            ),
            "email": text_value(
                item.get("emailAccount")
                or item.get("email")
                or item.get("mail")
                or item.get("邮件账号")
                or item.get("测评账号")
                or item.get("邮箱账号")
            ),
            "passInfo": text_value(
                item.get("passInfo")
                or item.get("pass")
                or item.get("passport")
                or item.get("通行证")
                or item.get("通行证信息")
            ),
        }
        if record["company"]:
            records.append(record)
    return records


def text_value(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def decode_header_value(value):
    if not value:
        return ""
    parts = []
    for chunk, charset in decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts)


def message_body(message):
    chunks = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace")
        if content_type == "text/html":
            text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.I | re.S)
            text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            chunks.append(text)
    return " ".join(chunks)


def fetch_mailbox_texts(count=50):
    host = persisted_mail_setting("JOB_MAIL_IMAP_HOST")
    address = persisted_mail_setting("JOB_MAIL_ADDRESS")
    password = persisted_mail_setting("JOB_MAIL_PASSWORD")
    missing = []
    if not host:
        missing.append("JOB_MAIL_IMAP_HOST")
    if not address:
        missing.append("JOB_MAIL_ADDRESS")
    if not password:
        missing.append("JOB_MAIL_PASSWORD")
    if missing:
        raise MailboxNotConfigured(
            "邮箱配置不完整，缺少：" + "、".join(missing)
        )

    port = int(os.environ.get("JOB_MAIL_IMAP_PORT", "993"))
    folder = os.environ.get("JOB_MAIL_FOLDER", "INBOX")
    count = max(1, count)
    sender = os.environ.get("JOB_MAIL_SENDER", "").strip()

    print(f"[连接] {host}:{port} / {address} / 文件夹 {folder} / 最近 {count} 条")

    mailbox = imaplib.IMAP4_SSL(host, port)
    try:
        try:
            mailbox.login(address, password)
        except imaplib.IMAP4.error as exc:
            if "163.com" in host or "126.com" in host or "yeah.net" in host:
                raise RuntimeError(
                    f"邮箱登录失败（{host}）：网易邮箱的 JOB_MAIL_PASSWORD 必须填「授权码」，"
                    "不能填登录密码。请在 163 邮箱网页版：设置 → POP3/SMTP/IMAP → "
                    "开启 IMAP 服务，生成授权码后重新填写。原始错误：" + str(exc)
                ) from exc
            raise

        imaplib.Commands["ID"] = ("AUTH",)
        mailbox._simple_command(
            "ID",
            '("name" "IMAPClient" "version" "1.0.0")',
        )

        status, data = mailbox.select(folder)
        if status != "OK":
            detail = data[0] if data and data[0] else ""
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", errors="replace")
            raise RuntimeError(f"无法打开邮箱文件夹 {folder}：{detail or status}")
        criteria = ["ALL"]
        if sender:
            criteria = ["FROM", sender]
        status, data = mailbox.search(None, *criteria)
        if status != "OK":
            return []

        ids = data[0].split()[-count:]
        messages = []
        for message_id in ids:
            status, fetched = mailbox.fetch(message_id, "(BODY.PEEK[])")
            if status != "OK" or not fetched or not fetched[0]:
                continue
            message = email.message_from_bytes(fetched[0][1])
            subject = decode_header_value(message.get("Subject"))
            body = message_body(message)
            if body:
                messages.append({
                    "subject": subject,
                    "body": body,
                    "from": decode_header_value(message.get("From")),
                    "date": decode_header_value(message.get("Date")),
                })
        return messages
    finally:
        try:
            mailbox.logout()
        except Exception:
            pass


def refresh_mailbox(count=50):
    messages = fetch_mailbox_texts(count)
    if not messages:
        return [], "邮箱中没有读取到最近邮件"

    assessment_messages = detect_assessment_messages(messages)
    if not assessment_messages:
        return [], "最近邮件中没有检测到测评邮件"

    combined = "\n\n".join(
        f"主题：{message['subject']} 正文：{message['body']}"
        for message in assessment_messages
    )[:30000]
    payload = extract_mail_info(combined)
    return normalize_records(payload), ""


def refresh_url(url):
    page_text = fetch_page_text(url)
    payload = extract_page_info(page_text)
    return normalize_records(payload)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-input", action="store_true")
    parser.add_argument("--count", type=int, default=50, choices=[50, 100, 300],
                        help="获取最近多少条邮件（50/100/300）")
    args = parser.parse_args()

    if not args.url:
        args.no_input = True

    if not args.url and not args.json and not args.no_input:
        args.url = input("url:").strip()

    message = ""
    updates = []
    try:
        if args.url:
            updates = refresh_url(args.url)
        else:
            updates, message = refresh_mailbox(args.count)
    except MailboxNotConfigured as exc:
        message = str(exc)
    except Exception as exc:
        if args.json:
            raise
        raise

    if args.json:
        print(json.dumps(
            {"updates": updates, "message": message},
            ensure_ascii=False,
        ))
        return

    if message:
        print(message)
    for update in updates:
        print(update)


if __name__ == "__main__":
    main()
