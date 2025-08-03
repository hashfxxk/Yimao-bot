# yimao_plugin/handlers.py

import asyncio
import json
import logging
import os
import random
import shutil
import re
import httpx
import datetime
import time
from urllib.parse import urlparse, urlunparse
from pathlib import Path
from typing import Literal

from jmcomic import create_option_by_file, download_album, JmcomicClient
from jmcomic.jm_exception import MissingAlbumPhotoException, PartialDownloadFailedException

from nonebot import on_message
from nonebot.rule import Rule
from nonebot.matcher import Matcher
from nonebot.typing import T_State
from nonebot.params import CommandArg
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.adapters.onebot.v11 import Bot, Event, Message, GroupMessageEvent, MessageSegment

from . import config, data_store, llm_client, tools, utils

logger = logging.getLogger("GeminiPlugin.handlers")

DownloadResult = Literal["ok", "not_found", "error"]


async def run_jm_download_task(bot: Bot, event: Event, album_id: str) -> DownloadResult:
    option_path = config.PROJECT_ROOT_DIR / "jm_option.yml"
    if not option_path.exists():
        logger.error("致命错误：JmComic配置文件 `jm_option.yml` 不存在！")
        return "error"
    base_dir = config.PROJECT_ROOT_DIR / "data" / "jmcomic"
    try:
        import yaml
        with open(option_path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)
            base_dir_from_cfg = cfg.get('dir_rule', {}).get('base_dir', str(base_dir))
            base_dir = Path(os.path.expanduser(base_dir_from_cfg))
    except Exception: pass
    final_pdf_path = base_dir / f"jm_{album_id}_0.pdf"
    temp_photo_dir = base_dir / album_id
    if final_pdf_path.exists():
        try: os.remove(final_pdf_path)
        except OSError as e: logger.error(f"清理旧PDF失败: {e}")
    if temp_photo_dir.exists():
        shutil.rmtree(temp_photo_dir, ignore_errors=True)
    final_photo_dir = temp_photo_dir
    try:
        option = create_option_by_file(str(option_path))
        client: JmcomicClient = option.build_jm_client()
        logger.info(f"开始使用安全模式下载禁漫 {album_id}...")
        await asyncio.to_thread(download_album, album_id, option)
        if not final_pdf_path.exists():
            if temp_photo_dir.exists(): shutil.rmtree(temp_photo_dir, ignore_errors=True)
            raise MissingAlbumPhotoException(f"PDF文件 {final_pdf_path} 未生成")
        try:
            album_detail = await asyncio.to_thread(client.get_album_detail, album_id)
            title = album_detail.title
            if title:
                invalid_chars = r'[\\/:*?"<>|]'
                safe_title = re.sub(invalid_chars, '_', title).strip()
                if safe_title:
                    new_path = base_dir / safe_title
                    if not new_path.exists() and temp_photo_dir.exists():
                        os.rename(temp_photo_dir, new_path)
                        final_photo_dir = new_path
        except Exception as rename_e:
            logger.error(f"获取标题或重命名文件夹时出错: {rename_e}")
        logger.info(f"开始上传文件 {final_pdf_path.name}...")
        api_to_call = "upload_group_file" if isinstance(event, GroupMessageEvent) else "upload_private_file"
        absolute_file_path = str(final_pdf_path.resolve())
        params = {"file": absolute_file_path, "name": final_pdf_path.name}
        if isinstance(event, GroupMessageEvent): params["group_id"] = event.group_id
        else: params["user_id"] = event.user_id
        await bot.call_api(api_to_call, **params, timeout=1800)
        logger.info(f"文件 {final_pdf_path.name} 上传成功。")
        try: 
            await bot.call_api("unset_msg_emoji_like", message_id=event.message_id, emoji_id='128164')
            await bot.call_api("set_msg_emoji_like", message_id=event.message_id, emoji_id='10024')
        except: pass
        return "ok"
    except (MissingAlbumPhotoException, PartialDownloadFailedException):
        logger.warning(f"ID {album_id} 下载失败")
        return "not_found"
    except Exception as e:
        logger.error(f"处理禁漫 {album_id} 时发生未知错误: {e}", exc_info=True)
        await bot.send(event, f"处理禁漫 {album_id} 时发生未知错误: {e}")
        try:
            await bot.call_api("unset_msg_emoji_like", message_id=event.message_id, emoji_id='128164')
            await bot.call_api("set_msg_emoji_like", message_id=event.message_id, emoji_id='10060')
        except: pass
        return "error"
    finally:
        if final_photo_dir and final_photo_dir.exists():
            shutil.rmtree(final_photo_dir, ignore_errors=True)
        elif temp_photo_dir.exists():
            shutil.rmtree(temp_photo_dir, ignore_errors=True)
        if final_pdf_path.exists():
            try: os.remove(final_pdf_path)
            except Exception as e: logger.error(f"清理最终PDF文件时出错: {e}")


