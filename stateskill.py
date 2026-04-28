import builtins
import sys
import os

if not getattr(builtins.print, '_patched_flush', False):
    _original_print = builtins.print
    def print(*args, **kwargs):
        kwargs.setdefault('flush', True)
        _original_print(*args, **kwargs)
    print._patched_flush = True
    builtins.print = print

from datetime import datetime, timedelta
from zenv import get_zdkit_env
from zdbase import ZFile
import requests
import json
import time
import re
import uuid
import tempfile
import traceback
from collections import OrderedDict

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
USER_GUID = config.get("user_guid")
projects = config.get("projects", [])

# 默认生成类型：日报
generate_type = "briefing"

# =============================================================================
# API 路由
# =============================================================================
ACCESS_TOKEN_ROUTE = "/api/user/platform/getAccessToken"
NOTE_JSON_ROUTE = "/platform/ws/noteInfo/getDocJson"
DOC_TREE_ROUTE = "/platform/api/main/doc/treeList"
SIGNED_URL_ROUTE = "/platform/api/main/storage/getSignedUrl"

WORKSPACE_SAVE_ROUTE = "/middle/server/api/workspace/save"
MD_INSERT_ROUTE = "/middle/server/api/file/md/insert"
MESSAGE_SEND_ROUTE = "/middle/server/api/msg/send"

CONVERSATION_ID_ROUTE = "/platform/peerup_chatbot/conversation/id"
WORKFLOW_MODEL_ROUTE = "/platform/peerup_chatbot/workflow/model"
WORKFLOW_MODEL_RESULT_ROUTE = "/platform/peerup_chatbot/workflow/model/result"

# =============================================================================
# 默认业务参数
# =============================================================================
DEFAULT_LLM_PARAMS = {"temperature": 0.5, "max_tokens": 4096}
MESSAGE_TEMPLATE_ID = "80"
PLATFORM_TYPE = "all"

# =============================================================================
# [工具] 通用辅助函数
# =============================================================================
def get_headers_with_ak(user_guid="", doc_id=""):
    """获取带 Access-Token 的通用请求头"""
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


def get_note_json_content(user_guid="", doc_id=""):
    """获取笔记原始 JSON"""
    headers = get_headers_with_ak(user_guid=user_guid, doc_id=doc_id)
    response = requests.get(
        url=BASE_URL + NOTE_JSON_ROUTE,
        headers=headers,
        params={"docId": doc_id}
    )
    return response.json()


def strip_markdown_wrapper(content):
    """去除 AI 返回内容外层 markdown 代码块包裹"""
    content = content.strip()

    if content.startswith("```markdown"):
        content = content[len("```markdown"):].lstrip("\n")
    elif content.startswith("```"):
        content = content[3:].lstrip("\n")

    if content.endswith("```"):
        content = content[:-3].rstrip("\n")

    return content


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


def normalize_receiver_guids(receiver_guids_raw):
    """将接收人配置统一标准化为 list"""
    if isinstance(receiver_guids_raw, str):
        return [receiver_guids_raw]
    return receiver_guids_raw or []


def build_note_title(date_title, project_name):
    """生成日报笔记标题"""
    return f"{date_title} {project_name} 日报"


def build_message_text(note_title, note_url):
    """生成站内消息的文本内容"""
    return f"【{note_title}】已生成，请点击查看。\n<a href='{note_url}'>点击查看详情</a>"


def load_prompt_text(prompt_file_guid, default_prompt):
    """
    读取远端 prompt 文件内容；失败时回退到默认 prompt
    """
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


def get_target_date_info(generate_weekend=False):
    """
    获取日报目标日期：
    - 统一逻辑：每天默认回看前一天（无论是否周末）
    """
    now = datetime.now()
    days_ago = 1
    target_date = now - timedelta(days=days_ago)
    return {
        "date_str": target_date.strftime("%Y-%m-%d"),
        "date_title": target_date.strftime("%Y/%m/%d"),
        "week_str": f"第{target_date.isocalendar()[1]}周",
        "month_str": target_date.strftime("%Y-%m"),
    }


def build_intermediate_markdown_file(project_guid, target_date_str, markdown_content):
    """
    将 Step 1 生成的中间 Markdown 写入系统临时目录
    """
    tmp_dir = tempfile.gettempdir()
    unique_suffix = uuid.uuid4().hex[:8]
    file_name = f"daily_{project_guid}_{target_date_str.replace('-', '')}_{unique_suffix}.md"
    file_path = os.path.join(tmp_dir, file_name)

    with open(file_path, "w", encoding="utf-8") as output_fp:
        output_fp.write(markdown_content)

    return file_path


def cleanup_temp_files(file_paths, project_name=""):
    """
    清理 Step 1 生成的中间临时文件
    """
    if not file_paths:
        return

    for file_path in file_paths:
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
                if project_name:
                    print(f"[Cleanup][{project_name}] 🧹 已删除临时文件: {file_path}")
                else:
                    print(f"[Cleanup] 🧹 已删除临时文件: {file_path}")
        except Exception as e:
            if project_name:
                print(f"[Cleanup][{project_name}] ⚠️ 删除临时文件失败: {file_path}, error={e}")
            else:
                print(f"[Cleanup] ⚠️ 删除临时文件失败: {file_path}, error={e}")


def build_text_block(block_type, text, mentions=None):
    return {
        "type": block_type,
        "text": (text or "").strip(),
        "mentions": mentions or []
    }


def build_table_block(table_headers, table_rows):
    return {
        "type": "table",
        "headers": table_headers or [],
        "rows": table_rows or []
    }


