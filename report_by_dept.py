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
from concurrent.futures import ThreadPoolExecutor, as_completed

# =============================================================================
# 全局配置加载
# =============================================================================
zenv_obj = get_zdkit_env()
BASE_URL = zenv_obj.zdkit._http_client.config.get("url")

try:
    with open(config_file.path, "r", encoding="utf-8") as config_fp:
        config = json.load(config_fp)
except Exception as e:
    print(f"❌ 配置文件读取失败: {e}")
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

llm_semaphore = threading.Semaphore(MAX_CONCURRENT_LLM)

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
def get_headers_with_ak(user_guid="", doc_id=""):
    response = requests.post(
        url=BASE_URL + ACCESS_TOKEN_ROUTE,
        json={"ak": AK, "sk": SK}
    )
    response_json = response.json()
    if not response_json.get("data"):
        raise Exception(f"获取 AccessToken 失败: {response_json}")
    access_token = response_json["data"].get("accessToken")
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
        return json.loads(match.group(1))
    raise ValueError("无法解析 JSON")


def load_prompt_text(prompt_file_guid, default_prompt):
    if not prompt_file_guid:
        return default_prompt
    try:
        signed_url_response = requests.get(
            BASE_URL + SIGNED_URL_ROUTE,
            headers=get_headers_with_ak(),
            params={"categoryGuid": prompt_file_guid}
        )
        signed_url = (signed_url_response.json().get("data") or {}).get("signedUrl")
        if not signed_url:
            return default_prompt
        return requests.get(signed_url, timeout=10).text
    except Exception:
        return default_prompt


def _create_chat_id(conversation_id="", id_type="conversation"):
    response = requests.post(
        BASE_URL + CONVERSATION_ID_ROUTE,
        headers=get_headers_with_ak(),
        json={"conversation_id": conversation_id, "type": id_type}
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
            json={"message_id": message_id}
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
        }
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
                print(f"    🔄 [尝试 {attempt + 1}/{max_retries}] 调用 AI 工作流...")
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
                    print(f"    ⚠️ AI 调用失败: {e}. {wait_time}秒后重试...")
                    time.sleep(wait_time)
                else:
                    print(f"    ❌ AI 连续 {max_retries} 次失败: {e}")
                    raise last_error


def get_note_json_content(user_guid="", doc_id=""):
    headers = get_headers_with_ak(user_guid=user_guid, doc_id=doc_id)
    response = requests.get(
        url=BASE_URL + NOTE_JSON_ROUTE,
        headers=headers,
        params={"docId": doc_id}
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
    # 1. 提及某人: [@name](mention:uid:user_id)
    # -> <span data-node-type="mention" data-guid="user_id"></span>
    content = re.sub(
        r"\[@([^\]]*)\]\(mention:[^:]+:([^)]+)\)",
        lambda m: f'<span data-node-type="mention" data-guid="{m.group(2)}"></span>',
        content
    )

    # 2. 提及链接: [text](mentionUrl:uid:type:url)
    # -> <a data-node-type="mentionUrl" data-url="url">text</a>
    content = re.sub(
        r"\[([^\]]+)\]\(mentionUrl:[^:]+:[^:]+:([^)]+)\)",
        lambda m: f'<a data-node-type="mentionUrl" data-url="{m.group(2)}">{m.group(1)}</a>',
        content
    )

    # 3. 高亮块
    content = re.sub(
        r":::highlight\[[^\]]*\]\n(.*?):::",
        lambda m: f'<div data-node-type="highlightBlock" data-content-markdown>\n{m.group(1).rstrip()}\n</div>',
        content,
        flags=re.DOTALL
    )

    return content


def insert_markdown_to_note(user_guid, note_guid, markdown_content):
    """
    写入笔记前先转换特殊节点（mention / mentionUrl / highlight）
    """
    headers = get_headers_with_ak(user_guid=user_guid)
    headers["Content-Type"] = "application/json; charset=utf-8"

    # 转换特殊节点为HTML格式
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
        data=body
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
        }
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
        params={"categoryGuid": file_guid}
    )
    signed_url = resp.json()["data"]["signedUrl"]
    response = requests.get(signed_url, timeout=10)
    response.encoding = 'utf-8'
    return json.loads(response.text)


