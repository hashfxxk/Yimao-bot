# yimao_plugin/data_store.py
import json
import logging
import os
from collections import deque
from pathlib import Path
import time
from typing import Dict, List, Deque, Optional

from pydantic import BaseModel, Field

from . import config

logger = logging.getLogger("GeminiPlugin.datastore")

# --- Pydantic 数据模型定义 ---
# 使用 Pydantic 来确保数据结构的一致性和可预测性。
class MemorySlot(BaseModel):
    summary: str = "（空插槽）"
    history: List[Dict] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.history

class ConversationMode(BaseModel):
    active_slot_index: int = 0
    slots: List[MemorySlot] = Field(
        default_factory=lambda: [MemorySlot() for _ in range(config.MEMORY_SLOTS_PER_USER)]
    )

class UserMemory(BaseModel):
    normal: ConversationMode = Field(default_factory=ConversationMode)
    slash: ConversationMode = Field(default_factory=ConversationMode)

# --- 运行时数据存储 ---
# 这些是程序运行时在内存中的数据，关闭时会持久化。
_user_memory_data: Dict[str, UserMemory] = {}
_history_deques: Dict[str, Dict[str, Dict[int, deque]]] = {}
_challenge_histories: Dict[str, Deque[Dict]] = {}
_group_summaries: Dict[str, str] = {}
_group_message_counters: Dict[str, int] = {}
_group_chat_history: Dict[str, Deque[str]] = {}
_group_cooldown_timers: Dict[str, float] = {}
_group_active_chat_message_counts: Dict[str, int] = {}
# 缓存机器人自己发送的合并转发内容，避免在处理对自己的回复时无法获取上下文。
# Key: message_id (int), Value: content (str)
_forward_content_cache: Dict[int, str] = {}


#文件持久化

def _get_memory_path() -> Path:
    return Path(config.MEMORY_FILE_PATH)

def _get_group_summary_path() -> Path:
    return Path(config.MEMORY_FILE_PATH).parent / "yimao_group_summaries.json"

def load_memory_from_file():
    global _user_memory_data, _history_deques
    path = _get_memory_path()
    if not path.exists(): return
    try:
        data = json.loads(path.read_text("utf-8"))
        for session_id, user_data in data.items():
            user_mem = UserMemory.parse_obj(user_data)
            _user_memory_data[session_id] = user_mem
            _history_deques[session_id] = {"normal": {}, "slash": {}}
            for i, slot in enumerate(user_mem.normal.slots):
                _history_deques[session_id]["normal"][i] = deque(slot.history, maxlen=config.NORMAL_CHAT_MAX_LENGTH)
            for i, slot in enumerate(user_mem.slash.slots):
                _history_deques[session_id]["slash"][i] = deque(slot.history, maxlen=config.SLASH_CHAT_MAX_LENGTH)
        logger.info(f"成功从 {path} 加载了 {len(_user_memory_data)} 位用户的分层记忆。")
    except Exception as e:
        logger.error(f"加载记忆文件 {path} 失败: {e}。将创建备份并开始新的记忆。")
        if path.exists():
            backup_path = path.with_suffix(f".bak.{os.urandom(4).hex()}")
            os.rename(path, backup_path)
        _user_memory_data, _history_deques = {}, {}

def load_group_summaries_from_file():
    global _group_summaries
    path = _get_group_summary_path()
    if not path.exists():
        logger.info(f"群组摘要文件 {path} 不存在，将使用空摘要开始。")
        return
    try:
        data = json.loads(path.read_text("utf-8"))
        if isinstance(data, dict):
            _group_summaries = data
            logger.info(f"成功从 {path} 加载了 {len(_group_summaries)} 个群组的摘要。")
        else: raise TypeError("摘要文件格式不正确")
    except Exception as e:
        logger.error(f"加载群组摘要文件 {path} 失败: {e}。")
        if path.exists():
            backup_path = path.with_suffix(f".bak.{os.urandom(4).hex()}")
            os.rename(path, backup_path)
        _group_summaries = {}

