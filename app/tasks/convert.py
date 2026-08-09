"""
Automatic conversion task for TS -> MP4 recordings
"""
import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Optional
from ..logger import logger
from ..core.config import AUTO_CONVERT, KEEP_TS, MIN_RECORDING_BYTES, MIN_RECORDING_SECONDS
from ..subprocess_utils import communicate_with_timeout as _communicate_with_timeout
from .monitor import get_media_created_at, get_video_duration


# Maximum conversion attempts before automatic skip
MAX_CONVERSION_ATTEMPTS = 3
SHORT_RECORDING_PROBE_BYTES = max(MIN_RECORDING_BYTES, 64 * 1024 * 1024)
STALE_CONVERSION_TEMP_SECONDS = 10 * 60
_CONVERSION_TEMP_RE = re.compile(r"^\.(?P<stem>.+)\.[^.]+\.tmp\.mp4$")


def _active_recording_stems(statuses: list[dict]) -> set[str]:
    stems: set[str] = set()
    for status in statuses or []:
        if not status.get("running"):
            continue
        record_path = status.get("record_path")
        if record_path:
            stems.add(Path(record_path).stem)
    return stems


def cleanup_stale_conversion_temps(
    records_root: Path,
    active_stems: set[str],
    *,
    now: Optional[float] = None,
    max_age_seconds: int = STALE_CONVERSION_TEMP_SECONDS,
) -> list[Path]:
    """Remove abandoned conversion outputs, never files tied to active recording."""
    if not records_root.exists():
        return []
    current_time = time.time() if now is None else float(now)
    deleted: list[Path] = []
    for candidate in records_root.rglob(".*.tmp.mp4"):
        match = _CONVERSION_TEMP_RE.match(candidate.name)
        if not match or match.group("stem") in active_stems:
            continue
        try:
            if current_time - candidate.stat().st_mtime < max_age_seconds:
                continue
            candidate.unlink()
            deleted.append(candidate)
        except OSError as exc:
            logger.warning(
                "Unable to delete abandoned temporary MP4",
                temp_file=str(candidate),
                error=str(exc),
            )
    return deleted


def _video_stream_map_from_probe(probe_data: dict) -> str:
    streams = probe_data.get("streams") if isinstance(probe_data, dict) else None
    if not isinstance(streams, list) or not streams:
        return "0:v:0"

    def numeric(value) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    ranked = sorted(
        enumerate(streams),
        key=lambda item: (
            numeric(item[1].get("height") or item[1].get("coded_height")),
            numeric(item[1].get("width") or item[1].get("coded_width")),
            numeric(item[1].get("bit_rate")),
        ),
        reverse=True,
    )
    return f"0:v:{ranked[0][0]}"


def _select_best_video_stream_map(probe_data: dict) -> str:
    """Return an FFmpeg -map value for the highest-quality video stream."""
    candidates = []
    for stream in probe_data.get("streams") or []:
        try:
            index = int(stream.get("index"))
        except (TypeError, ValueError):
            continue
        try:
            height = int(stream.get("height") or stream.get("coded_height") or 0)
        except (TypeError, ValueError):
            height = 0
        try:
            width = int(stream.get("width") or stream.get("coded_width") or 0)
        except (TypeError, ValueError):
            width = 0
        try:
            bitrate = int(stream.get("bit_rate") or 0)
        except (TypeError, ValueError):
            bitrate = 0
        candidates.append((height, width, bitrate, -index, index))

    if not candidates:
        return "0:v:0"
    return f"0:{sorted(candidates, reverse=True)[0][-1]}"


