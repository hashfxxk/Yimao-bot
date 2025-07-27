# yimao_plugin/__init__.py
import asyncio
import base64
import httpx
import logging
import datetime
import json
from typing import Optional

from nonebot import get_driver, on_command, on_message
from nonebot.rule import to_me
from nonebot.matcher import Matcher
from nonebot.params import CommandArg
from nonebot.adapters.onebot.v11 import Bot, Event, Message, GroupMessageEvent

from . import data_store, handlers, utils, config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("GeminiPlugin")
driver = get_driver()

@driver.on_startup
async def on_startup():
    logger.info("正在加载用户记忆...")
    data_store.load_memory_from_file()
    logger.info("正在加载群组长期记忆摘要...")
    data_store.load_group_summaries_from_file()
    logger.info("一猫AI插件已加载并准备就绪。")

@driver.on_shutdown
async def on_shutdown():
    logger.info("正在保存用户记忆...")
    data_store.save_memory_to_file()
    logger.info("正在保存群组长期记忆摘要...")
    data_store.save_group_summaries_to_file()
    logger.info("用户记忆和群组摘要已保存。")

# --- 指令注册 ---
# 保留那些前缀清晰、不会与@消息混淆的指令
jm_matcher = on_command("jm", aliases={"/jm"}, priority=5, block=True)
@jm_matcher.handle()
async def _(bot: Bot, event: Event, matcher: Matcher, args: Message = CommandArg()):
    album_id = args.extract_plain_text().strip()
    if not album_id.isdigit():
        await matcher.finish("ID格式错误，请输入纯数字的ID。")
    try:
        await bot.call_api("set_msg_emoji_like", message_id=event.message_id, emoji_id='128164')
    except: pass
    result = await handlers.run_jm_download_task(bot, event, album_id)
    if result == "not_found":
        await matcher.send(f"喵~ 找不到ID为 {album_id} 的本子。")
        try:
            await bot.call_api("unset_msg_emoji_like", message_id=event.message_id, emoji_id='128164')
            await bot.call_api("set_msg_emoji_like", message_id=event.message_id, emoji_id='10060')
        except: pass

random_jm_matcher = on_command("随机jm", aliases={"随机JM"}, priority=5, block=True)
@random_jm_matcher.handle()
async def _(bot: Bot, event: Event, matcher: Matcher):
    await handlers.handle_random_jm(bot, event, matcher)

challenge_matcher = on_command("#", priority=5, block=True)
@challenge_matcher.handle()
async def _(bot: Bot, matcher: Matcher, event: Event):
    await handlers.handle_challenge_chat(bot, matcher, event)


# --- 核心处理器：“总指挥官”模式 ---
# 这个处理器将接管所有@机器人的消息，并进行智能分发
at_me_handler = on_message(rule=to_me(), priority=10, block=True)
@at_me_handler.handle()
async def _(bot: Bot, matcher: Matcher, event: Event):
    # 1. 优先处理非纯文本的复杂情况（转发和回复）
    forward_id = next((seg.data["id"] for seg in event.message if seg.type == "forward"), None)
    if forward_id:
        await handle_forwarded_message(bot, matcher, event, forward_id)
        return
    if event.reply:
        await handle_reply_message(bot, matcher, event)
        return
    
    # 2. 将剩余的纯文本@消息视为指令或聊天
    text = event.get_plaintext().strip()
    cmd_parts = text.split()
    cmd = cmd_parts[0].lower() if cmd_parts else ""
    
    # 3. 指令分发系统：检查是否是特定指令
    if cmd == "help":
        await matcher.finish(utils.get_help_menu())
        return

    # 兼容 //restart 和 /restart
    if cmd.lstrip('/') == "restart":
        await handlers.handle_clear_command(matcher, event)
        return

    # 兼容 //memory, /memory, 和 memory
    if cmd.lstrip('/') == "memory":
        # 提取 memory 指令后的参数
        args_text = text.split(maxsplit=1)[1] if len(cmd_parts) > 1 else ""
        args_msg = Message(args_text)
        await handlers.handle_memory_command(matcher, event, args=args_msg)
        return

    # 4. 如果不是任何已知指令，则进入通用聊天处理器
    await handle_direct_at_message(bot, matcher, event)


# --- 辅助处理函数 (保持最终形态) ---

def _describe_message_content_sync(raw_message) -> str:
    if not raw_message: return "[一条空消息]"
    if isinstance(raw_message, str): return raw_message.strip()
    if isinstance(raw_message, list):
        text_parts = [seg['data']['text'] for seg in raw_message if seg.get('type') == 'text' and seg.get('data', {}).get('text', '').strip()]
        if text_parts: return "".join(text_parts).strip()
        for seg in raw_message:
            seg_type = seg.get('type')
            if seg_type == 'image': return "[一张图片]"
            if seg_type == 'face': return "[一个QQ表情]"
            if seg_type == 'record': return "[一条语音]"
            if seg_type not in ['reply', 'forward', 'json']: return f"[一条类型为'{seg_type}'的特殊消息]"
    return "[一条非文本消息]"

