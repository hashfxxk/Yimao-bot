# yimao_plugin/__init__.py
import asyncio
import base64
import httpx
import logging
import datetime
import json
from typing import Optional, List, Dict, Any

from nonebot import get_driver, on_command, on_message
from nonebot.rule import to_me
from nonebot.matcher import Matcher
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.adapters.onebot.v11 import Bot, Event, Message, GroupMessageEvent

from . import data_store, handlers, utils, config, llm_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("GeminiPlugin")
driver = get_driver()

# --- 健壮的纯文本提取辅助函数 ---
def _extract_text_from_raw_message(raw_msg: Any) -> str:
    if not raw_msg: return ""
    if isinstance(raw_msg, str): return raw_msg
    
    message_parts = raw_msg
    if isinstance(message_parts, dict):
        message_parts = [message_parts]
        
    if not isinstance(message_parts, list):
        return ""
        
    text_segments = []
    for segment in message_parts:
        if isinstance(segment, dict) and segment.get("type") == "text":
            text_segments.append(segment.get("data", {}).get("text", ""))
            
    return "".join(text_segments).strip()


# --- 生命周期钩子 ---
@driver.on_startup
async def on_startup():
    logger.info("正在加载用户记忆...")
    data_store.load_memory_from_file()
    logger.info("正在加载群组长期记忆摘要...")
    data_store.load_group_summaries_from_file()
    logger.info("正在加载猜病游戏历史...") 
    data_store.load_challenge_histories_from_file() 
    logger.info("正在加载猜病游戏排行榜...") 
    data_store.load_challenge_leaderboard_from_file() 
    logger.info("一猫AI插件已加载并准备就绪。")

@driver.on_shutdown
async def on_shutdown():
    logger.info("正在保存用户记忆...")
    data_store.save_memory_to_file()
    logger.info("正在保存群组长期记忆摘要...")
    data_store.save_group_summaries_to_file()
    logger.info("正在保存猜病游戏历史...") 
    data_store.save_challenge_histories_to_file() 
    logger.info("正在保存猜病游戏排行榜...") 
    data_store.save_challenge_leaderboard_to_file() 
    logger.info("用户记忆、群组摘要、游戏历史和排行榜已保存。") 


# --- 历史图片摘要迁移命令 ---
image_migrator = on_command("migrateimages", aliases={"迁移历史图片"}, permission=SUPERUSER, priority=5, block=True)
@image_migrator.handle()
async def handle_image_migration(matcher: Matcher):
    await matcher.send("正在开始扫描历史数据，查找需要摘要的旧图片... 这个过程可能会很长。")
    all_user_memory = data_store._user_memory_data
    tasks_to_run = []
    
    for session_id, user_memory in all_user_memory.items():
        for mode in ["normal", "slash"]:
            mode_memory = getattr(user_memory, mode)
            for slot in mode_memory.slots:
                if slot.is_empty: continue
                for record in slot.history:
                    content = record.get("content")
                    if isinstance(content, list):
                        for item in content:
                            if item.get("type") == "image_url" and "summary" not in item:
                                image_url = item.get("image_url", {}).get("url", "")
                                if image_url.startswith("data:image/jpeg;base64,"):
                                    tasks_to_run.append((item, image_url.split(",")[1]))

    if not tasks_to_run:
        await matcher.finish("扫描完成！没有找到需要迁移的历史图片。")
        return

    total_tasks = len(tasks_to_run)
    await matcher.send(f"扫描完成！共找到 {total_tasks} 张需要生成摘要的图片。开始后台迁移，将每隔30秒处理一张，请耐心等待...")

    async def migration_worker():
        processed_count = 0; api_rate_limit_delay = 30
        for image_item, b64_data in tasks_to_run:
            try:
                logger.info(f"正在迁移第 {processed_count + 1}/{total_tasks} 张历史图片...")
                summary = await llm_client.summarize_image_content(b64_data, model_to_use=config.SLASH_COMMAND_MODEL_NAME)
                image_item["summary"] = summary
                processed_count += 1
                logger.info(f"迁移成功 ({processed_count}/{total_tasks})。")
                if processed_count % 5 == 0:
                    data_store.save_memory_to_file()
                    logger.info("迁移进度已保存。")
                    await matcher.send(f"迁移进度：已完成 {processed_count}/{total_tasks}...")
                await asyncio.sleep(api_rate_limit_delay)
            except Exception as e:
                logger.error(f"迁移一张图片时发生错误: {e}", exc_info=True)
                await matcher.send(f"处理第 {processed_count + 1} 张图片时出错，跳过此张。错误: {e}")
        data_store.save_memory_to_file()
        logger.info("全部历史图片迁移任务完成！")
        await matcher.send(f"🎉 全部 {total_tasks} 张历史图片已成功迁移并生成摘要！")

    asyncio.create_task(migration_worker())
    await matcher.finish("后台迁移任务已启动。您现在可以正常使用机器人了。")