def extract_text_from_block_container(block_container):
    """
    从一个 blockContainer 中提取可读文本
    """
    if not block_container or block_container.get("type") != "blockContainer":
        return ""

    text_parts = []

    for item in block_container.get("content", []):
        item_type = item.get("type")

        if item_type in ("paragraph", "heading", "fheading", "bulletListItem", "numberedListItem"):
            inline_content = item.get("content", [])
            text = ""

            for inline_item in inline_content:
                inline_type = inline_item.get("type")

                if inline_type == "text":
                    text += inline_item.get("text", "")
                elif inline_type == "mention":
                    attrs = inline_item.get("attrs", {})
                    label = attrs.get("label", "?")
                    uid = attrs.get("uid", "")
                    user_id = attrs.get("id", "")
                    text += f"[@{label}](mention:{uid}:{user_id})"
                elif inline_type == "mentionUrl":
                    attrs = inline_item.get("attrs", {})
                    content = attrs.get("content", "")
                    original_url = attrs.get("originalUrl", "")
                    uid = attrs.get("uid", "")
                    data_type = attrs.get("dataType", 1)
                    text += f"[{content}](mentionUrl:{uid}:{data_type}:{original_url})"

            if text.strip():
                text_parts.append(text.strip())

        elif item_type == "codeBlock":
            code_parts = []
            for code_item in item.get("content", []):
                if code_item.get("type") == "text":
                    code_parts.append(code_item.get("text", ""))
            code_text = "\n".join(code_parts).strip()
            if code_text:
                text_parts.append(code_text)

        elif item_type == "blockContainer":
            nested_text = extract_text_from_block_container(item)
            if nested_text.strip():
                text_parts.append(nested_text.strip())

    return " ".join(text_parts).strip()


def build_mention_markdown(person_info, fallback_text="未知成员"):
    """
    person_info -> [@姓名](mention:uid:id)
    """
    if not person_info:
        return fallback_text

    label = person_info.get("label") or fallback_text
    uid = person_info.get("uid", "")
    user_id = person_info.get("id", "")

    if uid and user_id:
        return f"[@{label}](mention:{uid}:{user_id})"

    return label


def build_note_link_markdown(note_guid, base_url):
    link_uid = str(uuid.uuid4())
    return f"[原笔记](mentionUrl:{link_uid}:1:{base_url}/workspace/{note_guid})"


def find_pm_person_info(note_entries, project_config):
    """
    从已解析成员中反查部门负责人，匹配不到则回退到 pm_name
    优先级：
    1) pm_guid 匹配笔记中的成员 → person_info
    2) pm_name 配置 → 构造 fallback person_info
    """
    pm_guids = set()
    raw_pms = project_config.get("pm_guid")

    if isinstance(raw_pms, list):
        pm_guids.update([x for x in raw_pms if x])
    elif isinstance(raw_pms, str) and raw_pms:
        pm_guids.add(raw_pms)

    for note_entry in note_entries:
        parsed_result = note_entry.get("parsed_result", {})
        for member in parsed_result.get("members", []):
            person_info = member.get("person_info", {})
            if person_info.get("id", "") in pm_guids:
                return person_info

    # pm_guid 匹配失败，回退到 pm_name
    pm_name = project_config.get("pm_name")
    if pm_name:
        return {"label": pm_name, "uid": "", "id": ""}

    return None

def build_step3_note_header_line(step1_meta):
    """
    构造 Step3 笔记正文开头的一行元信息：
    **日期**：2026-04-09 ｜ **部门负责人**：mention ｜ **原笔记链接**：link1；link2
    """
    target_date_str = step1_meta.get("target_date_str", "")
    pm_person_info = step1_meta.get("pm_person_info")
    note_entries = step1_meta.get("note_entries", [])

    pm_markdown = build_mention_markdown(pm_person_info, fallback_text="部门负责人未识别")

    note_links = []
    seen = set()
    for note_entry in note_entries:
        note_guid = note_entry.get("note_guid")
        if note_guid and note_guid not in seen:
            seen.add(note_guid)
            note_links.append(build_note_link_markdown(note_guid, BASE_URL))

    note_links_text = "；".join(note_links) if note_links else "无"

    return (
        f"**日期**：{target_date_str} ｜ "
        f"**部门负责人**：{pm_markdown} ｜ "
        f"**原笔记链接**：{note_links_text}"
    )

def prepend_step3_note_header(ai_contents, step1_meta):
    """
    给 Step2 输出的长总结统一加上 Step3 的头部行
    """
    header_line = build_step3_note_header_line(step1_meta)
    wrapped_contents = []

    for content in ai_contents:
        wrapped_contents.append(f"{header_line}\n\n{content}")

    return wrapped_contents

