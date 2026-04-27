import builtins
import sys
import os
import requests
import json
import time
import re
import uuid
import tempfile
import traceback

from datetime import datetime, timedelta
from collections import OrderedDict
from zenv import get_zdkit_env
from zdbase import ZFile


# =============================================================================
# print flush patch
# =============================================================================
if not getattr(builtins.print, "_patched_flush", False):
    _original_print = builtins.print

    def print(*args, **kwargs):
        kwargs.setdefault("flush", True)
        _original_print(*args, **kwargs)

    print._patched_flush = True
    builtins.print = print


# =============================================================================
# Runtime Config
# =============================================================================
class RuntimeConfig:
    def __init__(self, config_file):
        zenv_obj = get_zdkit_env()
        self.base_url = zenv_obj.zdkit._http_client.config.get("url")

        try:
            with open(config_file.path, "r", encoding="utf-8") as config_fp:
                self.raw = json.load(config_fp)
        except Exception as e:
            print(f"❌ 配置文件读取失败: {e}")
            raise

        self.ak = self.raw.get("ak")
        self.sk = self.raw.get("sk")
        self.org_guid = self.raw.get("org_guid")
        self.user_guid = self.raw.get("user_guid")
        self.projects = self.raw.get("projects", [])

        self.generate_type = self.raw.get("generate_type", "briefing")

        self.default_llm_params = {"temperature": 0.5, "max_tokens": 4096}
        self.message_template_id = self.raw.get("message_template_id", "80")
        self.platform_type = self.raw.get("platform_type", "all")


