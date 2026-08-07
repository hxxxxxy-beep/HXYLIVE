from __future__ import annotations

from typing import Any


def _is_live_cover_url(value: object) -> bool:
    lowered = str(value or "").strip().lower()
    if not lowered:
        return False
    if "thumb.live.mmcdn.com/riw/" in lowered:
        return True
    if "roomimg.stream.highwebmedia.com/ri/" in lowered:
        return True
    if "doppiocdn." in lowered and "/snapshot/" in lowered:
        return True
    if "/previews/" in lowered and (
        "doppiocdn." in lowered or "static-proxy.strpst.com" in lowered
    ):
        return True
    return False


def sync_items_trusted(items: object) -> bool:
    return bool(getattr(items, "trusted", True))


def sync_items_skipped_reason(items: object) -> str | None:
    reason = getattr(items, "skipped_reason", None)
    return str(reason) if reason else None


def sync_items_authoritative(items: object) -> bool:
    return bool(getattr(items, "authoritative", True))


async def store_provider_following(db: Any, source_type: str, items: list[dict]) -> dict:
    if not sync_items_trusted(items):
        return {
            "synced": 0,
            "trusted": False,
            "authoritative": False,
            "skippedReason": sync_items_skipped_reason(items) or "Following sync skipped",
        }

    authoritative = sync_items_authoritative(items)
    synced_usernames = set()
    for item in items or []:
        username = item.get("username")
        if not username:
            continue
        thumbnail = item.get("thumbnail_url") or item.get("thumbnail")
        profile_image = str(
            item.get("profile_image_url")
            or item.get("profileImageUrl")
            or item.get("avatar_url")
            or item.get("avatarUrl")
            or ""
        ).strip() or None
        is_online = bool(item.get("is_online", item.get("isOnline", False)))
        if source_type == "chaturbate" and not is_online and thumbnail and "roomimg.stream.highwebmedia.com" in thumbnail:
            thumbnail = None
        # Live webcam covers are not durable face photos — keep them off profile_image.
        if profile_image and _is_live_cover_url(profile_image):
            profile_image = None
        await db.upsert_followed_model(
            username=username,
            display_name=item.get("display_name") or username,
            is_online=is_online,
            viewers=int(item.get("viewers") or 0),
            thumbnail_url=thumbnail,
            profile_image_url=profile_image,
            source_type=source_type,
            room_status=item.get("room_status") or item.get("roomStatus"),
        )
        synced_usernames.add(username)

    if authoritative:
        await db.remove_unfollowed(synced_usernames, source_type=source_type)
    return {
        "synced": len(synced_usernames),
        "trusted": True,
        "authoritative": authoritative,
        "skippedReason": None,
    }