# =============================================================================
# [核心] JSON 解析引擎（新版：member -> projects -> sections）
# =============================================================================
class DailyReportParser:
    """
    输出结构：
    {
        "meta": {
            "project_name": "...",
            "date": "...",
            "week": "..."
        },
        "members": [
            {
                "person_info": {...},
                "projects": [
                    {
                        "project_name": "...",
                        "sections": {
                            "progress": [],
                            "issue_help": [],
                            "next_focus": []
                        }
                    }
                ]
            }
        ]
    }
    """

    CONTAINER_BLOCK_TYPES = {"blockContainer", "blockGroup"}
    META_BLOCK_TYPES = {"heading", "fheading", "title"}
    MEMBER_HEADER_BLOCK_TYPES = {"heading", "fheading"}
    CONTENT_BLOCK_TYPES = {"bulletListItem", "numberedListItem", "paragraph", "codeBlock"}

    def __init__(self, project_config):
        self.project_name = project_config.get("project_name", "Unknown")
        self.generate_weekend = project_config.get("generate_weekend", False)

        self.date_patterns = [
            re.compile(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})"),
            re.compile(r"(\d{4}年\d{1,2}月\d{1,2}日)")
        ]
        self.week_patterns = [
            re.compile(r"第\s*([0-9]+)\s*周", re.I),
            re.compile(r"Week\s*([0-9]+)", re.I),
            re.compile(r"W([0-9]+)", re.I)
        ]

    def _normalize_text(self, text):
        return (text or "").replace("\u200b", "").replace("\xa0", " ").strip()

    def extract_text_and_mentions(self, inline_content):
        if not inline_content:
            return "", []

        text_parts = []
        mentions = []

        for item in inline_content:
            item_type = item.get("type")

            if item_type == "text":
                text_parts.append(item.get("text", ""))

            elif item_type == "mention":
                attrs = item.get("attrs", {})
                mentions.append(dict(attrs))

                uid = attrs.get("uid", "")
                user_id = attrs.get("id", "")
                label = attrs.get("label", "?")
                text_parts.append(f"[@{label}](mention:{uid}:{user_id})")

            elif item_type == "mentionUrl":
                attrs = item.get("attrs", {})
                content = attrs.get("content", "")
                original_url = attrs.get("originalUrl", "")
                uid = attrs.get("uid", "")
                data_type = attrs.get("dataType", 1)
                text_parts.append(f"[{content}](mentionUrl:{uid}:{data_type}:{original_url})")

        return "".join(text_parts).strip(), mentions

    def _extract_codeblock_text(self, block):
        code_parts = []
        for item in block.get("content", []):
            if item.get("type") == "text":
                code_parts.append(item.get("text", ""))
        return self._normalize_text("\n".join(code_parts))

    def _extract_project_info(self, text, mentions=None):
        text = self._normalize_text(text)
        m = re.match(r"^📌\s*[\[\【](.*?)[\]\】]", text)
        if not m:
            return None, []

        project_name = m.group(1).strip()
        after_bracket = text[m.end():].strip()
        products = []

        # 1) 先提取 mention product，保留为真正的 mention markdown
        mention_pattern = re.compile(r"\[@([^\]]+)\]\(mention:([^:]+):([^)]+)\)")
        consumed_spans = []

        for match in mention_pattern.finditer(after_bracket):
            label = match.group(1).strip()
            uid = match.group(2).strip()
            user_id = match.group(3).strip()

            products.append(f"[@{label}](mention:{uid}:{user_id})")
            consumed_spans.append(match.span())

        # 2) 把 mention 片段从字符串里去掉，避免后面正则重复提取
        remaining = after_bracket
        if consumed_spans:
            pieces = []
            last_idx = 0
            for start, end in consumed_spans:
                pieces.append(remaining[last_idx:start])
                last_idx = end
            pieces.append(remaining[last_idx:])
            remaining = " ".join(pieces)

        # 3) 再提取普通文本 @V1 / @V1+ 这种
        plain_parts = re.findall(r"@([^\s@]+)", remaining)
        for part in plain_parts:
            part = part.strip()
            if part:
                products.append(part)

        # 4) 去重
        deduped = []
        seen = set()
        for p in products:
            if p and p not in seen:
                seen.add(p)
                deduped.append(p)

        return project_name, deduped

    def _normalize_section_name(self, text):
        """
        统一映射 section：
        - ✅今日主要进展      -> progress
        - ⚠️困难及所需支援    -> issue_help
        - 📝下一步计划        -> next_focus
        - 📝Next Key Focus    -> next_focus
        """
        text = self._normalize_text(text)
        text_no_colon = text.replace("：", "").replace(":", "").strip()

        if text_no_colon == "✅今日主要进展":
            return "progress"

        if text_no_colon == "⚠️困难及所需支援":
            return "issue_help"

        if text_no_colon == "📝下一步计划（Next Key Focus）":
            return "next_focus"

        if text_no_colon == "📝Next Key Focus":
            return "next_focus"
        
        if text_no_colon == "📝下一步计划":
            return "next_focus"

        return None

    def _create_empty_project(self, project_name, products=None):
        return {
            "project_name": project_name,
            "products": products or [],
            "sections": {
                "progress": [],
                "issue_help": [],
                "next_focus": []
            }
        }

    def _find_or_create_project(self, member_obj, project_name, products=None):
        for proj in member_obj["projects"]:
            if proj["project_name"] == project_name and proj["products"] == (products or []):
                return proj

        new_proj = self._create_empty_project(project_name, products)
        member_obj["projects"].append(new_proj)
        return new_proj

    def parse(self, raw_json_data):
        root_blocks = (
            raw_json_data.get("data", {}).get("content", [])
            or raw_json_data.get("content", [])
        )

        meta_info = {
            "project_name": self.project_name,
            "date": None,
            "week": None,
        }

        members = []
        current_member = None
        current_project = None
        # depth -> section_name mapping, tracks the most recent section at each depth
        section_stack = {}

        def parse_table(table_block):
            headers = []
            rows = []

            for row_index, row in enumerate(table_block.get("content", [])):
                if row.get("type") != "tableRow":
                    continue

                row_cells = []

                for cell in row.get("content", []):
                    cell_text = ""

                    if cell.get("type") in ("tableHeader", "tableCell"):
                        cell_blocks = cell.get("content", [])
                        extracted_parts = []

                        for sub_block in cell_blocks:
                            if sub_block.get("type") == "blockContainer":
                                part = extract_text_from_block_container(sub_block)
                                if part.strip():
                                    extracted_parts.append(part.strip())

                        cell_text = " ".join(extracted_parts).strip()

                    elif cell.get("type") == "blockContainer":
                        cell_text = extract_text_from_block_container(cell)

                    row_cells.append(cell_text)

                if row_index == 0:
                    headers = row_cells
                else:
                    rows.append(row_cells)

            if not headers and rows:
                headers = rows[0]
                rows = rows[1:]

            return build_table_block(headers, rows)

        def append_block_to_section(block_obj):
            nonlocal current_member, current_project, section_stack

            if not current_member or not current_project or not section_stack:
                return

            # Find the nearest section by looking from current depth upward
            item_depth = block_obj.get("depth", 0)
            section_name = None
            for d in range(item_depth, -1, -1):
                section_name = section_stack.get(d)
                if section_name:
                    break
            if not section_name:
                return

            current_project["sections"][section_name].append(block_obj)

        def ensure_context_defaults():
            nonlocal current_member, current_project

            if not current_member:
                return False

            if not current_project:
                current_project = self._find_or_create_project(current_member, "未分类项目")

            return True

        def traverse(blocks, depth=0):
            nonlocal current_member, current_project, section_stack

            for block in blocks:
                block_type = block.get("type")

                # 1) Container recursion (no depth increase)
                if block_type in self.CONTAINER_BLOCK_TYPES:
                    if "content" in block:
                        traverse(block["content"], depth)
                    continue

                # 2) table handling
                if block_type == "table":
                    table_block = parse_table(block)
                    table_block["depth"] = depth
                    if ensure_context_defaults():
                        append_block_to_section(table_block)
                    if "content" in block and isinstance(block["content"], list):
                        traverse(block["content"], depth)
                    continue

                # 3) codeBlock handling
                if block_type == "codeBlock":
                    code_text = self._extract_codeblock_text(block)
                    if code_text:
                        item = {
                            "type": "code",
                            "text": code_text,
                            "mentions": [],
                            "depth": depth
                        }
                        if ensure_context_defaults():
                            append_block_to_section(item)
                    # Recurse into codeBlock children at same depth
                    if "content" in block and isinstance(block["content"], list):
                        traverse(block["content"], depth)
                    continue

                inline_content = block.get("content", [])
                text, mentions = self.extract_text_and_mentions(inline_content)
                text = self._normalize_text(text)

                # 4) meta extraction
                if block_type in self.META_BLOCK_TYPES:
                    if not meta_info["date"]:
                        for pattern in self.date_patterns:
                            match = pattern.search(text)
                            if match:
                                meta_info["date"] = match.group(1)
                                break

                    if not meta_info["week"]:
                        for pattern in self.week_patterns:
                            match = pattern.search(text)
                            if match:
                                meta_info["week"] = f"第{match.group(1)}周"
                                break

                # 5) Member recognition: heading/fheading + mention
                if block_type in self.MEMBER_HEADER_BLOCK_TYPES and mentions:
                    person_info = mentions[0]
                    current_member = {
                        "person_info": person_info,
                        "projects": []
                    }
                    members.append(current_member)

                    current_project = None
                    section_stack.clear()
                    continue

                if not current_member:
                    # Still recurse into children to find nested members/projects
                    if "content" in block and isinstance(block["content"], list):
                        traverse(block["content"], depth)
                    continue

                # 6) Project recognition
                project_matched = False
                project_name, products = self._extract_project_info(text, mentions)
                if (
                    project_name
                    and current_member
                    and block_type in ("bulletListItem", "paragraph", "heading", "fheading")
                ):
                    current_project = self._find_or_create_project(current_member, project_name, products)
                    section_stack.clear()
                    project_matched = True

                # 7) Section recognition: only for list items / paragraphs at current nesting level
                section_matched = False
                section_name = self._normalize_section_name(text)
                if section_name and block_type in ("bulletListItem", "numberedListItem", "paragraph"):
                    if not current_project:
                        current_project = self._find_or_create_project(current_member, "未分类项目")
                    section_stack[depth] = section_name
                    section_matched = True

                # 8) Content items: bulletListItem / numberedListItem / paragraph
                if block_type in ("bulletListItem", "numberedListItem", "paragraph"):
                    clean_text = re.sub(r"^[\d]+\.[\s]*|^[*-]\s*", "", text).strip()

                    # Skip pure section markers (headers with no real content beyond the section name)
                    if section_matched and clean_text == text:
                        # Recurse into children for potential nested content
                        if "content" in block and isinstance(block["content"], list):
                            child_depth = depth + 1 if block_type in ("bulletListItem", "numberedListItem") else depth
                            traverse(block["content"], child_depth)
                        continue

                    if block_type == "bulletListItem":
                        normalized_block_type = "bullet"
                    elif block_type == "numberedListItem":
                        normalized_block_type = "numbered"
                    else:
                        normalized_block_type = "paragraph"

                    if clean_text:
                        item = build_text_block(
                            block_type=normalized_block_type,
                            text=clean_text,
                            mentions=mentions
                        )
                        item["depth"] = depth
                        if ensure_context_defaults():
                            append_block_to_section(item)
                    # Non-section markers with no content still need child recursion
                    elif "content" in block and isinstance(block["content"], list):
                        child_depth = depth + 1 if block_type in ("bulletListItem", "numberedListItem") else depth
                        traverse(block["content"], child_depth)
                    continue

        traverse(root_blocks)

        if not meta_info["date"]:
            if self.generate_weekend:
                fallback_days_ago = 1
            else:
                fallback_days_ago = 3 if datetime.now().weekday() == 0 else 1

            fallback_date = datetime.now() - timedelta(days=fallback_days_ago)
            meta_info["date"] = fallback_date.strftime("%Y-%m-%d")

        return {
            "meta": meta_info,
            "members": members
        }