# =============================================================================
# Platform Client
# =============================================================================
class PlatformClient:
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

    def __init__(self, runtime: RuntimeConfig):
        self.runtime = runtime
        self.base_url = runtime.base_url
        self.ak = runtime.ak
        self.sk = runtime.sk
        self.user_guid = runtime.user_guid
        self.org_guid = runtime.org_guid
        self.message_template_id = runtime.message_template_id
        self.platform_type = runtime.platform_type

    def get_headers(self, user_guid="", doc_id=""):
        response = requests.post(
            url=self.base_url + self.ACCESS_TOKEN_ROUTE,
            json={"ak": self.ak, "sk": self.sk}
        )
        response_json = response.json()

        if not response_json.get("data"):
            raise Exception(f"获取 AccessToken 失败: {response_json}")

        access_token = response_json["data"].get("accessToken")

        headers = {
            "Access-Token": access_token,
            "ak": self.ak,
            "X-User-GUID": user_guid or self.user_guid,
        }

        if doc_id:
            headers["docId"] = doc_id

        return headers

    def get_note_json(self, user_guid="", doc_id=""):
        response = requests.get(
            url=self.base_url + self.NOTE_JSON_ROUTE,
            headers=self.get_headers(user_guid=user_guid, doc_id=doc_id),
            params={"docId": doc_id}
        )
        return response.json()

    def list_doc_tree(self, user_guid, project_guid, parent_guid):
        response = requests.post(
            url=self.base_url + self.DOC_TREE_ROUTE,
            headers=self.get_headers(user_guid=user_guid),
            json={"projectGuid": project_guid, "parentGuid": parent_guid}
        )
        return response.json().get("data") or []

    def load_prompt_text(self, prompt_file_guid, default_prompt):
        if not prompt_file_guid:
            return default_prompt

        try:
            signed_url_response = requests.get(
                self.base_url + self.SIGNED_URL_ROUTE,
                headers=self.get_headers(),
                params={"categoryGuid": prompt_file_guid}
            )
            signed_url = (signed_url_response.json().get("data") or {}).get("signedUrl")

            if not signed_url:
                return default_prompt

            return requests.get(signed_url, timeout=10).text

        except Exception:
            return default_prompt

    def _create_chat_id(self, conversation_id="", id_type="conversation"):
        response = requests.post(
            self.base_url + self.CONVERSATION_ID_ROUTE,
            headers=self.get_headers(),
            json={"conversation_id": conversation_id, "type": id_type}
        )
        response_json = response.json()
        return response_json.get("data", {}).get("id")

    def create_conversation_id(self):
        return self._create_chat_id("", "conversation")

    def create_message_id(self, conversation_id):
        return self._create_chat_id(conversation_id, "message")

    def poll_workflow_result(self, message_id, max_retries=120, interval=3):
        for _ in range(max_retries):
            response = requests.post(
                self.base_url + self.WORKFLOW_MODEL_RESULT_ROUTE,
                headers=self.get_headers(),
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

    def call_workflow_model(self, message_id, llm_name, llm_params, context_messages):
        response = requests.post(
            self.base_url + self.WORKFLOW_MODEL_ROUTE,
            headers=self.get_headers(),
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
            raise Exception(f"No task ID: {response_json}")

        return self.poll_workflow_result(task_message_id)

    def call_llm_with_retry(self, llm_name, llm_params, context_messages, max_retries=10):
        attempt = 0
        last_error = None

        while attempt < max_retries:
            try:
                print(f"  🔄 [尝试 {attempt + 1}/{max_retries}] 调用 AI 工作流...")

                conversation_id = self.create_conversation_id()
                message_id = self.create_message_id(conversation_id)

                return self.call_workflow_model(
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

    def insert_markdown_to_note(self, user_guid, note_guid, markdown_content, convert_special=True):
        clean_content = strip_markdown_wrapper(markdown_content)

        if convert_special:
            html_content = convert_special_nodes(clean_content)
        else:
            html_content = clean_content

        response = requests.post(
            self.base_url + self.MD_INSERT_ROUTE,
            headers=self.get_headers(user_guid=user_guid),
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

    def create_note(self, content, title, project_guid, parent_guid, tags, creator_guid=None, convert_special=True):
        creator_guid = creator_guid or self.user_guid

        if not project_guid:
            raise ValueError("target_project_guid 不能为空！")

        headers = self.get_headers()
        headers["X-User-GUID"] = creator_guid

        response = requests.post(
            self.base_url + self.WORKSPACE_SAVE_ROUTE,
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
            self.insert_markdown_to_note(
                user_guid=creator_guid,
                note_guid=doc_id,
                markdown_content=content,
                convert_special=convert_special
            )

        return doc_id

    def send_message(self, receiver_guids, title, content, sender_guid="", interactive_content=None, max_retries=3, retry_interval=5):
        payload = {
            "template_id": self.message_template_id,
            "receiver_guid": receiver_guids,
            "content": content,
            "org_guid": self.org_guid,
            "title": title,
            "platform_type": self.platform_type
        }

        if interactive_content is not None:
            payload["interactive_content"] = json.dumps(interactive_content, ensure_ascii=False)

        for attempt in range(1, max_retries + 1):
            try:
                response = requests.post(
                    url=self.base_url + self.MESSAGE_SEND_ROUTE,
                    headers=self.get_headers(user_guid=sender_guid),
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

    def send_webhook(self, webhook_url, card, max_retries=3, retry_interval=5):
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


# =============================================================================
# 通用工具函数
# =============================================================================
def strip_markdown_wrapper(content):
    content = (content or "").strip()

    if content.startswith("```markdown"):
        content = content[len("```markdown"):].lstrip("\n")
    elif content.startswith("```"):
        content = content[3:].lstrip("\n")

    if content.endswith("```"):
        content = content[:-3].rstrip("\n")

    return content


def convert_special_nodes(content):
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
    if isinstance(receiver_guids_raw, str):
        return [receiver_guids_raw]
    return receiver_guids_raw or []


def build_note_title(date_title, project_name):
    return f"{date_title} {project_name} 日报"


def build_message_text(note_title, note_url):
    return f"【{note_title}】已生成，请点击查看。\n<a href='{note_url}'>点击查看详情</a>"


def get_target_date_info():
    """
    获取日报目标日期：
    - 固定回看前一天
    """
    now = datetime.now()
    target_date = now - timedelta(days=1)

    return {
        "date_str": target_date.strftime("%Y-%m-%d"),
        "date_title": target_date.strftime("%Y/%m/%d"),
        "week_str": f"第{target_date.isocalendar()[1]}周",
        "month_str": target_date.strftime("%Y-%m"),
    }


def build_intermediate_markdown_file(project_guid, target_date_str, markdown_content):
    tmp_dir = tempfile.gettempdir()
    unique_suffix = uuid.uuid4().hex[:8]
    file_name = f"daily_{project_guid}_{target_date_str.replace('-', '')}_{unique_suffix}.md"
    file_path = os.path.join(tmp_dir, file_name)

    with open(file_path, "w", encoding="utf-8") as output_fp:
        output_fp.write(markdown_content)

    return file_path


def cleanup_temp_files(file_paths, project_name=""):
    if not file_paths:
        return

    for file_path in file_paths:
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
                prefix = f"[Cleanup][{project_name}]" if project_name else "[Cleanup]"
                print(f"{prefix} 🧹 已删除临时文件: {file_path}")
        except Exception as e:
            prefix = f"[Cleanup][{project_name}]" if project_name else "[Cleanup]"
            print(f"{prefix} ⚠️ 删除临时文件失败: {file_path}, error={e}")


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
    pm_guids = set()
    raw_pms = project_config.get("pm_guid")

    if isinstance(raw_pms, list):
        pm_guids.update([x for x in raw_pms if x])
    elif isinstance(raw_pms, str) and raw_pms:
        pm_guids.add(raw_pms)

    if project_config.get("user_guid"):
        pm_guids.add(project_config["user_guid"])
    if project_config.get("leader_guid"):
        pm_guids.add(project_config["leader_guid"])

    for note_entry in note_entries:
        parsed_result = note_entry.get("parsed_result", {})
        for member in parsed_result.get("members", []):
            person_info = member.get("person_info", {})
            if person_info.get("id", "") in pm_guids:
                return person_info

    return None


def build_step3_note_header_line(step1_meta, base_url):
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
            note_links.append(build_note_link_markdown(note_guid, base_url))

    note_links_text = "；".join(note_links) if note_links else "无"

    return (
        f"**日期**：{target_date_str} ｜ "
        f"**部门负责人**：{pm_markdown} ｜ "
        f"**原笔记链接**：{note_links_text}"
    )


def prepend_step3_note_header(ai_contents, step1_meta, base_url):
    header_line = build_step3_note_header_line(step1_meta, base_url)
    return [f"{header_line}\n\n{content}" for content in ai_contents]


# =============================================================================
# Parser Skill
# =============================================================================
class DailyReportParser:
    CONTAINER_BLOCK_TYPES = {"blockContainer", "blockGroup"}
    META_BLOCK_TYPES = {"heading", "fheading", "title"}
    MEMBER_HEADER_BLOCK_TYPES = {"heading", "fheading"}
    CONTENT_BLOCK_TYPES = {"bulletListItem", "numberedListItem", "paragraph", "codeBlock"}

    def __init__(self, project_config):
        self.project_name = project_config.get("project_name", "Unknown")

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

        platforms = []

        if mentions:
            for mention in mentions:
                label = mention.get("label", "").strip()
                uid = mention.get("uid", "")
                user_id = mention.get("id", "")
                if label and uid and user_id:
                    platforms.append(f"@{label}")

        plain_parts = re.findall(r"@([^\s@]+)", after_bracket)

        for part in plain_parts:
            part = part.strip()
            if part:
                platforms.append(part)

        deduped = []
        seen = set()

        for p in platforms:
            if p and p not in seen:
                seen.add(p)
                deduped.append(p)

        return project_name, deduped

    def _normalize_section_name(self, text):
        text = self._normalize_text(text)
        text_no_colon = text.replace("：", "").replace(":", "").strip()

        section_map = {
            "✅今日主要进展": "progress",
            "⚠️困难及所需支援": "issue_help",
            "📝下一步计划（Next Key Focus）": "next_focus",
            "📝Next Key Focus": "next_focus",
            "📝下一步计划": "next_focus",
        }

        return section_map.get(text_no_colon)

    def _create_empty_project(self, project_name, platforms=None):
        return {
            "project_name": project_name,
            "platforms": platforms or [],
            "sections": {
                "progress": [],
                "issue_help": [],
                "next_focus": []
            }
        }

    def _find_or_create_project(self, member_obj, project_name, platforms=None):
        for proj in member_obj["projects"]:
            if proj["project_name"] == project_name and proj.get("platforms", []) == (platforms or []):
                return proj

        new_proj = self._create_empty_project(project_name, platforms)
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
        current_section = None

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
            nonlocal current_member, current_project, current_section

            if not current_member or not current_project or not current_section:
                return

            current_project["sections"][current_section].append(block_obj)

        def ensure_context_defaults():
            nonlocal current_member, current_project, current_section

            if not current_member:
                return False

            if not current_project:
                current_project = self._find_or_create_project(current_member, "未分类项目")

            if not current_section:
                current_section = "progress"

            return True

        def traverse(blocks):
            nonlocal current_member, current_project, current_section

            for block in blocks:
                block_type = block.get("type")

                if block_type in self.CONTAINER_BLOCK_TYPES:
                    if "content" in block:
                        traverse(block["content"])
                    continue

                if block_type == "table":
                    table_block = parse_table(block)
                    if ensure_context_defaults():
                        append_block_to_section(table_block)
                    continue

                if block_type == "codeBlock":
                    code_text = self._extract_codeblock_text(block)
                    if code_text and ensure_context_defaults():
                        append_block_to_section({
                            "type": "code",
                            "text": code_text,
                            "mentions": []
                        })
                    continue

                inline_content = block.get("content", [])
                text, mentions = self.extract_text_and_mentions(inline_content)
                text = self._normalize_text(text)

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

                if block_type in self.MEMBER_HEADER_BLOCK_TYPES and mentions:
                    person_info = mentions[0]
                    current_member = {
                        "person_info": person_info,
                        "projects": []
                    }
                    members.append(current_member)

                    current_project = None
                    current_section = None
                    continue

                if not current_member:
                    if "content" in block and isinstance(block["content"], list):
                        traverse(block["content"])
                    continue

                project_name, platforms = self._extract_project_info(text, mentions)

                if (
                    project_name
                    and current_member
                    and block_type in ("bulletListItem", "paragraph", "heading", "fheading")
                ):
                    current_project = self._find_or_create_project(current_member, project_name, platforms)
                    current_section = None
                    continue

                section_name = self._normalize_section_name(text)

                if section_name and block_type in ("bulletListItem", "paragraph"):
                    if not current_project:
                        current_project = self._find_or_create_project(current_member, "未分类项目")
                    current_section = section_name
                    continue

                if block_type in ("bulletListItem", "numberedListItem", "paragraph"):
                    clean_text = re.sub(r"^[\d]+\.[\s]*|^[*-]\s*", "", text).strip()

                    if not clean_text:
                        if "content" in block and isinstance(block["content"], list):
                            traverse(block["content"])
                        continue

                    if block_type == "bulletListItem":
                        normalized_block_type = "bullet"
                    elif block_type == "numberedListItem":
                        normalized_block_type = "numbered"
                    else:
                        normalized_block_type = "paragraph"

                    if ensure_context_defaults():
                        text_block = build_text_block(
                            block_type=normalized_block_type,
                            text=clean_text,
                            mentions=mentions
                        )
                        append_block_to_section(text_block)

                if "content" in block and isinstance(block["content"], list):
                    traverse(block["content"])

        traverse(root_blocks)

        if not meta_info["date"]:
            fallback_date = datetime.now() - timedelta(days=1)
            meta_info["date"] = fallback_date.strftime("%Y-%m-%d")

        return {
            "meta": meta_info,
            "members": members
        }


class DailyReportParserSkill:
    def run(self, raw_json_data, project_config):
        parser = DailyReportParser(project_config)
        return parser.parse(raw_json_data)


# =============================================================================
# State Builder Skill
# =============================================================================
class DailyStateBuilder:
    def __init__(self, base_url):
        self.base_url = base_url

    def build_state_note_title(self, project_name, target_date_str, project_guid=""):
        safe_project_name = re.sub(r"[\\/:*?\"<>|]", "_", project_name or "Unknown")

        if project_guid:
            short_guid = project_guid[:8]
            return f"{target_date_str}_{safe_project_name}_daily_state_{short_guid}"

        return f"{target_date_str}_{safe_project_name}_daily_state"

    def flatten_parsed_result(self, parsed_result, note_entry, department_name="", target_date_str=""):
        items = []

        meta = parsed_result.get("meta", {})
        report_date = meta.get("date") or target_date_str

        note_guid = note_entry.get("note_guid", "")
        note_title = note_entry.get("note_title", "")

        for member in parsed_result.get("members", []):
            person_info = member.get("person_info", {})

            member_label = person_info.get("label", "")
            member_uid = person_info.get("uid", "")
            member_id = person_info.get("id", "")
            member_md = build_mention_markdown(person_info, fallback_text="未知成员")

            for project in member.get("projects", []):
                project_name = (project.get("project_name") or "未分类项目").strip()
                platforms = project.get("platforms") or []
                sections = project.get("sections", {})

                for section_key in ("progress", "issue_help", "next_focus"):
                    section_items = sections.get(section_key, [])

                    for idx, block in enumerate(section_items):
                        block_type = block.get("type", "paragraph")

                        if block_type == "table":
                            content = {
                                "headers": block.get("headers", []),
                                "rows": block.get("rows", [])
                            }
                            content_for_id = json.dumps(content, ensure_ascii=False)
                        else:
                            content = (block.get("text") or "").strip()
                            content_for_id = content

                        if not content:
                            continue

                        item_id = str(uuid.uuid5(
                            uuid.NAMESPACE_DNS,
                            f"{note_guid}|{member_id}|{project_name}|{section_key}|{idx}|{content_for_id}"
                        ))

                        items.append({
                            "item_id": item_id,
                            "date": report_date,
                            "department_name": department_name or meta.get("project_name", ""),
                            "note_guid": note_guid,
                            "note_title": note_title,
                            "source_url": f"{self.base_url}/workspace/{note_guid}" if note_guid else "",
                            "member": {
                                "label": member_label,
                                "uid": member_uid,
                                "id": member_id,
                                "mention_md": member_md
                            },
                            "project_name": project_name,
                            "platforms": platforms,
                            "section": section_key,
                            "block_type": block_type,
                            "content": content,
                            "mentions": block.get("mentions", []),
                        })

        return items

    def group_items_by_project(self, items):
        grouped = OrderedDict()

        for item in items:
            project_name = item.get("project_name") or "未分类项目"

            if project_name not in grouped:
                grouped[project_name] = {
                    "progress": [],
                    "issue_help": [],
                    "next_focus": []
                }

            section = item.get("section")
            if section in grouped[project_name]:
                grouped[project_name][section].append(item)

        return grouped

    def build_project_timelines(self, items):
        timelines = OrderedDict()

        sorted_items = sorted(
            items,
            key=lambda x: (
                x.get("project_name", ""),
                x.get("date", ""),
                x.get("section", "")
            )
        )

        for item in sorted_items:
            project_name = item.get("project_name") or "未分类项目"
            date = item.get("date") or "unknown_date"
            section = item.get("section")

            if project_name not in timelines:
                timelines[project_name] = OrderedDict()

            if date not in timelines[project_name]:
                timelines[project_name][date] = {
                    "progress": [],
                    "issue_help": [],
                    "next_focus": []
                }

            if section in timelines[project_name][date]:
                timelines[project_name][date][section].append(item)

        return timelines

    def build(self, project, target_date_str, parsed_note_entries):
        normalized_items = []

        for note_entry in parsed_note_entries:
            parsed_result = note_entry.get("parsed_result", {})

            normalized_items.extend(
                self.flatten_parsed_result(
                    parsed_result=parsed_result,
                    note_entry=note_entry,
                    department_name=project.get("project_name", ""),
                    target_date_str=target_date_str
                )
            )

        project_names = sorted(
            set([
                x.get("project_name")
                for x in normalized_items
                if x.get("project_name")
            ])
        )

        run_id = f"daily_{project.get('project_guid')}_{target_date_str}"

        state = {
            "run_meta": {
                "run_id": run_id,
                "mode": "daily",
                "target_date": target_date_str,
                "project_guid": project.get("project_guid"),
                "department_name": project.get("project_name", ""),
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            },
            "source": {
                "note_entries": [
                    {
                        "note_guid": x.get("note_guid"),
                        "note_title": x.get("note_title"),
                        "source_url": f"{self.base_url}/workspace/{x.get('note_guid')}"
                    }
                    for x in parsed_note_entries
                ]
            },
            "normalized_items": normalized_items,
            "grouped_by_project": self.group_items_by_project(normalized_items),
            "project_timelines": self.build_project_timelines(normalized_items),
            "check_results": {
                "parse_summary": {
                    "note_count": len(parsed_note_entries),
                    "item_count": len(normalized_items),
                    "project_count": len(project_names),
                    "project_names": project_names
                }
            },
            "llm_outputs": {},
            "final_outputs": {},
            "publish_result": {}
        }

        return state


def print_parser_summary(project_name, daily_state):
    """
    打印 Parser 解析质量摘要，方便平台日志排查。
    """
    parse_summary = (
        daily_state
        .get("check_results", {})
        .get("parse_summary", {})
    )

    note_count = parse_summary.get("note_count", 0)
    item_count = parse_summary.get("item_count", 0)
    project_count = parse_summary.get("project_count", 0)
    project_names = parse_summary.get("project_names", [])

    normalized_items = daily_state.get("normalized_items", [])

    section_counter = {
        "progress": 0,
        "issue_help": 0,
        "next_focus": 0
    }

    unclassified_count = 0

    for item in normalized_items:
        section = item.get("section")
        project = item.get("project_name")

        if section in section_counter:
            section_counter[section] += 1

        if not project or project == "未分类项目":
            unclassified_count += 1

    print(f"[ParserSummary][{project_name}] 📊 解析摘要：")
    print(f"  - 原始笔记数: {note_count}")
    print(f"  - 解析条目数: {item_count}")
    print(f"  - 识别项目数: {project_count}")
    print(f"  - 今日进展条目: {section_counter['progress']}")
    print(f"  - 困难求助条目: {section_counter['issue_help']}")
    print(f"  - 下一步计划条目: {section_counter['next_focus']}")
    print(f"  - 未分类项目条目: {unclassified_count}")

    if project_names:
        print(f"  - 识别项目列表: {'；'.join(project_names)}")
    else:
        print("  - 识别项目列表: 无")

    if item_count == 0:
        print(f"[ParserSummary][{project_name}] ⚠️ 未解析出任何日报条目，请检查模板结构或 Parser 规则")

    if project_count == 0:
        print(f"[ParserSummary][{project_name}] ⚠️ 未识别出任何项目，后续周报/项目汇总可能无法正常聚合")

    if unclassified_count > 0:
        print(f"[ParserSummary][{project_name}] ⚠️ 存在 {unclassified_count} 条未分类项目内容，建议检查项目标题格式")


class StateRepository:
    def __init__(self, client: PlatformClient, state_builder: DailyStateBuilder):
        self.client = client
        self.state_builder = state_builder

    def save_daily_state_note(self, project, daily_state):
        if not project.get("enable_state_save", True):
            print(f"[State][{project.get('project_name', '')}] ⏭ enable_state_save=False，跳过 state 保存")
            return None

        project_name = project.get("project_name", "")
        target_date_str = daily_state.get("run_meta", {}).get("target_date", "")

        state_target_project_guid = project.get("state_target_project_guid")
        state_target_parent_guid = project.get("state_target_parent_guid", "0")
        state_target_user_guid = project.get("state_target_user_guid") or self.client.user_guid

        if not state_target_project_guid:
            print(f"[State][{project_name}] ⚠️ 未配置 state_target_project_guid，跳过 state 保存")
            return None

        state_json = json.dumps(daily_state, ensure_ascii=False, indent=2)

        state_md = (
            f"# Daily State\n\n"
            f"**日期**：{target_date_str}\n\n"
            f"**部门/项目**：{project_name}\n\n"
            f"```json\n{state_json}\n```"
        )

        title = self.state_builder.build_state_note_title(
            project_name=project_name,
            target_date_str=target_date_str,
            project_guid=project.get("project_guid", "")
        )

        print(f"[State][{project_name}] 正在保存 daily_state...")

        doc_id = self.client.create_note(
            content=state_md,
            title=title,
            project_guid=state_target_project_guid,
            parent_guid=state_target_parent_guid,
            tags=["日报State", "AI", "JSON"],
            creator_guid=state_target_user_guid,
            convert_special=False
        )

        state_url = f"{self.client.base_url}/workspace/{doc_id}" if doc_id else ""

        print(f"[State][{project_name}] ✅ daily_state 已保存: {state_url}")

        return {
            "state_note_guid": doc_id,
            "state_note_url": state_url
        }


# =============================================================================
# Markdown Renderer
# =============================================================================
class DailyMarkdownRenderer:
    def __init__(self, base_url):
        self.base_url = base_url

    def aggregate_parsed_note_entries(self, note_entries):
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
                    platforms = project.get("platforms") or []
                    sections = project.get("sections", {})

                    platforms_tuple = tuple(platforms)

                    for section_key in ("progress", "issue_help", "next_focus"):
                        if project_name not in aggregated[section_key]:
                            aggregated[section_key][project_name] = OrderedDict()

                        member_key = (member_md, platforms_tuple)

                        if member_key not in aggregated[section_key][project_name]:
                            aggregated[section_key][project_name][member_key] = []

                        for item in sections.get(section_key, []):
                            item_copy = dict(item)
                            item_copy["note_guid"] = note_guid
                            aggregated[section_key][project_name][member_key].append(item_copy)

        return aggregated

    def render_table_markdown(self, headers, rows):
        if not headers and not rows:
            return []

        if not headers and rows:
            max_cols = max(len(row) for row in rows) if rows else 1
            headers = [f"列{i + 1}" for i in range(max_cols)]

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

    def render_grouped_section_markdown(self, section_title, project_map):
        lines = [f"# {section_title}", ""]
        has_any = False

        for project_name, member_map in project_map.items():
            project_has_content = any(member_map.values())
            if not project_has_content:
                continue

            has_any = True
            lines.append(f"## 📌 {project_name}")

            platform_groups = OrderedDict()

            for (member_md, platforms_tuple), items in member_map.items():
                if not items:
                    continue

                if platforms_tuple not in platform_groups:
                    platform_groups[platforms_tuple] = []

                platform_groups[platforms_tuple].append((member_md, items))

            sorted_groups = sorted(
                platform_groups.items(),
                key=lambda x: (-len(x[0]), x[0])
            )

            for platforms_tuple, entries in sorted_groups:
                if platforms_tuple:
                    header = " & ".join(platforms_tuple)
                else:
                    header = "无标签"

                lines.append(f"### {header}")

                for member_md, items in entries:
                    for item in items:
                        item_type = item.get("type", "paragraph")
                        text = (item.get("text") or "").strip()

                        if item_type == "table":
                            lines.append(f"- {member_md} [表格内容]")
                            headers = item.get("headers", [])
                            rows = item.get("rows", [])
                            table_lines = self.render_table_markdown(headers, rows)

                            for tl in table_lines:
                                lines.append(f"    {tl}")

                        elif item_type == "code":
                            code_text = text.replace("\r\n", "\n").strip()

                            if code_text:
                                lines.append(f"- {member_md} 代码块：")
                                lines.append("```")
                                lines.append(code_text)
                                lines.append("```")

                        else:
                            if text:
                                text_single_line = text.replace("\n", " / ").strip()
                                lines.append(f"- {member_md} {text_single_line}")

                lines.append("")

        if not has_any:
            lines.append("- 暂无")
            lines.append("")

        return "\n".join(lines).rstrip()

    def build_merged_daily_markdown(self, project_name, target_date_str, note_entries, project_config):
        pm_person_info = find_pm_person_info(note_entries, project_config)
        pm_markdown = build_mention_markdown(pm_person_info, fallback_text="部门负责人未识别")

        note_links = []
        seen = set()

        for note_entry in note_entries:
            note_guid = note_entry["note_guid"]
            if note_guid not in seen:
                seen.add(note_guid)
                note_links.append(build_note_link_markdown(note_guid, self.base_url))

        note_links_text = "；".join(note_links) if note_links else "无"

        aggregated = self.aggregate_parsed_note_entries(note_entries)

        merged_parts = [
            f"# 📅 {project_name} 日报汇总",
            f"**日期**：{target_date_str}",
            f"**部门负责人**：{pm_markdown} | **原笔记链接**：{note_links_text}",
            "",
            "---",
            "",
            self.render_grouped_section_markdown("今日核心进展", aggregated["progress"]),
            "",
            self.render_grouped_section_markdown("困难及所需支援", aggregated["issue_help"]),
            "",
            self.render_grouped_section_markdown("下一步计划", aggregated["next_focus"]),
            ""
        ]

        return "\n".join(merged_parts)


# =============================================================================
# Summary Skill
# =============================================================================
class DailySummarySkill:
    def __init__(self, client: PlatformClient, model):
        self.client = client
        self.model = model

    def run(self, md_file_list, project):
        try:
            project_name = project.get("project_name", "")
            prompt_file_guid = project.get("briefing_prompt_file_guid")

            print(f"[Step 2][{project_name}] 正在调用 AI 生成详细报告...")

            default_prompt = "请详细总结以下日报内容，保留关键数据和人员提及。\n{{markdown_content}}"
            prompt_text = self.client.load_prompt_text(prompt_file_guid, default_prompt)
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
                    llm_result = self.client.call_llm_with_retry(
                        llm_name=self.model.llm_name,
                        llm_params=self.model.llm_params,
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


class CardSummarySkill:
    def __init__(self, client: PlatformClient, runtime: RuntimeConfig, model):
        self.client = client
        self.runtime = runtime
        self.model = model

    def fallback_format_content(self, content, max_len=20000):
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

    def run(self, project, long_markdown):
        project_name = project.get("project_name", "")
        card_prompt_file_guid = project.get(f"{self.runtime.generate_type}_card_prompt_guid")

        default_prompt = self.runtime.raw.get(
            "card_prompt_default",
            "请将以下内容 {{markdown_content}} 整理为简洁的飞书消息卡片正文。"
            "格式要求：禁止使用任何标题语法（#、##），全部使用正文；仅必要时用加粗（**关键词**）强调；"
            "使用项目符号（•）组织内容；重点突出、不超过 300 字。"
        )

        prompt_text = self.client.load_prompt_text(card_prompt_file_guid, default_prompt)
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
            llm_result = self.client.call_llm_with_retry(
                llm_name=self.model.llm_name,
                llm_params=self.model.llm_params,
                context_messages=context_messages,
                max_retries=10
            )
            return strip_markdown_wrapper(llm_result)

        except Exception as e:
            print(f"⚠️ [Step 2.5][{project_name}] AI 生成在 10 次重试后仍失败 (Error: {e})")
            print("   -> 切换至格式化截断兜底模式")
            return self.fallback_format_content(long_markdown, max_len=20000)


# =============================================================================
# Publisher
# =============================================================================
class ReportPublisher:
    def __init__(self, client: PlatformClient):
        self.client = client

    def publish_daily_report(self, contents, project, step1_meta=None):
        try:
            project_name = project.get("project_name", "")

            date_info = get_target_date_info()

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

            header_line = build_step3_note_header_line(step1_meta, self.client.base_url) if step1_meta else ""

            for content in contents:
                cleaned_content = content

                lines = content.split("\n")
                if lines and re.match(r".*\d{4}-\d{2}-\d{2}.*[|｜].*", lines[0]):
                    cleaned_content = "\n".join(lines[1:]).lstrip("\n")

                title = build_note_title(date_info["date_title"], project_name)
                final_content = f"{header_line}\n\n{cleaned_content}" if header_line else cleaned_content

                doc_id = self.client.create_note(
                    content=final_content,
                    title=title,
                    project_guid=target_project_guid,
                    parent_guid=target_parent_guid,
                    tags=["日报", "AI"],
                    creator_guid=target_user_guid,
                    convert_special=True
                )

                if doc_id:
                    note_urls.append(f"{self.client.base_url}/workspace/{doc_id}")
                    note_titles.append(title)

            print(f"[Step 3][{project_name}] ✅ 笔记创建完成")
            return note_urls, note_titles

        except Exception as e:
            print(f"[Step 3] ❌ 发生异常: {e}")
            traceback.print_exc()
            return [], []


# =============================================================================
# Message Dispatcher
# =============================================================================
class MessageDispatcher:
    def __init__(self, client: PlatformClient, runtime: RuntimeConfig, card_summary_skill: CardSummarySkill):
        self.client = client
        self.runtime = runtime
        self.card_summary_skill = card_summary_skill

    def build_card_header_line(self, project, step1_meta=None):
        date_info = get_target_date_info()
        current_date = date_info["date_str"]

        pm_person_info = None
        if step1_meta:
            pm_person_info = step1_meta.get("pm_person_info")

        pm_markdown = build_mention_markdown(pm_person_info, fallback_text="部门负责人未识别")

        return f"**项目进展摘要 | {current_date} | 部门负责人：{pm_markdown}**"

    def build_feishu_card(self, title, card_content, note_url, source_note_urls=None):
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

    def send(self, note_url_list, note_title_list, project, content_list, step1_meta=None):
        try:
            project_name = project.get("project_name", "")
            generate_type = self.runtime.generate_type

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
            sender_guid = project.get(f"{generate_type}_target_user_guid", "") or self.client.user_guid

            if not note_url_list:
                print(f"[Step 4][{project_name}] ⚠️ 没有 URL 可发送")
                return

            card_header_line = self.build_card_header_line(project, step1_meta=step1_meta)

            source_note_urls = []

            if step1_meta:
                note_entries = step1_meta.get("note_entries", [])
                seen = set()

                for note_entry in note_entries:
                    note_guid = note_entry.get("note_guid")
                    if note_guid and note_guid not in seen:
                        seen.add(note_guid)
                        source_note_urls.append(f"{self.client.base_url}/workspace/{note_guid}")

            for note_title, note_url, full_content in zip(note_title_list, note_url_list, content_list):
                card_summary = self.card_summary_skill.run(project, full_content)
                final_card_content = f"{card_header_line}\n\n{card_summary}"

                card = self.build_feishu_card(
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
                            webhook_result = self.client.send_webhook(url, card)

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

                        response = self.client.send_message(
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
# Workflow
# =============================================================================
class DailyReportWorkflow:
    def __init__(
        self,
        runtime: RuntimeConfig,
        client: PlatformClient,
        parser_skill: DailyReportParserSkill,
        state_builder: DailyStateBuilder,
        state_repository: StateRepository,
        markdown_renderer: DailyMarkdownRenderer,
        summary_skill: DailySummarySkill,
        publisher: ReportPublisher,
        dispatcher: MessageDispatcher
    ):
        self.runtime = runtime
        self.client = client
        self.parser_skill = parser_skill
        self.state_builder = state_builder
        self.state_repository = state_repository
        self.markdown_renderer = markdown_renderer
        self.summary_skill = summary_skill
        self.publisher = publisher
        self.dispatcher = dispatcher

    def find_daily_note(self, user_guid, project_guid, folder_guid, target_date_str):
        note_list = self.client.list_doc_tree(
            user_guid=user_guid,
            project_guid=project_guid,
            parent_guid=folder_guid
        )

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

    def step1_load_parse_build_state(self, project):
        generated_files = []

        try:
            project_name = project["project_name"]
            project_guid = project["project_guid"]
            work_log_folder_guid = project["work_log_folder_guid"]

            project_user_guids = project.get(
                "user_guid_list",
                [project.get("user_guid") or project.get("leader_guid")]
            )

            date_info = get_target_date_info()
            target_date_str = date_info["date_str"]

            print(f"[Step 1][{project_name}] 目标日期: {target_date_str}")

            matched_notes = []

            for user_guid in project_user_guids:
                if not user_guid:
                    continue

                note_info = self.find_daily_note(
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
                return [], False, [], {}, {}

            print(f"[Step 1][{project_name}] ✅ 找到 {len(matched_notes)} 份笔记，解析中...")

            parsed_note_entries = []

            for matched_note in matched_notes:
                user_guid = matched_note["user_guid"]
                note_guid = matched_note["note_guid"]

                raw_json = self.client.get_note_json(user_guid=user_guid, doc_id=note_guid)
                parsed_result = self.parser_skill.run(raw_json, project)

                parsed_note_entries.append({
                    "note_guid": note_guid,
                    "note_title": matched_note.get("note_title", ""),
                    "parsed_result": parsed_result
                })

            merged_markdown = self.markdown_renderer.build_merged_daily_markdown(
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

            daily_state = self.state_builder.build(
                project=project,
                target_date_str=target_date_str,
                parsed_note_entries=parsed_note_entries
            )

            print_parser_summary(project_name, daily_state)

            state_publish_result = self.state_repository.save_daily_state_note(project, daily_state)

            if state_publish_result:
                daily_state["publish_result"]["state_note_guid"] = state_publish_result.get("state_note_guid")
                daily_state["publish_result"]["state_note_url"] = state_publish_result.get("state_note_url")

            return [
                ZFile(
                    path=intermediate_file_path,
                    source_name=os.path.basename(intermediate_file_path)
                )
            ], True, generated_files, step1_meta, daily_state

        except Exception as e:
            print(f"[Step 1] ❌ 发生异常: {e}")
            traceback.print_exc()
            return [], False, [], {}, {}

    def run_project(self, project):
        project_name = project.get("project_name", "Unknown")
        enable_ai = project.get("enable_briefing_summary", True)

        if not enable_ai:
            print(f"\n⏭ 跳过项目: {project_name} (enable_briefing_summary=False)")
            return

        print(f"\n▶ 处理项目: {project_name}")

        temp_files = []

        try:
            md_files, found, temp_files, step1_meta, daily_state = self.step1_load_parse_build_state(project)

            if not found:
                print(f"  ⚠️ 跳过 {project_name}")
                return

            ai_contents = self.summary_skill.run(md_files, project)

            cleanup_temp_files(temp_files, project_name=project_name)

            if not ai_contents:
                raise Exception("AI 生成内容为空")

            note_urls, note_titles = self.publisher.publish_daily_report(
                ai_contents,
                project,
                step1_meta=step1_meta
            )

            self.dispatcher.send(
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

    def run_all(self, projects):
        print("=" * 60)
        print(f"开始执行日报工作流 | 项目数: {len(projects)}")
        print("=" * 60)

        for project in projects:
            self.run_project(project)

        print("\n" + "=" * 60)
        print("全部任务执行完毕")
        print("=" * 60)


# =============================================================================
# 平台应用入口
# =============================================================================
runtime = RuntimeConfig(config_file)

client = PlatformClient(runtime)

parser_skill = DailyReportParserSkill()

state_builder = DailyStateBuilder(
    base_url=runtime.base_url
)

state_repository = StateRepository(
    client=client,
    state_builder=state_builder
)

markdown_renderer = DailyMarkdownRenderer(
    base_url=runtime.base_url
)

summary_skill = DailySummarySkill(
    client=client,
    model=model
)

card_summary_skill = CardSummarySkill(
    client=client,
    runtime=runtime,
    model=model
)
publisher = ReportPublisher(
    client=client
)

dispatcher = MessageDispatcher(
    client=client,
    runtime=runtime,
    card_summary_skill=card_summary_skill
)

workflow = DailyReportWorkflow(
    runtime=runtime,
    client=client,
    parser_skill=parser_skill,
    state_builder=state_builder,
    state_repository=state_repository,
    markdown_renderer=markdown_renderer,
    summary_skill=summary_skill,
    publisher=publisher,
    dispatcher=dispatcher
)

workflow.run_all(runtime.projects)