async def handle_random_jm(bot: Bot, event: Event, matcher: Matcher):
    max_retries = 10
    try:
        await bot.call_api("set_msg_emoji_like", message_id=event.message_id, emoji_id='128164')
    except Exception as e:
        logger.warning(f"为随机JM设置初始Emoji时失败: {e}")
    for i in range(max_retries):
        random_id = str(random.randint(1, 1500000))
        logger.info(f"随机JM尝试 #{i + 1}: 正在尝试ID {random_id}...")
        result: DownloadResult = await run_jm_download_task(bot, event, random_id)
        if result == "ok" or result == "error":
            logger.info(f"随机JM任务结束，状态: {result}")
            return
        elif result == "not_found":
            logger.info(f"ID {random_id} 未找到，将在1秒后重试...")
            await asyncio.sleep(1)
    logger.error(f"在尝试了 {max_retries} 次后，仍未找到有效的随机JM本子。")
    await matcher.send(f"喵呜~ 找了 {max_retries} 次都没找到存在的本子，今天运气不太好呢，要不你再试一次？")
    try:
        await bot.call_api("unset_msg_emoji_like", message_id=event.message_id, emoji_id='128164')
        await bot.call_api("set_msg_emoji_like", message_id=event.message_id, emoji_id='10060')
    except: pass

