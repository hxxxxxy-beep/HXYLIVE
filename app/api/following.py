"""
API Router: Following management
"""

import json
import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ..following_sync import store_provider_following
from ..logger import logger
from ..providers.base import ProviderError

router = APIRouter(prefix="/api", tags=["following"])

# Set by main.py at startup
_chaturbate_api = None
_auth_service = None
_db = None
_provider_registry = None
_DEFAULT_RETENTION_DAYS = 30
_MAX_RETENTION_DAYS = 365
_PRIVATE_ROOM_STATUSES = {
    "private",
    "group",
    "password_protected",
    "password protected",
    "hidden",
    "true_private",
    "private_spy",
}


def init(chaturbate_api, auth_service, db, provider_registry=None):
    global _chaturbate_api, _auth_service, _db, _provider_registry
    _chaturbate_api = chaturbate_api
    _auth_service = auth_service
    _db = db
    _provider_registry = provider_registry


async def _get_default_retention_days() -> int:
    if not _db:
        return _DEFAULT_RETENTION_DAYS
    try:
        raw = await _db.get_setting("default_retention_days")
        retention_days = int(raw) if raw is not None else _DEFAULT_RETENTION_DAYS
        return max(0, min(_MAX_RETENTION_DAYS, retention_days))
    except (ValueError, TypeError):
        return _DEFAULT_RETENTION_DAYS


def _provider_source_type(provider) -> str:
    return str(getattr(provider, "source_type", "") or "").strip().lower()


def _provider_display_name(provider, source_type: str) -> str:
    return getattr(provider, "display_name", None) or source_type or "Unknown"


def _registered_source_types() -> Optional[set[str]]:
    if not _provider_registry:
        return None
    return {
        source_type
        for provider in _provider_registry.all()
        for source_type in [_provider_source_type(provider)]
        if source_type
    }


async def _provider_login_summary(provider) -> dict:
    source_type = _provider_source_type(provider)
    summary = {
        "isLoggedIn": False,
        "username": None,
        "lastError": None,
        "hasCookies": False,
        "hasLocalStorage": False,
        "hasSession": False,
        "hasSavedSessionData": False,
        "hasSavedCredentials": False,
        "credentialsUpdatedAt": None,
    }

    caps = getattr(provider, "capabilities", None)
    if not getattr(caps, "can_login", False):
        summary["accountDisabled"] = True
        return summary

    auth = getattr(provider, "auth", None)
    if auth is not None and hasattr(auth, "get_status"):
        try:
            status = auth.get_status() or {}
            summary.update({
                "isLoggedIn": bool(status.get("isLoggedIn")),
                "username": status.get("username"),
                "lastError": status.get("lastError"),
                "hasCookies": bool(status.get("hasCookies") or status.get("isLoggedIn")),
            })
            summary["hasSession"] = bool(summary["isLoggedIn"])
            summary["hasSavedSessionData"] = bool(summary["hasCookies"])
        except Exception as exc:
            logger.debug("Provider auth status unavailable", source_type=source_type, error=str(exc))

    if _db and source_type:
        try:
            row = await _db.get_provider_session(source_type)
        except Exception as exc:
            logger.debug("Provider session status unavailable", source_type=source_type, error=str(exc))
            row = None
        if row:
            has_cookies = _stored_json_has_items(row.get("session_cookies"))
            has_local_storage = _stored_json_has_items(row.get("local_storage"))
            has_saved_session_data = bool(summary.get("hasCookies") or has_cookies or has_local_storage)
            summary["isLoggedIn"] = bool(summary["isLoggedIn"] or (row.get("is_logged_in") and has_saved_session_data))
            summary["username"] = summary["username"] or row.get("username") or row.get("credential_username")
            summary["lastError"] = summary["lastError"] or row.get("last_error")
            summary["hasCookies"] = bool(summary["hasCookies"] or has_cookies)
            summary["hasLocalStorage"] = has_local_storage
            summary["hasSavedSessionData"] = has_saved_session_data
            summary["hasSession"] = bool(summary["isLoggedIn"])
            summary["hasSavedCredentials"] = bool(
                row.get("credential_username") and row.get("credential_password")
            )
            summary["credentialsUpdatedAt"] = row.get("credentials_updated_at")

    return summary


