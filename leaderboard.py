"""Render the ITC blitz leaderboard — Slack Block Kit + Canvas."""

_MEDALS = ["\U0001F947", "\U0001F948", "\U0001F949"]  # gold, silver, bronze
_BAR_LEN = 10   # total bar slots
_FILLED  = "\U0001F7E9"  # 🟩
_EMPTY   = "⬜"      # ⬜


def _rank_label(i):
    """Medal for the top three, else a numbered position."""
    assert isinstance(i, int) and i >= 0, "i must be a non-negative int"
    return _MEDALS[i] if i < len(_MEDALS) else f"{i + 1}."


def _bar(count, max_count):
    """Green-square progress bar scaled to max_count."""
    assert count >= 0 and max_count >= 0, "counts must be non-negative"
    if max_count == 0:
        return _EMPTY * _BAR_LEN
    filled = round((count / max_count) * _BAR_LEN)
    filled = max(0, min(_BAR_LEN, filled))
    return _FILLED * filled + _EMPTY * (_BAR_LEN - filled)


def render_canvas_markdown(rows, title, total):
    """Markdown for a Slack Canvas — monospace table with green bars."""
    assert isinstance(rows, list), "rows must be a list"
    if not rows:
        return f"# {title}\n\n_No meetings booked yet. The board updates automatically as AEs book._"
    max_count = max((c for _, c in rows), default=0)
    leaders = [r for r in rows if r[1] > 0]
    zeroes  = [r for r in rows if r[1] == 0]
    name_w  = max(len(n) for n, _ in rows) + 1
    lines   = [f"# {title}\n"]
    rank = 0
    for name, count in leaders[:25]:
        medal = _rank_label(rank)
        bar   = _bar(count, max_count)
        lines.append(f"{medal} **{name:{name_w}}** {bar}  {count}")
        rank += 1
    if zeroes:
        lines.append("\n---\n")
        for name, _ in zeroes[:25]:
            bar = _bar(0, max_count or 1)
            lines.append(f"   {name:{name_w}} {bar}  0")
    lines.append(f"\n---\n🔥 **{total} booked as a team**")
    return "\n".join(lines)


def render_blocks(rows, title, total, last=None):
    """Block Kit message (fallback / hype companion). Uses green-square bars."""
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

    max_count = max((c for _, c in rows), default=0)
    leaders = [r for r in rows if r[1] > 0]
    zeroes  = [r for r in rows if r[1] == 0]
    lines = []
    rank  = 0
    for name, count in (leaders + zeroes)[:25]:
        bar  = _bar(count, max_count or 1)
        noun = "meeting" if count == 1 else "meetings"
        if count > 0:
            lines.append(f"{_rank_label(rank)}  *{name}*  {bar}  {count} {noun}")
            rank += 1
        else:
            lines.append(f"      {name}  {bar}  0")
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
    ctx = f"*{total}* meetings booked so far \U0001F525"
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": ctx}]})
    return blocks


def render_text(rows, title, total, last=None):
    """Plain-text fallback (notifications / logs)."""
    assert isinstance(rows, list), "rows must be a list"
    if not rows:
        return f"{title}\nNo meetings booked yet."
    max_count = max((c for _, c in rows), default=0)
    body = "\n".join(
        f"{_rank_label(i)} {name} {_bar(c, max_count or 1)} {c}"
        for i, (name, c) in enumerate(rows[:25])
    )
    return f"{title}\n{body}\nTotal: {total}"
