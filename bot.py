"""
Telegram Bot：输入 X (Twitter) 帖子链接，按类型返回视频 / 图片 / 文字。
数据来源：FxTwitter 公开 API（无需 X 官方开发者账号）。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telegram import InputMediaPhoto, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("x-bot")

STATUS_ID_RE = re.compile(
    r"(?:https?://)?(?:(?:www|mobile|m)\.)?"
    r"(?:twitter\.com|x\.com|fxtwitter\.com|vxtwitter\.com|fixupx\.com|fixvx\.com)"
    r"/[^/\s]+/status(?:es)?/(\d+)",
    re.IGNORECASE,
)

FX_API = "https://api.fxtwitter.com/2/status/{status_id}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_VIDEO_BYTES = 49 * 1024 * 1024  # 留一点余量，避免卡在 50MB 边界
MAX_CAPTION = 1024


def extract_status_id(text: str) -> str | None:
    match = STATUS_ID_RE.search(text.strip())
    return match.group(1) if match else None


def truncate(text: str, limit: int = MAX_CAPTION) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


async def fetch_post(status_id: str) -> dict:
    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": USER_AGENT}) as client:
        resp = await client.get(FX_API.format(status_id=status_id))
        resp.raise_for_status()
        data = resp.json()

    if data.get("code") not in (200, None) and "status" not in data:
        raise RuntimeError(data.get("message") or f"API 返回异常: {data.get('code')}")

    status = data.get("status")
    if not status:
        raise RuntimeError("未找到帖子内容（可能已删除或设为私密）")
    return status


def list_mp4_formats(video: dict) -> list[dict]:
    """收集可用 mp4 清晰度，按码率从高到低排序。"""
    formats = video.get("formats") or []
    mp4s = [
        f
        for f in formats
        if f.get("url")
        and (
            f.get("container") == "mp4"
            or ".mp4" in str(f.get("url", "")).split("?")[0].lower()
        )
    ]
    if not mp4s and video.get("url"):
        mp4s = [{"url": video["url"], "bitrate": video.get("bitrate") or 0, "filesize": video.get("filesize")}]
    mp4s.sort(key=lambda f: f.get("bitrate") or 0, reverse=True)
    return mp4s


def collect_media(status: dict) -> tuple[list[str], list[dict]]:
    """返回 (photo_urls, video_objects)。"""
    media = status.get("media") or {}
    photos: list[str] = []
    videos: list[dict] = []

    for photo in media.get("photos") or []:
        url = photo.get("url")
        if url:
            photos.append(url)

    for video in media.get("videos") or []:
        if list_mp4_formats(video) or video.get("url"):
            videos.append(video)

    external = media.get("external")
    if external and external.get("type") == "video" and external.get("url"):
        videos.append({"url": external["url"], "formats": []})

    return photos, videos


def build_caption(status: dict) -> str:
    author = status.get("author") or {}
    name = author.get("name") or ""
    screen = author.get("screen_name") or ""
    text = (status.get("text") or "").strip()
    header = f"{name} (@{screen})".strip() if screen else name
    if header and text:
        return truncate(f"{header}\n\n{text}")
    return truncate(header or text or "（无文字内容）")


async def download_file(url: str, suffix: str) -> Path:
    async with httpx.AsyncClient(
        timeout=180.0,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.content

    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    file_path = Path(path)
    file_path.write_bytes(data)
    return file_path


def compress_video(src: Path, max_bytes: int = MAX_VIDEO_BYTES) -> Path | None:
    """若本机有 ffmpeg，压缩到接近 Telegram 限制。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None

    fd, out = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    out_path = Path(out)
    # 目标约 45MB，码率按 2 分钟粗略估算；再不行逐步降
    for crf in (28, 32, 36):
        try:
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-i",
                    str(src),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    str(crf),
                    "-c:a",
                    "aac",
                    "-b:a",
                    "96k",
                    "-movflags",
                    "+faststart",
                    str(out_path),
                ],
                check=True,
                capture_output=True,
            )
            if out_path.exists() and 0 < out_path.stat().st_size <= max_bytes:
                return out_path
        except (subprocess.CalledProcessError, OSError) as exc:
            logger.warning("ffmpeg 压缩失败 crf=%s: %s", crf, exc)

    try:
        out_path.unlink(missing_ok=True)
    except OSError:
        pass
    return None


