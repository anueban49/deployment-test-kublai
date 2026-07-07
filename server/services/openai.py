# AI service (OpenAI Responses API) - the decision-making layer, for real use.
#
# Mirrors the contract of services/genai.py so consumers can swap providers by
# changing one import:
#   decide_action()   -> route + params extracted from the raw user message
#   filter_posts()    -> keep only the posts relevant to the user's query
# New here:
#   classify_post()   -> label ONE post "seller" | "buyer" from
#                        {post-author, message_rich, image (optional URL)}
#   classify_posts()  -> batch text-only version used at ingest time, so a
#                        20-post scrape costs one API call instead of twenty.
#
# NOTE: this module is named `openai.py` inside the `services` package. That is
# fine as long as `services/` itself is never on sys.path; tg_bot.py strips it
# (see the bootstrap there) so `from openai import OpenAI` below keeps resolving
# to the installed SDK.
import json
import logging

from openai import OpenAI

from config import OPENAI_API_KEY, OPENAI_MODEL

logger = logging.getLogger(__name__)

# Lazily built so importing this module without OPENAI_API_KEY set does not
# crash the whole app; callers just hit the fallback paths instead.
_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


def _generate_json(input_payload) -> dict:
    """Run one Responses API call in JSON mode and parse the result."""
    response = _get_client().responses.create(
        model=OPENAI_MODEL,
        input=input_payload,
        text={"format": {"type": "json_object"}},
    )
    return json.loads(response.output_text or "{}")


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

# Routers the AI is allowed to dispatch to. Extend this as new routers land.
# "chat" is not a data source: it means "no search - just answer the user".
AVAILABLE_ROUTERS = ["chat", "group_posts", "regional_search"]
DEFAULT_ROUTER = "regional_search"

_DECIDE_PROMPT = """\
You are the brain of a Telegram bot that helps real-estate agents in
Ulaanbaatar, Mongolia find BUYER leads - people posting on Facebook that they
are LOOKING FOR a place ("I'm looking for a 2bed 1bath apartment"). The user
writes in Mongolian or English. Decide how to handle their message.

Routes:
- "chat": the message is NOT a request to search posts - greetings ("hello",
  "сайн уу"), small talk, thanks, or questions about the bot. Put a short,
  friendly answer in "reply", in the user's language. Stay STRICTLY on the
  bot's topic (finding real-estate buyer leads); if the message is off-topic,
  the reply must politely steer back to what the bot can do: search buyer
  posts with a keyword, or show the latest buyer posts from their saved
  Facebook groups.
- "group_posts": the user asks for the latest posts from Facebook group(s)
  (words like "group", "групп", "бүлгэм", a facebook.com/groups/... link or a
  numeric group id, e.g. "what's the latest buyer posts from group X").
  Extract "group_id" if they named a specific one, else null.
- "regional_search": the user wants buyer posts about some topic or keyword
  (e.g. "any new posts about clients looking for a place?", "2 өрөө байр").
  The keyword is REQUIRED here; make it a short Facebook search query in the
  same language the user wrote in.

Return ONLY a JSON object with this exact shape:
{"route": "<chat|group_posts|regional_search>", "keyword": "<search keyword or empty string>", "group_id": "<group id/url the user mentioned, else null>", "reply": "<chat reply, empty string unless route is chat>"}

User message: """


def decide_action(message: str) -> dict:
    """Ask the AI how to handle the message: reply directly ("chat") or route
    to a data source, extracting the params.

    Returns {"route": str, "keyword": str, "group_id": str | None, "reply": str}.
    On any AI failure, falls back to a regional search with the raw message.
    """
    logger.info("AI action decision started for message=%r", message)
    fallback = {
        "route": DEFAULT_ROUTER, "keyword": message.strip(),
        "group_id": None, "reply": "",
    }

    try:
        decision = _generate_json(_DECIDE_PROMPT + message)
    except Exception:
        logger.exception("AI action decision failed; falling back to %r", DEFAULT_ROUTER)
        return fallback

    route = decision.get("route")
    if route not in AVAILABLE_ROUTERS:
        logger.warning("AI returned unknown route %r; using fallback", route)
        return fallback

    keyword = (decision.get("keyword") or "").strip()
    group_id = decision.get("group_id") or None
    reply = (decision.get("reply") or "").strip()
    if route == "regional_search" and not keyword:
        keyword = message.strip()

    result = {"route": route, "keyword": keyword, "group_id": group_id, "reply": reply}
    logger.info("AI action decision resolved to %r", result)
    return result


# ---------------------------------------------------------------------------
# Post filtering
# ---------------------------------------------------------------------------