async def describe_message_content_async(bot: Bot, msg_info: dict) -> str:
    message_id = msg_info.get("message_id")
    sender_id = msg_info.get("sender", {}).get("user_id")
    raw_message = msg_info.get("message")

    if message_id and sender_id and str(sender_id) == bot.self_id:
        cached_content = data_store.get_forward_content_from_cache(message_id)
        if cached_content:
            return f"[我之前发送的一段长消息，内容是：\n---\n{cached_content}\n---]"
            
    if not isinstance(raw_message, list):
        return _describe_message_content_sync(raw_message)

    forward_id = next((seg.get("data", {}).get("id") for seg in raw_message if seg.get("type") == "forward"), None)
    if forward_id:
        try:
            forwarded_messages = await bot.get_forward_msg(id=forward_id)
            if not forwarded_messages: return "[一段已无法打开的空聊天记录]"
            script = [f"{m['sender'].get('card') or m['sender'].get('nickname', '未知用户')}: {_describe_message_content_sync(m.get('content'))}" for m in forwarded_messages if _describe_message_content_sync(m.get('content'))]
            return f"[一段聊天记录，内容如下：\n---\n{'\n'.join(script)}\n---]"
        except Exception as e:
            logger.error(f"无法展开聊天记录 (ID: {forward_id}): {e}")
            return "[一段已无法打开的聊天记录]"

    json_seg = next((seg for seg in raw_message if seg.get("type") == "json"), None)
    if json_seg:
        try: return f"[{json.loads(json_seg.get('data',{}).get('data','{}')).get('prompt', '[合并转发]')}]"
        except (json.JSONDecodeError, AttributeError): return "[一条无法解析的JSON消息]"
    
    return _describe_message_content_sync(raw_message)

async def handle_forwarded_message(bot: Bot, matcher: Matcher, event: Event, forward_id: str):
    logger.info(f"检测到合并转发消息，ID: {forward_id}，正在解析...")
    try:
        msg_info_for_describe = {"message": [{"type": "forward", "data": {"id": forward_id}}]}
        desc = await describe_message_content_async(bot, msg_info_for_describe)
        user_question = event.get_plaintext().strip()
        prompt = f"请基于以下聊天记录，回答用户的问题。\n\n【聊天记录】\n{desc}\n\n【需要你回答的用户的问题】\n{user_question}"
        await handlers.handle_chat_session(bot, matcher, event, {"role": "user", "content": prompt})
    except Exception as e:
        logger.error(f"解析合并转发消息时出错: {e}", exc_info=True)
        await matcher.send("喵呜~ 我打不开这个聊天记录盒子...")

async def handle_reply_message(bot: Bot, matcher: Matcher, event: Event):
    try:
        replied_msg_info = await bot.get_msg(message_id=event.reply.message_id)
        if replied_msg_info.get('sender', {}).get('user_id') == int(bot.self_id):
            await handle_reply_to_bot(bot, matcher, event, replied_msg_info)
        else:
            await handle_reply_to_other(bot, matcher, event, replied_msg_info)
    except Exception as e:
        logger.error(f"处理引用回复时发生意外错误: {e}", exc_info=True)
        await matcher.send("喵呜~ 分析这段对话时我的大脑宕机了...")

async def handle_reply_to_bot(bot: Bot, matcher: Matcher, event: Event, replied_msg_info: dict):
    logger.info("处理对机器人消息的回复...")
    try:
        bot_content = await describe_message_content_async(bot, replied_msg_info)
        user_question = event.get_plaintext().strip()
        prompt = f"这是关于机器人一条历史发言的问题，请根据上下文回答用户。\n\n【机器人当时的发言】\n{bot_content}\n\n【需要你回答的用户的问题】\n{user_question}"
        await handlers.handle_chat_session(bot, matcher, event, {"role": "user", "content": prompt})
    except Exception as e:
        logger.error(f"处理对机器人回复时出错: {e}", exc_info=True)
        await matcher.send("喵呜~ 分析机器人自己的话时，我的大脑短路了...")

async def handle_reply_to_other(bot: Bot, matcher: Matcher, event: Event, replied_msg_info: dict):
    logger.info("处理对其他用户消息的回复...")
    try:
        user_question = event.get_plaintext().strip()
        raw_msg = replied_msg_info.get('message', [])
        img_url = next((s.get('data', {}).get('url') for s in raw_msg if s.get('type') == 'image' and s.get('data', {}).get('url')), None)
        
        if img_url:
            async with httpx.AsyncClient() as c:
                img_b64 = base64.b64encode((await c.get(img_url, timeout=60.0)).content).decode()
            content = [{"type": "text", "text": f"请分析这张图片并回答用户的问题。\n\n【需要你回答的用户的问题】\n{user_question}"}, 
                       {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}]
            await handlers.handle_chat_session(bot, matcher, event, {"role": "user", "content": content})
        else:
            replied_text = await describe_message_content_async(bot, replied_msg_info)
            sender_name = replied_msg_info['sender'].get('card') or replied_msg_info['sender'].get('nickname', '某人')
            prompt = f"这是关于一段对话的问题，请根据上下文回答用户。\n\n【“{sender_name}”的发言】\n{replied_text}\n\n【需要你回答的用户的问题】\n{user_question}"
            await handlers.handle_chat_session(bot, matcher, event, {"role": "user", "content": prompt})
    except Exception as e:
        logger.error(f"处理对他人回复时出错: {e}", exc_info=True)
        await matcher.send("喵呜~ 分析别人的话时，我的大脑短路了...")

async def handle_direct_at_message(bot: Bot, matcher: Matcher, event: Event):
    full_text = event.get_plaintext().strip()
    if not full_text: 
        await matcher.finish("喵呜？主人有什么事吗？")
    await handlers.handle_chat_session(bot, matcher, event, {"role": "user", "content": full_text})

# --- 主动聊天处理器 ---
active_chat_handler = on_message(priority=99, block=False)
@active_chat_handler.handle()
async def _(bot: Bot, event: Event):
    if isinstance(event, GroupMessageEvent): 
        await handlers.handle_active_chat_check(bot, event)