# =============================================================================
# Step 1: 查找与解析原始日报
# =============================================================================
def find_daily_note(user_guid, project_guid, folder_guid, target_date_str):
    """
    在指定目录下查找包含目标日期的日报笔记
    """
    response = requests.post(
        url=BASE_URL + DOC_TREE_ROUTE,
        headers=get_headers_with_ak(user_guid=user_guid),
        json={"projectGuid": project_guid, "parentGuid": folder_guid}
    )
    note_list = response.json().get("data")

    if not note_list:
        return None

    date_variants = [
        target_date_str,
        target_date_str.replace("-", "/"),
        target_date_str.replace("-", "."),
    ]

    for note in note_list:
        note_title = note.get("dataTitle", "")
        if any(date_variant in note_title for date_variant in date_variants):
            return {
                "categoryGuid": note.get("categoryGuid"),
                "dataTitle": note.get("dataTitle", "")
            }

    return None


def aggregate_parsed_note_entries(note_entries):
    aggregated = {
        "progress": OrderedDict(),
        "issue_help": OrderedDict(),
        "next_focus": OrderedDict()
    }

    for note_entry in note_entries:
        note_guid = note_entry["note_guid"]
        parsed_result = note_entry["parsed_result"]

        for member in parsed_result.get("members", []):
            person_info = member.get("person_info", {})
            member_md = build_mention_markdown(person_info, fallback_text="未知成员")

            for project in member.get("projects", []):
                project_name = (project.get("project_name") or "未分类项目").strip()
                products = project.get("products") or []
                sections = project.get("sections", {})

                products_tuple = tuple(products)

                for section_key in ("progress", "issue_help", "next_focus"):
                    if project_name not in aggregated[section_key]:
                        aggregated[section_key][project_name] = OrderedDict()

                    member_key = (member_md, products_tuple)
                    if member_key not in aggregated[section_key][project_name]:
                        aggregated[section_key][project_name][member_key] = []

                    for item in sections.get(section_key, []):
                        item_copy = dict(item)
                        item_copy["note_guid"] = note_guid
                        aggregated[section_key][project_name][member_key].append(item_copy)

    return aggregated