def filter_posts(posts: list[dict], query: str) -> list[dict]:
    """Use the AI to keep only the posts that match the user's query.

    Returns the matching posts (a subset of `posts`, order preserved). If the
    AI call fails, falls back to a plain case-insensitive substring match so
    the pipeline still returns something useful.
    """
    logger.info("AI data analysing started (%d posts, query=%r)", len(posts), query)

    if not posts:
        return []
    if not query:
        return posts

    catalogue = [
        {"index": i, "message": (p.get("message") or p.get("message_rich") or "")}
        for i, p in enumerate(posts)
    ]
    prompt = (
        "You are filtering Facebook posts for a real-estate agent.\n"
        f"The agent is looking for: {query!r}\n\n"
        "Here are the posts as JSON (each has an 'index' and 'message'):\n"
        f"{json.dumps(catalogue, ensure_ascii=False)}\n\n"
        "Return ONLY a JSON object of the shape "
        '{"indexes": [<integer indexes of the relevant posts>]}. '
        'If none match, return {"indexes": []}.'
    )

    try:
        result = _generate_json(prompt)
        indexes = result.get("indexes", [])
        matched = [posts[i] for i in indexes if isinstance(i, int) and 0 <= i < len(posts)]
        logger.info("AI data analysing finished: %d/%d posts matched", len(matched), len(posts))
        return matched
    except Exception:
        logger.exception("AI data analysing failed; falling back to substring match")
        needle = query.casefold()
        matched = [
            p for p in posts
            if needle in (p.get("message") or "").casefold()
            or needle in (p.get("message_rich") or "").casefold()
        ]
        logger.info("Fallback substring match kept %d/%d posts", len(matched), len(posts))
        return matched


# ---------------------------------------------------------------------------
# Seller / buyer classification
# ---------------------------------------------------------------------------

POST_TYPES = ("seller", "buyer")

_CLASSIFY_RULES = """\
Classify the Facebook post for a real-estate agent in Ulaanbaatar:
- "seller": the author OFFERS property (selling, renting out, listing). Words
  like зарна, түрээслүүлнэ, "for sale", "for rent".
- "buyer": the author is LOOKING FOR property (buying, renting). Words like
  авна, түрээслэнэ, хайж байна, "looking for", "want to buy/rent".
If it is neither (news, ads for services, unrelated), use null.
"""


def classify_post(author: str | None, message_rich: str, image: str | None = None) -> str | None:
    """Classify one post as "seller" / "buyer" (or None) from its author name,
    rich message text and, when available, its photo URL."""
    prompt = (
        _CLASSIFY_RULES
        + f"\nPost author: {author or 'unknown'}\nPost text:\n{message_rich}\n\n"
        'Return ONLY a JSON object: {"type": "seller" | "buyer" | null}'
    )
    content = [{"type": "input_text", "text": prompt}]
    if image:
        content.append({"type": "input_image", "image_url": image})

    try:
        result = _generate_json([{"role": "user", "content": content}])
    except Exception:
        logger.exception("AI post classification failed")
        return None

    post_type = result.get("type")
    return post_type if post_type in POST_TYPES else None


def classify_posts(posts: list[dict]) -> list[str | None]:
    """Batch-classify posts (text only) in a single API call.

    Takes simplified posts (the shape api/*_search.py produce) and returns a
    list aligned with `posts`: "seller", "buyer" or None for each.
    """
    if not posts:
        return []
    logger.info("AI classification started (%d posts)", len(posts))

    catalogue = [
        {
            "index": i,
            "author": (p.get("author") or {}).get("name"),
            "message": p.get("message_rich") or p.get("message") or "",
        }
        for i, p in enumerate(posts)
    ]
    prompt = (
        _CLASSIFY_RULES
        + "\nHere are the posts as JSON (each has 'index', 'author', 'message'):\n"
        + json.dumps(catalogue, ensure_ascii=False)
        + '\n\nReturn ONLY a JSON object of the shape '
        '{"classifications": [{"index": <int>, "type": "seller" | "buyer" | null}, ...]} '
        "with one entry per post."
    )

    types: list[str | None] = [None] * len(posts)
    try:
        result = _generate_json(prompt)
        for entry in result.get("classifications", []):
            i = entry.get("index")
            t = entry.get("type")
            if isinstance(i, int) and 0 <= i < len(posts) and t in POST_TYPES:
                types[i] = t
    except Exception:
        logger.exception("AI batch classification failed; posts stay unclassified")

    logger.info(
        "AI classification finished: %d seller, %d buyer, %d unclassified",
        types.count("seller"), types.count("buyer"), types.count(None),
    )
    return types