def _stored_json_has_items(raw_value: object) -> bool:
    if not raw_value:
        return False
    try:
        parsed = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
    except Exception:
        return False
    if isinstance(parsed, list):
        return any(bool(item) for item in parsed)
    if isinstance(parsed, dict):
        return bool(parsed)
    return False


def _provider_capabilities(provider) -> dict:
    caps = getattr(provider, "capabilities", None)
    if not caps:
        return {}
    return {
        "can_login": bool(getattr(caps, "can_login", False)),
        "can_follow": bool(getattr(caps, "can_follow", False)),
        "can_sync_following": bool(getattr(caps, "can_sync_following", False)),
        "can_discover": bool(getattr(caps, "can_discover", False)),
        "can_stream": bool(getattr(caps, "can_stream", True)),
        "can_record": bool(getattr(caps, "can_record", True)),
        "uses_browser": bool(getattr(caps, "uses_browser", False)),
        "uses_ytdlp": bool(getattr(caps, "uses_ytdlp", False)),
    }


def _source_type_for_model(model: dict) -> str:
    return str(model.get("source_type") or model.get("platform") or "chaturbate").strip().lower()


def _is_private_model(model: dict) -> bool:
    return (model.get("room_status") or model.get("roomStatus") or "").lower() in _PRIVATE_ROOM_STATUSES


def _model_viewers(model: dict) -> int:
    if not bool(model.get("is_online")) or _is_private_model(model):
        return 0
    try:
        return int(model.get("viewers") or 0)
    except (TypeError, ValueError):
        return 0


def _model_status_rank(model: dict) -> int:
    if bool(model.get("is_online")) and not _is_private_model(model):
        return 0
    if _is_private_model(model):
        return 1
    return 2


def _sort_following_models(models: list[dict]) -> list[dict]:
    return sorted(
        models,
        key=lambda model: (
            -_model_viewers(model),
            _model_status_rank(model),
            _source_type_for_model(model),
            str(model.get("username") or model.get("name") or "").lower(),
        ),
    )


def _provider_counts(models: list[dict]) -> dict:
    online = [m for m in models if bool(m.get("is_online")) or _is_private_model(m)]
    return {
        "totalCount": len(models),
        "onlineCount": len(online),
        "offlineCount": len(models) - len(online),
    }


def _is_live_cover_face_url(value: object) -> bool:
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


def _model_face_url(model: dict) -> str:
    url = str(
        model.get("profile_image_url")
        or model.get("profileImageUrl")
        or ""
    ).strip()
    if url and not _is_live_cover_face_url(url):
        return url
    return ""


async def _persist_followed_face(model: dict, face: str) -> None:
    if not _db or not face:
        return
    username = str(model.get("username") or "").strip()
    if not username:
        return
    try:
        await _db.upsert_followed_model(
            username=username,
            display_name=model.get("display_name") or model.get("displayName") or username,
            is_online=bool(model.get("is_online")),
            viewers=int(model.get("viewers") or 0),
            thumbnail_url=model.get("thumbnail_url") or model.get("thumbnail"),
            profile_image_url=face,
            source_type=_source_type_for_model(model),
            room_status=model.get("room_status"),
        )
    except Exception:
        pass