async def handle_chat_session(bot: Bot, matcher: Matcher, event: Event, user_message_payload: dict):
    session_id = event.get_session_id()
    if isinstance(event, GroupMessageEvent):
        try:
            await bot.call_api("set_msg_emoji_like", message_id=event.message_id, emoji_id='128164')
        except: pass
    
    # 【最终修正】不再有摘要，content就是一切
    content = user_message_payload["content"]

    prompt_text = ""
    if isinstance(content, str):
        prompt_text = content
    elif isinstance(content, list):
        for item in content:
            if item.get('type') == 'text':
                prompt_text = item['text']
                break

    is_slash_mode = prompt_text.lstrip().startswith('/')
    mode = "slash" if is_slash_mode else "normal"

    data_store.update_slot_summary_if_needed(session_id, mode, prompt_text)
    
    history = data_store.get_active_history(session_id, mode)
    
    now_ts_str = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ")
    
    # 【最终修正】构造忠实、完整的历史记录
    history_record_for_user = {"role": "user", "message_id": event.message_id}
    
    if isinstance(content, str):
         history_record_for_user['content'] = content if is_slash_mode else now_ts_str + content
    else: # is list
        history_content_copy = [dict(item) for item in content]
        for item in history_content_copy:
            if item.get('type') == 'text':
                item['text'] = item['text'] if is_slash_mode else now_ts_str + item['text']
                break
        history_record_for_user['content'] = history_content_copy
        
    history.append(history_record_for_user)
    
    messages_for_api = list(history)

    if mode == "slash":
        model, system_prompt, use_function_calling = config.SLASH_COMMAND_MODEL_NAME, "", False
        if len(history) == 1:
            first_turn_payload = messages_for_api[0]
            original_content = ""
            if isinstance(first_turn_payload.get('content'), str):
                original_content = first_turn_payload['content'].lstrip(now_ts_str)
                first_turn_payload['content'] = f"{config.SLASH_COMMAND_SYSTEM_PROMPT}\n\n---\n\n{original_content.lstrip('/')}"
            elif isinstance(first_turn_payload.get('content'), list):
                 for item in first_turn_payload['content']:
                     if item.get('type') == 'text':
                         original_content = item['text'].lstrip(now_ts_str)
                         item['text'] = f"{config.SLASH_COMMAND_SYSTEM_PROMPT}\n\n---\n\n{original_content.lstrip('/')}"
                         break
    else: 
        model, use_function_calling = config.DEFAULT_MODEL_NAME, True
        
        # 检查是否为群聊且在特殊配置列表中
        if isinstance(event, GroupMessageEvent) and str(event.group_id) in config.EMOTIONLESS_PROMPT_GROUP_IDS:
            system_prompt = config.EMOTIONLESS_SYSTEM_PROMPT
            logger.info(f"群组 {event.group_id} 在特殊配置中，使用无情感Prompt。")
        else:
            system_prompt = config.DEFAULT_SYSTEM_PROMPT_TEMPLATE        
    logger.info(f"会话 {session_id} (模式: {mode}) 收到请求。")
    try:
        max_turns = 5
        for _ in range(max_turns):
            api_response = await llm_client.call_gemini_api(messages_for_api, system_prompt, model, use_function_calling)
            if "error" in api_response:
                error_msg_from_api = api_response["error"].get("message", "发生未知错误")
                await matcher.send(f"喵呜~ API出错了: {error_msg_from_api}")
                if history: history.pop()
                break
            
            response_message = api_response["choices"][0]["message"]
            
            if response_message.get("tool_calls"):
                logger.info("模型请求调用工具...")
                assistant_message = {"role": "assistant", "tool_calls": response_message["tool_calls"]}
                if response_message.get("content"):
                    assistant_message["content"] = response_message["content"]
                
                messages_for_api.append(assistant_message)
                history.append(assistant_message) 

                for tool_call in response_message["tool_calls"]:
                    function_name = tool_call["function"]["name"]
                    function_args = json.loads(tool_call["function"].get("arguments", "{}"))
                    if function_name in tools.available_tools:
                        function_to_call = tools.available_tools[function_name]
                        tool_output = await function_to_call(**function_args) if asyncio.iscoroutinefunction(function_to_call) else function_to_call(**function_args)
                        tool_response = {"tool_call_id": tool_call["id"], "role": "tool", "name": function_name, "content": tool_output}
                        messages_for_api.append(tool_response)
                        history.append(tool_response)
                    else:
                        tool_error_response = {"tool_call_id": tool_call["id"], "role": "tool", "name": function_name, "content": f"错误: 函数 '{function_name}' 未定义。"}
                        messages_for_api.append(tool_error_response)
                        history.append(tool_error_response)
                continue
            else:
                response_content = response_message.get("content", "")
                if isinstance(event, GroupMessageEvent) and response_content:
                    history_for_active_chat = data_store.get_group_history(str(event.group_id))
                    bot_name = "Loki" if mode == "slash" else (await bot.get_login_info())['nickname'] or "一猫"
                    # 这里我们只记录干净的、最终的回复内容
                    structured_message = {
                        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "user_id": bot.self_id,
                        "user_name": bot_name,
                        "content": response_content,
                        "is_bot": True
                    }
                    history_for_active_chat.append(structured_message)
                    logger.debug(f"[回写] 已记录机器人聊天回复到群({event.group_id})历史。")
                assistant_timestamped_content = response_content if is_slash_mode else datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ") + response_content
                assistant_message_payload = {"role": "assistant", "content": assistant_timestamped_content}
                sent_msg_receipt = None

                if len(response_content) > config.FORWARD_TRIGGER_THRESHOLD:
                    bot_name = "Loki" if mode == "slash" else "一猫"
                    sent_msg_receipt = await utils.send_long_message_as_forward(bot, event, response_content, bot_name)
                elif response_content:
                    sent_msg_receipt = await matcher.send(Message(response_content))
                else:
                    logger.warning(f"从API收到了空的响应内容: {api_response}")
                    await matcher.send("喵~ 我好像没什么好说的...")

                if sent_msg_receipt and 'message_id' in sent_msg_receipt:
                    assistant_message_payload['message_id'] = int(sent_msg_receipt['message_id'])
                    assistant_message_payload['response_to_id'] = event.message_id
                
                history.append(assistant_message_payload)
                break
        else:
            await matcher.send("喵呜~ 我思考得太久了...")
        
        if "error" not in locals().get("api_response", {}):
            data_store.save_memory_to_file()

        if isinstance(event, GroupMessageEvent):
            try:
                await bot.call_api("unset_msg_emoji_like", message_id=event.message_id, emoji_id='128164')
                await bot.call_api("set_msg_emoji_like", message_id=event.message_id, emoji_id='10024')
            except: pass
            
    except Exception as e:
        if history and history[-1] is history_record_for_user:
            history.pop()
        logger.error(f"处理聊天时出错: {e}", exc_info=True)
        await matcher.send("喵呜~ 我的大脑好像被毛线缠住啦！请检查后台日志。")


