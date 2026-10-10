import hashlib
import json
import logging
import os
import re
import uuid

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BOT_ID = os.environ.get("BOT_ID")
BOT_ALIAS_ID = os.environ.get("BOT_ALIAS_ID")
LOCALE_ID = os.environ.get("LOCALE_ID", "en_US")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers":
        "Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token",
    "Access-Control-Allow-Methods": "OPTIONS,POST",
    "Content-Type": "application/json",
}

_lex_client = None


def get_lex():
    global _lex_client
    if _lex_client is None:
        import boto3
        _lex_client = boto3.client("lexv2-runtime")
    return _lex_client


def api_response(status, bot_texts):
    body = {
        "messages": [
            {"type": "unstructured", "unstructured": {"text": t}} for t in bot_texts
        ]
    }
    return {"statusCode": status, "headers": CORS_HEADERS, "body": json.dumps(body)}


def parse_body(event):
    if "body" not in event:
        return event
    body = event["body"]
    if isinstance(body, str):
        body = json.loads(body) if body else {}
    return body or {}


def extract_text(body):
    try:
        return body["messages"][0]["unstructured"]["text"].strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""


def get_session_id(event, body):
    candidate = body.get("sessionId") if isinstance(body, dict) else None
    if not candidate:
        ctx = (event.get("requestContext") or {}).get("identity") or {}
        seed = f"{ctx.get('sourceIp', '')}|{ctx.get('userAgent', '')}"
        candidate = hashlib.sha256(seed.encode()).hexdigest()[:32] if seed != "|" \
            else uuid.uuid4().hex
    candidate = re.sub(r"[^0-9a-zA-Z._:-]", "-", str(candidate))[:100]
    return candidate if len(candidate) >= 2 else uuid.uuid4().hex


def lambda_handler(event, context):
    logger.info("Request event: %s", json.dumps(event))
    if event.get("httpMethod") == "OPTIONS":
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}
    try:
        body = parse_body(event)
    except json.JSONDecodeError:
        return api_response(400, ["Sorry, I couldn't read that request."])

    user_text = extract_text(body)
    if not user_text:
        return api_response(400, ["Please type a message."])

    session_id = get_session_id(event, body)

    try:
        resp = get_lex().recognize_text(
            botId=BOT_ID,
            botAliasId=BOT_ALIAS_ID,
            localeId=LOCALE_ID,
            sessionId=session_id,
            text=user_text,
        )
    except Exception:
        logger.exception("Lex call failed")
        return api_response(500, ["Sorry, something went wrong talking to the bot."])

    logger.info("Lex response messages: %s", json.dumps(resp.get("messages", [])))

    texts = [m["content"] for m in resp.get("messages", []) if m.get("content")]
    if not texts:
        texts = ["Sorry, I didn't catch that. Could you rephrase?"]
    return api_response(200, texts)