async def _enrich_following_face_photos(followed: list[dict]) -> None:
    """Resolve durable face photos for Following rows.

    Sync-capable providers (Chaturbate / Stripchat) skip status refresh, and the
    followed_models table historically only stored live thumbnails. Without this
    pass the UI only has letter avatars after live-cover rejection.
    """
    if not followed:
        return

    cb_models = [
        model for model in followed
        if _source_type_for_model(model) == "chaturbate" and not _model_face_url(model)
    ]
    if cb_models and _chaturbate_api and hasattr(_chaturbate_api, "resolve_profile_images"):
        usernames = [
            str(model.get("username") or "").strip().lower()
            for model in cb_models
            if str(model.get("username") or "").strip()
        ]
        images: dict = {}
        try:
            images = await asyncio.wait_for(
                _chaturbate_api.resolve_profile_images(usernames),
                timeout=45,
            ) or {}
        except Exception as exc:
            logger.debug("Following Chaturbate face enrich failed", error=str(exc))
            images = {}
        for model in cb_models:
            username = str(model.get("username") or "").strip().lower()
            face = str(images.get(username) or "").strip()
            if not face or _is_live_cover_face_url(face):
                continue
            model["profile_image_url"] = face
            model["profileImageUrl"] = face
            await _persist_followed_face(model, face)

    if not _provider_registry:
        return
    providers = {
        _provider_source_type(provider): provider
        for provider in _provider_registry.all()
    }
    stripchat = providers.get("stripchat")
    if not stripchat or not hasattr(stripchat, "list_live_models"):
        return

    sc_models = [
        model for model in followed
        if _source_type_for_model(model) == "stripchat" and not _model_face_url(model)
    ]

    async def enrich_stripchat(model: dict) -> None:
        username = str(model.get("username") or "").strip()
        if not username:
            return
        try:
            payload = await asyncio.wait_for(
                stripchat.list_live_models(page=1, limit=1, search=username),
                timeout=15,
            )
        except Exception as exc:
            logger.debug(
                "Following Stripchat face enrich failed",
                username=username,
                error=str(exc),
            )
            return
        match = next(
            (
                item for item in (payload.get("models") or [])
                if str(item.get("username") or "").strip().lower() == username.lower()
            ),
            None,
        )
        if not match:
            return
        face = str(
            match.get("profile_image_url")
            or match.get("profileImageUrl")
            or ""
        ).strip()
        if not face or _is_live_cover_face_url(face):
            return
        model["profile_image_url"] = face
        model["profileImageUrl"] = face
        await _persist_followed_face(model, face)

    if sc_models:
        await asyncio.gather(*(enrich_stripchat(model) for model in sc_models))


async def _refresh_discoverable_follow_statuses(followed: list[dict]) -> None:
    """Refresh local-only follows from providers with a direct status lookup."""
    if not _provider_registry or not followed:
        return
    providers = {
        _provider_source_type(provider): provider
        for provider in _provider_registry.all()
    }

    async def refresh(model: dict) -> None:
        source_type = _source_type_for_model(model)
        provider = providers.get(source_type)
        caps = getattr(provider, "capabilities", None)
        if (
            not provider
            or getattr(caps, "can_sync_following", False)
            or not getattr(caps, "can_discover", False)
        ):
            return
        username = str(model.get("username") or "").strip()
        if not username:
            return
        try:
            # Bilibili room ids are numeric; keyword search misses them. Use the
            # room meta path so Following gets real uname + face.
            if source_type == "bilibili" and hasattr(provider, "resolve_watch_meta"):
                meta = await asyncio.wait_for(
                    provider.resolve_watch_meta(username),
                    timeout=15,
                )
                if not meta:
                    model.update({"is_online": False, "viewers": 0, "room_status": "offline"})
                    return
                is_online = bool(meta.get("isOnline"))
                display_name = str(meta.get("displayName") or model.get("display_name") or username).strip()
                profile_image = str(meta.get("profileImageUrl") or "").strip()
                if _is_live_cover_face_url(profile_image):
                    profile_image = ""
                thumbnail = (
                    meta.get("thumbnail")
                    or profile_image
                    or model.get("thumbnail_url")
                )
                model.update({
                    "is_online": is_online,
                    "viewers": int(meta.get("viewers") or 0) if is_online else 0,
                    "room_status": "public" if is_online else "offline",
                    "display_name": display_name,
                    "displayName": display_name,
                    "thumbnail_url": thumbnail,
                    "profile_image_url": profile_image,
                    "profileImageUrl": profile_image,
                })
                if _db and display_name:
                    try:
                        await _db.upsert_followed_model(
                            username=username,
                            display_name=display_name,
                            is_online=is_online,
                            viewers=int(meta.get("viewers") or 0) if is_online else 0,
                            thumbnail_url=thumbnail,
                            profile_image_url=profile_image or None,
                            source_type=source_type,
                            room_status="public" if is_online else "offline",
                        )
                    except Exception:
                        pass
                return

            if not hasattr(provider, "list_live_models"):
                return
            payload = await asyncio.wait_for(
                provider.list_live_models(page=1, limit=1, search=username),
                timeout=15,
            )
            match = next(
                (
                    item for item in (payload.get("models") or [])
                    if str(item.get("username") or "").strip().lower() == username.lower()
                ),
                None,
            )
            if match is None:
                model.update({"is_online": False, "viewers": 0, "room_status": "offline"})
                return
            is_online = bool(match.get("is_online", False))
            display_name = str(
                match.get("display_name")
                or match.get("displayName")
                or model.get("display_name")
                or username
            ).strip()
            profile_image = (
                match.get("profile_image_url")
                or match.get("profileImageUrl")
                or model.get("profile_image_url")
                or model.get("profileImageUrl")
                or ""
            )
            if _is_live_cover_face_url(profile_image):
                profile_image = ""
            model.update({
                "is_online": is_online,
                "viewers": int(match.get("viewers") or 0) if is_online else 0,
                "room_status": match.get("room_status") or ("public" if is_online else "offline"),
                "display_name": display_name,
                "displayName": display_name,
                "thumbnail_url": match.get("thumbnail") or match.get("thumbnail_url") or model.get("thumbnail_url"),
                # Face photo only — UI rejects live covers and shows a letter avatar.
                "profile_image_url": profile_image,
                "profileImageUrl": profile_image,
            })
        except Exception as exc:
            logger.debug(
                "Fresh following status unavailable",
                source_type=source_type,
                username=username,
                error=str(exc),
            )

    await asyncio.gather(*(refresh(model) for model in followed))