async def handle_memory_command(matcher: Matcher, event: Event, args: Message = CommandArg()):
    session_id = event.get_session_id()
    command = event.get_plaintext().strip()
    mode = "slash" if command.startswith('//') else "normal"
    arg_str = args.extract_plain_text().strip()
    if not arg_str:
        summary_text = data_store.get_memory_summary_list(session_id, mode)
        await matcher.send(summary_text)
    else:
        try:
            slot_num = int(arg_str)
            success, message = data_store.set_active_slot(session_id, mode, slot_num - 1)
            await matcher.send(message)
            if success:
                data_store.save_memory_to_file()
        except ValueError:
            await matcher.send("无效的指令。请输入数字编号。")


async def handle_challenge_chat(bot: Bot, matcher: Matcher, event: Event):
    # 【新增】用户黑名单检查
    if str(event.user_id) in config.USER_BLACKLIST_IDS:
        logger.info(f"用户 {event.user_id} 在黑名单中，已忽略其猜病挑战指令。")
        await matcher.finish()
    # --- 1. 初始化与上下文获取 ---
    session_id = event.get_session_id()
    user_id_str = str(event.user_id)
    user_text = event.get_plaintext().lstrip('#').strip()
    history = data_store.get_or_create_challenge_history(session_id)
    player_name = event.sender.card or event.sender.nickname or user_id_str
    shopkeeper_name = f"{player_name}的神秘店长"
    group_id_str = str(event.group_id) if isinstance(event, GroupMessageEvent) else None

    # --- 指令处理 ---
    # 【新增】排行榜指令
    if user_text.lower() in ["rank", "排行榜", "leaderboard"]:
        if not group_id_str:
            await matcher.send("排行榜功能仅在群聊中可用哦。")
            return
        leaderboard = data_store.get_leaderboard(group_id_str)
        if not leaderboard:
            await matcher.send("本群还没有人成功攻略猫娘，快来成为第一人吧！")
            return
            
        rank_list = ["🏆 本群猫娘速通排行榜 🏆"]
        for i, record in enumerate(leaderboard):
            # 为了保护隐私，此处默认显示昵称，括号内是QQ号用于区分重名
            rank_list.append(f"第 {i+1} 名: {record.get('user_name', '未知玩家')} ({record.get('user_id', '未知ID')})\n所用字数: {record.get('char_count', 'N/A')}")
        
        await matcher.send("\n\n".join(rank_list))
        return

    # 【修改】历史记录指令保持不变
    if user_text.lower() in ["history", "历史"]:
        if not history:
            await matcher.send("你和猫娘们还没有任何对话记录哦，快去开启故事吧！")
            return

        history_text_parts = []
        for record in history:
            role = record.get("role")
            content = record.get("content", "")
            if role == "user":
                history_text_parts.append(f"你：{content}")
            elif role == "assistant":
                history_text_parts.append(f"旁白/猫娘：\n{content}")
        
        full_history_text = "\n\n---\n\n".join(history_text_parts)
        await utils.send_long_message_as_forward(
            bot, event, full_history_text, f"{player_name}的游戏记录"
        )
        return
        
    # --- 游戏逻辑 ---
    if isinstance(event, GroupMessageEvent):
        try: await bot.call_api("set_msg_emoji_like", message_id=event.message_id, emoji_id='128164')
        except: pass

    # 【修改】游戏重置时，也要重置字数计数器
    is_reset_command = user_text.lower() in ["新游戏", "重置", "restart"]
    is_new_game = is_reset_command or not history
    messages_for_api = []
    if is_new_game:
        history.clear()
        data_store.reset_challenge_char_count(session_id) # 重置字数
        if is_reset_command: await matcher.send("...记忆已重置，咖啡馆的故事重新开始了。")
        messages_for_api = []
    else:
        # 【修改】将用户输入的字数计入
        data_store.increment_challenge_char_count(session_id, user_text)
        user_message_payload = {"role": "user", "content": user_text}
        history.append(user_message_payload)
        messages_for_api = list(history)

    logger.info(f"会话 {session_id} (店长: {shopkeeper_name}) - 新游戏: {is_new_game} | 用户输入: '{user_text}'")

    try:
        api_response = await llm_client.call_gemini_api(
            messages=messages_for_api,
            system_prompt_content=config.CHALLENGE_SYSTEM_PROMPT,
            model_to_use=config.CHALLENGE_MODEL_NAME,
            use_tools=False
        )

        if "error" in api_response:
            raise RuntimeError(api_response.get("error", {}).get("message", "发生未知API错误"))

        full_response_content = api_response["choices"][0]["message"].get("content", "")
        
        game_state_jsons = re.findall(r"<GAME_STATE>(.*?)</GAME_STATE>", full_response_content, re.DOTALL)
        narrative_content = re.sub(r"<GAME_STATE>.*?</GAME_STATE>", "", full_response_content, flags=re.DOTALL).strip()
        
        feedback_messages = []
        has_victory = False # 【新增】标记本回合是否达成了攻略
        for json_str in game_state_jsons:
            try:
                game_data = json.loads(json_str)
                status, char = game_data.get("status"), game_data.get("character", "她")
                feedback = ""
                if status == "trust_up": feedback = f"（{char}对你的信赖似乎上升了。{game_data.get('reason', '')}）"
                elif status == "trust_down": feedback = f"（{char}对你的信赖似乎下降了。{game_data.get('reason', '')}）"
                elif status == "victory":
                    feedback = f"（🎉🎉🎉 恭喜！你与{char}的羁绊达成了！现在可以和她进行更深入的日常互动了~）"
                    has_victory = True # 标记为胜利
                if feedback: feedback_messages.append(feedback)
            except json.JSONDecodeError: logger.error(f"解析游戏状态JSON失败: {json_str}")
        
        # --- 整合与发送 ---
        feedback_block = "\n".join(feedback_messages)
        
        # 【修改】在反馈块后追加字数统计
        current_char_count = data_store.get_challenge_char_count(session_id)
        char_count_feedback = f"(本局游戏您已输入 {current_char_count} 字)"
        
        final_content_parts = []
        if narrative_content: final_content_parts.append(narrative_content)
        if feedback_block: final_content_parts.append(feedback_block)
        
        # 总是添加字数统计反馈
        final_content_parts.append(char_count_feedback)
        
        # 使用两个换行符分隔，视觉效果更好
        final_content = "\n\n".join(part for part in final_content_parts if part).strip()

        if final_content:
            if narrative_content:
                history.append({"role": "assistant", "content": narrative_content})

            if len(final_content) > config.FORWARD_TRIGGER_THRESHOLD:
                await utils.send_long_message_as_forward(bot, event, final_content, shopkeeper_name)
            else:
                await matcher.send(Message(final_content))
        elif not is_new_game:
            await matcher.send("...她似乎没什么反应。")

        # 【新增】处理胜利和排行榜逻辑
        if has_victory and group_id_str:
            # 检查此玩家是否已在本局游戏中上过榜，防止重复记录
            # 一个简单的检查方法：如果历史记录中已经有超过一个victory，说明不是第一次
            # 注意：这里的检查是在本次回复的内容加入history之前，所以判断数量为1
            victory_count_in_history = sum(1 for msg in history if msg.get('role') == 'assistant' and "恭喜！你与" in msg.get("content", ""))
            
            if victory_count_in_history == 0: # 如果历史中还没有胜利记录，说明这是第一次
                data_store.update_leaderboard(group_id_str, user_id_str, player_name, current_char_count)
                await matcher.send(f"🎉恭喜 {player_name} 首次攻略成功！您的成绩已记录到本群速通排行榜！\n使用 `#排行榜` 查看。")

        if isinstance(event, GroupMessageEvent):
            try:
                await bot.call_api("unset_msg_emoji_like", message_id=event.message_id, emoji_id='128164')
                await bot.call_api("set_msg_emoji_like", message_id=event.message_id, emoji_id='10024')
            except: pass
            
    except Exception as e:
        if not is_new_game and history and history[-1]['role'] == 'user':
            history.pop()
        logger.error(f"处理猫娘咖啡馆时发生错误: {e}", exc_info=True)
        await matcher.send(f"...[叙事模块故障: {e}]...")
        
        if isinstance(event, GroupMessageEvent):
            try:
                await bot.call_api("unset_msg_emoji_like", message_id=event.message_id, emoji_id='128164')
                await bot.call_api("set_msg_emoji_like", message_id=event.message_id, emoji_id='10060')
            except: pass




            

