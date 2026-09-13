from __future__ import annotations

from typing import Any, Optional, Sequence


class FollowedSyncResult(list):
    def __init__(
        self,
        values=(),
        trusted: bool = True,
        skipped_reason: Optional[str] = None,
        authoritative: bool = True,
    ):
        super().__init__(values)
        self.trusted = trusted
        self.skipped_reason = skipped_reason
        self.authoritative = bool(authoritative and trusted)


def _is_live_cover_url(value: object) -> bool:
    lowered = str(value or "").strip().lower()
    if not lowered:
        return False
    if "thumb.live.mmcdn.com/riw/" in lowered:
        return True
    # /ri/ room posters look like live screenshots — not self-set faces.
    if "roomimg.stream.highwebmedia.com/ri/" in lowered:
        return True
    if "thumb.live.mmcdn.com/ri/" in lowered and "/riw/" not in lowered:
        return True
    if "doppiocdn." in lowered and "/snapshot/" in lowered:
        return True
    if "/previews/" in lowered and (
        "doppiocdn." in lowered or "static-proxy.strpst.com" in lowered
    ):
        return True
    if "previews-ttv" in lowered or "live_user_" in lowered:
        return True
    return False


def sync_items_trusted(items: object) -> bool:
    return bool(getattr(items, "trusted", True))


def sync_items_skipped_reason(items: object) -> str | None:
    reason = getattr(items, "skipped_reason", None)
    return str(reason) if reason else None


def sync_items_authoritative(items: object) -> bool:
    return bool(getattr(items, "authoritative", True))


def filter_following_items(
    items: Sequence[dict] | FollowedSyncResult,
    usernames: Sequence[str] | None,
) -> list[dict] | FollowedSyncResult:
    """Keep only remote follow rows whose username is in ``usernames``.

    ``usernames=None`` leaves the list unchanged (full sync). Matching is
    case-insensitive. Preserves FollowedSyncResult trust metadata.
    """
    if usernames is None:
        return items  # type: ignore[return-value]
    wanted = {
        str(name or "").strip().lower()
        for name in usernames
        if str(name or "").strip()
    }
    filtered = [
        item
        for item in (items or [])
        if str((item or {}).get("username") or "").strip().lower() in wanted
    ]
    if isinstance(items, FollowedSyncResult):
        return FollowedSyncResult(
            filtered,
            trusted=sync_items_trusted(items),
            skipped_reason=sync_items_skipped_reason(items),
            authoritative=sync_items_authoritative(items),
        )
    return filtered


async def ensure_streamer_card(
    db: Any,
    username: str,
    source_type: str,
    *,
    display_name: Optional[str] = None,
    profile_image_url: Optional[str] = None,
    channel_url: Optional[str] = None,
    auto_record: bool = False,
) -> str:
    """Ensure a Media streamer card (+ source row) exists.

    Does not touch remote site follows or the followed_models cache.
    Does not wipe enrichment on an existing card.
    """
    channel = str(username or "").strip()
    if not channel:
        return ""
    source = str(source_type or "chaturbate").strip().lower() or "chaturbate"
    label = str(display_name or channel).strip() or channel
    face = str(profile_image_url or "").strip() or None
    if face and _is_live_cover_url(face):
        face = None
    want_record = bool(auto_record)

    existing = await db.get_media_profile(channel)
    if not existing:
        payload: dict[str, Any] = {"display_name": label}
        if face:
            payload["profile_image_url"] = face
        await db.upsert_media_profile(channel, payload)

    sources = await db.get_media_profile_sources(channel)
    matched = None
    for row in sources or []:
        if (
            str(row.get("source_type") or "").strip().lower() == source
            and str(row.get("channel_username") or "").strip().lower() == channel.lower()
        ):
            matched = row
            break
    if not matched:
        await db.upsert_media_profile_source(
            profile_username=channel,
            source_type=source,
            channel_username=channel,
            channel_url=channel_url,
            auto_record=want_record,
            record_quality="best",
            retention_days=30,
            record_path=None,
        )
    elif want_record and not bool(matched.get("auto_record")):
        await db.set_media_profile_auto_record(channel, True, source_type=source)
    return channel


# Back-compat alias used by older call sites / tests.
ensure_streamer_card_for_follow = ensure_streamer_card