async def _build_provider_summaries(followed: list[dict], any_logged_in: bool) -> tuple[list[dict], dict, bool]:
    by_source: dict[str, list[dict]] = {}
    for model in followed:
        by_source.setdefault(_source_type_for_model(model), []).append(model)

    providers = []
    per_source_logins = {}
    seen_sources = set()

    if _provider_registry:
        for provider in _provider_registry.all():
            source_type = _provider_source_type(provider)
            if not source_type:
                continue
            status = await _provider_login_summary(provider)
            caps = _provider_capabilities(provider)
            models = by_source.get(source_type, [])
            seen_sources.add(source_type)
            per_source_logins[source_type] = bool(status.get("isLoggedIn"))
            any_logged_in = any_logged_in or bool(status.get("isLoggedIn"))
            providers.append({
                "sourceType": source_type,
                "displayName": _provider_display_name(provider, source_type),
                "capabilities": caps,
                "status": status,
                **_provider_counts(models),
            })

    if not _provider_registry:
        for source_type, models in sorted(by_source.items()):
            if source_type in seen_sources:
                continue
            per_source_logins[source_type] = per_source_logins.get(source_type, False)
            providers.append({
                "sourceType": source_type,
                "displayName": source_type.capitalize(),
                "capabilities": {},
                "status": {
                    "isLoggedIn": False,
                    "username": None,
                    "lastError": None,
                    "hasCookies": False,
                    "hasSavedCredentials": False,
                    "credentialsUpdatedAt": None,
                },
                **_provider_counts(models),
            })

    return providers, per_source_logins, any_logged_in