async def update_summary_for_group(group_id: str, history_list: list):
    logger.info(f"正在为群组 {group_id} 生成摘要...")
    old_summary = data_store.get_group_summary(group_id)
    
    # 将结构化的历史记录转换成对AI友好的字符串格式
    def format_history_for_summary(hist_list):
        formatted = []
        for msg in hist_list:
            if isinstance(msg, dict): # 兼容新格式
                formatted.append(f"{msg['timestamp']} [用户ID:{msg['user_id']} (昵称:{msg['user_name']})]: {msg['content']}")
            else: # 兼容可能存在的旧格式字符串
                formatted.append(str(msg))
        return "\n".join(formatted)

    history_str = format_history_for_summary(history_list)
    
    summary_prompt = f"""
    你是一个社群观察家，你的任务是阅读一段群聊记录和旧的群聊摘要，然后生成一个新的、更完善的摘要。
    【旧摘要】
    {old_summary}
    【近期聊天记录】
    {history_str}
    【你的任务】
    请根据以上信息，提炼并更新群聊摘要。摘要应包含：
    1.  群聊的核心主题或氛围。
    2.  识别出几位最活跃的群友及其典型特征（请使用他们的昵称，但要基于用户ID来区分不同的人）。
    3.  记录一些群内最近发生的、可能会在未来被再次提到的大事或流行的梗。
    请以简洁、客观的语言输出新的摘要。
    """
    try:
        api_response = await llm_client.call_gemini_api(
            messages=[{"role": "user", "content": summary_prompt}],
            system_prompt_content="",
            model_to_use=config.DEFAULT_MODEL_NAME,
            use_tools=False
        )
        new_summary = api_response["choices"][0]["message"].get("content", "").strip()
        if new_summary:
            data_store.update_group_summary(group_id, new_summary)
    except Exception as e:
        logger.error(f"为群组 {group_id} 生成摘要时出错: {e}")
    


