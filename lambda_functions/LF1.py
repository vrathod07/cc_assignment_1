import json
import logging
import os
import re
from datetime import datetime, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

QUEUE_URL = os.environ.get("QUEUE_URL")
STATE_TABLE = os.environ.get("STATE_TABLE", "user-state")

# Keep in sync with: Lex CuisineType slot values, Yelp scraper CUISINES, OpenSearch.
SUPPORTED_CUISINES = {"chinese", "italian", "japanese", "mexican", "indian", "thai"}
SUPPORTED_LOCATIONS = {"manhattan", "new york", "new york city", "nyc", "ny"}
MAX_PARTY_SIZE = 20
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

YES_WORDS = {"yes", "y", "yeah", "yep", "yup", "sure", "ok", "okay", "please",
             "same", "definitely", "absolutely", "correct", "right"}
NO_WORDS = {"no", "n", "nope", "nah", "not", "different", "new"}

_sqs_client = None
_state_table = None


def get_sqs():
    global _sqs_client
    if _sqs_client is None:
        import boto3
        _sqs_client = boto3.client("sqs")
    return _sqs_client


def get_state_table():
    global _state_table
    if _state_table is None:
        import boto3
        _state_table = boto3.resource("dynamodb").Table(STATE_TABLE)
    return _state_table


def slot_value(slots, name):
    slot = (slots or {}).get(name)
    if not slot:
        return None
    value = slot.get("value") or {}
    return value.get("interpretedValue") or value.get("originalValue")


def slot_original(slots, name):
    slot = (slots or {}).get(name)
    if not slot:
        return None
    value = slot.get("value") or {}
    return value.get("originalValue") or value.get("interpretedValue")


def plain(text):
    return {"contentType": "PlainText", "content": text}


def elicit_slot(intent, slot_name, message, session_attrs):
    intent.setdefault("slots", {})[slot_name] = None
    return {
        "sessionState": {
            "sessionAttributes": session_attrs,
            "dialogAction": {"type": "ElicitSlot", "slotToElicit": slot_name},
            "intent": intent,
        },
        "messages": [plain(message)],
    }


def delegate(intent, session_attrs):
    return {
        "sessionState": {
            "sessionAttributes": session_attrs,
            "dialogAction": {"type": "Delegate"},
            "intent": intent,
        }
    }


def close(intent, message, session_attrs, state="Fulfilled"):
    """Tell Lex: this intent is finished; say `message` to the user."""
    intent["state"] = state
    return {
        "sessionState": {
            "sessionAttributes": session_attrs,
            "dialogAction": {"type": "Close"},
            "intent": intent,
        },
        "messages": [plain(message)],
    }


def now_local():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:  # tz database missing: fall back to server time
        return datetime.now()


def norm_location(value):
    v = (value or "").strip().lower()
    return "manhattan" if v in SUPPORTED_LOCATIONS else v


def interpret_yes_no(text):
    words = re.findall(r"[a-z']+", (text or "").lower())
    if not words:
        return None
    if words[0] in YES_WORDS:
        return True
    if words[0] in NO_WORDS:
        return False
    has_yes = any(w in YES_WORDS for w in words)
    has_no = any(w in NO_WORDS for w in words)
    if has_yes and not has_no:
        return True
    if has_no and not has_yes:
        return False
    return None


def get_last_search(user_id):
    if not user_id:
        return None
    try:
        return get_state_table().get_item(Key={"UserID": user_id}).get("Item")
    except Exception:
        logger.exception("Could not read last search for %s", user_id)
        return None


def same_search(prev, location, cuisine):
    if not prev or not prev.get("recommendations"):
        return False
    return (norm_location(prev.get("location")) == norm_location(location)
            and (prev.get("cuisine") or "").strip().lower() == (cuisine or "").strip().lower())


def same_search_question(prev, location, cuisine):
    names = [r.get("Name") for r in prev.get("recommendations", []) if r.get("Name")][:3]
    listing = f" I suggested: {', '.join(names)}." if names else ""
    return (f"Welcome back! Last time you searched for {cuisine} restaurants in {location}.{listing} "
            "Would you like the same recommendations as last time? (yes or no)")