# =============================================================================
# [核心] org_config 读取与叶子部门发现
# =============================================================================
def load_org_config(org_config_path):
    with open(org_config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    nodes = config.get("org_config", {}).get("nodes", {})
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
        timeout=10
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
    date_pattern = re.compile(r'(\d{4})[-/.]?(\d{1,2})[-/.]?(\d{1,2})')
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
# [核心] 日报 Markdown → 结构化 JSON（复用 daily_extract_v3.py 逻辑）
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
# [核心] 结构化 JSON → 摘要 Markdown
# =============================================================================
def build_summary_prompt(dept_obj, prompt_template):
    dept_json_str = json.dumps(dept_obj, ensure_ascii=False, indent=2)
    return prompt_template.replace("{{dept_json}}", dept_json_str)


def _replace_placeholders(md_text, dept_obj, dept_name_from_config="", dept_leader_name_from_config=""):
    dept_name = dept_name_from_config
    dept_leader_name = dept_leader_name_from_config
    source_note_link = dept_obj.get("source_note_link", "")
    md_text = md_text.replace("[[DEPT_NAME]]", dept_name)
    md_text = md_text.replace("[[DEPT_LEADER_NAME]]", dept_leader_name)
    # 将 SOURCE_NOTE_LINK 替换为 mentionUrl 格式（更稳定）
    if source_note_link:
        link_uid = str(uuid.uuid4())
        mention_url = f"[查看源笔记](mentionUrl:{link_uid}:1:{source_note_link})"
        md_text = md_text.replace("[[SOURCE_NOTE_LINK]]", mention_url)
    else:
        md_text = md_text.replace("[[SOURCE_NOTE_LINK]]", "")
    return md_text


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
        print(f"    ⚠️ [{dept_name}] AI 卡片摘要生成失败: {e}")
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
        json=payload
    )


