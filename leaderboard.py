"""Render the ITC blitz leaderboard as Slack Block Kit blocks."""

_MEDALS = ["\U0001F947", "\U0001F948", "\U0001F949"]  # gold, silver, bronze


def _rank_label(i):
    """Medal for the top three, else a numbered position."""
    assert isinstance(i, int) and i >= 0, "i must be a non-negative int"
    return _MEDALS[i] if i < len(_MEDALS) else f"{i + 1}."


def render_blocks(rows, title, total, last=None):
    """Build Slack blocks for the leaderboard.

    rows  : [(name, count), ...] already sorted (see store.standings)
    title : header string
    total : total booked meetings across the team
    last  : optional {"detail": str, "name": str} most recent booking
    """
    assert isinstance(rows, list), "rows must be a list"
    assert isinstance(total, int) and total >= 0, "total must be a non-negative int"
    header = {"type": "header", "text": {"type": "plain_text", "text": title[:150], "emoji": True}}
    blocks = [header]

    if not rows:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": "_No meetings booked yet. Booked meetings appear here automatically as AEs book them._"},
        })
        return blocks

    lines = []
    leaders = [r for r in rows if r[1] > 0]
    zeroes = [r for r in rows if r[1] == 0]
    rank = 0
    for name, count in (leaders + zeroes)[:25]:  # bounded render
        if count > 0:
            noun = "meeting" if count == 1 else "meetings"
            lines.append(f"{_rank_label(rank)}  *{name}*: {count} {noun}")
            rank += 1
        else:
            lines.append(f"—  {name}: 0 meetings")
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
    ctx = f"*{total}* meetings booked so far \U0001F525"
    if last and last.get("detail"):
        ctx += f"  ·  latest: {last['detail']} ({last.get('name', '')})"
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": ctx}]})
    return blocks


def render_text(rows, title, total, last=None):
    """Plain-text fallback (notifications / logs)."""
    assert isinstance(rows, list), "rows must be a list"
    if not rows:
        return f"{title}\nNo meetings booked yet."
    body = "\n".join(f"{_rank_label(i)} {name} - {count}" for i, (name, count) in enumerate(rows[:25]))
    tail = f"\nTotal: {total}"
    if last and last.get("detail"):
        tail += f" | latest: {last['detail']} ({last.get('name', '')})"
    return f"{title}\n{body}{tail}"