# --- 其他指令注册 ---
jm_matcher = on_command("jm", aliases={"/jm"}, priority=5, block=True)
@jm_matcher.handle()
async def _(bot: Bot, event: Event, matcher: Matcher, args: Message = CommandArg()):
    album_id = args.extract_plain_text().strip()
    if not album_id.isdigit(): await matcher.finish("ID格式错误，请输入纯数字的ID。")
    try: await bot.call_api("set_msg_emoji_like", message_id=event.message_id, emoji_id='128164')
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
async def _(bot: Bot, event: Event, matcher: Matcher): await handlers.handle_random_jm(bot, event, matcher)

clear_group_mem_matcher = on_command("cleargroupmemory", aliases={"清空群记忆"}, permission=SUPERUSER, priority=5, block=True)
@clear_group_mem_matcher.handle()
async def _(bot: Bot, event: Event, matcher: Matcher):
    if not isinstance(event, GroupMessageEvent): await matcher.finish("该指令只能在群聊中使用。")
    group_id = str(event.group_id)
    try:
        cleared_count = data_store.clear_all_memory_for_group(group_id)
        if cleared_count > 0:
            data_store.save_memory_to_file()
            await matcher.send(f"操作成功：已清空本群 {cleared_count} 位用户的全部对话记忆。")
        else: await matcher.send("本群尚无任何用户的对话记忆，无需操作。")
    except Exception as e:
        logger.error(f"清空群组 {group_id} 记忆时发生错误: {e}", exc_info=True)
        await matcher.send(f"执行清空操作时发生内部错误，请查看后台日志。")

# --- 核心处理器：“总指挥官”模式 ---
at_me_handler = on_message(rule=to_me(), priority=10, block=True)
@at_me_handler.handle()
async def _(bot: Bot, matcher: Matcher, event: MessageEvent): 
    if str(event.user_id) in config.USER_BLACKLIST_IDS:
        logger.info(f"用户 {event.user_id} 在黑名单中，已忽略其@消息。")
        await matcher.finish()

    text = event.get_plaintext().strip()
    cmd_parts = text.split()
    cmd = cmd_parts[0].lower() if cmd_parts else ""

    if cmd.lstrip('/') == "restart":
        session_id = event.get_session_id()
        confirmed_mode = data_store.check_and_clear_restart_confirmation(session_id)
        if confirmed_mode:
            current_mode = "slash" if cmd.startswith('//') else "normal"
            if current_mode == confirmed_mode:
                result_message = data_store.clear_active_slot(session_id, confirmed_mode)
                data_store.save_memory_to_file()
                await matcher.finish(f"已确认。{result_message}")
            else: await matcher.finish("模式不匹配，已取消清空操作。")
        else:
            current_mode = "slash" if cmd.startswith('//') else "normal"
            data_store.set_restart_confirmation(session_id, current_mode)
            cmd_prefix = "//" if current_mode == "slash" else "/"
            await matcher.send(f"⚠️警告：您确定要清空当前【{current_mode.capitalize()}模式】的记忆吗？\n这个操作无法撤销！\n请在30秒内再次输入 @一猫 {cmd_prefix}restart 进行确认。")
        return

    if text.startswith("#"):
        await handlers.handle_challenge_chat(bot, matcher, event)
        return

    if cmd.lstrip('/') == "help": await matcher.finish(utils.get_help_menu())

    if cmd.lstrip('/') == "memory":
        args_text = text.split(maxsplit=1)[1] if len(cmd_parts) > 1 else ""
        await handlers.handle_memory_command(matcher, event, args=Message(args_text))
        return
    
    forward_id = next((seg.data["id"] for seg in event.message if seg.type == "forward"), None)
    if forward_id:
        await handle_forwarded_message(bot, matcher, event, forward_id)
        return

    if event.reply:
        await handle_reply_message(bot, matcher, event)
        return

    await handle_direct_at_message(bot, matcher, event)