def save_memory_to_file():
    path = _get_memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data_to_save = {}
    for session_id, user_mem in _user_memory_data.items():
        if session_id in _history_deques:
            if "normal" in _history_deques[session_id]:
                for i, slot in enumerate(user_mem.normal.slots):
                    if i in _history_deques[session_id]["normal"]:
                        slot.history = list(_history_deques[session_id]["normal"][i])
            if "slash" in _history_deques[session_id]:
                for i, slot in enumerate(user_mem.slash.slots):
                    if i in _history_deques[session_id]["slash"]:
                        slot.history = list(_history_deques[session_id]["slash"][i])
        data_to_save[session_id] = user_mem.dict()
    try: path.write_text(json.dumps(data_to_save, ensure_ascii=False, indent=2), "utf-8")
    except Exception as e: logger.error(f"保存记忆至 {path} 时出错: {e}", exc_info=True)

def save_group_summaries_to_file():
    path = _get_group_summary_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try: path.write_text(json.dumps(_group_summaries, ensure_ascii=False, indent=2), "utf-8")
    except Exception as e: logger.error(f"保存群组摘要至 {path} 时出错: {e}", exc_info=True)


def cache_forward_content(message_id: int, content: str):
    """缓存一条由机器人发送的合并转发消息的原始内容。"""
    if len(_forward_content_cache) > 500:
        _forward_content_cache.pop(next(iter(_forward_content_cache)))
    _forward_content_cache[message_id] = content
    logger.info(f"已缓存合并转发消息 {message_id} 的内容。")

def get_forward_content_from_cache(message_id: int) -> Optional[str]:
    """从缓存中获取合并转发的原始内容。"""
    content = _forward_content_cache.get(message_id)
    if content:
        logger.info(f"从缓存中命中合并转发消息 {message_id} 的内容。")
    return content

def get_group_history(group_id: str) -> Deque[str]:
    if group_id not in _group_chat_history: _group_chat_history[group_id] = deque(maxlen=config.GROUP_HISTORY_MAX_LENGTH)
    return _group_chat_history[group_id]

def check_and_set_cooldown(group_id: str) -> bool:
    now = time.time()
    last_speak_time = _group_cooldown_timers.get(group_id, 0)
    if now - last_speak_time > config.ACTIVE_CHAT_COOLDOWN:
        _group_cooldown_timers[group_id] = now
        return True
    return False

def get_or_create_challenge_history(session_id: str) -> Deque[Dict]:
    if session_id not in _challenge_histories: _challenge_histories[session_id] = deque(maxlen=config.CHALLENGE_CHAT_MAX_LENGTH)
    return _challenge_histories[session_id]

def clear_challenge_history(session_id: str) -> None:
    if session_id in _challenge_histories:
        _challenge_histories[session_id].clear()
        logger.info(f"已清空会话 {session_id} 的猜病挑战历史。")

def _get_or_create_user_memory(session_id: str) -> UserMemory:
    if session_id not in _user_memory_data:
        _user_memory_data[session_id] = UserMemory()
        _history_deques[session_id] = {"normal": {}, "slash": {}}
        for i in range(config.MEMORY_SLOTS_PER_USER):
            _history_deques[session_id]["normal"][i] = deque(maxlen=config.NORMAL_CHAT_MAX_LENGTH)
            _history_deques[session_id]["slash"][i] = deque(maxlen=config.SLASH_CHAT_MAX_LENGTH)
    return _user_memory_data[session_id]

def get_active_history(session_id: str, mode: str) -> deque:
    user_mem = _get_or_create_user_memory(session_id)
    mode_mem = user_mem.normal if mode == "normal" else user_mem.slash
    active_index = mode_mem.active_slot_index
    if active_index not in _history_deques[session_id][mode]:
         _history_deques[session_id][mode][active_index] = deque(maxlen=config.NORMAL_CHAT_MAX_LENGTH if mode == "normal" else config.SLASH_CHAT_MAX_LENGTH)
    return _history_deques[session_id][mode][active_index]

def get_active_chat_message_count(group_id: str) -> int:
    """获取指定群组的当前主动聊天消息计数。"""
    return _group_active_chat_message_counts.get(group_id, 0)

def increment_active_chat_message_count(group_id: str):
    """为指定群组的主动聊天消息计数器+1。"""
    count = _group_active_chat_message_counts.get(group_id, 0)
    _group_active_chat_message_counts[group_id] = count + 1
    logger.debug(f"群({group_id}) 主动聊天计数器增加到: {_group_active_chat_message_counts[group_id]}")

