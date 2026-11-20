"""Reading review requests in Slack (services/reviews.py): who a message asks to review which pull
requests, whether a reply in a thread asks in words, and who already claimed a PR (👀). Pure
functions over the text Slack sent: nothing here calls Slack or the database."""
import re

PR_URL_RE = re.compile(r"https://github\.com/([A-Za-z0-9-]+/[A-Za-z0-9_.-]+)/pull/([0-9]{1,9})")
MENTION_OR_PR_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>|" + PR_URL_RE.pattern)
REVIEWING = "eyes"  # the reaction on a message that asked, while its PRs are reviewed (reviews.py)
# A reply in a thread that mentions the developer without linking PRs asks them to review the
# thread's PRs only with words that ask for it, and none that say not to.
REVIEW = r"(?:re-?review|review|approve)"
# A gap between two words of an ask is at most this many characters: a long run of text can't make
# the match backtrack for long. Only the start of a message is read for asks.
GAP = r"[^.?!\n]{0,200}"
MAX_ASK_CHARS = 4000
REVIEW_ASK_RE = re.compile(
    rf"\b(?:can|could|would|will)\s+you\b{GAP}\b(?:{REVIEW}|look|check)\b"  # can you review / take a look
    rf"|\b(?:please|pls|plz|kindly)\b{GAP}\b(?:{REVIEW}|look|check)\b"  # please review / please check these
    rf"|\b{REVIEW}\s+(?:this|these|it|them|the\s+prs?|pls|plz|please)\b"  # review these
    rf"|^\s*(?:<@[^>]+>[\s,:]*)+{REVIEW}\b"  # "@majid review"
    r"|\bptal\b|\b(?:take|have)\s+a\s+look\b", re.IGNORECASE)
NOT_ASKED_RE = re.compile(r"\b(?:no\s+need|not\s+needed|don'?t|do\s+not|doesn'?t\s+need|never\s?mind|nvm)\b", re.IGNORECASE)
AGAIN_RE = re.compile(r"re-?review|review\s+(it\s+)?again", re.IGNORECASE)


def asks_for_review(text: str) -> bool:
    """Whether the text asks for a review in words ("can you take a look"). Bounded in time."""
    return bool(REVIEW_ASK_RE.search(text[:MAX_ASK_CHARS]))


def asks_again(text: str) -> bool:
    """Whether a reply in a thread asks for the review again ("re-review please")."""
    return bool(AGAIN_RE.search(text[:MAX_ASK_CHARS]))


def mention_groups(text: str) -> list[tuple[set[str], list[tuple[str, int]]]]:
    """Who a Slack message asks to review which PRs: [(members, [(owner/name, number)])]. One message
    can ask several people about different PRs ("@a @b: <fe PRs> @c @d: <services PRs>"), so a PR
    belongs to the mentions just before it, or to the first ones when it comes before any mention."""
    groups: list[tuple[set[str], list[tuple[str, int]]]] = []
    before: list[tuple[str, int]] = []
    for mentioned, github, number in MENTION_OR_PR_RE.findall(text):
        if mentioned:
            if not groups or groups[-1][1]:
                groups.append((set(), []))
            groups[-1][0].add(mentioned)
        else:
            (groups[-1][1] if groups else before).append((github, int(number)))
    if groups:
        groups[0][1][:0] = before
    return groups


def asked_prs(text: str, member: str) -> list[tuple[str, int]]:
    """The PRs (owner/name, number) a Slack message asks the member to review."""
    asked = [pr for members, prs in mention_groups(text) if member in members for pr in prs]
    return list(dict.fromkeys(asked))


def asked_in_reply(text: str, parent: str, member: str) -> list[tuple[str, int]]:
    """The PRs a reply in the thread of the message `parent` asks the member to review when it
    links none itself ("@majid can you review these?"): those `parent` asked them about, else all
    it links. Only when the reply mentions them and asks for a review in words, and none say not to."""
    mentioned = {m for members, _ in mention_groups(text) for m in members}
    if member not in mentioned or PR_URL_RE.search(text) or not asks_for_review(text) or NOT_ASKED_RE.search(text):
        return []
    return asked_prs(parent, member) or list(dict.fromkeys((g, int(n)) for g, n in PR_URL_RE.findall(parent)))


def claimed_by(message: dict, member: str, github: str, number: int) -> str | None:
    """Who, of those this message asks about the PR, reacted 👀 on it before the member (reviewing
    it), or None. The first 👀 of those asked wins, so when two developers' ShadowDevs both react,
    one of them reviews."""
    pr = (github.lower(), number)
    asked = set().union(*(members for members, prs in mention_groups(str(message.get("text") or ""))
                          if pr in {(g.lower(), n) for g, n in prs}))
    eyes = next((r.get("users") or [] for r in message.get("reactions") or [] if r.get("name") == REVIEWING), [])
    first = next((u for u in eyes if u in asked or u == member), None)
    return first if first and first != member else None