def render_table_markdown(headers, rows):
    if not headers and not rows:
        return []

    if not headers and rows:
        max_cols = max(len(row) for row in rows) if rows else 1
        headers = [f"列{i+1}" for i in range(max_cols)]

    col_count = len(headers)
    normalized_rows = []
    for row in rows:
        row = row[:col_count] + [""] * max(0, col_count - len(row))
        normalized_rows.append(row)

    lines = []
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
    for row in normalized_rows:
        lines.append("| " + " | ".join(row) + " |")

    return lines


def render_grouped_section_markdown(section_title, project_map):
    """
    渲染：
    # 今日核心进展
    ## 📌 用户认证模块
    ### V1 & V2
    - @王五 完成了数据迁移
    ### V1
    - @张三 完成了登录接口开发
    ### V2
    - @李四 完成了权限校验测试
    - @赵六 完成了文档编写
    """
    lines = [f"# {section_title}", ""]
    has_any = False

    for project_name, member_map in project_map.items():
        project_has_content = any(member_map.values())
        if not project_has_content:
            continue

        has_any = True
        lines.append(f"## 📌 {project_name}")

        products_groups = OrderedDict()
        for (member_md, products_tuple), items in member_map.items():
            if not items:
                continue
            if products_tuple not in products_groups:
                products_groups[products_tuple] = []
            products_groups[products_tuple].append((member_md, items))

        sorted_groups = sorted(
            products_groups.items(),
            key=lambda x: (-len(x[0]), x[0])
        )

        for products_tuple, entries in sorted_groups:
            if products_tuple:
                header = " & ".join(products_tuple)
            else:
                header = "无标签"
            lines.append(f"### {header}")

            for member_md, items in entries:
                for item in items:
                    item_type = item.get("type", "paragraph")
                    text = (item.get("text") or "").strip()
                    depth = item.get("depth", 0)
                    indent = "    " * depth  # 4 spaces per level

                    if item_type == "table":
                        lines.append(f"{indent}- {member_md} [表格内容]")
                        headers = item.get("headers", [])
                        rows = item.get("rows", [])
                        table_lines = render_table_markdown(headers, rows)
                        for tl in table_lines:
                            lines.append(f"{indent}    {tl}")
                    elif item_type == "code":
                        code_text = text.replace("\r\n", "\n").strip()
                        if code_text:
                            lines.append(f"{indent}- {member_md} 代码块：")
                            lines.append(f"{indent}```")
                            lines.append(code_text)
                            lines.append(f"{indent}```")
                    else:
                        if text:
                            text_single_line = text.replace("\n", " / ").strip()
                            lines.append(f"{indent}- {member_md} {text_single_line}")

            lines.append("")

    if not has_any:
        lines.append("- 暂无")
        lines.append("")

    return "\n".join(lines).rstrip()


def build_merged_daily_markdown(project_name, target_date_str, note_entries, project_config):
    """
    新版 merged markdown：
    - 顶部保留日期与部门负责人
    - 每个 section/project 按 member 合并
    - 正文开头增加：
      **部门负责人：mention | 原笔记链接：...**
    """
    pm_person_info = find_pm_person_info(note_entries, project_config)
    pm_markdown = build_mention_markdown(pm_person_info, fallback_text="部门负责人未识别")

    # 原笔记链接去重后统一列出来
    note_links = []
    seen = set()
    for note_entry in note_entries:
        note_guid = note_entry["note_guid"]
        if note_guid not in seen:
            seen.add(note_guid)
            note_links.append(build_note_link_markdown(note_guid, BASE_URL))

    note_links_text = "；".join(note_links) if note_links else "无"

    aggregated = aggregate_parsed_note_entries(note_entries)

    merged_parts = [
        f"# 📅 {project_name} 日报汇总",
        f"**日期**：{target_date_str}",
        f"**部门负责人**：{pm_markdown} | **原笔记链接**：{note_links_text}",
        "",
        "---",
        "",
        render_grouped_section_markdown("今日核心进展", aggregated["progress"]),
        "",
        render_grouped_section_markdown("困难及所需支援", aggregated["issue_help"]),
        "",
        render_grouped_section_markdown("下一步计划", aggregated["next_focus"]),
        ""
    ]

    return "\n".join(merged_parts)


def step1_summary_note(project):
    """
    Step 1:
    - 根据目标日期查找项目日报
    - 解析原始 JSON
    - 合并为一份适合输入 LLM 的中间 Markdown
    """
    generated_files = []

    try:
        project_name = project["project_name"]
        project_guid = project["project_guid"]
        work_log_folder_guid = project["work_log_folder_guid"]
        project_user_guids = project.get(
            "user_guid_list",
            [project.get("user_guid") or project.get("leader_guid")]
        )

        date_info = get_target_date_info(
            generate_weekend=project.get("generate_weekend", False)
        )
        target_date_str = date_info["date_str"]

        print(f"[Step 1][{project_name}] 目标日期: {target_date_str}")

        matched_notes = []

        for user_guid in project_user_guids:
            if not user_guid:
                continue

            note_info = find_daily_note(
                user_guid=user_guid,
                project_guid=project_guid,
                folder_guid=work_log_folder_guid,
                target_date_str=target_date_str
            )

            if note_info:
                matched_notes.append({
                    "user_guid": user_guid,
                    "note_guid": note_info["categoryGuid"],
                    "note_title": note_info["dataTitle"]
                })

        if not matched_notes:
            print(f"[Step 1][{project_name}] ❌ 未找到笔记")
            return [], False, [], {}

        print(f"[Step 1][{project_name}] ✅ 找到 {len(matched_notes)} 份笔记，解析中...")

        parser = DailyReportParser(project)
        parsed_note_entries = []

        for matched_note in matched_notes:
            user_guid = matched_note["user_guid"]
            note_guid = matched_note["note_guid"]

            raw_json = get_note_json_content(user_guid=user_guid, doc_id=note_guid)
            parsed_result = parser.parse(raw_json)

            parsed_note_entries.append({
                "note_guid": note_guid,
                "note_title": matched_note.get("note_title", ""),
                "parsed_result": parsed_result
            })

        merged_markdown = build_merged_daily_markdown(
            project_name=project_name,
            target_date_str=target_date_str,
            note_entries=parsed_note_entries,
            project_config=project
        )

        intermediate_file_path = build_intermediate_markdown_file(
            project_guid=project_guid,
            target_date_str=target_date_str,
            markdown_content=merged_markdown
        )

        print(f"[Step 1][{project_name}] 📝 中间文件已生成: {intermediate_file_path}")

        generated_files.append(intermediate_file_path)

        step1_meta = {
            "pm_person_info": find_pm_person_info(parsed_note_entries, project),
            "target_date_str": target_date_str,
            "note_entries": parsed_note_entries
        }

        return [
            ZFile(
                path=intermediate_file_path,
                source_name=os.path.basename(intermediate_file_path)
            )
        ], True, generated_files, step1_meta

    except Exception as e:
        print(f"[Step 1] ❌ 发生异常: {e}")
        traceback.print_exc()
        return [], False, [], {}