async def download_video_under_limit(
    video: dict,
    temp_files: list[Path],
) -> tuple[Path | None, str | None, str]:
    """
    依次尝试从高到低清晰度，找到不超过限制的文件。
    返回 (本地路径, 所用直链, 说明信息)。
    """
    formats = list_mp4_formats(video)
    if not formats:
        return None, None, "没有可用视频地址"

    # 若 API 提供 filesize，优先跳过明显超限的
    candidates = []
    for fmt in formats:
        size = fmt.get("filesize")
        if isinstance(size, (int, float)) and size > MAX_VIDEO_BYTES:
            continue
        candidates.append(fmt)
    if not candidates:
        candidates = list(formats)  # 全超限也仍尝试下载，后面再压缩

    last_url = candidates[0]["url"]
    last_path: Path | None = None

    for fmt in candidates:
        url = fmt["url"]
        last_url = url
        path = await download_file(url, ".mp4")
        temp_files.append(path)
        size = path.stat().st_size
        if size <= MAX_VIDEO_BYTES:
            note = ""
            # 若不是最高清，提示一下
            if formats and url != formats[0]["url"]:
                note = f"已自动选用较小清晰度（{size / 1024 / 1024:.1f} MB）"
            return path, url, note
        last_path = path

    # 全部超限：尝试 ffmpeg 压缩最大的那个（通常画质最好）
    if last_path is not None:
        compressed = compress_video(last_path)
        if compressed is not None:
            temp_files.append(compressed)
            return compressed, last_url, "原视频超限，已自动压缩后发送"

    return None, last_url, (
        f"视频约 {last_path.stat().st_size / 1024 / 1024:.1f} MB，"
        f"超过 Telegram Bot 限制（50 MB），且无法压到限制内"
        if last_path
        else "无法下载可用视频"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "把 X / Twitter 帖子链接发给我即可。\n\n"
        "支持：\n"
        "• 视频帖 → 下载视频发回（过大时自动换小清晰度/压缩）\n"
        "• 图片帖 → 下载图片发回\n"
        "• 纯文字 → 直接返回文字\n\n"
        "示例：\n"
        "https://x.com/user/status/1234567890"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return

    status_id = extract_status_id(message.text)
    if not status_id:
        await message.reply_text(
            "未识别到有效的 X 帖子链接。\n"
            "请发送类似：https://x.com/用户名/status/数字ID"
        )
        return

    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    status_msg = await message.reply_text("正在解析帖子…")

    temp_files: list[Path] = []
    try:
        status = await fetch_post(status_id)
        photos, videos = collect_media(status)
        caption = build_caption(status)

        # 1) 视频
        if videos:
            await status_msg.edit_text("检测到视频，正在选择合适清晰度并下载…")
            await context.bot.send_chat_action(
                chat_id=message.chat_id, action=ChatAction.UPLOAD_VIDEO
            )
            for i, video in enumerate(videos):
                path, url, note = await download_video_under_limit(video, temp_files)
                if path is None:
                    await message.reply_text(
                        f"{note}\n改为发送直链：\n{url or '（无）'}"
                    )
                    continue

                send_caption = caption if i == 0 else None
                if note and send_caption:
                    send_caption = truncate(f"{send_caption}\n\n（{note}）")
                elif note:
                    send_caption = note

                with path.open("rb") as f:
                    await message.reply_video(
                        video=f,
                        caption=send_caption,
                        supports_streaming=True,
                    )
            await status_msg.delete()
            return

        # 2) 图片
        if photos:
            await status_msg.edit_text(f"检测到 {len(photos)} 张图片，正在下载…")
            await context.bot.send_chat_action(
                chat_id=message.chat_id, action=ChatAction.UPLOAD_PHOTO
            )

            if len(photos) == 1:
                path = await download_file(photos[0], ".jpg")
                temp_files.append(path)
                if path.stat().st_size > MAX_PHOTO_BYTES:
                    await message.reply_text(
                        f"图片过大，改为发送直链：\n{photos[0]}\n\n{caption}"
                    )
                else:
                    with path.open("rb") as f:
                        await message.reply_photo(photo=f, caption=caption)
            else:
                opened_files = []
                try:
                    media_group: list[InputMediaPhoto] = []
                    for i, url in enumerate(photos[:10]):
                        path = await download_file(url, ".jpg")
                        temp_files.append(path)
                        if path.stat().st_size > MAX_PHOTO_BYTES:
                            await message.reply_text(
                                f"第 {i + 1} 张图片过大，直链：\n{url}"
                            )
                            continue
                        handle = path.open("rb")
                        opened_files.append(handle)
                        media_group.append(
                            InputMediaPhoto(
                                media=handle,
                                caption=caption if not media_group else None,
                            )
                        )
                    if media_group:
                        await message.reply_media_group(media=media_group)
                finally:
                    for handle in opened_files:
                        handle.close()
            await status_msg.delete()
            return

        # 3) 纯文字
        await status_msg.edit_text(caption, disable_web_page_preview=True)

    except httpx.HTTPStatusError as exc:
        logger.exception("HTTP 错误")
        await status_msg.edit_text(f"请求失败：HTTP {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("处理失败")
        await status_msg.edit_text(f"处理失败：{exc}")
    finally:
        for path in temp_files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "未设置 TELEGRAM_BOT_TOKEN。\n"
            "请复制 .env.example 为 .env，填入 BotFather 发给你的 Token。"
        )

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot 已启动，按 Ctrl+C 停止")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