# --- 辅助函数 ---
async def build_multimodal_content(event: MessageEvent) -> List[Dict[str, Any]]:
    content_list, text_parts = [], []
    for seg in event.message:
        if seg.type == 'text': text_parts.append(seg.data.get('text', ''))
        elif seg.type == 'image':
            img_url = seg.data.get('url')
            if img_url:
                try:
                    async with httpx.AsyncClient() as c:
                        resp = await c.get(img_url, timeout=60.0)
                        resp.raise_for_status()
                        img_b64 = base64.b64encode(resp.content).decode()
                        content_list.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}})
                except Exception as e:
                    logger.error(f"下载图片失败: {img_url}, error: {e}")
                    text_parts.append("[图片下载失败]")
    full_text = "".join(text_parts).strip()
    if full_text: content_list.insert(0, {"type": "text", "text": full_text})
    return content_list

async def handle_forwarded_message(bot: Bot, matcher: Matcher, event: MessageEvent, forward_id: str):
    logger.info(f"检测到合并转发消息，ID: {forward_id}，正在解析...")
    try:
        forwarded_messages = await bot.get_forward_msg(id=forward_id)
        if not forwarded_messages: desc = "[一段已无法打开的空聊天记录]"
        else:
            script = [
                f"{m['sender'].get('card') or m['sender'].get('nickname', '未知')}: "
                f"{_extract_text_from_raw_message(m.get('content')) or '[非文本消息]'}"
                for m in forwarded_messages
            ]
            desc = f"[一段聊天记录，内容如下：\n---\n{'\n'.join(script)}\n---]"
        
        user_question = event.get_plaintext().strip()
        prompt = f"请基于以下聊天记录，回答用户的问题。\n\n【聊天记录】\n{desc}\n\n【需要你回答的用户的问题】\n{user_question}"
        await handlers.handle_chat_session(bot, matcher, event, prompt)
    except Exception as e:
        logger.error(f"解析合并转发消息时出错: {e}", exc_info=True)
        await matcher.send("喵呜~ 我打不开这个聊天记录盒子...")


# 【最终修复】恢复原始的、功能正确的 handle_reply_message 逻辑
async def handle_reply_message(bot: Bot, matcher: Matcher, event: MessageEvent):
    try:
        replied_msg_info = await bot.get_msg(message_id=event.reply.message_id)
        
        # 区分是回复机器人还是回复他人
        if replied_msg_info.get('sender', {}).get('user_id') == int(bot.self_id):
            await handle_reply_to_bot(bot, matcher, event, replied_msg_info)
        else:
            await handle_reply_to_other(bot, matcher, event, replied_msg_info)
            
    except Exception as e:
        logger.error(f"处理引用回复时发生意外错误: {e}", exc_info=True)
        await matcher.send("喵呜~ 分析这段对话时我的大脑宕机了...")