async def _best_video_stream_map(ts_path: Path, ffmpeg_path: str = "ffmpeg") -> str:
    ffprobe_path = ffmpeg_path.replace("ffmpeg", "ffprobe")
    cmd = [
        ffprobe_path,
        "-v", "error",
        "-select_streams", "v",
        "-show_entries", "stream=index,width,height,coded_width,coded_height,bit_rate",
        "-of", "json",
        str(ts_path),
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await _communicate_with_timeout(process, 30)
        if process.returncode == 0 and stdout:
            return _select_best_video_stream_map(json.loads(stdout.decode("utf-8")))
        error_msg = stderr.decode("utf-8", errors="replace") if stderr else ""
        logger.debug("ffprobe video stream selection failed", ts_file=str(ts_path), error=error_msg[:300])
    except Exception as e:
        logger.debug("ffprobe video stream selection unavailable", ts_file=str(ts_path), error=str(e))
    return "0:v:0"


async def convert_ts_to_mp4(
    ts_path: Path,
    mp4_path: Optional[Path] = None,
    ffmpeg_path: str = "ffmpeg"
) -> tuple[bool, Optional[Path], Optional[int]]:
    """
    Convert a TS file to MP4 with optimized compression

    Returns:
        (success, mp4_path, mp4_size)
    """
    if not ts_path.exists():
        logger.error("TS file not found", ts_path=str(ts_path))
        return False, None, None
    try:
        ts_size = ts_path.stat().st_size
    except OSError as exc:
        logger.error("TS file unreadable", ts_path=str(ts_path), error=str(exc))
        return False, None, None
    if ts_size <= 0:
        logger.error("TS file empty", ts_path=str(ts_path))
        return False, None, None

    # Generate the MP4 filename if not provided
    if mp4_path is None:
        mp4_path = ts_path.with_suffix('.mp4')

    # FFmpeg must never write directly to the published path: an interrupted
    # process can otherwise leave a truncated MP4 that the next scan mistakes
    # for a successful conversion. Keep the real MP4 suffix so FFmpeg can infer
    # the output container, then atomically replace the destination on success.
    temp_mp4_path = mp4_path.with_name(
        f".{mp4_path.stem}.{uuid.uuid4().hex}.tmp{mp4_path.suffix}"
    )

    logger.info("TS->MP4 remux started",
               ts_file=ts_path.name,
               mp4_file=mp4_path.name)

    # Remux only: the HLS source is already H.264+AAC, so copying codecs
    # produces a valid MP4 almost instantly with near-zero CPU usage. Keep a
    # conventional video-then-audio stream order for broad player compatibility.
    # -bsf:a aac_adtstoasc : reformat ADTS AAC (TS) into MP4-friendly ASC
    video_map = await _best_video_stream_map(ts_path, ffmpeg_path)
    cmd = [
        ffmpeg_path,
        "-nostdin", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts+igndts",
        "-i", str(ts_path),
        "-map", video_map,
        "-map", "0:a:0?",
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        "-y",
        str(temp_mp4_path)
    ]

    try:
        # Start the conversion
        logger.debug("Commande FFmpeg", command=" ".join(cmd[:8]) + "...")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await _communicate_with_timeout(process, 3600)

        if process.returncode == 0 and temp_mp4_path.exists() and temp_mp4_path.stat().st_size > 0:
            # Publish only a complete, non-empty FFmpeg output. Path.replace()
            # uses os.replace(), which is atomic while both files are siblings.
            mp4_size = temp_mp4_path.stat().st_size
            reduction = ((ts_size - mp4_size) / ts_size) * 100
            temp_mp4_path.replace(mp4_path)

            logger.success("Conversion succeeded",
                         ts_file=ts_path.name,
                         mp4_file=mp4_path.name,
                         ts_size_mb=f"{ts_size / 1024 / 1024:.1f}",
                         mp4_size_mb=f"{mp4_size / 1024 / 1024:.1f}",
                         reduction_percent=f"{reduction:.1f}%")

            return True, mp4_path, mp4_size
        else:
            # A zero exit code without a usable output is still a failed
            # conversion and must not make the caller delete the TS source.
            if process.returncode == 0:
                error_msg = "FFmpeg produced no non-empty MP4 output"
            else:
                error_msg = stderr.decode('utf-8') if stderr else "Unknown error"
            logger.error("FFmpeg conversion error",
                        ts_file=ts_path.name,
                        error=error_msg[:500])
            return False, None, None

    except Exception as e:
        logger.error("Conversion exception",
                    ts_file=ts_path.name,
                    error=str(e),
                    exc_info=True)
        return False, None, None
    finally:
        # Covers FFmpeg failures, output validation failures, exceptions, and
        # task cancellation. Never leave a stale temp file for later scans.
        try:
            temp_mp4_path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(
                "Unable to delete temporary MP4",
                temp_file=str(temp_mp4_path),
                error=str(e),
            )


async def _is_valid_existing_mp4(mp4_path: Path, ffmpeg_path: str = "ffmpeg") -> bool:
    """Return whether an existing MP4 is safe to adopt and use to remove its TS."""
    try:
        if not mp4_path.is_file() or mp4_path.stat().st_size <= 0:
            return False
    except OSError:
        return False

    # A stale direct-to-final FFmpeg output can be non-empty but lack a usable
    # MP4 index. ffprobe reports no duration for those interrupted files.
    return await get_video_duration(mp4_path, ffmpeg_path) > 0


async def _get_recording_settings(db) -> tuple[bool, bool]:
    """Read auto_convert and keep_ts settings from DB, falling back to env var defaults."""
    auto_convert_val = await db.get_setting("auto_convert")
    keep_ts_val = await db.get_setting("keep_ts")

    if auto_convert_val is not None:
        auto_convert = auto_convert_val.lower() in {"1", "true", "yes"}
    else:
        auto_convert = AUTO_CONVERT

    if keep_ts_val is not None:
        keep_ts = keep_ts_val.lower() in {"1", "true", "yes"}
    else:
        keep_ts = KEEP_TS

    return auto_convert, keep_ts


async def auto_convert_recordings_task(db, output_dir: Path, ffmpeg_manager, ffmpeg_path: str = "ffmpeg"):
    """
    Task that scans all .ts files and converts them if they are not currently recording.
    Respects auto_convert and keep_ts settings from DB.
    """
    logger.info("Automatic conversion task started", task="auto-convert")

    # INITIAL SCAN: scan all existing TS files at startup
    logger.info("Initial scan of existing TS files", task="auto-convert")
    try:
        records_root = output_dir / "records"
        if records_root.exists():
            stale_temps = cleanup_stale_conversion_temps(
                records_root,
                _active_recording_stems(ffmpeg_manager.list_status()),
            )
            if stale_temps:
                logger.info(
                    "Abandoned temporary MP4s deleted",
                    task="auto-convert",
                    count=len(stale_temps),
                )
            for user_dir in records_root.iterdir():
                if user_dir.is_dir():
                    username = user_dir.name
                    for ts_file in user_dir.rglob("*.ts"):
                        # Check whether already in the DB
                        recordings = await db.get_recordings(username)
                        existing = next((r for r in recordings if r['filename'] == ts_file.name), None)

                        if not existing:
                            # Add to the DB
                            logger.info("Indexing existing file", username=username, file=ts_file.name)
                            recording_id = f"{username}_{ts_file.stem}"
                            await db.add_or_update_recording(
                                username=username,
                                filename=ts_file.name,
                                file_path=str(ts_file),
                                file_size=ts_file.stat().st_size,
                                recording_id=recording_id,
                                duration_seconds=0,
                                is_converted=False,
                                created_at=await get_media_created_at(
                                    ts_file,
                                    ffmpeg_path,
                                    fallback_timestamp=int(ts_file.stat().st_mtime),
                                ),
                            )
        logger.success("Initial scan finished", task="auto-convert")
    except Exception as e:
        logger.error("Initial scan error", error=str(e), exc_info=True)

    while True:
        try:
            # Scan every 2 minutes (remux is cheap but glob/stat over large dirs is not free)
            await asyncio.sleep(120)

            # Read settings from DB each iteration (runtime changeable)
            auto_convert, keep_ts = await _get_recording_settings(db)

            # Scan ALL user folders under /records to find .ts files
            records_root = output_dir / "records"
            if not records_root.exists():
                continue

            # Fetch active sessions to know which files are currently recording
            active_sessions = ffmpeg_manager.list_status()
            active_recordings = {}  # {username: recording_filename}
            for session in active_sessions:
                if session.get('running'):
                    username = session.get('person')
                    record_path = session.get('record_path', '')
                    if username and record_path:
                        filename = Path(record_path).name
                        active_recordings[username] = filename
            stale_temps = cleanup_stale_conversion_temps(
                records_root,
                _active_recording_stems(active_sessions),
            )
            if stale_temps:
                logger.info(
                    "Abandoned temporary MP4s deleted",
                    task="auto-convert",
                    count=len(stale_temps),
                )

            logger.debug("Sessions actives", active_count=len(active_recordings), active_users=list(active_recordings.keys()))

            for user_dir in records_root.iterdir():
                if not user_dir.is_dir():
                    continue

                username = user_dir.name

                # Load recordings ONCE per user (avoids N+1 DB calls)
                user_recordings = await db.get_recordings(username)
                recordings_by_filename = {r['filename']: r for r in user_recordings}

                # Scan ALL .ts files in the user folder
                for ts_file in user_dir.rglob("*.ts"):
                    ts_path = Path(ts_file)

                    # Check whether this file is currently recording
                    if username in active_recordings and active_recordings[username] == ts_file.name:
                        logger.debug("File currently recording, skip",
                                   username=username,
                                   file=ts_file.name)
                        continue

                    # Check whether the MP4 already exists
                    mp4_path = ts_path.with_suffix('.mp4')
                    if mp4_path.exists() and await _is_valid_existing_mp4(mp4_path, ffmpeg_path):
                        logger.debug("MP4 already exists, skip conversion",
                                   username=username,
                                   file=ts_file.name)

                        existing = recordings_by_filename.get(ts_file.name)

                        if existing and not existing.get('is_converted'):
                            # Update the DB
                            created_at = await get_media_created_at(
                                ts_path,
                                ffmpeg_path,
                                fallback_timestamp=int(ts_path.stat().st_mtime),
                            )
                            await db.add_or_update_recording(
                                username=username,
                                filename=ts_file.name,
                                file_path=str(ts_path),
                                file_size=ts_path.stat().st_size if ts_path.exists() else existing['file_size'],
                                recording_id=existing.get('recording_id'),
                                duration_seconds=existing.get('duration_seconds', 0),
                                thumbnail_path=existing.get('thumbnail_path'),
                                mp4_path=str(mp4_path),
                                mp4_size=mp4_path.stat().st_size,
                                is_converted=True,
                                created_at=created_at,
                            )
                            logger.info("DB updated for existing MP4",
                                      username=username,
                                      file=ts_file.name)

                        # Only delete TS if keep_ts is disabled
                        if not keep_ts and ts_path.exists():
                            try:
                                ts_path.unlink()
                                logger.success("TS file deleted (MP4 already exists)",
                                             username=username,
                                             ts_file=ts_file.name,
                                             mp4_file=mp4_path.name)
                            except Exception as e:
                                logger.error("Error deleting TS",
                                           ts_file=ts_file.name,
                                           error=str(e))
                        continue
                    elif mp4_path.exists():
                        logger.warning(
                            "Existing MP4 invalid, new conversion required",
                            username=username,
                            ts_file=ts_file.name,
                            mp4_file=mp4_path.name,
                        )

                    # Ensure the TS file is stable (not modified for 180s)
                    last_modified = ts_path.stat().st_mtime
                    if time.time() - last_modified < 180:
                        # File still being written
                        logger.debug("File modified recently, waiting for stability",
                                   file=ts_path.name,
                                   last_modified_ago=f"{time.time() - last_modified:.0f}s")
                        continue

                    existing = recordings_by_filename.get(ts_file.name)
                    ts_size = ts_path.stat().st_size
                    candidate_duration = int((existing or {}).get('duration_seconds') or 0)
                    if ts_size <= SHORT_RECORDING_PROBE_BYTES and candidate_duration == 0 and ts_size > 0:
                        candidate_duration = await get_video_duration(ts_path, ffmpeg_path)

                    is_short_fragment = (
                        ts_size == 0
                        or (candidate_duration == 0 and ts_size < MIN_RECORDING_BYTES)
                        or (0 < candidate_duration < MIN_RECORDING_SECONDS)
                    )
                    if is_short_fragment:
                        try:
                            if ts_path.exists():
                                ts_path.unlink()
                            await db.delete_recording(username, ts_file.name)
                            logger.warning(
                                "Fragment ignored before conversion",
                                username=username,
                                filename=ts_file.name,
                                duration_seconds=candidate_duration,
                                file_size=ts_size,
                                min_seconds=MIN_RECORDING_SECONDS,
                                min_bytes=MIN_RECORDING_BYTES,
                            )
                        except Exception as e:
                            logger.error(
                                "Error deleting fragment before conversion",
                                username=username,
                                filename=ts_file.name,
                                error=str(e),
                            )
                        continue

                    # If auto_convert is disabled, just index the TS file in DB
                    if not auto_convert:
                        if not existing:
                            recording_id = f"{username}_{ts_file.stem}"
                            # Calculate duration from TS file
                            duration = candidate_duration
                            if duration == 0:
                                duration = await get_video_duration(ts_path, ffmpeg_path)
                            created_at = await get_media_created_at(
                                ts_path,
                                ffmpeg_path,
                                fallback_timestamp=int(ts_path.stat().st_mtime),
                            )
                            await db.add_or_update_recording(
                                username=username,
                                filename=ts_file.name,
                                file_path=str(ts_path),
                                file_size=ts_path.stat().st_size,
                                recording_id=recording_id,
                                duration_seconds=duration if duration > 0 else 0,
                                is_converted=False,
                                created_at=created_at,
                            )
                            logger.info("TS indexed (auto-convert disabled)",
                                      username=username,
                                      filename=ts_file.name)
                        continue

                    # Skip if too many failed attempts for this file
                    attempts = (existing or {}).get('conversion_attempts') or 0
                    if attempts >= MAX_CONVERSION_ATTEMPTS:
                        logger.debug("Conversion skipped (too many failures)",
                                   username=username,
                                   filename=ts_file.name,
                                   attempts=attempts,
                                   task="auto-convert")
                        continue

                    # File is not currently recording, it can be converted
                    logger.info("Starting automatic conversion",
                              username=username,
                              filename=ts_file.name,
                              attempt=attempts + 1,
                              task="auto-convert")

                    success, mp4_path_result, mp4_size = await convert_ts_to_mp4(
                        ts_path,
                        mp4_path,
                        ffmpeg_path
                    )

                    if success and mp4_path_result:
                        # existing already comes from the recordings_by_filename cache
                        recording_id = existing.get('recording_id') if existing else f"{username}_{ts_file.stem}"

                        # Reset failure counter because conversion succeeded
                        if (existing or {}).get('conversion_attempts'):
                            await db.reset_conversion_failure(recording_id)

                        # Recompute duration on the MP4 file now that it is stable
                        final_duration = await get_video_duration(mp4_path_result, ffmpeg_path)

                        # Use the recomputed duration or the existing one if calculation fails
                        if final_duration > 0:
                            duration_to_use = final_duration
                            logger.info("Duration recomputed after conversion",
                                      username=username,
                                      filename=ts_file.name,
                                      duration=final_duration)
                        else:
                            duration_to_use = existing.get('duration_seconds', 0) if existing else 0

                        created_at = await get_media_created_at(
                            ts_path,
                            ffmpeg_path,
                            fallback_timestamp=int(ts_path.stat().st_mtime) if ts_path.exists() else existing.get('created_at') if existing else None,
                        )
                        await db.add_or_update_recording(
                            username=username,
                            filename=ts_file.name,
                            file_path=str(ts_path),
                            file_size=ts_path.stat().st_size if ts_path.exists() else 0,
                            recording_id=recording_id,
                            duration_seconds=duration_to_use,
                            thumbnail_path=existing.get('thumbnail_path') if existing else None,
                            mp4_path=str(mp4_path_result),
                            mp4_size=mp4_size,
                            is_converted=True,
                            created_at=created_at,
                        )

                        # Only delete TS if keep_ts is disabled
                        if not keep_ts:
                            try:
                                if ts_path.exists():
                                    ts_path.unlink()
                                    logger.success("TS file deleted after conversion",
                                                 username=username,
                                                 ts_file=ts_file.name,
                                                 mp4_file=mp4_path_result.name)
                            except Exception as e:
                                logger.error("Error deleting TS",
                                           ts_file=ts_file.name,
                                           error=str(e))
                        else:
                            logger.info("TS file kept (keep_ts enabled)",
                                      username=username,
                                      ts_file=ts_file.name)

                        logger.success("Recording converted and indexed",
                                     username=username,
                                     filename=ts_file.name,
                                     mp4_file=mp4_path_result.name)
                    else:
                        # Track the failure in DB to avoid infinite retries and inform the UI
                        # Ensure the recording exists first
                        if not existing:
                            duration = await get_video_duration(ts_path, ffmpeg_path)
                            recording_id = f"{username}_{ts_file.stem}"
                            created_at = await get_media_created_at(
                                ts_path,
                                ffmpeg_path,
                                fallback_timestamp=int(ts_path.stat().st_mtime),
                            )
                            await db.add_or_update_recording(
                                username=username,
                                filename=ts_file.name,
                                file_path=str(ts_path),
                                file_size=ts_path.stat().st_size,
                                recording_id=recording_id,
                                duration_seconds=duration if duration > 0 else 0,
                                is_converted=False,
                                created_at=created_at,
                            )
                        error_msg = f"FFmpeg conversion failed (attempt {attempts + 1}/{MAX_CONVERSION_ATTEMPTS})"
                        await db.mark_conversion_failed(username, ts_file.name, error_msg)
                        logger.error("Conversion failed",
                                   username=username,
                                   filename=ts_file.name,
                                   attempt=attempts + 1,
                                   max_attempts=MAX_CONVERSION_ATTEMPTS)

                    # Small yield between conversions; remux is nearly instant
                    await asyncio.sleep(1)

        except Exception as e:
            logger.error("Error in conversion task",
                        error=str(e),
                        exc_info=True)
            await asyncio.sleep(60)
