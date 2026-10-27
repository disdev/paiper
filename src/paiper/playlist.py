"""Pure slot selection; a single explicit post-display write advances history."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from paiper.cache import ContentCache
from paiper.content import Schedule, SourceRecord
from paiper.dashboard import Page, content_token
from paiper.preferences import Preferences


@dataclass(frozen=True)
class Selection:
    page: Page
    state: dict


def daypart(now: datetime, preferences: Preferences) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Selection time must be aware")
    hour = now.astimezone(ZoneInfo(preferences.timezone)).hour
    return (
        "morning"
        if 6 <= hour < 9
        else "day"
        if 9 <= hour < 17
        else "evening"
        if 17 <= hour < 22
        else "night"
    )


def select_page(
    pages: tuple[Page, ...],
    records: dict[str, SourceRecord],
    now: datetime,
    preferences: Preferences,
    state: dict,
    *,
    page: str | None = None,
) -> Selection:
    """Return a proposal without I/O. Overrides never bypass expiry/flight limits."""
    part = daypart(now, preferences)
    result = deepcopy(state)
    recent = result.get("recent_ids", [])
    flight_seen = result.get("aircraft", {})
    available = []

    for candidate in pages:
        if candidate.expires_at and now >= candidate.expires_at:
            continue
        if candidate.key == "flights":
            last = result.get("last_flight")
            seen = flight_seen.get(candidate.id)
            if (
                last and now < datetime.fromisoformat(last) + timedelta(minutes=30)
            ) or (seen and now < datetime.fromisoformat(seen) + timedelta(hours=1)):
                continue
        available.append(candidate)

    def first(key):
        candidates = [p for p in available if p.key == key]
        if key == "cardinals":
            club_news = [p for p in candidates if p.layout == "club-news"]
            if club_news:
                candidates = club_news
        if not candidates:
            return None

        # Unseen stories/photos first, then the least recently shown when exhausted.
        def rank(candidate):
            ids = tuple(
                content_token(i) for i in candidate.content_ids or (candidate.id,)
            )
            return max(
                (recent.index(i) + 1 if i in recent else 0 for i in ids), default=0
            )

        return min(candidates, key=rank)

    selected = first("alert")
    priority = False
    finals = result.get("finals", [])
    final_id = None
    record = records.get("cardinals")
    live_game = None
    # Poll freshness is not a game state. Never give an old cached Live an override.
    if (
        record
        and isinstance(record.payload, Schedule)
        and record.fetched_at
        and not record.error
        and timedelta(0) <= now - record.fetched_at <= timedelta(minutes=10)
    ):
        live_game = next(
            (g for g in record.payload.games if g.abstract_status == "Live"), None
        )
        final_game = next(
            (
                g
                for g in record.payload.games
                if g.abstract_status == "Final"
                and str(g.id) not in finals
                and abs(
                    (
                        now.astimezone(ZoneInfo(preferences.timezone)).date() - g.date
                    ).days
                )
                <= 1
            ),
            None,
        )
        if final_game:
            final_id = str(final_game.id)
    if not selected and page is not None:
        selected = first(page)
        if selected is None:
            raise ValueError(f"Page {page!r} is unavailable or currently bounded")
    if not selected and not result.get("last_priority", False):
        game_id = str(live_game.id) if live_game else final_id
        if game_id:
            selected = next((p for p in available if p.id == "game:" + game_id), None)
            priority = selected is not None
            if priority and not live_game:
                finals = (finals + [final_id])[-64:]
    cursors = result.get("cursors", {})
    playlist = preferences.playlists[part]
    if not selected:
        start = cursors.get(part, 0) % len(playlist)
        for offset in range(len(playlist)):
            index = (start + offset) % len(playlist)
            key = playlist[index]
            if live_game and result.get("last_priority") and key == "cardinals":
                continue
            selected = first(key)
            if selected:
                cursors[part] = (index + 1) % len(playlist)
                break
    if not selected:
        selected = first("verse") or first("gallery")
    if selected is None:
        raise ValueError("No eligible page")
    for ident in selected.content_ids or (selected.id,):
        ident = content_token(ident)
        recent = [item for item in recent if item != ident] + [ident]
    result.update(
        cursors=cursors, recent_ids=recent[-128:], finals=finals, last_priority=priority
    )
    if selected.key == "flights":
        result["last_flight"] = now.isoformat()
        flight_seen[selected.id] = now.isoformat()
        result["aircraft"] = {
            key: at
            for key, at in list(flight_seen.items())[-64:]
            if now < datetime.fromisoformat(at) + timedelta(hours=1)
        }
    return Selection(selected, result)


def record_success(cache: ContentCache, selection: Selection, now: datetime) -> None:
    """Call only after render_and_display returns True; one short cache transaction."""
    state = deepcopy(selection.state)
    if selection.page.key == "flights":
        state["last_flight"] = now.isoformat()
        state["aircraft"][selection.page.id] = now.isoformat()
    state["last_success"] = {
        "page": selection.page.key,
        "id": selection.page.id,
        "at": now.isoformat(),
        "source": selection.page.source,
        "freshness": selection.page.freshness,
    }
    cache.set_state("playlist", state)