async def handle_reply_to_bot(bot: Bot, matcher: Matcher, event: MessageEvent, replied_msg_info: dict):
    logger.info("处理对机器人消息的回复...")
    # 使用安全的方式提取被回复的机器人消息内容
    bot_content_text = _extract_text_from_raw_message(replied_msg_info.get('message'))
    if not bot_content_text:
        # 如果机器人发的是图片等，给一个通用描述
        bot_content_text = "[机器人之前发送的一条非文本消息]"

    user_question = event.get_plaintext().strip()
    prompt = f"这是关于你的一条历史发言的问题。请根据上下文回答用户。\n\n【机器人当时的发言】\n{bot_content_text}\n\n【用户现在的问题】\n{user_question}"
    
    # 组合成多模态内容发送
    final_content = await build_multimodal_content(event)
    text_part = next((p for p in final_content if p.get('type') == 'text'), None)
    if text_part:
        text_part['text'] = prompt
    else:
        final_content.insert(0, {'type': 'text', 'text': prompt})
        
    await handlers.handle_chat_session(bot, matcher, event, final_content)


async def handle_reply_to_other(bot: Bot, matcher: Matcher, event: MessageEvent, replied_msg_info: dict):
    logger.info("处理对其他用户消息的回复...")
    user_question = event.get_plaintext().strip()
    
    raw_msg = replied_msg_info.get('message', '')
    if isinstance(raw_msg, dict): raw_msg = [raw_msg]
    if not isinstance(raw_msg, list): raw_msg = [] # 再次保护
    
    # 优先处理被回复消息中的图片
    img_url = next((seg.get('data', {}).get('url') for seg in raw_msg if seg.get('type') == 'image' and seg.get('data', {}).get('url')), None)
    
    if img_url:
        try:
            # 下载被回复的图片
            async with httpx.AsyncClient() as c:
                resp = await c.get(img_url, timeout=60.0)
                resp.raise_for_status()
                img_b64 = base64.b64encode(resp.content).decode()

            # 构建多模态内容，包含用户自己的问题和被回复的图片
            content_list = await build_multimodal_content(event)
            # 将被回复的图片数据添加到列表
            content_list.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
            })
            
            # 确保有文本部分来承载问题
            text_part = next((p for p in content_list if p.get('type') == 'text'), None)
            prompt_text = f"请结合分析这张被回复的图片，来回答用户的问题。\n\n用户的问题是：{user_question}"
            if text_part:
                # 如果用户回复时也带了文字，合并
                text_part['text'] = f"{prompt_text}\n用户的补充说明：{text_part['text']}"
            else:
                content_list.insert(0, {"type": "text", "text": prompt_text})

            await handlers.handle_chat_session(bot, matcher, event, content_list)
            return
        except Exception as e:
            logger.error(f"处理被回复的图片时出错: {e}", exc_info=True)
            await matcher.send("喵呜~ 我好像打不开被回复的这张图片...")
            return

    # 如果被回复的不是图片，则走纯文本上下文逻辑
    sender_info = replied_msg_info.get('sender', {})
    sender_id, sender_name = sender_info.get('user_id', '未知ID'), sender_info.get('card') or sender_info.get('nickname', '某人')
    replied_text = _extract_text_from_raw_message(raw_msg) or "[非文本消息]"

    prompt = f"""
    请根据上下文回答用户的问题。这是一个关于其他用户历史发言的提问。

    【历史发言情景】
    - 发言者ID: {sender_id}
    - 发言者昵称: {sender_name}
    - 发言内容: {replied_text}

    【用户现在的问题】
    {user_question}
    """
    await handlers.handle_chat_session(bot, matcher, event, prompt.strip())


async def handle_direct_at_message(bot: Bot, matcher: Matcher, event: MessageEvent):
    user_content_list = await build_multimodal_content(event)

    if not user_content_list or (len(user_content_list) == 1 and user_content_list[0]['type'] == 'text' and not user_content_list[0]['text']):
        await matcher.finish("喵呜？主人有什么事吗？")

    if len(user_content_list) == 1 and user_content_list[0]['type'] == 'text':
        await handlers.handle_chat_session(bot, matcher, event, user_content_list[0]['text'])
    else:
        await handlers.handle_chat_session(bot, matcher, event, user_content_list)


active_chat_handler = on_message(priority=99, block=False)
@active_chat_handler.handle()
async def _(bot: Bot, event: Event):
    if isinstance(event, GroupMessageEvent): 
        await handlers.handle_active_chat_check(bot, event)