def _normalize_follow_item(item: dict, source_type: str) -> dict[str, Any] | None:
    username = str((item or {}).get("username") or "").strip()
    if not username:
        return None
    thumbnail = item.get("thumbnail_url") or item.get("thumbnail")
    profile_image = str(
        item.get("profile_image_url")
        or item.get("profileImageUrl")
        or item.get("avatar_url")
        or item.get("avatarUrl")
        or ""
    ).strip() or None
    is_online = bool(item.get("is_online", item.get("isOnline", False)))
    if source_type == "chaturbate" and not is_online and thumbnail and "roomimg.stream.highwebmedia.com" in str(thumbnail):
        thumbnail = None
    if profile_image and _is_live_cover_url(profile_image):
        profile_image = None
    display_name = item.get("display_name") or item.get("displayName") or username
    followers = item.get("followers")
    if followers is None:
        followers = item.get("num_followers")
    try:
        followers_value = None if followers is None else int(followers)
    except (TypeError, ValueError):
        followers_value = None
    # Chaturbate roomlist often defaults missing counts to 0; do not persist that.
    if source_type == "chaturbate" and followers_value == 0:
        followers_value = None
    return {
        "username": username,
        "display_name": display_name,
        "is_online": is_online,
        "viewers": int(item.get("viewers") or 0),
        "thumbnail_url": thumbnail,
        "profile_image_url": profile_image,
        "source_type": source_type,
        "room_status": item.get("room_status") or item.get("roomStatus"),
        "channel_url": item.get("channel_url") or item.get("channelUrl"),
        "followers": followers_value,
    }


async def refresh_remote_follow_cache(db: Any, source_type: str, items: list[dict]) -> dict:
    """Replace followed_models for one site from a trusted remote list. Never creates cards."""
    if not sync_items_trusted(items):
        return {
            "synced": 0,
            "trusted": False,
            "authoritative": False,
            "skippedReason": sync_items_skipped_reason(items) or "Following sync skipped",
        }

    authoritative = sync_items_authoritative(items)
    synced_usernames: set[str] = set()
    for item in items or []:
        normalized = _normalize_follow_item(item if isinstance(item, dict) else {}, source_type)
        if not normalized:
            continue
        await db.upsert_followed_model(
            username=normalized["username"],
            display_name=normalized["display_name"],
            is_online=normalized["is_online"],
            viewers=normalized["viewers"],
            thumbnail_url=normalized["thumbnail_url"],
            profile_image_url=normalized["profile_image_url"],
            source_type=source_type,
            room_status=normalized["room_status"],
            followers=normalized.get("followers"),
        )
        synced_usernames.add(normalized["username"])

    if authoritative:
        await db.remove_unfollowed(synced_usernames, source_type=source_type)
    return {
        "synced": len(synced_usernames),
        "trusted": True,
        "authoritative": authoritative,
        "skippedReason": None,
        "disabledRecording": [],
    }


async def store_provider_following(db: Any, source_type: str, items: list[dict]) -> dict:
    """Refresh remote-follow cache only. Cards are created explicitly by the user."""
    return await refresh_remote_follow_cache(db, source_type, items)


def classify_follow_reconcile(
    *,
    source_type: str,
    remote_items: Sequence[dict],
    local_cards: Sequence[dict],
) -> dict[str, list[dict]]:
    """Split local cards vs remote follows into cardOnly / remoteOnly / both."""
    source = str(source_type or "").strip().lower()
    remote_by_user: dict[str, dict] = {}
    for raw in remote_items or []:
        if not isinstance(raw, dict):
            continue
        normalized = _normalize_follow_item(raw, source)
        if not normalized:
            continue
        key = normalized["username"].lower()
        remote_by_user[key] = {
            "username": normalized["username"],
            "displayName": normalized["display_name"],
            "profileImageUrl": normalized["profile_image_url"],
            "channelUrl": normalized.get("channel_url"),
            "isOnline": normalized["is_online"],
            "viewers": normalized["viewers"],
            "followers": normalized.get("followers"),
            "sourceType": source,
        }

    card_by_user: dict[str, dict] = {}
    for card in local_cards or []:
        if not isinstance(card, dict):
            continue
        username = str(
            card.get("channelUsername")
            or card.get("channel_username")
            or card.get("username")
            or ""
        ).strip()
        if not username:
            continue
        key = username.lower()
        card_by_user[key] = {
            "username": username,
            "profileUsername": str(card.get("profileUsername") or card.get("profile_username") or username).strip() or username,
            "displayName": str(card.get("displayName") or card.get("display_name") or username).strip() or username,
            "profileImageUrl": card.get("profileImageUrl") or card.get("profile_image_url"),
            "channelUrl": card.get("channelUrl") or card.get("channel_url"),
            "sourceType": source,
            "autoRecord": bool(card.get("autoRecord", card.get("auto_record", False))),
        }

    card_only: list[dict] = []
    both: list[dict] = []
    for key, card in card_by_user.items():
        remote = remote_by_user.get(key)
        if remote:
            both.append({
                **card,
                **{
                    k: remote[k]
                    for k in ("isOnline", "viewers", "followers", "profileImageUrl")
                    if k in remote and remote[k] not in (None, "")
                },
            })
        else:
            card_only.append(card)

    remote_only: list[dict] = []
    for key, remote in remote_by_user.items():
        if key not in card_by_user:
            remote_only.append(remote)

    def _sort_key(row: dict) -> str:
        return str(row.get("displayName") or row.get("username") or "").lower()

    return {
        "cardOnly": sorted(card_only, key=_sort_key),
        "remoteOnly": sorted(remote_only, key=_sort_key),
        "both": sorted(both, key=_sort_key),
    }