def is_bilibili_card() -> Rule:
    """
    它会检查消息段中是否包含json类型，并且json内容中包含B站小程序的固定AppID '1109937557'。
    """
    async def _checker(event: GroupMessageEvent) -> bool:
        if not isinstance(event, GroupMessageEvent):
            return False
        
        for seg in event.message:
            if seg.type == "json":
                try:
                    json_data = json.loads(seg.data.get("data", "{}"))
                    
                    if json_data.get("meta", {}).get("detail_1", {}).get("appid") == "1109937557":
                        return True
                except (json.JSONDecodeError, AttributeError):
                    continue
        return False
    return Rule(_checker)

async def expand_b23_url(short_url: str) -> str:
    """
    访问短链接，返回长链接。
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            head_response = await client.head(short_url, timeout=10.0)
            long_url_with_params = str(head_response.url)
            
            parsed_url = urlparse(long_url_with_params)
            
            clean_url = urlunparse(parsed_url._replace(params='', query='', fragment=''))
            
            return clean_url
            
    except Exception as e:
        logger.error(f"展开或净化B站短链接 {short_url} 时出错: {e}")
        return short_url

bili_card_parser = on_message(rule=is_bilibili_card(), priority=20, block=False)

@bili_card_parser.handle()
async def handle_bili_card(bot: Bot, event: GroupMessageEvent, matcher: Matcher):
    for seg in event.message:
        if seg.type == "json":
            try:
                json_data = json.loads(seg.data.get("data", "{}"))
                short_url = json_data.get("meta", {}).get("detail_1", {}).get("qqdocurl")
                
                if short_url:
                    long_url = await expand_b23_url(short_url)
                    
                    reply_text = long_url
                    
                    reply_segment = MessageSegment.reply(id_=event.message_id)
                    text_segment = MessageSegment.text(reply_text)
                    message_to_send = Message([reply_segment, text_segment])
                    
                    logger.info(f"成功解析并展开B站链接: {short_url} -> {long_url}")
                    await matcher.send(message_to_send)
                    
                    # 【核心修改】在这里把机器人的发言写回历史记录
                    history = data_store.get_group_history(str(event.group_id))
                    bot_name = (await bot.get_login_info())['nickname'] or "一猫"
                    structured_message = {
                        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "user_id": bot.self_id,
                        "user_name": bot_name,
                        "content": reply_text,
                        "is_bot": True
                    }
                    history.append(structured_message)
                    logger.debug(f"[回写] 已记录B站解析回复到群({event.group_id})历史。")
                    
                    return

            except Exception as e:
                logger.error(f"解析B站小程序时出错: {e}", exc_info=True)

group_message_recorder = on_message(priority=1, block=False)

@group_message_recorder.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    """
    这个处理器拥有最高优先级，像一个忠实的书记官，
    在任何功能被触发之前，就将所有群聊消息记录到历史中。
    【已升级】现在能正确处理 @ 和 回复。
    """
    if str(event.user_id) in config.USER_BLACKLIST_IDS:
        return # 不记录黑名单用户的消息
    # 只处理群聊消息
    if not isinstance(event, GroupMessageEvent):
        return
        
    group_id = str(event.group_id)
    user_id = str(event.user_id)
    history = data_store.get_group_history(group_id)
    
    try:
        member_info = await bot.get_group_member_info(group_id=event.group_id, user_id=int(user_id))
        user_name = member_info.get('card') or member_info.get('nickname') or user_id
    except Exception:
        user_name = event.sender.nickname or user_id
        
    # 【核心修改】使用我们新的格式化函数来获取完整的消息内容
    message_text = await format_message_for_history(bot, event)
    
    structured_message = {
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": user_id,
        "user_name": user_name,
        "content": message_text,
        "is_bot": user_id == bot.self_id
    }
    history.append(structured_message)
    logger.debug(f"[记录员 V2] 已记录群({group_id})消息: {user_name}: {message_text[:50]}...")
    
    if user_id != bot.self_id:
        data_store.increment_active_chat_message_count(group_id)
        if data_store.increment_and_check_summary_trigger(group_id):
            asyncio.create_task(update_summary_for_group(group_id, list(history)))

async def format_message_for_history(bot: Bot, event: GroupMessageEvent) -> str:
    """
    将一条复杂的 GroupMessageEvent 转换成对AI友好的、包含上下文的单行文本。
    - 能够解析并描述 @某人。
    - 能够解析并描述 回复。
    - 能够描述图片、表情等非文本内容。
    """
    message = event.message
    full_text_parts = []

    # 1. 处理引用回复 (reply)
    if event.reply:
        try:
            replied_msg_info = await bot.get_msg(message_id=event.reply.message_id)
            replied_sender_info = replied_msg_info.get('sender', {})
            replied_user_id = replied_sender_info.get('user_id')
            replied_user_name = replied_sender_info.get('card') or replied_sender_info.get('nickname', f'用户{replied_user_id}')
            
            # 简化被回复消息的内容
            replied_content_raw = replied_msg_info.get('message', '')
            replied_content = Message(replied_content_raw).extract_plain_text().strip()
            if not replied_content:
                # 如果没文本，就给个通用描述
                replied_content = "[一条非文本消息]"
            
            # 构建回复部分的文本
            reply_prefix = f"回复({replied_user_name}: “{replied_content[:20]}...”) "
            full_text_parts.append(reply_prefix)

        except Exception as e:
            logger.warning(f"获取被回复消息({event.reply.message_id})失败: {e}, 无法在历史中构建引用上下文。")
            full_text_parts.append("[回复了一条消息] ")

    # 2. 遍历消息段，处理 @、文本和其他内容
    for seg in message:
        if seg.type == 'text':
            full_text_parts.append(seg.data.get('text', ''))
        elif seg.type == 'at':
            at_user_id = seg.data.get('qq')
            if at_user_id == 'all':
                full_text_parts.append('@全体成员 ')
            else:
                try:
                    # 尝试获取被@用户的群名片
                    user_info = await bot.get_group_member_info(group_id=event.group_id, user_id=int(at_user_id))
                    user_name = user_info.get('card') or user_info.get('nickname', f'用户{at_user_id}')
                    full_text_parts.append(f"@{user_name} ")
                except Exception:
                    full_text_parts.append(f"[@一位成员] ") # 获取失败时的兜底
        elif seg.type == 'image':
            full_text_parts.append('[图片]')
        elif seg.type == 'face':
            full_text_parts.append('[表情]')
        elif seg.type == 'record':
            full_text_parts.append('[语音]')
        elif seg.type == 'json':
            full_text_parts.append('[小程序/卡片]')
        # 忽略 reply 段，因为它已经在前面处理过了
        elif seg.type == 'reply':
            continue
        # 其他未处理类型
        else:
            full_text_parts.append(f"[{seg.type}]")
            
    final_text = "".join(full_text_parts).strip()
    # 如果处理完还是空的（例如，消息只包含一个reply段），提供一个保底描述
    return final_text if final_text else "[一条内容未知的消息]"

# 2. 主动聊天决策者 (注意：这个就是你 `__init__.py` 文件末尾的 `active_chat_handler` 所调用的函数)
# 我们把它放在这里，但让 `__init__.py` 来调用
async def handle_active_chat_check(bot: Bot, event: GroupMessageEvent):
    """这个处理器只在所有其他处理器都运行完毕后，才根据完整的历史记录进行决策。"""
    group_id = str(event.group_id)
    
    # 1. 基础条件检查：功能是否开启，是否在白名单内
    if not config.ACTIVE_CHAT_ENABLED or group_id not in config.ACTIVE_CHAT_WHITELIST:
        return
        
    # 2. 【新增】消息计数检查：群聊是否足够“热闹”
    current_count = data_store.get_active_chat_message_count(group_id)
    if current_count < config.ACTIVE_CHAT_MESSAGE_THRESHOLD:
        logger.debug(f"[主动聊天] 群({group_id}) 消息计数未达到阈值 ({current_count}/{config.ACTIVE_CHAT_MESSAGE_THRESHOLD})，跳过决策。")
        return
        
    # 3. 冷却时间检查：距离上次主动发言是否足够久
    if not data_store.check_and_set_cooldown(group_id):
        logger.debug(f"[主动聊天] 群({group_id}) 尚在冷却时间内，跳过决策。")
        return

    history = data_store.get_group_history(group_id)
    if not history: return


    group_summary = data_store.get_group_summary(group_id)

    def format_history_for_prompt(hist_list):
        formatted = []
        for msg in hist_list:
            if isinstance(msg, dict):
                formatted.append(f"[{msg['timestamp']}] [用户ID:{msg['user_id']} (昵称:{msg['user_name']})]: {msg['content']}")
            elif isinstance(msg, str):
                formatted.append(msg)
        return formatted

    history_for_prompt = format_history_for_prompt(list(history))
    if not history_for_prompt: return
    
    recent_history_prompt = history_for_prompt[:-1]
    new_message_prompt = history_for_prompt[-1]

    decision_payload = {
        "group_summary": group_summary,
        "recent_history": recent_history_prompt,
        "new_message": new_message_prompt
    }
    
    decision_messages = [{"role": "user", "content": json.dumps(decision_payload, ensure_ascii=False)}]
    system_prompt_with_time = config.ACTIVE_CHAT_DECISION_PROMPT.format(current_time=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    
    try:
        logger.info(f"[主动聊天] 群({group_id}) 正在进行决策...")
        api_response = await llm_client.call_gemini_api(
            messages=decision_messages,
            system_prompt_content=system_prompt_with_time,
            model_to_use=config.ACTIVE_CHAT_DECISION_MODEL,
            use_tools=False
        )
        if "error" in api_response:
            logger.error(f"[主动聊天] 决策API调用失败: {api_response['error']}")
            return

        response_content = api_response["choices"][0]["message"].get("content", "")
        if response_content.startswith("```json"):
            response_content = response_content.strip("```json").strip("```").strip()
        
        decision_data = json.loads(response_content)
        
        if decision_data.get("should_reply") is True:
            reply_text = decision_data.get("reply_content", "").strip()
            if reply_text:
                logger.info(f"[主动聊天] 决定回复群({group_id})，内容: {reply_text}")
                await bot.send(event, message=reply_text)
                
                # 【核心修改】成功发言后，立即重置消息计数器
                data_store.reset_active_chat_message_count(group_id)
                
                bot_name = (await bot.get_login_info())['nickname'] or "一猫"
                structured_message = {
                    "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "user_id": bot.self_id,
                    "user_name": bot_name,
                    "content": reply_text,
                    "is_bot": True
                }
                history.append(structured_message)
                logger.debug(f"[回写] 已记录主动聊天回复到群({group_id})历史。")
    except json.JSONDecodeError:
        logger.warning(f"[主动聊天] 解析决策JSON失败: {response_content}")
    except Exception as e:
        logger.error(f"[主动聊天] 处理过程中发生未知错误: {e}", exc_info=True)