@router.get("/following")
async def get_following():
    """Returns all followed models across active providers with online
    status and isTracked flag."""
    try:
        # Per-source login status (fallback if the provider registry is not ready).
        per_source_logins: dict = {}
        any_logged_in = False
        if _auth_service and not _provider_registry:
            cb_status = _auth_service.get_status()
            per_source_logins["chaturbate"] = bool(cb_status.get("isLoggedIn"))
            any_logged_in = per_source_logins["chaturbate"] or any_logged_in

        # Read the local follow cache across all sources.
        followed = []
        tracked_map = {}
        if _db:
            try:
                registered_sources = _registered_source_types()
                followed = await _db.get_all_followed()
                tracked_models = await _db.get_all_models()
                if registered_sources is not None:
                    followed = [
                        item for item in followed
                        if _source_type_for_model(item) in registered_sources
                    ]
                    tracked_models = [
                        item for item in tracked_models
                        if _source_type_for_model(item) in registered_sources
                    ]
                tracked_map = {
                    (
                        m["username"],
                        _source_type_for_model(m),
                    ): m
                    for m in tracked_models
                }
                await _refresh_discoverable_follow_statuses(followed)
                await _enrich_following_face_photos(followed)
            except Exception as e:
                logger.warning("DB read failed in /api/following", error=str(e))
                followed = []
                tracked_map = {}

        media_faces: dict[str, str] = {}
        try:
            from urllib.parse import quote

            for profile in await _db.get_all_media_profiles():
                uname = str(profile.get("username") or "").strip().lower()
                if not uname:
                    continue
                local_path = str(profile.get("profile_image_path") or "").strip()
                face = str(profile.get("profile_image_url") or "").strip()
                if local_path:
                    version = abs(hash(local_path)) % 1000000
                    media_faces[uname] = (
                        f"/api/media-profiles/{quote(uname, safe='')}/profile-image?v={version}"
                    )
                elif face and not _is_live_cover_face_url(face):
                    media_faces[uname] = face
        except Exception:
            media_faces = {}

        for model in followed:
            model_source = _source_type_for_model(model)
            tracked = tracked_map.get((model["username"], model_source))
            if not tracked and model_source == "chaturbate":
                tracked = tracked_map.get((model["username"], ""))
            model["isTracked"] = tracked is not None
            model["is_recording"] = bool(tracked and tracked.get("is_recording"))
            # Surface cached room_status for UI (distinguer Private d'Offline)
            if tracked and tracked.get("room_status"):
                model["room_status"] = tracked.get("room_status")
            # Priority: source_type on the followed row > tracked model > chaturbate
            model["source_type"] = (
                model.get("source_type")
                or (tracked.get("source_type") if tracked else None)
                or "chaturbate"
            )
            # Normalize DB face column for the FE (reject leftover live covers).
            face = _model_face_url(model)
            if face:
                model["profile_image_url"] = face
                model["profileImageUrl"] = face
            else:
                model["profile_image_url"] = ""
                model["profileImageUrl"] = ""
            # Prefer durable Media face photo over live covers / empty avatars.
            media_face = media_faces.get(str(model.get("username") or "").strip().lower())
            if media_face:
                model["profile_image_url"] = media_face
                model["profileImageUrl"] = media_face

        followed = _sort_following_models(followed)
        providers, registry_logins, any_logged_in = await _build_provider_summaries(followed, any_logged_in)
        per_source_logins.update(registry_logins)
        by_provider = {}
        for model in followed:
            by_provider.setdefault(_source_type_for_model(model), []).append(model)

        online = [m for m in followed if m.get("is_online")]
        offline = [m for m in followed if not m.get("is_online")]

        return {
            "models": followed,
            "online": online,
            "offline": offline,
            "onlineCount": len(online),
            "offlineCount": len(offline),
            "isLoggedIn": any_logged_in,
            "perSource": per_source_logins,
            "providers": providers,
            "byProvider": by_provider,
            "message": None,
        }
    except Exception as e:
        # Never return 500 on this endpoint: the front-end displays better with an
        # empty list (refilled on next fetch) than with a network error
        logger.error("Error /api/following", error=str(e), exc_info=True)
        return {
            "models": [],
            "online": [],
            "offline": [],
            "onlineCount": 0,
            "offlineCount": 0,
            "isLoggedIn": False,
            "perSource": {},
            "message": "Temporary error, retrying...",
        }


