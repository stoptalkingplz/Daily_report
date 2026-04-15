from datetime import datetime, timedelta
from zenv import get_zdkit_env
from zdbase import ZFile

import requests
import json
import time
import re
import uuid
import os
import tempfile
import traceback
import threading
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

# =============================================================================
# 日志
# =============================================================================
logger = logging.getLogger("daily_summary_workflow")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(levelname)s] %(asctime)s | %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

# =============================================================================
# 全局配置加载
# =============================================================================
zenv_obj = get_zdkit_env()
BASE_URL = zenv_obj.zdkit._http_client.config.get("url")

try:
    with open(config_file.path, "r", encoding="utf-8") as config_fp:
        config = json.load(config_fp)
except Exception as e:
    logger.exception("❌ 配置文件读取失败")
    raise

AK = config.get("ak")
SK = config.get("sk")
ORG_GUID = config.get("org_guid")
USER_GUID = config.get("user_guid", "")
SUMMARY_OUTPUT_PROJECT_GUID = config.get("summary_output_project_guid", "")
SUMMARY_OUTPUT_PARENT_GUID = config.get("summary_output_parent_guid", "0")
DAILY_EXTRACT_PROMPT_FILE_GUID = config.get("daily_extract_prompt_file_guid", "")
SUMMARY_PROMPT_FILE_GUID = config.get("summary_prompt_file_guid", "")
NON_LEAF_SUMMARY_PROMPT_FILE_GUID = config.get("non_leaf_summary_prompt_file_guid", "")
CARD_PROMPT_FILE_GUID = config.get("card_prompt_file_guid", "")
MESSAGE_RECEIVER_GUIDS = config.get("message_receiver_guids", [])
MESSAGE_SENDER_GUID = config.get("message_sender_guid", "")
MESSAGE_TEMPLATE_ID = config.get("message_template_id", "80")
PLATFORM_TYPE = config.get("platform_type", "all")
MAX_CONCURRENT_LLM = config.get("max_concurrent_llm", 10)
MAX_CONCURRENT_DEPT = config.get("max_concurrent_dept", 5)
CARD_MODE = config.get("card_mode", "children")  # children / parent_summary

llm_semaphore = threading.Semaphore(MAX_CONCURRENT_LLM)

# =============================================================================
# token 缓存
# =============================================================================
_access_token_cache = {
    "token": "",
    "expire_at": 0.0,
}
_access_token_lock = threading.Lock()
ACCESS_TOKEN_TTL_SECONDS = 60 * 25  # 保守缓存 25 分钟

# =============================================================================
# API 路由
# =============================================================================
ACCESS_TOKEN_ROUTE          = "/api/user/platform/getAccessToken"
DOC_TREE_LIST_ROUTE         = "/platform/api/main/doc/treeList"
NOTE_JSON_ROUTE             = "/platform/ws/noteInfo/getDocJson"
SIGNED_URL_ROUTE            = "/platform/api/main/storage/getSignedUrl"
WORKSPACE_SAVE_ROUTE        = "/middle/server/api/workspace/save"
MD_INSERT_ROUTE             = "/middle/server/api/file/md/insert"
CONVERSATION_ID_ROUTE       = "/platform/peerup_chatbot/conversation/id"
WORKFLOW_MODEL_ROUTE        = "/platform/peerup_chatbot/workflow/model"
WORKFLOW_MODEL_RESULT_ROUTE = "/platform/peerup_chatbot/workflow/model/result"
MESSAGE_SEND_ROUTE          = "/middle/server/api/msg/send"

# =============================================================================
# [工具] 共用辅助函数
# =============================================================================
def get_access_token():
    now_ts = time.time()
    with _access_token_lock:
        cached_token = _access_token_cache["token"]
        expire_at = _access_token_cache["expire_at"]
        if cached_token and now_ts < expire_at:
            return cached_token

        response = requests.post(
            url=BASE_URL + ACCESS_TOKEN_ROUTE,
            json={"ak": AK, "sk": SK},
            timeout=15
        )
        response_json = response.json()
        if not response_json.get("data"):
            raise Exception(f"获取 AccessToken 失败: {response_json}")

        access_token = response_json["data"].get("accessToken")
        if not access_token:
            raise Exception(f"AccessToken 为空: {response_json}")

        _access_token_cache["token"] = access_token
        _access_token_cache["expire_at"] = now_ts + ACCESS_TOKEN_TTL_SECONDS
        return access_token


def get_headers_with_ak(user_guid="", doc_id=""):
    access_token = get_access_token()
    headers = {
        "Access-Token": access_token,
        "ak": AK,
        "X-User-GUID": user_guid or USER_GUID,
    }
    if doc_id:
        headers["docId"] = doc_id
    return headers