def summarize_dept(dept_obj, prompt_file_guid, dept_name_from_config="", dept_leader_name_from_config=""):
    if not prompt_file_guid:
        raise ValueError("summary_prompt_file_guid 未配置，必须从外部文件读取 prompt")
    prompt_template = load_prompt_text(prompt_file_guid, "")
    prompt_text = build_summary_prompt(dept_obj, prompt_template)
    context_messages = [
        {
            "role": "system",
            "content": "你是日报摘要助手，请严格按照要求输出 Markdown 摘要。",
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
    return _replace_placeholders(result, dept_obj, dept_name_from_config, dept_leader_name_from_config)


def summarize_from_children_md(children_md, dept_name_from_config="", dept_leader_name_from_config="", prompt_file_guid=""):
    if not prompt_file_guid:
        raise ValueError("non_leaf_summary_prompt_file_guid 未配置，必须从外部文件读取 prompt")
    prompt_template = load_prompt_text(prompt_file_guid, "")
    prompt_text = prompt_template.replace("{{markdown_content}}", children_md[:12000])
    context_messages = [
        {
            "role": "system",
            "content": "你是部门日报汇总助手，请基于子部门摘要生成部门级汇总。",
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
    result = result.replace("[[DEPT_NAME]]", dept_name_from_config)
    result = result.replace("[[DEPT_LEADER_NAME]]", dept_leader_name_from_config)
    # 非叶子节点没有单一源笔记，清空链接占位符
    result = result.replace("[[SOURCE_NOTE_LINK]]", "")
    return result


# =============================================================================
# 主流程
# =============================================================================

from collections import defaultdict

def process_dept(dept_id, dept, target_date, dept_children_summary_map):
    dept_name = dept.get("dept_name", "Unknown")
    is_leaf = dept.get("is_leaf", False)
    is_root = dept.get("is_root", False)
    folder_guid = dept.get("output_folder_guid", "")
    project_guid = dept.get("project_guid", "")
    leader_guid = dept.get("leader_guid", "")
    leader_name = dept.get("leader_name", "")
    parent_dept_id = dept.get("parent_dept_id", "")

    if is_root:
        print(f"  ⏭ 跳过根节点 {dept_name}：不进行AI处理")
        return None

    node_type = "叶子节点" if is_leaf else "非叶非根节点"
    print(f"\n  ▶ 处理部门: {dept_name} ({dept_id}) [{node_type}]")

    try:
        if is_leaf:
            if not folder_guid:
                print(f"    ⏭ 跳过 {dept_name}：无 output_folder_guid")
                return None
            if not project_guid:
                print(f"    ⏭ 跳过 {dept_name}：无 project_guid")
                return None

            print(f"    [1] 查找 {target_date} 的日报...")
            result = find_daily_note_by_date(leader_guid, project_guid, folder_guid, target_date)
            if not result:
                print(f"    ⚠️ {dept_name} 未找到 {target_date} 的日报，跳过")
                return None

            note_guid, creator_guid, note_title = result
            print(f"    [1] 找到日报: {note_title} (GUID: {note_guid})")

            print(f"    [2] 读取日报内容...")
            note_json = get_note_json_content(user_guid=leader_guid, doc_id=note_guid)
            markdown_content = extract_markdown_from_note_json(note_json)
            if not markdown_content.strip():
                print(f"    ⚠️ {dept_name} 日报内容为空，跳过")
                return None

            print(f"    [3] AI 抽取结构化 JSON...")
            extracted_json = extract_daily_report_to_json(
                markdown_content,
                prompt_file_guid=DAILY_EXTRACT_PROMPT_FILE_GUID
            )
            if "dept" in extracted_json and len(extracted_json["dept"]) > 0:
                extracted_json["dept"][0]["source_note_link"] = f"{BASE_URL}/workspace/{note_guid}"

            dept_list = extracted_json.get("dept", [])
            if not dept_list:
                print(f"    ⚠️ {dept_name} 抽取结果中无 dept 数据，跳过")
                return None

            results = []
            for idx, dept_obj in enumerate(dept_list):
                sub_dept_name = dept_obj.get("dept_name", dept_name)
                print(f"    [4] AI 生成摘要: {sub_dept_name} (prompt=叶子)...")
                summary_md = summarize_dept(
                    dept_obj,
                    prompt_file_guid=SUMMARY_PROMPT_FILE_GUID,
                    dept_name_from_config=dept_name,
                    dept_leader_name_from_config=leader_name
                )
                results.append({
                    "dept_name": dept_name,
                    "dept_id": dept_id,
                    "summary_md": summary_md.strip(),
                    "note_guid": note_guid,
                    "parent_dept_id": parent_dept_id,
                    "is_leaf": is_leaf
                })

            print(f"    ✅ {dept_name} 处理完成")
            return results

        else:
            children_md = dept_children_summary_map.get(dept_id, "")
            if not children_md.strip():
                print(f"    ⚠️ {dept_name} 无子部门摘要可用，跳过")
                return None

            print(f"    [1] 使用子部门聚合摘要作为输入 ({len(children_md)} 字符)...")
            print(f"    [2] AI 生成部门级汇总摘要 (prompt=非叶非根)...")
            summary_md = summarize_from_children_md(
                children_md,
                dept_name_from_config=dept_name,
                dept_leader_name_from_config=leader_name,
                prompt_file_guid=NON_LEAF_SUMMARY_PROMPT_FILE_GUID
            )
            results = [{
                "dept_name": dept_name,
                "dept_id": dept_id,
                "summary_md": summary_md.strip(),
                "note_guid": "",
                "parent_dept_id": parent_dept_id,
                "is_leaf": is_leaf
            }]

            print(f"    ✅ {dept_name} 处理完成")
            return results

    except Exception as e:
        print(f"    ❌ {dept_name} 处理中断: {e}")
        traceback.print_exc()
        return None


print("=" * 60)
print("开始执行日报自动摘要流程")
print("=" * 60)

all_nodes = config.get("org_config", {}).get("nodes", {})
if not all_nodes:
    print("❌ org_config.nodes 为空，退出")
    raise SystemExit(1)

max_depth = max(node.get("depth", 0) for node in all_nodes.values())
min_depth = min(node.get("depth", 0) for node in all_nodes.values())

# 计算目标日期：周一回看上周五，其他时间回看昨天
now = datetime.now()
days_ago = 3 if now.weekday() == 0 else 1
target_date = (now - timedelta(days=days_ago)).strftime("%Y-%m-%d")

print(f"📋 目标日期: {target_date}")
print(f"📋 最大深度: {max_depth}，最小深度: {min_depth}")
print(f"📋 总节点数: {len(all_nodes)}")
print(f"📋 并发配置: MAX_CONCURRENT_DEPT={MAX_CONCURRENT_DEPT}, MAX_CONCURRENT_LLM={MAX_CONCURRENT_LLM}")

dept_children_summary_map = {}

for current_depth in range(max_depth, min_depth - 1, -1):
    depth_nodes = {
        k: v for k, v in all_nodes.items()
        if v.get("depth") == current_depth
    }
    if not depth_nodes:
        continue

    print(f"\n{'=' * 60}")
    print(f"▶ 处理深度 {current_depth} | 节点数: {len(depth_nodes)}")
    print("=" * 60)

    depth_summaries = []

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DEPT) as executor:
        future_to_dept = {
            executor.submit(process_dept, dept_id, dept, target_date, dept_children_summary_map): dept_id
            for dept_id, dept in depth_nodes.items()
        }
        for future in as_completed(future_to_dept):
            dept_id = future_to_dept[future]
            try:
                result = future.result()
                if result:
                    depth_summaries.extend(result)
            except Exception as e:
                print(f"    ❌ {dept_id} 线程异常: {e}")
                traceback.print_exc()

    if not depth_summaries:
        print(f"  ⚠️ 深度 {current_depth} 无摘要产出，跳过聚合")
        continue

    grouped = defaultdict(list)
    for s in depth_summaries:
        grouped[s["parent_dept_id"]].append(s)

    for parent_id, summaries in grouped.items():
        parent_node = all_nodes.get(parent_id, {})
        parent_name = parent_node.get("dept_name", parent_id or "未分组")
        parent_folder = parent_node.get("output_folder_guid", "")
        parent_project = parent_node.get("project_guid", "")
        parent_leader_guid = parent_node.get("leader_guid", "")
        parent_leader_name = parent_node.get("leader_name", "")
        parent_is_root = parent_node.get("is_root", False)

        print(f"\n  📂 聚合父部门 [{parent_name}] 下 {len(summaries)} 个子部门摘要")

        summary_md_list = [s["summary_md"] for s in summaries]
        merged_md = "\n\n---\n\n".join(summary_md_list) if len(summary_md_list) > 1 else (summary_md_list[0] if summary_md_list else "")

        if not merged_md.strip():
            print(f"    ⚠️ [{parent_name}] 聚合内容为空，跳过")
            continue

        dept_children_summary_map[parent_id] = merged_md

        saved_note_url = ""
        if parent_folder and parent_project:
            agg_note_title = f"{parent_name}_{target_date}_日报"
            try:
                print(f"    💾 保存聚合笔记到 [{parent_name}] 文件夹: {agg_note_title}")
                agg_doc_id = create_note_api(
                    content=merged_md,
                    title=agg_note_title,
                    project_guid=parent_project,
                    parent_guid=parent_folder,
                    tags=["日报摘要", "AI总结"],
                    creator_guid=USER_GUID
                )
                if agg_doc_id:
                    saved_note_url = f"{BASE_URL}/workspace/{agg_doc_id}"
                    print(f"    ✅ 聚合笔记已保存 (GUID: {agg_doc_id})")
                else:
                    print(f"    ❌ 聚合笔记保存失败")
            except Exception as e:
                print(f"    ❌ 聚合笔记保存异常: {e}")
        else:
            print(f"    ⚠️ [{parent_name}] 无 output_folder_guid 或 project_guid，跳过保存聚合笔记")
            print(f"    摘要内容预览:")
            print(merged_md[:2000])
            if len(merged_md) > 2000:
                print(f"\n... (共 {len(merged_md)} 字符)")

        card_parts = []
        for summary_info in summaries:
            dept_name = summary_info["dept_name"]
            summary_md = summary_info["summary_md"]
            print(f"    🤖 [{dept_name}] AI 二次总结生成卡片内容...")
            dept_card_content = generate_card_content(
                dept_name=dept_name,
                summary_md=summary_md,
                prompt_file_guid=CARD_PROMPT_FILE_GUID
            )
            card_parts.append(f"**{dept_name}**\n{dept_card_content}")

        merged_card_content = "\n\n".join(card_parts)
        card_title = f"{parent_name} 日报摘要 {target_date}"
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
                print(f"    📩 发送飞书卡片消息给 {len(receiver_guids)} 人 (含父部门leader)...")
                response = send_message_api(
                    receiver_guids=receiver_guids,
                    title=card_title,
                    content=text_content,
                    sender_guid=sender_guid,
                    interactive_content=card
                )
                if response.status_code == 200 and response.json().get("data"):
                    print(f"    ✅ 飞书卡片发送成功 [{parent_name}]")
                else:
                    print(f"    ❌ 飞书卡片发送失败 [{parent_name}]: {response.text}")
            except Exception as e:
                print(f"    ❌ 飞书卡片发送异常 [{parent_name}]: {e}")
        else:
            print(f"    ⚠️ 无接收人（父部门无leader且未配置message_receiver_guids），跳过飞书卡片发送")

print(f"\n{'=' * 60}")
print("全部摘要任务执行完毕")
print("=" * 60)