@router.post("/following/sync")
async def sync_following():
    """
    Sync all providers that expose a verified remote following API.
    Providers without remote sync keep their local follows untouched.
    """
    if not _db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    if _provider_registry:
        results = []
        total_synced = 0
        for provider in _provider_registry.all():
            caps = getattr(provider, "capabilities", None)
            if not getattr(caps, "can_sync_following", False):
                continue
            source_type = _provider_source_type(provider)
            display_name = _provider_display_name(provider, source_type)
            try:
                items = await asyncio.wait_for(provider.sync_following(), timeout=60)
                stored = await _store_provider_following(source_type, items)
                synced = stored["synced"]
                total_synced += synced
                results.append({
                    "sourceType": source_type,
                    "displayName": display_name,
                    "synced": synced,
                    "trusted": stored["trusted"],
                    "authoritative": stored["authoritative"],
                    "skippedReason": stored["skippedReason"],
                    "success": True,
                })
            except asyncio.TimeoutError:
                logger.warning("Provider following sync timeout", source_type=source_type)
                results.append({
                    "sourceType": source_type,
                    "displayName": display_name,
                    "synced": 0,
                    "success": False,
                    "error": "Sync timeout",
                })
            except ProviderError as exc:
                logger.warning("Provider following sync failed", source_type=source_type, error=str(exc))
                results.append({
                    "sourceType": source_type,
                    "displayName": display_name,
                    "synced": 0,
                    "success": False,
                    "error": str(exc),
                })
            except Exception as exc:
                logger.error("Provider following sync error", source_type=source_type, error=str(exc), exc_info=True)
                results.append({
                    "sourceType": source_type,
                    "displayName": display_name,
                    "synced": 0,
                    "success": False,
                    "error": str(exc),
                })
        await _db.reconcile_model_sources_from_followed()
        if not results:
            return {
                "synced": 0,
                "results": [],
                "localOnly": True,
                "message": "No provider exposes remote following sync; local follows only",
            }
        return {
            "synced": total_synced,
            "results": results,
            "localOnly": False,
            "message": f"Synced {total_synced} remote follows",
        }

    if not _chaturbate_api or not _auth_service:
        raise HTTPException(status_code=503, detail="Chaturbate API not initialized")
    status = _auth_service.get_status()
    if not status.get("isLoggedIn"):
        raise HTTPException(status_code=401, detail="Chaturbate session missing")
    try:
        items = await _chaturbate_api.get_followed_models()
    except Exception as exc:
        logger.error("Chaturbate following sync failed", error=str(exc), exc_info=True)
        raise HTTPException(status_code=502, detail=f"Chaturbate sync failed: {exc}")
    stored = await _store_provider_following("chaturbate", items)
    synced = stored["synced"]
    await _db.reconcile_model_sources_from_followed()
    return {
        "synced": synced,
        "trusted": stored["trusted"],
        "authoritative": stored["authoritative"],
        "skippedReason": stored["skippedReason"],
        "results": [{
            "sourceType": "chaturbate",
            "displayName": "Chaturbate",
            "synced": synced,
            "trusted": stored["trusted"],
            "authoritative": stored["authoritative"],
            "skippedReason": stored["skippedReason"],
            "success": True,
        }],
        "localOnly": False,
        "message": f"Chaturbate: {synced} follows synced",
    }


async def _store_provider_following(source_type: str, items: list[dict]) -> dict:
    return await store_provider_following(_db, source_type, items)


@router.post("/following/{username}/track")
async def track_followed_model(
    username: str,
    source_type: Optional[str] = Query(None, alias="source_type"),
    source: Optional[str] = Query(None),
):
    """
    Add a followed model to the HXYLIVE models table for recording.
    """
    if not _db:
        raise HTTPException(status_code=503, detail="Database not initialized")

    requested_source = (source_type or source or "").strip().lower() or None
    if requested_source and _provider_registry and requested_source not in (_registered_source_types() or set()):
        raise HTTPException(status_code=404, detail=f"Source '{requested_source}' unavailable")

    # Check if already tracked
    existing = await _db.get_model(username, source_type=requested_source)
    if existing:
        return {"message": f"{username} is already tracked", "alreadyTracked": True}

    followed = await _db.get_followed_model(username, source_type=requested_source)
    source_type = (followed or {}).get("source_type") or requested_source or "chaturbate"

    # Add to models table
    await _db.add_or_update_model(
        username=username,
        auto_record=True,
        record_quality="best",
        retention_days=await _get_default_retention_days(),
        source_type=source_type,
    )

    return {"message": f"{username} added to tracking", "tracked": True}