def strip_markdown_wrapper(content):
    content = (content or "").strip()
    if content.startswith("```json"):
        content = content[len("```json"):].lstrip("\n")
    elif content.startswith("```markdown"):
        content = content[len("```markdown"):].lstrip("\n")
    elif content.startswith("```"):
        content = content[3:].lstrip("\n")
    if content.endswith("```"):
        content = content[:-3].rstrip("\n")
    return content.strip()


def safe_json_loads(text):
    clean_text = strip_markdown_wrapper(text)
    try:
        return json.loads(clean_text)
    except Exception:
        pass

    match = re.search(r'(\{.*\}|\[.*\])', clean_text, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except Exception:
            pass

    logger.error("JSON 解析失败，原始输出片段: %s", clean_text[:3000])
    raise ValueError("无法解析 JSON")


def load_prompt_text(prompt_file_guid, default_prompt):
    if not prompt_file_guid:
        return default_prompt
    try:
        signed_url_response = requests.get(
            BASE_URL + SIGNED_URL_ROUTE,
            headers=get_headers_with_ak(),
            params={"categoryGuid": prompt_file_guid},
            timeout=15
        )
        signed_url = (signed_url_response.json().get("data") or {}).get("signedUrl")
        if not signed_url:
            return default_prompt
        return requests.get(signed_url, timeout=15).text
    except Exception:
        logger.warning("加载 prompt 失败，使用默认 prompt，prompt_file_guid=%s", prompt_file_guid)
        return default_prompt


def _create_chat_id(conversation_id="", id_type="conversation"):
    response = requests.post(
        BASE_URL + CONVERSATION_ID_ROUTE,
        headers=get_headers_with_ak(),
        json={"conversation_id": conversation_id, "type": id_type},
        timeout=20
    )
    return response.json().get("data", {}).get("id")


def create_conversation_id():
    return _create_chat_id("", "conversation")


def create_message_id(conversation_id):
    return _create_chat_id(conversation_id, "message")


def poll_workflow_result(message_id, max_retries=120, interval=3):
    for _ in range(max_retries):
        response = requests.post(
            BASE_URL + WORKFLOW_MODEL_RESULT_ROUTE,
            headers=get_headers_with_ak(),
            json={"message_id": message_id},
            timeout=20
        )
        data = response.json().get("data", {})
        status = data.get("status")
        if status == "completed":
            return data.get("content")
        if status == "failed":
            raise Exception(f"AI Failed: {data.get('error_message')}")
        time.sleep(interval)
    raise Exception("AI Timeout")


def call_workflow_model(message_id, llm_name, llm_params, context_messages):
    response = requests.post(
        BASE_URL + WORKFLOW_MODEL_ROUTE,
        headers=get_headers_with_ak(),
        json={
            "message_id": message_id,
            "llm_config": {"llm_name": llm_name, "llm_params": llm_params},
            "context_messages": context_messages
        },
        timeout=30
    )
    task_message_id = response.json().get("data", {}).get("message_id")
    if not task_message_id:
        raise Exception("No task ID")
    return poll_workflow_result(task_message_id)


def _call_llm_with_retry(llm_name, llm_params, context_messages, max_retries=10):
    with llm_semaphore:
        attempt = 0
        last_error = None
        while attempt < max_retries:
            try:
                logger.info("🔄 [尝试 %s/%s] 调用 AI 工作流...", attempt + 1, max_retries)
                conversation_id = create_conversation_id()
                message_id = create_message_id(conversation_id)
                return call_workflow_model(
                    message_id=message_id,
                    llm_name=llm_name,
                    llm_params=llm_params,
                    context_messages=context_messages
                )
            except Exception as e:
                last_error = e
                attempt += 1
                if attempt < max_retries:
                    wait_time = min(2 ** (attempt - 1), 30)
                    logger.warning("⚠️ AI 调用失败: %s. %s 秒后重试...", e, wait_time)
                    time.sleep(wait_time)
                else:
                    logger.error("❌ AI 连续 %s 次失败: %s", max_retries, e)
                    raise last_error


def get_note_json_content(user_guid="", doc_id=""):
    headers = get_headers_with_ak(user_guid=user_guid, doc_id=doc_id)
    response = requests.get(
        url=BASE_URL + NOTE_JSON_ROUTE,
        headers=headers,
        params={"docId": doc_id},
        timeout=20
    )
    return response.json()


def extract_markdown_from_note_json(note_json):
    root_blocks = (
        note_json.get("data", {}).get("content", [])
        or note_json.get("content", [])
    )
    markdown_lines = []

    def process_inline_content(inline_content):
        text_parts = []
        for item in inline_content:
            item_type = item.get("type")
            if item_type == "text":
                text_parts.append(item.get("text", ""))
            elif item_type == "mention":
                attrs = item.get("attrs", {})
                label = attrs.get("label", "?")
                uid = attrs.get("uid", "")
                user_id = attrs.get("id", "")
                text_parts.append(f"[@{label}](mention:{uid}:{user_id})")
        return "".join(text_parts)

    def traverse(blocks, level=0):
        for block in blocks:
            block_type = block.get("type")

            if block_type == "heading":
                inline_content = block.get("content", [])
                text = process_inline_content(inline_content)
                attrs = block.get("attrs", {})
                heading_level = int(attrs.get("level", 1))
                markdown_lines.append(f"{'#' * heading_level} {text}")

            elif block_type == "fheading":
                inline_content = block.get("content", [])
                text = process_inline_content(inline_content)
                attrs = block.get("attrs", {})
                heading_level = int(attrs.get("level", 1))
                markdown_lines.append(f"{'#' * heading_level} {text}")

            elif block_type == "paragraph":
                inline_content = block.get("content", [])
                text = process_inline_content(inline_content)
                if text.strip():
                    markdown_lines.append(text)

            elif block_type == "bulletListItem":
                inline_content = block.get("content", [])
                text = process_inline_content(inline_content)
                if text.strip():
                    indent = "    " * level
                    markdown_lines.append(f"{indent}- {text}")

            elif block_type == "numberedListItem":
                inline_content = block.get("content", [])
                text = process_inline_content(inline_content)
                if text.strip():
                    indent = "    " * level
                    markdown_lines.append(f"{indent}1. {text}")

            elif block_type in ("blockContainer", "blockGroup"):
                if "content" in block:
                    traverse(block["content"], level)

            if "content" in block and isinstance(block["content"], list):
                if block_type in ("bulletListItem", "numberedListItem"):
                    traverse(block["content"], level + 1)

    traverse(root_blocks)
    return "\n".join(markdown_lines)


def _convert_special_nodes(content):
    """
    将旧式 Markdown 特殊语法转换为 md/insert 接口支持的 HTML 格式
    """
    content = re.sub(
        r"\[@([^\]]*)\]\(mention:[^:]+:([^)]+)\)",
        lambda m: f'<span data-node-type="mention" data-guid="{m.group(2)}"></span>',
        content
    )

    content = re.sub(
        r"\[([^\]]+)\]\(mentionUrl:[^:]+:[^:]+:([^)]+)\)",
        lambda m: f'<a data-node-type="mentionUrl" data-url="{m.group(2)}">{m.group(1)}</a>',
        content
    )

    content = re.sub(
        r":::highlight\[[^\]]*\]\n(.*?):::",
        lambda m: f'<div data-node-type="highlightBlock" data-content-markdown>\n{m.group(1).rstrip()}\n</div>',
        content,
        flags=re.DOTALL
    )

    return content


def insert_markdown_to_note(user_guid, note_guid, markdown_content):
    headers = get_headers_with_ak(user_guid=user_guid)
    headers["Content-Type"] = "application/json; charset=utf-8"

    html_content = _convert_special_nodes(markdown_content)

    body = json.dumps(
        {
            "note_guid": note_guid,
            "markdown_content": html_content,
            "mode": "w",
            "location": 1
        },
        ensure_ascii=False
    ).encode("utf-8")

    response = requests.post(
        BASE_URL + MD_INSERT_ROUTE,
        headers=headers,
        data=body,
        timeout=30
    )
    if response.status_code != 200:
        raise Exception(f"写入笔记失败: {response.text}")
    return response.json()


def create_note_api(content, title, project_guid, parent_guid, tags, creator_guid=None):
    creator_guid = creator_guid or USER_GUID
    headers = get_headers_with_ak()
    headers["X-User-GUID"] = creator_guid
    if not project_guid:
        raise ValueError("target_project_guid 不能为空！")

    response = requests.post(
        BASE_URL + WORKSPACE_SAVE_ROUTE,
        headers=headers,
        json={
            "project_guid": project_guid,
            "parent_guid": parent_guid,
            "target": {"name": title, "type": 1, "tags": tags},
            "creator_guid": creator_guid
        },
        timeout=30
    )
    response_json = response.json()
    if response.status_code != 200 or not response_json.get("data"):
        raise Exception(f"创建笔记 API 返回错误: {response_json}")

    doc_id = response_json.get("data", {}).get("guid")
    if doc_id:
        insert_markdown_to_note(creator_guid, doc_id, content)
    return doc_id


def load_json_from_direct_file(file_guid, user_guid=""):
    headers = get_headers_with_ak(user_guid=user_guid)
    resp = requests.get(
        BASE_URL + SIGNED_URL_ROUTE,
        headers=headers,
        params={"categoryGuid": file_guid},
        timeout=15
    )
    signed_url = resp.json()["data"]["signedUrl"]
    response = requests.get(signed_url, timeout=15)
    response.encoding = "utf-8"
    return json.loads(response.text)


def get_target_date():
    return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


def build_summary_result(dept_id, dept_name, dept_summary_md, parent_dept_id, note_guid="", is_leaf=False):
    return {
        "dept_name": dept_name,
        "dept_id": dept_id,
        "summary_md": (dept_summary_md or "").strip(),
        "note_guid": note_guid,
        "parent_dept_id": parent_dept_id,
        "is_leaf": is_leaf
    }


def split_summary_blocks(children_md):
    return [b.strip() for b in (children_md or "").split("\n\n---\n\n") if b.strip()]


def truncate_children_md_by_block(children_md, max_chars=12000):
    blocks = split_summary_blocks(children_md)
    result = []
    total = 0
    sep = "\n\n---\n\n"

    for block in blocks:
        sep_len = len(sep) if result else 0
        if total + sep_len + len(block) > max_chars:
            break
        if result:
            total += len(sep)
        result.append(block)
        total += len(block)

    return sep.join(result)


def build_summary_header(dept_name, leader_name="", source_note_link=""):
    lines = [f"# {dept_name}"]

    meta_items = []
    if leader_name:
        meta_items.append(f"负责人：{leader_name}")

    if source_note_link:
        link_uid = str(uuid.uuid4())
        mention_url = f"[查看源笔记](mentionUrl:{link_uid}:1:{source_note_link})"
        meta_items.append(f"原日报链接：{mention_url}")

    if meta_items:
        lines.append(" | ".join(meta_items))

    return "\n".join(lines)


def normalize_summary_body(body_md, expected_sections, fallback_first_section_title):
    """
    对模型正文输出做轻量标准化和兜底
    """
    text = strip_markdown_wrapper(body_md or "")

    # 去掉模型误输出的一些头部
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append(line)
            continue
        if stripped.startswith("# "):
            continue
        if stripped.startswith("负责人："):
            continue
        if "原日报链接：" in stripped:
            continue
        lines.append(line)
    text = "\n".join(lines).strip()

    if not text:
        sections = [f"## {expected_sections[0]}\n- 暂无", f"## {expected_sections[1]}\n- 暂无"] if len(expected_sections) >= 2 else [f"## {fallback_first_section_title}\n- 暂无"]
        return "\n\n".join(sections)

    normalized = text

    # 如果完全没有 section，就兜底包装
    has_any_expected = any(f"## {section}" in normalized for section in expected_sections)
    if not has_any_expected:
        if len(expected_sections) >= 2:
            normalized = f"## {fallback_first_section_title}\n{normalized}\n\n## {expected_sections[1]}\n- 暂无"
        else:
            normalized = f"## {fallback_first_section_title}\n{normalized}"

    # 缺 section 时补空壳
    for idx, section in enumerate(expected_sections):
        if f"## {section}" not in normalized:
            normalized = f"{normalized}\n\n## {section}\n- 暂无".strip()

    return normalized.strip()

# =============================================================================
# [核心] org_config 读取与叶子部门发现
# =============================================================================
def load_org_config(org_config_path):
    with open(org_config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    nodes = cfg.get("org_config", {}).get("nodes", {})
    leaf_depts = [node for node in nodes.values() if node.get("is_leaf") is True]
    return leaf_depts

# =============================================================================
# [核心] 文件夹列表与日报查找
# =============================================================================
def get_tree_list(user_guid, project_guid, parent_guid):
    headers = get_headers_with_ak(user_guid=user_guid)
    response = requests.post(
        url=BASE_URL + DOC_TREE_LIST_ROUTE,
        headers=headers,
        json={"projectGuid": project_guid, "parentGuid": parent_guid},
        timeout=15
    )
    data = response.json().get("data", [])
    if isinstance(data, dict):
        return data.get("list", [])
    return data


def find_daily_note_by_date(user_guid, project_guid, folder_guid, target_date_str, exclude_title_keywords=None):
    note_list = get_tree_list(user_guid, project_guid, folder_guid)
    if not note_list:
        return None

    exclude_keywords = exclude_title_keywords or ["日报摘要", "飞书卡片"]
    date_pattern = re.compile(r"(\d{4})[-/.]?(\d{1,2})[-/.]?(\d{1,2})")
    for item in note_list:
        if item.get("dataType") != 1:
            continue
        title = item.get("dataTitle", "")
        if any(kw in title for kw in exclude_keywords):
            continue
        for match in date_pattern.finditer(title):
            y, m, d = match.groups()
            date_str = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
            if date_str == target_date_str:
                return (
                    item.get("dataGuid"),
                    item.get("creatorGuid", user_guid),
                    title
                )
    return None

# =============================================================================
# [核心] 日报 Markdown → 结构化 JSON
# =============================================================================
def extract_daily_report_to_json(markdown_content, prompt_file_guid):
    if not prompt_file_guid:
        raise ValueError("daily_extract_prompt_file_guid 未配置，必须从外部文件读取 prompt")

    prompt_template = load_prompt_text(prompt_file_guid, "")
    prompt_text = prompt_template.replace("{{markdown_content}}", markdown_content)

    context_messages = [
        {
            "role": "system",
            "content": "你是日报结构化抽取助手，请严格按照要求输出合法 JSON。",
            "variables": []
        },
        {
            "role": "user",
            "content": prompt_text,
            "variables": []
        }
    ]

    llm_result = _call_llm_with_retry(
        llm_name=model.llm_name,
        llm_params=model.llm_params,
        context_messages=context_messages,
        max_retries=10
    )

    extracted_data = safe_json_loads(llm_result)
    return extracted_data

# =============================================================================
# [核心] 结构化 JSON → 摘要正文
# =============================================================================
def build_summary_prompt(dept_obj, prompt_template):
    dept_json_str = json.dumps(dept_obj, ensure_ascii=False, indent=2)
    return prompt_template.replace("{{dept_json}}", dept_json_str)


def summarize_leaf_body(dept_obj, prompt_file_guid):
    if not prompt_file_guid:
        raise ValueError("summary_prompt_file_guid 未配置，必须从外部文件读取 prompt")

    prompt_template = load_prompt_text(prompt_file_guid, "")
    prompt_text = build_summary_prompt(dept_obj, prompt_template)

    context_messages = [
        {
            "role": "system",
            "content": "你是日报摘要助手，请基于结构化日报数据生成摘要正文。只输出正文，不要输出标题、负责人、原日报链接。",
            "variables": []
        },
        {
            "role": "user",
            "content": prompt_text,
            "variables": []
        }
    ]

    result = _call_llm_with_retry(
        llm_name=model.llm_name,
        llm_params=model.llm_params,
        context_messages=context_messages,
        max_retries=10
    )
    return strip_markdown_wrapper(result)


def summarize_non_leaf_body(children_daily_md, prompt_file_guid=""):
    if not prompt_file_guid:
        raise ValueError("non_leaf_summary_prompt_file_guid 未配置，必须从外部文件读取 prompt")

    prompt_template = load_prompt_text(prompt_file_guid, "")
    truncated_children_daily_md = truncate_children_md_by_block(children_daily_md, max_chars=12000)
    prompt_text = prompt_template.replace("{{markdown_content}}", truncated_children_daily_md)

    context_messages = [
        {
            "role": "system",
            "content": "你是部门日报汇总助手，请基于子部门摘要拼接形成的日报正文，生成部门级汇总正文。只输出正文，不要输出标题、负责人、原日报链接。",
            "variables": []
        },
        {
            "role": "user",
            "content": prompt_text,
            "variables": []
        }
    ]

    result = _call_llm_with_retry(
        llm_name=model.llm_name,
        llm_params=model.llm_params,
        context_messages=context_messages,
        max_retries=10
    )
    return strip_markdown_wrapper(result)


def generate_card_content(dept_name, summary_md, prompt_file_guid):
    if not prompt_file_guid:
        raise ValueError("card_prompt_file_guid 未配置，必须从外部文件读取 prompt")

    prompt_template = load_prompt_text(prompt_file_guid, "")
    user_content = prompt_template.replace("{{markdown_content}}", summary_md[:8000])

    context_messages = [
        {
            "role": "system",
            "content": "你是内容整理助手，请输出纯文本摘要，不要 Markdown 代码块标记。",
            "variables": []
        },
        {
            "role": "user",
            "content": user_content,
            "variables": []
        }
    ]

    try:
        llm_result = _call_llm_with_retry(
            llm_name=model.llm_name,
            llm_params=model.llm_params,
            context_messages=context_messages,
            max_retries=10
        )
        return strip_markdown_wrapper(llm_result)
    except Exception as e:
        logger.warning("⚠️ [%s] AI 卡片摘要生成失败: %s", dept_name, e)
        if len(summary_md) > 20000:
            return summary_md[:20000] + "\n\n[系统提示：AI 生成失败，此为自动截断的预览]"
        return summary_md


def build_feishu_card(title, card_content, note_url):
    return {
        "schema": "2.0",
        "header": {
            "padding": "12px 8px 12px 8px",
            "template": "blue",
            "title": {
                "content": title,
                "tag": "plain_text"
            }
        },
        "body": {
            "vertical_spacing": "12px",
            "elements": [
                {
                    "tag": "markdown",
                    "content": card_content,
                    "margin": "0px",
                    "text_size": "normal"
                },
                {
                    "tag": "column_set",
                    "flex_mode": "stretch",
                    "horizontal_spacing": "8px",
                    "margin": "0px",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "auto",
                            "elements": [
                                {
                                    "tag": "button",
                                    "type": "primary_filled",
                                    "width": "fill",
                                    "margin": "4px 0px 4px 0px",
                                    "text": {
                                        "tag": "plain_text",
                                        "content": "查看详情"
                                    },
                                    "behaviors": [
                                        {
                                            "type": "open_url",
                                            "default_url": note_url
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }
    }


def send_message_api(receiver_guids, title, content, sender_guid="", interactive_content=None):
    payload = {
        "template_id": MESSAGE_TEMPLATE_ID,
        "receiver_guid": receiver_guids,
        "content": content,
        "org_guid": ORG_GUID,
        "title": title,
        "platform_type": PLATFORM_TYPE
    }
    if interactive_content is not None:
        payload["interactive_content"] = json.dumps(interactive_content)

    return requests.post(
        url=BASE_URL + MESSAGE_SEND_ROUTE,
        headers=get_headers_with_ak(user_guid=sender_guid),
        json=payload,
        timeout=30
    )

# =============================================================================
# 主流程 - 单部门处理
# =============================================================================
def process_leaf_dept(dept_id, dept, target_date, dept_daily_note_url_map):
    dept_name = dept.get("dept_name", "Unknown")
    folder_guid = dept.get("output_folder_guid", "")
    project_guid = dept.get("project_guid", "")
    leader_guid = dept.get("leader_guid", "")
    leader_name = dept.get("leader_name", "")
    parent_dept_id = dept.get("parent_dept_id", "")

    logger.info("▶ 处理部门: %s (%s) [叶子节点]", dept_name, dept_id)

    if not folder_guid:
        logger.warning("⏭ 跳过 %s：无 output_folder_guid", dept_name)
        return None
    if not project_guid:
        logger.warning("⏭ 跳过 %s：无 project_guid", dept_name)
        return None

    logger.info("[%s] [1] 查找 %s 的日报...", dept_name, target_date)
    result = find_daily_note_by_date(leader_guid, project_guid, folder_guid, target_date)
    if not result:
        logger.warning("⚠️ %s 未找到 %s 的日报，跳过", dept_name, target_date)
        return None

    note_guid, creator_guid, note_title = result
    logger.info("[%s] [1] 找到日报: %s (GUID: %s)", dept_name, note_title, note_guid)

    source_link = f"{BASE_URL}/workspace/{note_guid}"
    dept_daily_note_url_map[dept_id] = source_link

    logger.info("[%s] [2] 读取日报内容...", dept_name)
    note_json = get_note_json_content(user_guid=leader_guid, doc_id=note_guid)
    daily_note_markdown = extract_markdown_from_note_json(note_json)
    if not daily_note_markdown.strip():
        logger.warning("⚠️ %s 日报内容为空，跳过", dept_name)
        return None

    logger.info("[%s] [3] AI 抽取结构化 JSON...", dept_name)
    extracted_json = extract_daily_report_to_json(
        daily_note_markdown,
        prompt_file_guid=DAILY_EXTRACT_PROMPT_FILE_GUID
    )

    dept_list = extracted_json.get("dept", [])
    if not dept_list:
        logger.warning("⚠️ %s 抽取结果中无 dept 数据，跳过", dept_name)
        return None

    results = []
    for dept_obj in dept_list:
        sub_dept_name = dept_obj.get("dept_name", dept_name)
        logger.info("[%s] [4] AI 生成摘要正文: %s", dept_name, sub_dept_name)

        body_md = summarize_leaf_body(
            dept_obj,
            prompt_file_guid=SUMMARY_PROMPT_FILE_GUID
        )
        body_md = normalize_summary_body(
            body_md=body_md,
            expected_sections=["关键进展", "风险与支持"],
            fallback_first_section_title="关键进展"
        )

        header_md = build_summary_header(
            dept_name=dept_name,
            leader_name=leader_name,
            source_note_link=source_link
        )

        dept_summary_md = f"{header_md}\n\n{body_md.strip()}" if body_md.strip() else header_md

        results.append(
            build_summary_result(
                dept_id=dept_id,
                dept_name=dept_name,
                dept_summary_md=dept_summary_md,
                parent_dept_id=parent_dept_id,
                note_guid=note_guid,
                is_leaf=True
            )
        )

    logger.info("✅ %s 处理完成", dept_name)
    return results


def process_non_leaf_dept(dept_id, dept, dept_daily_md_map, dept_daily_note_url_map):
    dept_name = dept.get("dept_name", "Unknown")
    leader_name = dept.get("leader_name", "")
    parent_dept_id = dept.get("parent_dept_id", "")

    logger.info("▶ 处理部门: %s (%s) [非叶非根节点]", dept_name, dept_id)

    current_dept_daily_md = dept_daily_md_map.get(dept_id, "")
    if not current_dept_daily_md.strip():
        logger.warning("⚠️ %s 无本级日报正文可用，跳过", dept_name)
        return None

    source_note_link = dept_daily_note_url_map.get(dept_id, "")
    if source_note_link:
        logger.info("[%s] [0] 使用本部门日报链接: %s", dept_name, source_note_link)
    else:
        logger.info("[%s] [0] 暂无本部门日报链接，头部原日报链接留空", dept_name)

    logger.info("[%s] [1] 使用本级日报正文作为输入 (%s 字符)...", dept_name, len(current_dept_daily_md))
    logger.info("[%s] [2] AI 生成部门级汇总正文...", dept_name)

    body_md = summarize_non_leaf_body(
        current_dept_daily_md,
        prompt_file_guid=NON_LEAF_SUMMARY_PROMPT_FILE_GUID
    )
    body_md = normalize_summary_body(
        body_md=body_md,
        expected_sections=["综合进展", "风险与支持"],
        fallback_first_section_title="综合进展"
    )

    header_md = build_summary_header(
        dept_name=dept_name,
        leader_name=leader_name,
        source_note_link=source_note_link
    )

    dept_summary_md = f"{header_md}\n\n{body_md.strip()}" if body_md.strip() else header_md

    result = build_summary_result(
        dept_id=dept_id,
        dept_name=dept_name,
        dept_summary_md=dept_summary_md,
        parent_dept_id=parent_dept_id,
        note_guid="",
        is_leaf=False
    )

    logger.info("✅ %s 处理完成", dept_name)
    return [result]


def process_dept(dept_id, dept, target_date, dept_daily_md_map, dept_daily_note_url_map):
    dept_name = dept.get("dept_name", "Unknown")
    is_leaf = dept.get("is_leaf", False)
    is_root = dept.get("is_root", False)

    if is_root:
        logger.info("⏭ 跳过根节点 %s：不进行AI处理", dept_name)
        return None

    try:
        if is_leaf:
            return process_leaf_dept(dept_id, dept, target_date, dept_daily_note_url_map)
        return process_non_leaf_dept(dept_id, dept, dept_daily_md_map, dept_daily_note_url_map)
    except Exception as e:
        logger.exception("❌ %s 处理中断: %s", dept_name, e)
        return None

# =============================================================================
# 主流程
# =============================================================================
logger.info("=" * 60)
logger.info("开始执行日报自动摘要流程")
logger.info("=" * 60)

all_nodes = config.get("org_config", {}).get("nodes", {})
if not all_nodes:
    logger.error("❌ org_config.nodes 为空，退出")
    raise SystemExit(1)

max_depth = max(node.get("depth", 0) for node in all_nodes.values())
min_depth = min(node.get("depth", 0) for node in all_nodes.values())

target_date = get_target_date()

logger.info("📋 目标日期: %s", target_date)
logger.info("📋 最大深度: %s，最小深度: %s", max_depth, min_depth)
logger.info("📋 总节点数: %s", len(all_nodes))
logger.info("📋 并发配置: MAX_CONCURRENT_DEPT=%s, MAX_CONCURRENT_LLM=%s", MAX_CONCURRENT_DEPT, MAX_CONCURRENT_LLM)
logger.info("📋 卡片模式: %s", CARD_MODE)

# 给上一级继续汇总的“本级日报正文”
# 叶子层不会直接写入这里；父层聚合时写入
dept_daily_md_map = {}

# 保存“每个部门当天日报链接”
# 叶子：原始日报链接
# 非叶子：下级摘要拼接后保存下来的本级日报链接
dept_daily_note_url_map = {}

for current_depth in range(max_depth, min_depth - 1, -1):
    depth_nodes = {
        k: v for k, v in all_nodes.items()
        if v.get("depth") == current_depth
    }
    if not depth_nodes:
        continue

    logger.info("%s", "=" * 60)
    logger.info("▶ 处理深度 %s | 节点数: %s", current_depth, len(depth_nodes))
    logger.info("%s", "=" * 60)

    depth_summaries = []

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DEPT) as executor:
        future_to_dept = {
            executor.submit(
                process_dept,
                dept_id,
                dept,
                target_date,
                dept_daily_md_map,
                dept_daily_note_url_map
            ): dept_id
            for dept_id, dept in depth_nodes.items()
        }

        for future in as_completed(future_to_dept):
            dept_id = future_to_dept[future]
            try:
                result = future.result()
                if result:
                    depth_summaries.extend(result)
            except Exception as e:
                logger.exception("❌ %s 线程异常: %s", dept_id, e)

    if not depth_summaries:
        logger.warning("⚠️ 深度 %s 无摘要产出，跳过聚合", current_depth)
        continue

    grouped_by_parent = defaultdict(list)
    for summary_result in depth_summaries:
        grouped_by_parent[summary_result["parent_dept_id"]].append(summary_result)

    for parent_id, child_summary_results in grouped_by_parent.items():
        parent_node = all_nodes.get(parent_id, {})
        parent_name = parent_node.get("dept_name", parent_id or "未分组")
        parent_folder = parent_node.get("output_folder_guid", "")
        parent_project = parent_node.get("project_guid", "")
        parent_leader_guid = parent_node.get("leader_guid", "")

        logger.info("📂 聚合父部门 [%s] 下 %s 个子部门摘要", parent_name, len(child_summary_results))

        child_summary_md_list = [s["summary_md"] for s in child_summary_results]
        parent_daily_note_md = (
            "\n\n---\n\n".join(child_summary_md_list)
            if len(child_summary_md_list) > 1
            else (child_summary_md_list[0] if child_summary_md_list else "")
        )

        if not parent_daily_note_md.strip():
            logger.warning("⚠️ [%s] 聚合内容为空，跳过", parent_name)
            continue

        # 保存为“本级日报正文”，供下一轮本级摘要生成使用
        dept_daily_md_map[parent_id] = parent_daily_note_md

        saved_note_url = ""
        if parent_folder and parent_project:
            daily_note_title = f"{parent_name}_{target_date}_日报"
            try:
                logger.info("💾 保存本级日报到 [%s] 文件夹: %s", parent_name, daily_note_title)
                agg_doc_id = create_note_api(
                    content=parent_daily_note_md,
                    title=daily_note_title,
                    project_guid=parent_project,
                    parent_guid=parent_folder,
                    tags=["日报摘要", "AI总结"],
                    creator_guid=USER_GUID
                )
                if agg_doc_id:
                    saved_note_url = f"{BASE_URL}/workspace/{agg_doc_id}"
                    dept_daily_note_url_map[parent_id] = saved_note_url
                    logger.info("✅ 本级日报已保存 (GUID: %s)", agg_doc_id)
                    logger.info("✅ 本级日报链接已记录: %s", saved_note_url)
                else:
                    logger.error("❌ 本级日报保存失败")
            except Exception as e:
                logger.exception("❌ 本级日报保存异常 [%s]: %s", parent_name, e)
        else:
            logger.warning("⚠️ [%s] 无 output_folder_guid 或 project_guid，跳过保存本级日报", parent_name)
            logger.info("摘要内容预览:\n%s", parent_daily_note_md[:2000])

        card_title = f"{parent_name} 日报摘要 {target_date}"

        if CARD_MODE == "parent_summary":
            # 用父部门综合摘要（当前轮还拿不到本轮 parent_summary，只能用子项拼接之外的策略）
            # 当前设计下仍建议 children，更符合你的“本级日报=下级拼接”逻辑
            merged_card_content = "\n\n".join(
                f"**{item['dept_name']}**\n{generate_card_content(item['dept_name'], item['summary_md'], CARD_PROMPT_FILE_GUID)}"
                for item in child_summary_results
            )
        else:
            card_parts = []
            for summary_info in child_summary_results:
                child_dept_name = summary_info["dept_name"]
                child_summary_md = summary_info["summary_md"]
                logger.info("🤖 [%s] AI 二次总结生成卡片内容...", child_dept_name)
                dept_card_content = generate_card_content(
                    dept_name=child_dept_name,
                    summary_md=child_summary_md,
                    prompt_file_guid=CARD_PROMPT_FILE_GUID
                )
                card_parts.append(f"**{child_dept_name}**\n{dept_card_content}")
            merged_card_content = "\n\n".join(card_parts)

        card = build_feishu_card(card_title, merged_card_content, saved_note_url)

        receiver_guids = []
        if parent_leader_guid:
            receiver_guids.append(parent_leader_guid)
        for guid in MESSAGE_RECEIVER_GUIDS:
            if guid and guid not in receiver_guids:
                receiver_guids.append(guid)

        if receiver_guids:
            text_content = f"【{card_title}】已生成，请点击查看。\n<a href='{saved_note_url}'>点击查看详情</a>"
            sender_guid = MESSAGE_SENDER_GUID or USER_GUID

            try:
                logger.info("📩 发送飞书卡片消息给 %s 人 (含父部门leader)...", len(receiver_guids))
                response = send_message_api(
                    receiver_guids=receiver_guids,
                    title=card_title,
                    content=text_content,
                    sender_guid=sender_guid,
                    interactive_content=card
                )
                if response.status_code == 200 and response.json().get("data"):
                    logger.info("✅ 飞书卡片发送成功 [%s]", parent_name)
                else:
                    logger.error("❌ 飞书卡片发送失败 [%s]: %s", parent_name, response.text)
            except Exception as e:
                logger.exception("❌ 飞书卡片发送异常 [%s]: %s", parent_name, e)
        else:
            logger.warning("⚠️ 无接收人（父部门无leader且未配置message_receiver_guids），跳过飞书卡片发送")

logger.info("%s", "=" * 60)
logger.info("全部摘要任务执行完毕")
logger.info("%s", "=" * 60)