def reset_active_chat_message_count(group_id: str):
    """重置指定群组的主动聊天消息计数器为0。"""
    if group_id in _group_active_chat_message_counts:
        _group_active_chat_message_counts[group_id] = 0
        logger.info(f"群({group_id}) 主动聊天计数器已重置为0。")

def update_slot_summary_if_needed(session_id: str, mode: str, prompt: str):
    user_mem = _get_or_create_user_memory(session_id)
    mode_mem = user_mem.normal if mode == "normal" else user_mem.slash
    active_slot = mode_mem.slots[mode_mem.active_slot_index]
    if active_slot.is_empty:
        active_slot.summary = (prompt[:30] + '...') if len(prompt) > 30 else prompt

def get_memory_summary_list(session_id: str, mode: str) -> str:
    user_mem = _get_or_create_user_memory(session_id)
    mode_mem = user_mem.normal if mode == "normal" else user_mem.slash
    mode_name = "普通对话 (一猫)" if mode == "normal" else "Loki 对话"
    output = [f"当前记忆列表: {mode_name}"]
    for i, slot in enumerate(mode_mem.slots):
        prefix = " >" if i == mode_mem.active_slot_index else "  "
        output.append(f"{prefix} [{i + 1}] {slot.summary}")
    cmd_prefix = "/" if mode == "normal" else "//"
    output.extend([f"\n使用 {cmd_prefix}memory n 切换记忆", f"使用 {cmd_prefix}restart 清空当前记忆"])
    return "\n".join(output)

def set_active_slot(session_id: str, mode: str, slot_index: int) -> tuple[bool, str]:
    if not (0 <= slot_index < config.MEMORY_SLOTS_PER_USER):
        return False, f"无效的插槽编号。请输入 1-{config.MEMORY_SLOTS_PER_USER} 之间的数字。"
    user_mem = _get_or_create_user_memory(session_id)
    mode_mem = user_mem.normal if mode == "normal" else user_mem.slash
    mode_mem.active_slot_index = slot_index
    summary = mode_mem.slots[slot_index].summary
    return True, f"已切换到记忆插槽 [{slot_index + 1}]。\n摘要: {summary}"

def clear_active_slot(session_id: str, mode: str) -> str:
    user_mem = _get_or_create_user_memory(session_id)
    mode_mem = user_mem.normal if mode == "normal" else user_mem.slash
    active_index = mode_mem.active_slot_index
    active_slot = mode_mem.slots[active_index]
    if active_index in _history_deques[session_id][mode]:
        _history_deques[session_id][mode][active_index].clear()
    active_slot.summary = "（空插槽）"
    active_slot.history = []
    return f"当前记忆插槽 [{active_index + 1}] 已清空。"

def get_group_summary(group_id: str) -> str:
    return _group_summaries.get(group_id, "（暂无关于本群的长期记忆）")

def update_group_summary(group_id: str, new_summary: str):
    _group_summaries[group_id] = new_summary
    logger.info(f"已更新群组 {group_id} 的摘要，正在保存到文件...")
    save_group_summaries_to_file()

def increment_and_check_summary_trigger(group_id: str) -> bool:
    global _group_message_counters
    count = _group_message_counters.get(group_id, 0) + 1
    if count >= config.GROUP_HISTORY_MAX_LENGTH:
        _group_message_counters[group_id] = 0
        return True
    _group_message_counters[group_id] = count
    return False

def find_user_question_id_by_bot_response_id(group_id: str, bot_message_id: int) -> Optional[int]:
    logger.info(f"在群组 {group_id} 的实时内存中查找响应 {bot_message_id} 的原始提问...")
    group_prefix = f"{group_id}_"
    for session_id, modes in _history_deques.items():
        if not session_id.startswith(group_prefix): continue
        for mode, slots in modes.items():
            for slot_index, history_deque in slots.items():
                for record in reversed(history_deque):
                    if record.get('role') == 'assistant' and record.get('message_id') == bot_message_id:
                        original_question_id = record.get('response_to_id')
                        if original_question_id:
                            logger.info(f"找到匹配！机器人消息 {bot_message_id} 是对用户消息 {original_question_id} 的回应。")
                            return original_question_id
    logger.warning(f"在群组 {group_id} 的所有实时记忆中，未能找到机器人消息 {bot_message_id} 的原始提问。")
    return None