# =============================================================================
# Step 2: 调用 LLM 生成详细总结
# =============================================================================
def _create_chat_id(conversation_id="", id_type="conversation"):
    response = requests.post(
        BASE_URL + CONVERSATION_ID_ROUTE,
        headers=get_headers_with_ak(),
        json={"conversation_id": conversation_id, "type": id_type}
    )
    response_json = response.json()
    return response_json.get("data").get("id")


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
        response_json = response.json()
        data = response_json.get("data", {})

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
            "llm_config": {
                "llm_name": llm_name,
                "llm_params": llm_params
            },
            "context_messages": context_messages
        }
    )

    response_json = response.json()
    task_message_id = response_json.get("data", {}).get("message_id")

    if not task_message_id:
        raise Exception("No task ID")

    return poll_workflow_result(task_message_id)


def _call_llm_with_retry(llm_name, llm_params, context_messages, max_retries=10):
    attempt = 0
    last_error = None

    while attempt < max_retries:
        try:
            print(f"  🔄 [尝试 {attempt + 1}/{max_retries}] 调用 AI 工作流...")

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
                print(f"  ⚠️ AI 调用失败: {e}. {wait_time}秒后重试...")
                time.sleep(wait_time)
            else:
                print(f"  ❌ AI 调用连续 {max_retries} 次失败，放弃重试。错误: {e}")
                raise last_error


def step2_llm_process(md_file_list, project):
    """
    Step 2:
    - 读取 Step 1 生成的中间 Markdown
    - 调用 LLM 生成详细日报总结
    """
    try:
        project_name = project.get("project_name", "")
        prompt_file_guid = project.get("briefing_prompt_file_guid")

        print(f"[Step 2][{project_name}] 正在调用 AI 生成详细报告...")

        default_prompt = "请详细总结以下日报内容，保留关键数据和人员提及。\n{{markdown_content}}"
        prompt_text = load_prompt_text(prompt_file_guid, default_prompt)
        final_prompt = f"项目背景：{project_name}。\n{prompt_text}"

        llm_results = []

        for md_file in md_file_list:
            with open(md_file.path, "r", encoding="utf-8") as md_fp:
                markdown_content = md_fp.read()

            user_content = final_prompt.replace("{{markdown_content}}", markdown_content)

            context_messages = [
                {
                    "role": "system",
                    "content": "你是专业的日报汇总助手，请输出 Markdown 格式。",
                    "variables": []
                },
                {
                    "role": "user",
                    "content": user_content,
                    "variables": []
                }
            ]

            print(f"[Step 2] 当前输入内容长度: {len(markdown_content)} 字符")

            try:
                llm_result = _call_llm_with_retry(
                    llm_name=model.llm_name,
                    llm_params=model.llm_params,
                    context_messages=context_messages,
                    max_retries=10
                )
                llm_results.append(strip_markdown_wrapper(llm_result))
            except Exception as retry_err:
                raise Exception(f"项目 {project_name} 的 AI 生成在重试后仍失败: {retry_err}")

        print(f"[Step 2][{project_name}] ✅ AI 详细报告生成完成")
        return ["\n\n".join(llm_results)]

    except Exception as e:
        print(f"[Step 2] ❌ 发生异常: {e}")
        traceback.print_exc()
        return []


# =============================================================================
# Step 2.5: 二次调用 AI 生成卡片摘要
# =============================================================================
def generate_card_content(project, long_markdown):
    """
    对长内容进行二次摘要，生成适合飞书卡片展示的短摘要
    """
    project_name = project.get("project_name", "")
    card_prompt_file_guid = project.get(f"{generate_type}_card_prompt_guid")

    default_prompt = config.get(
        "card_prompt_default",
        "请将以下内容 {{markdown_content}} 整理为简洁的飞书消息卡片正文。"
        "格式要求：禁止使用任何标题语法（#、##），全部使用正文；仅必要时用加粗（**关键词**）强调；"
        "使用项目符号（•）组织内容；重点突出、不超过 300 字。"
    )

    prompt_text = load_prompt_text(card_prompt_file_guid, default_prompt)

    def fallback_format_content(content, max_len=20000):
        header_pattern = r"\*\*日期：\*\*\s*(\d{4}-\d{2}-\d{2}).*?$"

        def replace_header(match):
            date_str = match.group(1)
            return f"**项目进展摘要 | {date_str}**"

        content = re.sub(header_pattern, replace_header, content, flags=re.MULTILINE)

        h3_pattern = r"^###\s+(.+?)\s*$"

        def replace_h3(match):
            title_text = match.group(1).strip()
            return f"**{title_text}**"

        content = re.sub(h3_pattern, replace_h3, content, flags=re.MULTILINE)

        if len(content) > max_len:
            truncated = content[:max_len]
            suffix = "\n\n......\n[系统提示：AI 生成失败，此为自动截断的格式化预览]"
            return truncated + suffix
        return content

    user_content = prompt_text.replace("{{markdown_content}}", long_markdown[:8000])

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
        print(f"⚠️ [Step 2.5][{project_name}] AI 生成在 10 次重试后仍失败 (Error: {e})")
        print("   -> 切换至格式化截断兜底模式")
        return fallback_format_content(long_markdown, max_len=20000)


