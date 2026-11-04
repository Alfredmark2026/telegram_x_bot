"""
Telegram Bot：
1) X 帖子链接 → 下载视频 / 图片 / 文字
2) 普通文字 → DeepSeek 对话
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
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
DEEPSEEK_API = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip() or "deepseek-chat"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_VIDEO_BYTES = 49 * 1024 * 1024
MAX_CAPTION = 1024
TELEGRAM_TEXT_LIMIT = 4000
MAX_HISTORY_TURNS = 20  # 每边各算，总条约 40 条消息

MODE_CHAT = "chat"
MODE_TRANSLATE = "translate"

MODE_PROMPTS = {
    MODE_CHAT: (
        "你是 Alfred 的聊天 Bot，由 DeepSeek 驱动。"
        "自我介绍时请说：我是 Alfred 的聊天 Bot，由 DeepSeek 驱动。"
        "回答友好、简洁，不要过度啰嗦。"
        "用户如果发来 X/Twitter 链接，系统会单独处理下载，你主要负责普通聊天与问答。"
    ),
    MODE_TRANSLATE: (
        "假设你是一位精通中英互译的专业人士，对于翻译有深入的理解。"
        "请将用户发来的英文／中文段落翻译成英文／中文。\n\n"
        "规则：\n"
        "- 翻译时要准确传达英文／中文对话事实和背景。\n"
        "- 分成两次翻译，并且回答每一次结果：\n"
        "第一次结果要根据英文／中文内容直译，不要遗漏任何信息；\n"
        "第二次结果要根据第一次直译的结果重新意译，遵守原意的前提下让内容更通俗易懂，"
        "符合英文／中文表达习惯。\n\n"
        "输出格式必须严格如下（不要额外寒暄）：\n"
        "【直译】\n"
        "...\n\n"
        "【意译】\n"
        "...\n\n"
        "判断方向：中文原文→译成英文；英文原文→译成中文；其他语言→译成中文并在末尾注明语种。"
        "专有名词、代码、链接尽量保留原文。"
    ),
}

MODE_LABELS = {
    MODE_CHAT: "聊天模式",
    MODE_TRANSLATE: "翻译模式",
}

# chat_id -> [{"role": "...", "content": "..."}, ...]
chat_histories: dict[int, list[dict[str, str]]] = defaultdict(list)
# chat_id -> mode name
chat_modes: dict[int, str] = defaultdict(lambda: MODE_CHAT)


def get_mode(chat_id: int) -> str:
    mode = chat_modes.get(chat_id, MODE_CHAT)
    return mode if mode in MODE_PROMPTS else MODE_CHAT


def set_mode(chat_id: int, mode: str) -> str:
    if mode not in MODE_PROMPTS:
        raise ValueError(f"未知模式：{mode}")
    chat_modes[chat_id] = mode
    # 切换人设时清空旧对话，避免串戏
    chat_histories.pop(chat_id, None)
    return mode


def extract_status_id(text: str) -> str | None:
    match = STATUS_ID_RE.search(text.strip())
    return match.group(1) if match else None


def truncate(text: str, limit: int = MAX_CAPTION) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def split_telegram_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    text = text.strip() or "（空回复）"
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks


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
        mp4s = [
            {
                "url": video["url"],
                "bitrate": video.get("bitrate") or 0,
                "filesize": video.get("filesize"),
            }
        ]
    mp4s.sort(key=lambda f: f.get("bitrate") or 0, reverse=True)
    return mp4s


def collect_media(status: dict) -> tuple[list[str], list[dict]]:
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
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None

    fd, out = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    out_path = Path(out)
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
    formats = list_mp4_formats(video)
    if not formats:
        return None, None, "没有可用视频地址"

    candidates = []
    for fmt in formats:
        size = fmt.get("filesize")
        if isinstance(size, (int, float)) and size > MAX_VIDEO_BYTES:
            continue
        candidates.append(fmt)
    if not candidates:
        candidates = list(formats)

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
            if formats and url != formats[0]["url"]:
                note = f"已自动选用较小清晰度（{size / 1024 / 1024:.1f} MB）"
            return path, url, note
        last_path = path

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


def get_deepseek_api_key() -> str:
    return os.getenv("DEEPSEEK_API_KEY", "").strip()


async def fetch_deepseek_balance() -> str:
    api_key = get_deepseek_api_key()
    if not api_key:
        return (
            "还没配置 DEEPSEEK_API_KEY。\n"
            "请在服务器 .env 写入后执行：systemctl restart x-telegram-bot"
        )

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            "https://api.deepseek.com/user/balance",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"查询失败 HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()

    available = data.get("is_available")
    status = "可用" if available else "不足/不可用"
    lines = [f"DeepSeek 账户状态：{status}", ""]

    infos = data.get("balance_infos") or []
    if not infos:
        lines.append("未返回余额明细。")
        return "\n".join(lines)

    for info in infos:
        currency = info.get("currency") or "?"
        total = info.get("total_balance") or "0"
        granted = info.get("granted_balance") or "0"
        topped = info.get("topped_up_balance") or "0"
        lines.append(f"币种：{currency}")
        lines.append(f"• 总余额：{total}")
        lines.append(f"• 赠送余额：{granted}")
        lines.append(f"• 充值余额：{topped}")
        lines.append("")

    return "\n".join(lines).strip()


async def ask_deepseek(chat_id: int, user_text: str) -> str:
    api_key = get_deepseek_api_key()
    if not api_key:
        return (
            "还没配置 DeepSeek。请在服务器 .env 里加入：\n"
            "DEEPSEEK_API_KEY=你的密钥\n"
            "然后执行：systemctl restart x-telegram-bot"
        )

    history = chat_histories[chat_id]
    history.append({"role": "user", "content": user_text})
    # 只保留最近若干轮
    if len(history) > MAX_HISTORY_TURNS * 2:
        chat_histories[chat_id] = history[-MAX_HISTORY_TURNS * 2 :]
        history = chat_histories[chat_id]

    mode = get_mode(chat_id)
    system_prompt = MODE_PROMPTS[mode]
    temperature = 0.2 if mode == MODE_TRANSLATE else 0.7
    messages = [{"role": "system", "content": system_prompt}, *history]

    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(
            DEEPSEEK_API,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": DEEPSEEK_MODEL,
                "messages": messages,
                "temperature": temperature,
                "stream": False,
            },
        )
        if resp.status_code >= 400:
            detail = resp.text[:500]
            raise RuntimeError(f"DeepSeek HTTP {resp.status_code}: {detail}")
        data = resp.json()

    try:
        answer = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise RuntimeError(f"DeepSeek 返回格式异常: {data}") from exc

    history.append({"role": "assistant", "content": answer})
    return answer


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    mode = get_mode(chat_id)
    await update.message.reply_text(
        "我是 Alfred 的聊天 Bot，由 DeepSeek 驱动。\n\n"
        f"当前模式：{MODE_LABELS[mode]}\n\n"
        "我可以：\n"
        "1) 聊天 / 翻译（可切换人设）\n"
        "2) 发 X / Twitter 帖子链接 → 下载视频 / 图片 / 文字\n\n"
        "命令：\n"
        "/translate — 切换到翻译模式\n"
        "/chat — 切换到聊天模式\n"
        "/mode — 查看或切换模式\n"
        "/balance — 查询 DeepSeek API 余额\n"
        "/clear — 清空当前对话记忆\n"
        "/help — 查看说明"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is not None:
        chat_histories.pop(chat_id, None)
    await update.message.reply_text("已清空对话记忆，可以重新开始聊。")


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )
    try:
        text = await fetch_deepseek_balance()
        await update.message.reply_text(text)
    except Exception as exc:  # noqa: BLE001
        logger.exception("查询 DeepSeek 余额失败")
        await update.message.reply_text(f"查询余额失败：{exc}")


async def mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    args = context.args or []

    if not args:
        mode = get_mode(chat_id)
        await update.message.reply_text(
            f"当前模式：{MODE_LABELS[mode]}\n\n"
            "切换方式：\n"
            "/translate — 翻译模式\n"
            "/chat — 聊天模式\n"
            "/mode translate\n"
            "/mode chat"
        )
        return

    raw = args[0].strip().lower()
    alias = {
        "translate": MODE_TRANSLATE,
        "translation": MODE_TRANSLATE,
        "翻译": MODE_TRANSLATE,
        "chat": MODE_CHAT,
        "聊天": MODE_CHAT,
    }
    mode = alias.get(raw)
    if not mode:
        await update.message.reply_text("未知模式。可用：translate / chat")
        return

    set_mode(chat_id, mode)
    if mode == MODE_TRANSLATE:
        await update.message.reply_text("OK")
    else:
        await update.message.reply_text(
            "已切换到聊天模式。可以正常对话了。\n"
            "（切换模式时已清空旧对话记忆）"
        )


async def translate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    set_mode(chat_id, MODE_TRANSLATE)
    # 对齐你的提示词约定：进入翻译模式后先回复 OK，等待下一条正文
    await update.message.reply_text("OK")


async def chat_mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    set_mode(chat_id, MODE_CHAT)
    await update.message.reply_text(
        "已切换到聊天模式。可以正常对话了。\n"
        "（切换模式时已清空旧对话记忆）"
    )


async def handle_x_link(message, context: ContextTypes.DEFAULT_TYPE, status_id: str) -> None:
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    status_msg = await message.reply_text("正在解析帖子…")

    temp_files: list[Path] = []
    try:
        status = await fetch_post(status_id)
        photos, videos = collect_media(status)
        caption = build_caption(status)

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


async def handle_chat(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    try:
        answer = await ask_deepseek(message.chat_id, message.text.strip())
        for chunk in split_telegram_text(answer):
            await message.reply_text(chunk)
    except Exception as exc:  # noqa: BLE001
        logger.exception("DeepSeek 对话失败")
        await message.reply_text(f"聊天失败：{exc}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return

    status_id = extract_status_id(message.text)
    if status_id:
        await handle_x_link(message, context, status_id)
    else:
        await handle_chat(message, context)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "未设置 TELEGRAM_BOT_TOKEN。\n"
            "请复制 .env.example 为 .env，填入 BotFather 发给你的 Token。"
        )

    if not os.getenv("DEEPSEEK_API_KEY", "").strip():
        logger.warning("未设置 DEEPSEEK_API_KEY，聊天功能不可用，X 下载仍可用")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("mode", mode_cmd))
    app.add_handler(CommandHandler("translate", translate_cmd))
    app.add_handler(CommandHandler("chat", chat_mode_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot 已启动，按 Ctrl+C 停止")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
