import base64
import json
import logging
import os
import ssl
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

QUEUE_URL = os.environ.get("QUEUE_URL")
OS_ENDPOINT = (os.environ.get("OS_ENDPOINT") or "").rstrip("/")
OS_USER = os.environ.g
OS_PASS = os.environ.get("OS_PASS")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL")
TABLE_NAME = os.environ.get("TABLE_NAME", "yelp-restaurants")
STATE_TABLE = os.environ
NUM_RESULTS = int(os.environ.get("NUM_RESULTS", "3"))
MAX_RECEIVES = int(os.environ.get("MAX_RECEIVES", "3"))
MAX_MESSAGES_PER_RUN = 10

_clients = {}


def client(name):
    """Create boto3 clients lazily (keeps local tests free of AWS setup)."""
    if name not in _clients:
        import boto3
        _clients[name] = boto3.client(name)
    return _clients[name]


def restaurants_table():
    if "restaurants" not in _clients:
        import boto3
        _clients["restaurants"] = boto3.resource("dynamodb").Table(TABLE_NAME)
    return _clients["restaurants"]


def state_table():
    if "state" not in _clients:
        import boto3
        _clients["state"] = boto3.resource("dynamodb").Table(STATE_TABLE)
    return _clients["state"]


def search_restaurant_ids(cuisine, size=NUM_RESULTS):
    query = {
        "size": size,
        "_source": ["RestaurantID", "Cuisine"],
        "query": {
            "function_score": {
                "query": {"match": {"Cuisine": cuisine}},
                "random_score": {},
                "boost_mode": "replace",
            }
        },
    }
    req = urllib.request.Request(
        f"{OS_ENDPOINT}/restaurants/_search",
        data=json.dumps(query).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Basic " + base64.b64encode(f"{OS_USER}:{OS_PASS}".encode()).decode(),
        },
    )
    with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    hits = data.get("hits", {}).get("hits", [])
    return [h["_source"]["RestaurantID"] for h in hits if "RestaurantID" in h.get("_source", {})]


def get_restaurant(business_id):
    return restaurants_table().get_item(Key={"BusinessID": business_id}).get("Item")


def load_state(user_id):
    """Saved last search for this user, or None. Never raises."""
    if not user_id:
        return None
    try:
        return state_table().get_item(Key={"UserID": user_id}).get("Item")
    except Exception:
        logger.exception("Could not read saved state for %s", user_id)
        return None


def save_state(user_id, request, restaurants):
    """Remember the search and what we suggested. Never raises (the email is already sent)."""
    if not user_id or not restaurants:
        return
    try:
        state_table().put_item(Item={
            "UserID": user_id,
            "location": request.get("location") or "N/A",
            "cuisine": request.get("cuisine") or "N/A",
            "recommendations": [
                {"BusinessID": r.get("BusinessID") or "N/A",
                 "Name": r.get("Name") or "N/A",
                 "Address": r.get("Address") or "N/A"}
                for r in restaurants
            ],
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        })
        logger.info("Saved last search for %s", user_id)
    except Exception:
        logger.exception("Could not save state for %s (email was already sent)", user_id)


def pretty_time(time_str):
    try:
        return datetime.strptime(time_str, "%H:%M").strftime("%-I:%M %p")
    except (TypeError, ValueError):
        return time_str or "your chosen time"


def pretty_day(date_str):
    if not date_str:
        return "your chosen day"
    try:
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        today = datetime.now().strftime("%Y-%m-%d")
    if date_str == today:
        return "today"
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%b %-d, %Y")
    except ValueError:
        return date_str


def build_email(request, restaurants, repeat=False):
    cuisine = request.get("cuisine", "")
    people = request.get("people", "")
    people_txt = f"{people} {'person' if str(people) == '1' else 'people'}"
    same = " (the same ones as last time)" if repeat else ""
    lines = [f"Hello! Here are my {cuisine} restaurant suggestions{same} for {people_txt}, "
             f"for {pretty_day(request.get('date'))} at {pretty_time(request.get('time'))}:", ""]
    for i, r in enumerate(restaurants, 1):
        lines.append(f"{i}. {r.get('Name', 'Unknown')}, located at {r.get('Address', 'address unavailable')}")
    lines += ["", "Enjoy your meal!"]
    return "\n".join(lines)


def send_email(to_address, subject, body):
    client("ses").send_email(
        Source=SENDER_EMAIL,
        Destination={"ToAddresses": [to_address]},
        Message={"Subject": {"Data": subject}, "Body": {"Text": {"Data": body}}},
    )


def process_request(request):
    cuisine = request["cuisine"]
    email = request["email"]
    user_id = request.get("userId")

    restaurants = None
    repeat = False

    if request.get("useLast"):
        prev = load_state(user_id)
        if (prev and prev.get("recommendations")
                and (prev.get("cuisine") or "").lower() == cuisine.lower()):
            restaurants = prev["recommendations"]
            repeat = True
            logger.info("Reusing %d saved restaurants for %s", len(restaurants), user_id)
        else:
            logger.info("useLast requested but nothing matching was saved - searching fresh")

    if restaurants is None:
        restaurants = []
        for rid in search_restaurant_ids(cuisine):
            item = get_restaurant(rid)
            if item:
                restaurants.append(item)
        logger.info("Found %d %s restaurants for %s", len(restaurants), cuisine, email)

    if not restaurants:
        body = (f"Hello! Sorry, I couldn't find any {cuisine} restaurants right now. "
                "Please try another cuisine.")
    else:
        body = build_email(request, restaurants, repeat=repeat)
    send_email(email, f"Your {cuisine} restaurant suggestions", body)

    if restaurants and not repeat:
        save_state(user_id, request, restaurants)


def lambda_handler(event, context):
    sqs = client("sqs")
    processed = 0

    for _ in range(MAX_MESSAGES_PER_RUN):
        resp = sqs.receive_message(
            QueueUrl=QUEUE_URL,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=0,
            VisibilityTimeout=60,
            AttributeNames=["ApproximateReceiveCount"],
        )
        messages = resp.get("Messages", [])
        if not messages:
            break

        msg = messages[0]
        receipt = msg["ReceiptHandle"]
        receives = int(msg.get("Attributes", {}).get("ApproximateReceiveCount", "1"))

        try:
            request = json.loads(msg["Body"])
            process_request(request)
        except Exception:
            logger.exception("Failed to process message (attempt %d): %s", receives, msg.get("Body"))
            if receives >= MAX_RECEIVES:
                logger.error("Dropping message after %d failed attempts", receives)
                sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=receipt)
            continue

        sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=receipt)  # only after a successful send
        processed += 1

    logger.info("Processed %d message(s)", processed)
    return {"processed": processed}