# =============================================================================
# Step 3: 创建笔记并写入 AI 总结
# =============================================================================
def insert_markdown_to_note(user_guid, note_guid, markdown_content):
    clean_content = strip_markdown_wrapper(markdown_content)
    html_content = _convert_special_nodes(clean_content)

    response = requests.post(
        BASE_URL + MD_INSERT_ROUTE,
        headers=get_headers_with_ak(user_guid=user_guid),
        json={
            "note_guid": note_guid,
            "markdown_content": html_content,
            "mode": "w",
            "location": 1
        }
    )

    if response.status_code != 200:
        raise Exception(f"写入笔记失败: {response.text}")

    return response.json()


def create_note_api(content, title, project_guid, parent_guid, tags, creator_guid=None):
    creator_guid = creator_guid or USER_GUID
    headers = get_headers_with_ak()
    headers["X-User-GUID"] = creator_guid

    if not project_guid:
        raise ValueError("briefing_target_project_guid 不能为空！")

    response = requests.post(
        BASE_URL + WORKSPACE_SAVE_ROUTE,
        headers=headers,
        json={
            "project_guid": project_guid,
            "parent_guid": parent_guid,
            "target": {
                "name": title,
                "type": 1,
                "tags": tags
            },
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


def step3_generate_notes(contents, project, step1_meta=None):
    """
    Step 3:
    - 创建 AI 日报笔记
    - 将 Step 2 的长总结写入笔记
    - 笔记正文开头加上 header 行
    """
    try:
        project_name = project.get("project_name", "")
        date_info = get_target_date_info(
            generate_weekend=project.get("generate_weekend", False)
        )

        target_project_guid = project.get("briefing_target_project_guid")
        target_parent_guid = project.get("briefing_target_parent_guid", "0")
        target_user_guid = project.get("briefing_target_user_guid")

        if not target_project_guid:
            raise ValueError(
                f"配置错误: project '{project_name}' 的 briefing_target_project_guid 为空！"
            )

        print(f"[Step 3][{project_name}] 正在创建笔记...")

        note_urls = []
        note_titles = []

        # 构建 header 行
        header_line = build_step3_note_header_line(step1_meta) if step1_meta else ""

        for content in contents:
            # ✅ 移除 AI 生成内容中已有的 header 行
            cleaned_content = content
            
            # 移除首行如果是日期/部门负责人/原笔记链接的格式
            lines = content.split("\n")
            if lines and re.match(r".*\d{4}-\d{2}-\d{2}.*[|｜].*", lines[0]):
                # 跳过第一行（已有的 header）
                cleaned_content = "\n".join(lines[1:]).lstrip("\n")
            
            title = build_note_title(date_info["date_title"], project_name)
            
            # 将新的 header 行插入到清理后的正文开头
            final_content = f"{header_line}\n\n{cleaned_content}" if header_line else cleaned_content

            doc_id = create_note_api(
                content=final_content,
                title=title,
                project_guid=target_project_guid,
                parent_guid=target_parent_guid,
                tags=["日报", "AI"],
                creator_guid=target_user_guid
            )

            if doc_id:
                note_urls.append(f"{BASE_URL}/workspace/{doc_id}")
                note_titles.append(title)

        print(f"[Step 3][{project_name}] ✅ 笔记创建完成")
        return note_urls, note_titles

    except Exception as e:
        print(f"[Step 3] ❌ 发生异常: {e}")
        traceback.print_exc()
        return [], []

# =============================================================================
# Step 4: 发送消息
# =============================================================================

def send_webhook(webhook_url, card, max_retries=3, retry_interval=5):
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(
                url=webhook_url,
                headers={"Content-Type": "application/json"},
                json={"msg_type": "interactive", "card": card},
                timeout=10
            )
            result = response.json()

            if result.get("code") == 0 or result.get("StatusCode") == 0:
                return result

            if attempt < max_retries:
                print(f"  -> ⚠️ Webhook 发送失败 (尝试 {attempt}/{max_retries}): {result}，{retry_interval}秒后重试...")
                time.sleep(retry_interval)
            else:
                return result
        except Exception as e:
            if attempt < max_retries:
                print(f"  -> ⚠️ Webhook 发送异常 (尝试 {attempt}/{max_retries}): {e}，{retry_interval}秒后重试...")
                time.sleep(retry_interval)
            else:
                raise

    return {"code": -1, "msg": "max retries exceeded"}


def send_message_api(receiver_guids, title, content, sender_guid="", interactive_content=None, max_retries=3, retry_interval=5):
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

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(
                url=BASE_URL + MESSAGE_SEND_ROUTE,
                headers=get_headers_with_ak(user_guid=sender_guid),
                json=payload,
                timeout=10
            )

            if response.status_code == 200 and response.json().get("data"):
                return response

            if attempt < max_retries:
                print(f"  -> ⚠️ 个人消息发送失败 (尝试 {attempt}/{max_retries}): {response.text}，{retry_interval}秒后重试...")
                time.sleep(retry_interval)
            else:
                return response
        except Exception as e:
            if attempt < max_retries:
                print(f"  -> ⚠️ 个人消息发送异常 (尝试 {attempt}/{max_retries}): {e}，{retry_interval}秒后重试...")
                time.sleep(retry_interval)
            else:
                raise

    return None


def build_card_header_line(project, step1_meta=None):
    date_info = get_target_date_info(
        generate_weekend=project.get("generate_weekend", False)
    )
    current_date = date_info["date_str"]

    pm_person_info = None
    if step1_meta:
        pm_person_info = step1_meta.get("pm_person_info")

    pm_markdown = build_mention_markdown(pm_person_info, fallback_text="部门负责人未识别")

    return f"**项目进展摘要 | {current_date} | 部门负责人：{pm_markdown}**"


def build_feishu_card(title, card_content, note_url, source_note_urls=None):
    """
    构造飞书卡片，包含"查看源笔记"和"查看AI总结"两个按钮
    
    Args:
        title: 卡片标题
        card_content: 卡片正文内容
        note_url: AI总结笔记的URL (查看AI总结按钮)
        source_note_urls: 源笔记的URL列表 (查看源笔记按钮)
    """
    # 提取源笔记URL
    source_url = None
    if source_note_urls:
        if isinstance(source_note_urls, str):
            source_url = source_note_urls
        elif isinstance(source_note_urls, list) and source_note_urls:
            source_url = source_note_urls[0]
    
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
                            "width": "weighted",
                            "weight": 1,
                            "elements": [
                                {
                                    "tag": "button",
                                    "type": "primary_filled",
                                    "width": "fill",
                                    "margin": "4px 0px 4px 0px",
                                    "text": {
                                        "tag": "plain_text",
                                        "content": "查看源笔记"
                                    },
                                    "behaviors": [
                                        {
                                            "type": "open_url",
                                            "default_url": source_url if source_url else note_url
                                        }
                                    ]
                                }
                            ]
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [
                                {
                                    "tag": "button",
                                    "type": "secondary",
                                    "width": "fill",
                                    "margin": "4px 0px 4px 0px",
                                    "text": {
                                        "tag": "plain_text",
                                        "content": "查看AI总结"
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

def step4_send_messages(note_url_list, note_title_list, project, content_list, step1_meta=None):
    """
    Step 4:
    - 对长日报做二次摘要
    - 构造飞书卡片（包含源笔记和AI总结两个按钮）
    - 卡片正文开头增加：
      **项目进展摘要 | current_date | 部门负责人：mention**
    """
    try:
        project_name = project.get("project_name", "")

        raw_webhook_config = project.get(f"{generate_type}_webhook_url", [])

        if isinstance(raw_webhook_config, str):
            webhook_urls = [raw_webhook_config]
        elif isinstance(raw_webhook_config, list):
            webhook_urls = raw_webhook_config
        else:
            webhook_urls = []

        receiver_guids = normalize_receiver_guids(
            project.get(f"{generate_type}_sender_guid", [])
        )
        sender_guid = project.get(f"{generate_type}_target_user_guid", "") or USER_GUID

        if not note_url_list:
            print(f"[Step 4][{project_name}] ⚠️ 没有 URL 可发送")
            return

        card_header_line = build_card_header_line(project, step1_meta=step1_meta)
        
        # 提取源笔记的URL列表
        source_note_urls = []
        if step1_meta:
            note_entries = step1_meta.get("note_entries", [])
            seen = set()
            for note_entry in note_entries:
                note_guid = note_entry.get("note_guid")
                if note_guid and note_guid not in seen:
                    seen.add(note_guid)
                    source_note_urls.append(f"{BASE_URL}/workspace/{note_guid}")

        for note_title, note_url, full_content in zip(note_title_list, note_url_list, content_list):
            card_summary = generate_card_content(project, full_content)
            final_card_content = f"{card_header_line}\n\n{card_summary}"
            
            # 传入源笔记URL列表
            card = build_feishu_card(
                note_title, 
                final_card_content, 
                note_url,
                source_note_urls=source_note_urls
            )

            has_sent_any = False

            if webhook_urls:
                for idx, url in enumerate(webhook_urls, 1):
                    try:
                        print(f"[Step 4][{project_name}] 📢 正在发送群消息 (Webhook {idx}/{len(webhook_urls)})...")
                        webhook_result = send_webhook(url, card)

                        if webhook_result.get("code") == 0 or webhook_result.get("StatusCode") == 0:
                            print(f"  -> ✅ 群消息发送成功: {url[:30]}...")
                            has_sent_any = True
                        else:
                            print(f"  -> ❌ 群消息发送失败（已重试3次）({url}): {webhook_result}")
                    except Exception as e:
                        print(f"  -> ❌ 群消息发送异常（已重试3次）({url}): {e}")
            else:
                print(f"[Step 4][{project_name}] 📢 未配置 Webhook 地址，跳过群消息发送")

            if receiver_guids:
                try:
                    print(f"[Step 4][{project_name}] 📩 正在发送个人消息给 {len(receiver_guids)} 人...")
                    text_content = build_message_text(note_title, note_url)
                    response = send_message_api(
                        receiver_guids=receiver_guids,
                        title=note_title,
                        content=text_content,
                        sender_guid=sender_guid,
                        interactive_content=card
                    )
                    if response and response.status_code == 200 and response.json().get("data"):
                        print("  -> ✅ 个人消息发送成功")
                        has_sent_any = True
                    else:
                        print(f"  -> ❌ 个人消息发送失败（已重试3次）: {response.text if response else '无响应'}")
                except Exception as e:
                    print(f"  -> ❌ 个人消息发送异常（已重试3次）: {e}")

            if not has_sent_any and not webhook_urls and not receiver_guids:
                print(f"[Step 4][{project_name}] ⚠️ 未配置 Webhook 且未配置接收人，跳过发送步骤")

        print(f"[Step 4][{project_name}] ✅ 消息分发流程结束")

    except Exception as e:
        print(f"[Step 4] ❌ 发生异常: {e}")
        traceback.print_exc()

# =============================================================================
# 主执行流程
# =============================================================================
print("=" * 60)
print(f"开始执行日报工作流 | 项目数: {len(projects)}")
print("=" * 60)

for project in projects:
    project_name = project.get("project_name", "Unknown")

    enable_ai = project.get("enable_briefing_summary", True)
    if not enable_ai:
        print(f"\n⏭ 跳过项目: {project_name} (enable_briefing_summary=False)")
        continue

    print(f"\n▶ 处理项目: {project_name}")

    temp_files = []

    try:
        md_files, found, temp_files, step1_meta = step1_summary_note(project)
        if not found:
            print(f"  ⚠️ 跳过 {project_name}")
            continue

        ai_contents = step2_llm_process(md_files, project)

        cleanup_temp_files(temp_files, project_name=project_name)

        if not ai_contents:
            raise Exception("AI 生成内容为空")
        
        final_note_contents = prepend_step3_note_header(ai_contents, step1_meta)

        note_urls, note_titles = step3_generate_notes(ai_contents, project, step1_meta=step1_meta)

        step4_send_messages(
            note_urls,
            note_titles,
            project,
            ai_contents,
            step1_meta=step1_meta
        )

        print(f"✅ {project_name} 流程结束")

    except Exception as e:
        cleanup_temp_files(temp_files, project_name=project_name)

        print(f"❌ {project_name} 流程中断: {e}")
        traceback.print_exc()

print("\n" + "=" * 60)
print("全部任务执行完毕")
print("=" * 60)