def validate_dining(slots):
    location = slot_value(slots, "Location")
    if location and location.strip().lower() not in SUPPORTED_LOCATIONS:
        typed = slot_original(slots, "Location") or location
        return ("Location",
                f"Sorry, I can't fulfill requests for {typed}. "
                "I currently only cover Manhattan. Please enter a valid location.")

    cuisine = slot_value(slots, "Cuisine")
    if cuisine and cuisine.strip().lower() not in SUPPORTED_CUISINES:
        options = ", ".join(sorted(c.title() for c in SUPPORTED_CUISINES))
        return ("Cuisine",
                f"Sorry, I don't have {cuisine} restaurants yet. "
                f"Please choose one of: {options}.")

    people = slot_value(slots, "NumberOfPeople")
    if people:
        try:
            count = int(float(people))
        except (TypeError, ValueError):
            return ("NumberOfPeople",
                    "Please give me the party size as a whole number, like 2.")
        if count < 1 or count > MAX_PARTY_SIZE:
            return ("NumberOfPeople",
                    f"I can book for 1 to {MAX_PARTY_SIZE} people. "
                    "How many people are in your party?")

    date_str = slot_value(slots, "DiningDate")
    now = now_local()
    if date_str:
        try:
            dining_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return ("DiningDate", "I didn't understand that date. What date?")
        if dining_date < now.date():
            return ("DiningDate",
                    "That date has already passed. What date would you like?")

        time_str = slot_value(slots, "DiningTime")
        if time_str and dining_date == now.date():
            try:
                dining_time = datetime.strptime(time_str, "%H:%M").time()
                if dining_time < now.time():
                    return ("DiningTime",
                            "That time has already passed today. What time would you like?")
            except ValueError:
                pass  # unusual time format: let it through

    email = slot_value(slots, "Email")
    if email and not EMAIL_RE.match(email.strip()):
        return ("Email",
                "That doesn't look like a valid email address. "
                "Could you type it again?")

    return None


def handle_greeting(event):
    state = event["sessionState"]
    return close(state["intent"], "Hi there, how can I help?",
                 state.get("sessionAttributes") or {})


def handle_thank_you(event):
    state = event["sessionState"]
    return close(state["intent"], "You're welcome!",
                 state.get("sessionAttributes") or {})


def handle_dining(event):
    state = event["sessionState"]
    intent = state["intent"]
    slots = intent.get("slots") or {}
    attrs = dict(state.get("sessionAttributes") or {})
    user_id = event.get("sessionId")

    if event.get("invocationSource") == "DialogCodeHook":
        problem = validate_dining(slots)
        if problem:
            slot_name, message = problem
            return elicit_slot(intent, slot_name, message, attrs)

        answer = slot_value(slots, "UseLastSearch")
        if answer is not None:
            if interpret_yes_no(answer) is None:
                return elicit_slot(
                    intent, "UseLastSearch",
                    "Sorry, I didn't catch that. Would you like the same recommendations "
                    "as last time? (yes or no)", attrs)
        else:
            location = slot_value(slots, "Location")
            cuisine = slot_value(slots, "Cuisine")
            if location and cuisine and attrs.get("lastSearchChecked") != "true":
                attrs["lastSearchChecked"] = "true"
                prev = get_last_search(user_id)
                if same_search(prev, location, cuisine):
                    logger.info("Returning user %s repeated their search", user_id)
                    return elicit_slot(intent, "UseLastSearch",
                                       same_search_question(prev, location, cuisine), attrs)
        return delegate(intent, attrs)

    use_last = interpret_yes_no(slot_value(slots, "UseLastSearch")) is True
    payload = {
        "userId": user_id,
        "useLast": use_last,
        "location": slot_value(slots, "Location"),
        "cuisine": slot_value(slots, "Cuisine"),
        "date": slot_value(slots, "DiningDate"),
        "time": slot_value(slots, "DiningTime"),
        "people": int(float(slot_value(slots, "NumberOfPeople") or 0)),
        "email": (slot_value(slots, "Email") or "").strip(),
        "requestedAt": datetime.now(timezone.utc).isoformat(),
    }

    attrs.pop("lastSearchChecked", None)

    try:
        if not QUEUE_URL:
            raise RuntimeError("QUEUE_URL environment variable is not set")
        get_sqs().send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps(payload))
        logger.info("Pushed to SQS: %s", payload)
    except Exception:
        logger.exception("Failed to send request to SQS")
        return close(intent,
                     "Sorry, I couldn't save your request just now. Please try again in a moment.",
                     attrs, state="Failed")

    if use_last:
        msg = (f"You're all set. I'll email you the same {payload['cuisine']} recommendations "
               f"as last time at {payload['email']} shortly! Have a good day.")
    else:
        msg = (f"You're all set. Expect my {payload['cuisine']} suggestions at "
               f"{payload['email']} shortly! Have a good day.")
    return close(intent, msg, attrs)


HANDLERS = {
    "GreetingIntent": handle_greeting,
    "ThankYouIntent": handle_thank_you,
    "DiningSuggestionsIntent": handle_dining,
}


def lambda_handler(event, context):
    logger.info("Lex event: %s", json.dumps(event))
    state = event["sessionState"]
    intent_name = state["intent"]["name"]

    handler = HANDLERS.get(intent_name)
    if handler:
        return handler(event)

    return close(state["intent"],
                 "Sorry, I didn't catch that. I can suggest restaurants - "
                 "just say \"I need restaurant suggestions\".",
                 state.get("sessionAttributes") or {}, state="Fulfilled")