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

generate_type = "weekly"


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
DEFAULT_LLM_PARAMS = {"temperature": 0.3, "max_tokens": 4096}
MESSAGE_TEMPLATE_ID = "80"
PLATFORM_TYPE = "all"

DEFAULT_BATCH_NUMBER = 5
MAX_BATCH_NUMBER = 8


# =============================================================================
# 通用辅助函数
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


def get_note_json_content(user_guid="", doc_id=""):
    headers = get_headers_with_ak(user_guid=user_guid, doc_id=doc_id)
    response = requests.get(
        url=BASE_URL + NOTE_JSON_ROUTE,
        headers=headers,
        params={"docId": doc_id}
    )
    return response.json()


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


def _convert_special_nodes(content):
    content = re.sub(
        r"\[@([^\]]*)\]\(mention:[^:]*:([^)]+)\)",
        lambda m: f'<span data-node-type="mention" data-guid="{m.group(2)}"></span>',
        content
    )

    content = re.sub(
        r"\[([^\]]+)\]\(mentionUrl:[^:]*:[^:]*:([^)]+)\)",
        lambda m: f'<a data-node-type="mentionUrl" data-url="{m.group(2)}">{m.group(1)}</a>',
        content
    )

    content = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>',
        content
    )

    content = re.sub(
        r":::highlight\[[^\]]*\]\n(.*?):::",
        lambda m: (
            f'<div data-node-type="highlightBlock" data-content-markdown>\n'
            f'{m.group(1).rstrip()}\n'
            f'</div>'
        ),
        content,
        flags=re.DOTALL
    )

    return content


def normalize_receiver_guids(receiver_guids_raw):
    if isinstance(receiver_guids_raw, str):
        return [receiver_guids_raw] if receiver_guids_raw else []
    return receiver_guids_raw or []


def build_message_text(note_title, note_url):
    return f"【{note_title}】已生成，请点击查看。\n<a href='{note_url}'>点击查看详情</a>"


def load_prompt_text(prompt_file_guid, default_prompt):
    if not prompt_file_guid:
        return default_prompt

    try:
        signed_url_response = requests.get(
            BASE_URL + SIGNED_URL_ROUTE,
            headers=get_headers_with_ak(),
            params={"categoryGuid": prompt_file_guid},
            timeout=10
        )
        signed_url = (signed_url_response.json().get("data") or {}).get("signedUrl")
        if not signed_url:
            return default_prompt

        return requests.get(signed_url, timeout=10).text
    except Exception as e:
        print(f"⚠️ Prompt 文件读取失败，使用默认 prompt: {e}")
        return default_prompt


def get_last_week_info():
    today = datetime.now()
    last_monday = today - timedelta(days=today.weekday() + 7)
    week_dates = [last_monday + timedelta(days=i) for i in range(7)]

    return {
        "start_date": week_dates[0].strftime("%Y-%m-%d"),
        "end_date": week_dates[-1].strftime("%Y-%m-%d"),
        "start_title": week_dates[0].strftime("%Y/%m/%d"),
        "end_title": week_dates[-1].strftime("%Y/%m/%d"),
        "date_list": [d.strftime("%Y-%m-%d") for d in week_dates],
        "week_number": week_dates[0].isocalendar()[1],
    }


def build_intermediate_markdown_file(project_guid, target_date_str, markdown_content):
    tmp_dir = tempfile.gettempdir()
    unique_suffix = uuid.uuid4().hex[:8]
    file_name = f"weekly_{project_guid}_{target_date_str.replace('-', '')}_{unique_suffix}.md"
    file_path = os.path.join(tmp_dir, file_name)

    with open(file_path, "w", encoding="utf-8") as output_fp:
        output_fp.write(markdown_content or "")

    return file_path


def build_intermediate_json_file(project_guid, target_date_str, json_content, suffix=""):
    tmp_dir = tempfile.gettempdir()
    unique_suffix = uuid.uuid4().hex[:8]
    name_suffix = f"_{suffix}" if suffix else ""
    file_name = f"weekly_{project_guid}_{target_date_str.replace('-', '')}{name_suffix}_{unique_suffix}.json"
    file_path = os.path.join(tmp_dir, file_name)

    with open(file_path, "w", encoding="utf-8") as output_fp:
        json.dump(json_content, output_fp, ensure_ascii=False, indent=2)

    return file_path


def cleanup_temp_files(file_paths, project_name=""):
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


def mention_to_markdown(mention_obj):
    if not mention_obj:
        return "[@未知](mention::)"

    attrs = mention_obj.get("attrs", mention_obj)
    uid = attrs.get("uid", "")
    user_id = attrs.get("id", "")
    label = attrs.get("label", "未知")
    return f"[@{label}](mention:{uid}:{user_id})"


def build_weekly_note_title(week_info, project_name):
    year = week_info["start_date"][:4]
    week_number = week_info["week_number"]
    return f"{year}#W{week_number:02d} {project_name}周报"


# =============================================================================
# blockContainer 文本抽取
# =============================================================================
def extract_text_from_inline_content(inline_content):
    text_parts = []

    for inline_item in inline_content or []:
        inline_type = inline_item.get("type")

        if inline_type == "text":
            text_parts.append(inline_item.get("text", ""))

        elif inline_type == "mention":
            attrs = inline_item.get("attrs", {})
            label = attrs.get("label", "?")
            uid = attrs.get("uid", "")
            user_id = attrs.get("id", "")
            text_parts.append(f"[@{label}](mention:{uid}:{user_id})")

        elif inline_type == "mentionUrl":
            attrs = inline_item.get("attrs", {})
            content = attrs.get("content", "")
            original_url = attrs.get("originalUrl", "")
            uid = attrs.get("uid", "")
            data_type = attrs.get("dataType", 1)
            text_parts.append(f"[{content}](mentionUrl:{uid}:{data_type}:{original_url})")

    return "".join(text_parts).strip()


def extract_text_from_block_container(block_container):
    if not block_container or block_container.get("type") != "blockContainer":
        return ""

    text_parts = []

    for item in block_container.get("content", []):
        item_type = item.get("type")

        if item_type in (
            "paragraph",
            "heading",
            "fheading",
            "bulletListItem",
            "numberedListItem",
        ):
            text = extract_text_from_inline_content(item.get("content", []))
            if text:
                text_parts.append(text)

        elif item_type == "blockContainer":
            nested_text = extract_text_from_block_container(item)
            if nested_text:
                text_parts.append(nested_text)

    return " ".join(text_parts).strip()


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


# =============================================================================
# 日报 JSON Parser：只负责稳定抽原文，不做复杂语义
# =============================================================================
class DailyReportParser:
    CONTAINER_BLOCK_TYPES = {"blockContainer", "blockGroup"}
    MEMBER_HEADER_BLOCK_TYPES = {"heading", "fheading"}
    CONTENT_BLOCK_TYPES = {"bulletListItem", "numberedListItem", "paragraph"}

    def __init__(self, project_config):
        self.project_name = project_config.get("project_name", "Unknown")

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

    def parse_table(self, table_block):
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
                            if part:
                                extracted_parts.append(part)

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

    def parse(self, raw_json_data):
        root_blocks = (
            raw_json_data.get("data", {}).get("content", [])
            or raw_json_data.get("content", [])
        )

        members = []
        current_member = None

        def traverse(blocks):
            nonlocal current_member

            for block in blocks:
                block_type = block.get("type")

                if block_type in self.CONTAINER_BLOCK_TYPES:
                    if "content" in block:
                        traverse(block["content"])
                    continue

                if block_type == "table":
                    if current_member:
                        table_block = self.parse_table(block)
                        current_member["raw_blocks"].append(table_block)
                        current_member["raw_content"].append("[表格内容]")
                    continue

                inline_content = block.get("content", [])
                text, mentions = self.extract_text_and_mentions(inline_content)

                # 有 @ 的 heading / fheading 视为成员标题
                if block_type in self.MEMBER_HEADER_BLOCK_TYPES:
                    if mentions:
                        person_info = mentions[0]
                        current_member = {
                            "person_info": person_info,
                            "raw_blocks": [],
                            "raw_content": [],
                            "full_text": ""
                        }
                        members.append(current_member)
                        continue

                if block_type in self.CONTENT_BLOCK_TYPES:
                    if not current_member:
                        continue

                    clean_text = re.sub(r"^[\d]+\.[\s]*|^[*-]\s*", "", text).strip()
                    if not clean_text:
                        continue

                    if block_type == "bulletListItem":
                        normalized_block_type = "bullet"
                    elif block_type == "numberedListItem":
                        normalized_block_type = "numbered"
                    else:
                        normalized_block_type = "paragraph"

                    text_block = build_text_block(
                        block_type=normalized_block_type,
                        text=clean_text,
                        mentions=mentions
                    )

                    current_member["raw_blocks"].append(text_block)
                    current_member["raw_content"].append(clean_text)

                if "content" in block and isinstance(block["content"], list):
                    traverse(block["content"])

        traverse(root_blocks)

        for member in members:
            lines = []
            for block in member.get("raw_blocks", []):
                block_type = block.get("type")

                if block_type in ("paragraph", "bullet", "numbered"):
                    text = block.get("text", "")
                    if text:
                        lines.append(text)

                elif block_type == "table":
                    headers = block.get("headers", [])
                    rows = block.get("rows", [])

                    if headers:
                        lines.append(" | ".join(headers))

                    for row in rows:
                        row_text = " | ".join([str(x) for x in row if str(x).strip()])
                        if row_text:
                            lines.append(row_text)

            member["full_text"] = "\n".join(lines)

        return {"members": members}


# =============================================================================
# Step 1：查找与解析上周日报
# =============================================================================
def find_weekly_notes(user_guid, project_guid, folder_guid, date_list):
    response = requests.post(
        url=BASE_URL + DOC_TREE_ROUTE,
        headers=get_headers_with_ak(user_guid=user_guid),
        json={"projectGuid": project_guid, "parentGuid": folder_guid}
    )

    response_json = response.json()
    note_list = response_json.get("data") or []
    matched_notes = []

    date_variants_map = {}
    for date_str in date_list:
        date_variants_map[date_str] = [
            date_str,
            date_str.replace("-", "/"),
            date_str.replace("-", "."),
        ]

    for note in note_list:
        note_title = note.get("dataTitle", "")
        note_guid = note.get("categoryGuid")

        if not note_guid:
            continue

        for date_str, variants in date_variants_map.items():
            if any(v in note_title for v in variants):
                matched_notes.append({
                    "date": date_str,
                    "categoryGuid": note_guid,
                    "dataTitle": note_title
                })
                break

    return matched_notes


def aggregate_weekly_json(parsed_note_entries):
    user_map = {}
    actual_dates = set()
    source_urls = {}

    for entry in parsed_note_entries:
        report_date = entry["date"]
        note_guid = entry["note_guid"]
        parsed_result = entry["parsed_result"]

        actual_dates.add(report_date)
        source_urls[report_date] = f"{BASE_URL}/workspace/{note_guid}"

        for member in parsed_result.get("members", []):
            person_info = member.get("person_info", {})
            user_id = person_info.get("id")
            user_label = person_info.get("label", "未知用户")

            if not user_id or not user_label:
                continue

            mention_obj = {
                "type": "mention",
                "attrs": person_info
            }

            key = (user_id, user_label)
            content = member.get("full_text", "").strip()
            if not content:
                content = "（当日无正文内容）"

            if key not in user_map:
                user_map[key] = {
                    "user_name": mention_obj,
                    "reports_dict": {}
                }

            if report_date in user_map[key]["reports_dict"]:
                user_map[key]["reports_dict"][report_date] += "\n" + content
            else:
                user_map[key]["reports_dict"][report_date] = content

    final_users = []

    for key in sorted(user_map.keys(), key=lambda x: x[1]):
        user_data = user_map[key]
        reports = []

        for date in sorted(user_data["reports_dict"].keys()):
            reports.append({
                "date": date,
                "content": user_data["reports_dict"][date]
            })

        final_users.append({
            "user_name": user_data["user_name"],
            "reports": reports
        })

    week_number = None
    if actual_dates:
        first_date = datetime.strptime(sorted(actual_dates)[0], "%Y-%m-%d")
        week_number = first_date.isocalendar()[1]

    return {
        "metadata": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "range_dates": sorted(list(actual_dates)),
            "source_urls": source_urls,
            "week_number": week_number
        },
        "users": final_users
    }


def step1_weekly_summary_note(project):
    generated_files = []

    try:
        project_name = project["project_name"]
        project_guid = project["project_guid"]
        work_log_folder_guid = project["work_log_folder_guid"]
        project_user_guids = project.get(
            "user_guid_list",
            [project.get("user_guid") or project.get("leader_guid")]
        )

        week_info = get_last_week_info()
        print(f"[Step 1][{project_name}] 目标周期: {week_info['start_date']} ~ {week_info['end_date']}")

        matched_notes = []
        seen_note_guids = set()

        for user_guid in project_user_guids:
            if not user_guid:
                continue

            user_notes = find_weekly_notes(
                user_guid=user_guid,
                project_guid=project_guid,
                folder_guid=work_log_folder_guid,
                date_list=week_info["date_list"]
            )

            for note in user_notes:
                note_guid = note["categoryGuid"]

                if note_guid in seen_note_guids:
                    continue

                seen_note_guids.add(note_guid)

                matched_notes.append({
                    "date": note["date"],
                    "user_guid": user_guid,
                    "note_guid": note_guid,
                    "note_title": note["dataTitle"]
                })

        if not matched_notes:
            print(f"[Step 1][{project_name}] ❌ 未找到上周日报笔记")
            return {}, False, []

        matched_notes.sort(key=lambda x: (x["date"], x["note_title"]))
        print(f"[Step 1][{project_name}] ✅ 找到 {len(matched_notes)} 份笔记，解析中...")

        parser = DailyReportParser(project)
        parsed_note_entries = []

        for matched_note in matched_notes:
            raw_json = get_note_json_content(
                user_guid=matched_note["user_guid"],
                doc_id=matched_note["note_guid"]
            )
            parsed_result = parser.parse(raw_json)

            parsed_note_entries.append({
                "date": matched_note["date"],
                "note_guid": matched_note["note_guid"],
                "note_title": matched_note["note_title"],
                "parsed_result": parsed_result
            })

        weekly_json = aggregate_weekly_json(parsed_note_entries)

        json_file_path = build_intermediate_json_file(
            project_guid=project_guid,
            target_date_str=f"{week_info['start_date']}_to_{week_info['end_date']}",
            json_content=weekly_json,
            suffix="raw"
        )
        generated_files.append(json_file_path)

        print(f"[Step 1][{project_name}] 📦 原始聚合 JSON 已生成: {json_file_path}")

        return weekly_json, True, generated_files

    except Exception as e:
        print(f"[Step 1] ❌ 发生异常: {e}")
        traceback.print_exc()
        return {}, False, []


# =============================================================================
# Step 2：规则生成 FactItem，不再让 LLM 输出 JSON
# =============================================================================
SECTION_KEYWORDS = {
    "help": [
        "需要支持", "需支持", "需要协助", "需协助", "请协助", "待协调", "需要协调",
        "待确认", "需确认", "需要确认", "待决策", "需要决策", "希望支持", "麻烦协助"
    ],
    "risk": [
        "风险", "困难", "问题", "阻塞", "异常", "失败", "延期", "延迟", "不稳定",
        "无法", "缺少", "不足", "卡住", "受限", "报错", "error", "fail", "issue"
    ],
    "next_focus": [
        "下周", "下一步", "后续", "计划", "预计", "准备", "继续", "将", "拟",
        "待开展", "后面", "接下来"
    ],
    "progress": [
        "完成", "推进", "修复", "上线", "发布", "验证", "交付", "实现", "整理",
        "输出", "支持", "对齐", "分析", "定位", "优化", "开发", "测试", "联调",
        "确认", "评估", "梳理", "更新"
    ]
}


NOISE_PATTERNS = [
    r"^今日主要进展[:：]?$",
    r"^核心进展[:：]?$",
    r"^困难所需支援[:：]?$",
    r"^困难及风险[:：]?$",
    r"^下一步[:：]?$",
    r"^下周计划[:：]?$",
    r"^成员日报[:：]?$",
    r"^项目[:：]?$",
    r"^无$",
    r"^暂无$",
    r"^无明显.*$",
    r"^none$",
    r"^NA$",
    r"^N/A$",
]


def normalize_fact_text(text):
    text = (text or "").strip()

    if not text:
        return ""

    text = text.replace("\u200b", "")
    text = text.replace("\ufeff", "")
    text = re.sub(r"^[\d一二三四五六七八九十]+[、.．]\s*", "", text)
    text = re.sub(r"^[\-*•]\s*", "", text)
    text = re.sub(r"\s+", " ", text)
    text = text.strip()

    for pattern in NOISE_PATTERNS:
        if re.match(pattern, text, flags=re.IGNORECASE):
            return ""

    if len(text) <= 1:
        return ""

    return text


def split_report_content_to_lines(content):
    content = (content or "").replace("\r\n", "\n").replace("\r", "\n")
    raw_lines = []

    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue

        # 对过长的一行做轻量切分
        parts = re.split(r"[；;]\s*", line)

        for part in parts:
            part = part.strip()
            if part:
                raw_lines.append(part)

    return raw_lines


def classify_section(text):
    text = (text or "").strip()
    if not text:
        return "unknown"

    # 注意顺序：help/risk/next_focus 要优先于 progress
    for section in ["help", "risk", "next_focus", "progress"]:
        for keyword in SECTION_KEYWORDS[section]:
            if keyword.lower() in text.lower():
                return section

    return "progress"


def infer_project_name(text, project_config):
    text = (text or "").strip()

    # 1. 优先项目别名表
    project_aliases = project_config.get("project_aliases", {})

    for canonical_name, aliases in project_aliases.items():
        names = [canonical_name] + (aliases or [])
        for alias in names:
            if alias and alias in text:
                return canonical_name, "alias"

    # 2. 识别【项目名】
    bracket_match = re.search(r"【([^】]{2,50})】", text)
    if bracket_match:
        candidate = bracket_match.group(1).strip()
        if candidate:
            return candidate, "bracket"

    # 3. 识别常见“项目：xxx”
    project_match = re.search(r"(?:项目|课题|模块|主题)[:：]\s*([^\s|，,。；;]{2,50})", text)
    if project_match:
        candidate = project_match.group(1).strip()
        if candidate:
            return candidate, "prefix"

    # 4. 兜底
    return "未分类", "fallback"


def get_fact_confidence(project_source, section):
    confidence = 0.5

    if project_source == "alias":
        confidence += 0.35
    elif project_source == "bracket":
        confidence += 0.25
    elif project_source == "prefix":
        confidence += 0.2

    if section != "unknown":
        confidence += 0.1

    return min(confidence, 0.99)


def build_fact_items_from_weekly_json(weekly_json, project_config):
    fact_items = []

    metadata = weekly_json.get("metadata", {})
    source_urls = metadata.get("source_urls", {})
    users = weekly_json.get("users", [])

    for user in users:
        mention_obj = user.get("user_name", {})
        attrs = mention_obj.get("attrs", {})
        user_id = attrs.get("id", "")
        user_label = attrs.get("label", "未知")

        reports = user.get("reports", [])

        for report in reports:
            report_date = report.get("date", "")
            content = report.get("content", "")

            lines = split_report_content_to_lines(content)

            line_idx = 0

            for raw_line in lines:
                clean_line = normalize_fact_text(raw_line)

                if not clean_line:
                    continue

                line_idx += 1

                section = classify_section(clean_line)
                project_name, project_source = infer_project_name(clean_line, project_config)

                fact_id = f"{report_date}_{user_id}_{line_idx}"

                fact_items.append({
                    "fact_id": fact_id,
                    "date": report_date,
                    "member": mention_obj,
                    "member_label": user_label,
                    "project_name": project_name,
                    "project_source": project_source,
                    "section": section,
                    "text": clean_line,
                    "source_url": source_urls.get(report_date, ""),
                    "confidence": get_fact_confidence(project_source, section)
                })

    return fact_items


# =============================================================================
# Step 3：按项目聚合 FactItem
# =============================================================================
def group_fact_items_by_project(fact_items):
    project_map = {}

    for item in fact_items:
        project_name = item.get("project_name") or "未分类"
        section = item.get("section") or "unknown"

        if project_name not in project_map:
            project_map[project_name] = {
                "project_name": project_name,
                "progress": [],
                "risk": [],
                "help": [],
                "next_focus": [],
                "unknown": []
            }

        if section not in project_map[project_name]:
            section = "unknown"

        project_map[project_name][section].append(item)

    projects = list(project_map.values())
    projects.sort(key=lambda x: (x["project_name"] == "未分类", x["project_name"]))

    return {
        "projects": projects
    }


def deduplicate_fact_items(items):
    seen = set()
    deduped = []

    for item in items:
        member_id = ((item.get("member") or {}).get("attrs") or {}).get("id", "")
        text = item.get("text", "").strip()
        key = (member_id, text)

        if key in seen:
            continue

        seen.add(key)
        deduped.append(item)

    return deduped


def render_fact_item(item):
    mention_md = mention_to_markdown(item.get("member", {}))
    text = item.get("text", "").strip()

    # 避免重复 mention
    if text.startswith(mention_md):
        return f"- {text}"

    return f"- {mention_md} {text}"


def render_grouped_projects_to_markdown(grouped_projects, include_low_confidence=True):
    parts = []

    projects = grouped_projects.get("projects", [])

    for project in projects:
        project_name = project.get("project_name", "未分类")

        progress_items = deduplicate_fact_items(project.get("progress", []))
        risk_items = deduplicate_fact_items(project.get("risk", []))
        help_items = deduplicate_fact_items(project.get("help", []))
        next_items = deduplicate_fact_items(project.get("next_focus", []))
        unknown_items = deduplicate_fact_items(project.get("unknown", []))

        if not include_low_confidence:
            progress_items = [x for x in progress_items if x.get("confidence", 0) >= 0.6]
            risk_items = [x for x in risk_items if x.get("confidence", 0) >= 0.6]
            help_items = [x for x in help_items if x.get("confidence", 0) >= 0.6]
            next_items = [x for x in next_items if x.get("confidence", 0) >= 0.6]

        # 如果某项目完全没内容，跳过
        if not any([progress_items, risk_items, help_items, next_items, unknown_items]):
            continue

        parts.append(f"## 📌 {project_name}")
        parts.append("")

        parts.append("### ✅ 本周核心进展")
        if progress_items:
            for item in progress_items:
                parts.append(render_fact_item(item))
        else:
            parts.append("- 本周暂无明确进展。")
        parts.append("")

        parts.append("### ❗ 困难风险及所需支持")
        combined_risk_help = risk_items + help_items
        if combined_risk_help:
            for item in combined_risk_help:
                parts.append(render_fact_item(item))
        else:
            parts.append("- 本周无阻塞性困难。")
        parts.append("")

        parts.append("### 🙌 Next Key Focus")
        if next_items:
            for item in next_items:
                parts.append(render_fact_item(item))
        else:
            parts.append("- 按既定路线图推进中。")
        parts.append("")

        if unknown_items:
            parts.append("### 📝 其他记录")
            for item in unknown_items:
                parts.append(render_fact_item(item))
            parts.append("")

        parts.append("---")
        parts.append("")

    return "\n".join(parts).strip()


def extract_progress_text_from_grouped_projects(grouped_projects):
    lines = []

    for project in grouped_projects.get("projects", []):
        project_name = project.get("project_name", "未分类")
        progress_items = deduplicate_fact_items(project.get("progress", []))

        if not progress_items:
            continue

        lines.append(f"【{project_name}】")

        for item in progress_items:
            mention_md = mention_to_markdown(item.get("member", {}))
            text = item.get("text", "").strip()
            lines.append(f"- {mention_md} {text}")

    return "\n".join(lines).strip()


# =============================================================================
# LLM 调用：只用于摘要和卡片，不再用于 JSON 结构化
# =============================================================================
def _create_chat_id(conversation_id="", id_type="conversation"):
    response = requests.post(
        BASE_URL + CONVERSATION_ID_ROUTE,
        headers=get_headers_with_ak(),
        json={"conversation_id": conversation_id, "type": id_type}
    )
    response_json = response.json()
    return response_json.get("data", {}).get("id")


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
        raise Exception(f"No task ID: {response_json}")

    return poll_workflow_result(task_message_id)


def _call_llm_with_retry(llm_name, llm_params, context_messages, max_retries=3):
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
                wait_time = min(2 ** (attempt - 1), 15)
                print(f"    ⚠️ AI 调用失败: {e}. {wait_time}秒后重试...")
                time.sleep(wait_time)
            else:
                print(f"    ❌ AI 调用连续 {max_retries} 次失败，放弃重试。错误: {e}")
                raise last_error


def generate_key_summary(progress_content, project):
    project_name = project.get("project_name", "Unknown")
    prompt_file_guid = project.get("weekly_key_summary_prompt_file_guid")

    default_prompt = """请基于以下"本周核心进展"内容，用一段 80~150 字的客观、平实文字总结本周整体进度。

要求：
1. 只基于输入，不允许新增事实。
2. 不要使用"表现优异"、"进展神速"等主观评价。
3. 不要写空泛判断，例如"整体进展顺利"、"符合预期"，除非输入明确体现。
4. 优先保留具体完成内容、验证内容、交付内容、问题修复内容。
5. 如果输入为空或无实质进展，输出："本周暂无核心产出。"
6. 只输出一段文字，不要加标题，不要 Markdown。

输入内容：
{{progress_content}}
"""

    if not progress_content.strip():
        return "本周暂无核心产出。"

    prompt_text = load_prompt_text(prompt_file_guid, default_prompt)
    user_content = prompt_text.replace("{{progress_content}}", progress_content[:12000])

    context_messages = [
        {
            "role": "system",
            "content": "你是周报摘要生成助手，只基于输入生成客观摘要。",
            "variables": []
        },
        {
            "role": "user",
            "content": user_content,
            "variables": []
        }
    ]

    try:
        llm_name = getattr(model, "llm_name", None)
        llm_params = getattr(model, "llm_params", None) or DEFAULT_LLM_PARAMS

        if not llm_name:
            raise ValueError("model.llm_name 不能为空")

        llm_result = _call_llm_with_retry(
            llm_name=llm_name,
            llm_params=llm_params,
            context_messages=context_messages,
            max_retries=3
        )

        summary = strip_markdown_wrapper(llm_result)
        if not summary:
            return "本周暂无核心产出。"

        return summary

    except Exception as e:
        print(f"⚠️ [Key Summary][{project_name}] AI 生成失败，使用规则 fallback: {e}")
        return generate_key_summary_fallback(progress_content)


def generate_key_summary_fallback(progress_content):
    lines = [
        line.strip()
        for line in progress_content.split("\n")
        if line.strip() and not line.strip().startswith("【")
    ]

    if not lines:
        return "本周暂无核心产出。"

    selected = lines[:5]
    text = "；".join([re.sub(r"^[-*]\s*", "", x) for x in selected])

    if len(text) > 180:
        text = text[:180] + "..."

    return text or "本周暂无核心产出。"


# =============================================================================
# 最终 Markdown
# =============================================================================
def build_final_markdown_v2(weekly_json, body_markdown, key_summary):
    metadata = weekly_json.get("metadata", {})
    range_dates = metadata.get("range_dates", [])
    source_urls = metadata.get("source_urls", {})
    week_number = metadata.get("week_number", "")

    start_date = range_dates[0] if range_dates else ""
    end_date = range_dates[-1] if range_dates else ""

    parts = []

    parts.append(f"**日期范围：** {start_date} 至 {end_date} | **周数：** 第 {week_number} 周")
    parts.append("")
    parts.append("**源日报链接：**")

    for report_date in sorted(source_urls.keys()):
        source_url = source_urls.get(report_date, "")
        parts.append(f"- {report_date}: [{source_url}]({source_url})")

    parts.append("")
    parts.append("---")
    parts.append("")

    parts.append("### 🎉 团队关键进展")
    parts.append(key_summary or "本周暂无核心产出。")
    parts.append("")

    if body_markdown:
        parts.append(body_markdown.strip())
    else:
        parts.append("本周暂无可汇总内容。")

    return "\n".join(parts)


# =============================================================================
# API 请求与笔记写入
# =============================================================================
def _request_with_retry(method, url, max_retries=3, **kwargs):
    kwargs.setdefault("timeout", 30)
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            if method == "post":
                response = requests.post(url, **kwargs)
            else:
                response = requests.get(url, **kwargs)

            return response

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_error = e

            if attempt < max_retries:
                wait = min(2 ** attempt, 10)
                print(f"    ⚠️ {url.split('/')[-1]} 第 {attempt} 次请求失败: {e}, {wait}s 后重试...")
                time.sleep(wait)
            else:
                raise last_error


def insert_markdown_to_note(user_guid, note_guid, markdown_content, max_retries=3):
    clean_content = strip_markdown_wrapper(markdown_content)
    html_content = _convert_special_nodes(clean_content)

    response = _request_with_retry(
        "post",
        BASE_URL + MD_INSERT_ROUTE,
        max_retries=max_retries,
        headers=get_headers_with_ak(user_guid=user_guid),
        json={
            "note_guid": note_guid,
            "markdown_content": html_content,
            "mode": "w",
            "location": 1
        },
        timeout=60
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

    response = _request_with_retry(
        "post",
        BASE_URL + WORKSPACE_SAVE_ROUTE,
        max_retries=3,
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

    if doc_id and content:
        try:
            insert_markdown_to_note(creator_guid, doc_id, content, max_retries=5)

        except Exception as e:
            print(f"    ⚠️ 笔记已创建(doc_id={doc_id})但内容写入失败: {e}")
            print("    → 将在 5s 后单独重试写入...")
            time.sleep(5)

            try:
                insert_markdown_to_note(creator_guid, doc_id, content, max_retries=5)
                print("    ✅ 重试写入成功")

            except Exception as e2:
                print(f"    ❌ 重试写入仍失败: {e2}，笔记已创建但内容为空，doc_id={doc_id}")

    return doc_id


def write_debug_note_to_worklog_folder(project, title, markdown_content, extra_tags=None):
    project_name = project.get("project_name", "")
    project_guid = project.get("project_guid")
    work_log_folder_guid = project.get("work_log_folder_guid")
    creator_guid = project.get("weekly_target_user_guid") or USER_GUID

    tags = ["周报", "调试"]
    if extra_tags:
        tags.extend(extra_tags)

    doc_id = create_note_api(
        content=markdown_content,
        title=title,
        project_guid=project_guid,
        parent_guid=work_log_folder_guid,
        tags=tags,
        creator_guid=creator_guid
    )

    if doc_id:
        debug_url = f"{BASE_URL}/workspace/{doc_id}"
        print(f"[Debug][{project_name}] 🧪 调试笔记已写回 work log folder: {debug_url}")
        return debug_url

    return ""


# =============================================================================
# Step 4：创建正式周报笔记
# =============================================================================
def create_final_weekly_note(content, project, week_info):
    try:
        project_name = project.get("project_name", "")
        target_project_guid = project.get("weekly_target_project_guid")
        target_parent_guid = project.get("weekly_target_parent_guid", "0")
        target_user_guid = project.get("weekly_target_user_guid")

        if not target_project_guid:
            raise ValueError(f"配置错误: project '{project_name}' 的 weekly_target_project_guid 为空！")

        print(f"[Step 4][{project_name}] 正在创建正式周报笔记...")

        title = build_weekly_note_title(week_info, project_name)

        doc_id = create_note_api(
            content=content,
            title=title,
            project_guid=target_project_guid,
            parent_guid=target_parent_guid,
            tags=["周报", "AI"],
            creator_guid=target_user_guid
        )

        if not doc_id:
            return [], []

        note_url = f"{BASE_URL}/workspace/{doc_id}"

        print(f"[Step 4][{project_name}] ✅ 正式周报笔记创建完成: {note_url}")

        return [note_url], [title]

    except Exception as e:
        print(f"[Step 4] ❌ 发生异常: {e}")
        traceback.print_exc()
        return [], []


# =============================================================================
# Step 5：发送消息
# =============================================================================
def generate_card_content(project, long_markdown, week_info=None):
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
        content = re.sub(
            r"^###\s+(.+?)\s*$",
            lambda m: f"**{m.group(1).strip()}**",
            content,
            flags=re.MULTILINE
        )

        if len(content) > max_len:
            truncated = content[:max_len]
            suffix = "\n\n......\n[系统提示：AI 生成失败，此为自动截断的格式化预览]"
            return truncated + suffix

        return content

    if week_info is None:
        week_info = get_last_week_info()

    start_date = week_info["start_date"]
    end_date = week_info["end_date"]

    summary_prefix = f"**本周摘要 | {start_date} 至 {end_date}**\n\n"

    meta_header = (
        f"时间范围：{start_date} 至 {end_date} | "
        f"第{week_info['week_number']}周"
    )

    card_input_markdown = f"{meta_header}\n\n{long_markdown[:8000]}"
    user_content = prompt_text.replace("{{markdown_content}}", card_input_markdown)

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
        llm_name = getattr(model, "llm_name", None)
        llm_params = getattr(model, "llm_params", None) or DEFAULT_LLM_PARAMS

        if not llm_name:
            raise ValueError("model.llm_name 不能为空")

        llm_result = _call_llm_with_retry(
            llm_name=llm_name,
            llm_params=llm_params,
            context_messages=context_messages,
            max_retries=3
        )

        return summary_prefix + strip_markdown_wrapper(llm_result)

    except Exception as e:
        print(f"⚠️ [Card][{project_name}] AI 生成失败，使用 fallback: {e}")
        return summary_prefix + fallback_format_content(card_input_markdown, max_len=20000)


def build_feishu_card(title, card_content, note_url, source_note_entries=None):
    elements = [
        {
            "tag": "markdown",
            "content": card_content,
            "margin": "0px",
            "text_size": "normal"
        }
    ]

    if source_note_entries:
        elements.append({"tag": "hr"})

        total_count = len(source_note_entries)
        display_entries = source_note_entries[:5]
        has_more = total_count > 5

        elements.append({
            "tag": "markdown",
            "content": f"**源日报入口**（共 {total_count} 篇）",
            "margin": "0px",
            "text_size": "normal"
        })

        button_items = []

        for item in display_entries:
            date_text = item.get("date", "")
            short_date = date_text[5:] if len(date_text) >= 10 else date_text
            btn_text = f"{short_date} 日报"

            button_items.append({
                "text": btn_text,
                "url": item.get("url", ""),
                "type": "default"
            })

        if has_more:
            button_items.append({
                "text": "更多日报",
                "url": note_url,
                "type": "default"
            })

        for i in range(0, len(button_items), 2):
            pair = button_items[i:i + 2]
            columns = []

            for item in pair:
                columns.append({
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {
                            "tag": "button",
                            "type": item.get("type", "default"),
                            "width": "fill",
                            "margin": "4px 0px 4px 0px",
                            "text": {
                                "tag": "plain_text",
                                "content": item.get("text", "查看")
                            },
                            "behaviors": [
                                {
                                    "type": "open_url",
                                    "default_url": item.get("url", "")
                                }
                            ]
                        }
                    ]
                })

            if len(columns) == 1:
                columns.append({
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": []
                })

            elements.append({
                "tag": "column_set",
                "flex_mode": "stretch",
                "horizontal_spacing": "8px",
                "margin": "0px",
                "columns": columns
            })

    elements.append({
        "tag": "column_set",
        "flex_mode": "stretch",
        "horizontal_spacing": "8px",
        "margin": "8px 0px 0px 0px",
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
                            "content": "查看完整周报"
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
    })

    return {
        "schema": "2.0",
        "header": {
            "padding": "12px 8px 12px 8px",
            "template": "orange",
            "title": {
                "content": title,
                "tag": "plain_text"
            }
        },
        "body": {
            "vertical_spacing": "12px",
            "elements": elements
        }
    }


def send_webhook(webhook_url, card):
    response = requests.post(
        url=webhook_url,
        headers={"Content-Type": "application/json"},
        json={"msg_type": "interactive", "card": card}
    )
    return response.json()


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
        payload["interactive_content"] = json.dumps(interactive_content, ensure_ascii=False)

    return requests.post(
        url=BASE_URL + MESSAGE_SEND_ROUTE,
        headers=get_headers_with_ak(user_guid=sender_guid),
        json=payload
    )


def step5_send_messages(note_url_list, note_title_list, project, content_list, week_info=None, source_note_entries=None):
    try:
        project_name = project.get("project_name", "")

        raw_webhook_config = project.get(f"{generate_type}_webhook_url", [])

        if isinstance(raw_webhook_config, str):
            webhook_urls = [raw_webhook_config] if raw_webhook_config else []
        elif isinstance(raw_webhook_config, list):
            webhook_urls = raw_webhook_config
        else:
            webhook_urls = []

        receiver_guids = normalize_receiver_guids(
            project.get(f"{generate_type}_sender_guid", [])
        )
        sender_guid = project.get(f"{generate_type}_target_user_guid", "") or USER_GUID

        if not note_url_list:
            print(f"[Step 5][{project_name}] ⚠️ 没有 URL 可发送")
            return

        for note_title, note_url, full_content in zip(note_title_list, note_url_list, content_list):
            card_summary = generate_card_content(project, full_content, week_info=week_info)

            card = build_feishu_card(
                note_title,
                card_summary,
                note_url,
                source_note_entries=source_note_entries
            )

            has_sent_any = False

            if webhook_urls:
                for idx, url in enumerate(webhook_urls, 1):
                    try:
                        print(f"[Step 5][{project_name}] 📢 正在发送群消息 (Webhook {idx}/{len(webhook_urls)})...")
                        webhook_result = send_webhook(url, card)

                        if webhook_result.get("code") == 0 or webhook_result.get("StatusCode") == 0:
                            print(f"  -> ✅ 群消息发送成功: {url[:30]}...")
                            has_sent_any = True
                        else:
                            print(f"  -> ❌ 群消息发送失败 ({url}): {webhook_result}")

                    except Exception as e:
                        print(f"  -> ❌ 群消息发送异常 ({url}): {e}")
            else:
                print(f"[Step 5][{project_name}] 📢 未配置 Webhook 地址，跳过群消息发送")

            if receiver_guids:
                try:
                    print(f"[Step 5][{project_name}] 📩 正在发送个人消息给 {len(receiver_guids)} 人...")

                    text_content = build_message_text(note_title, note_url)

                    response = send_message_api(
                        receiver_guids=receiver_guids,
                        title=note_title,
                        content=text_content,
                        sender_guid=sender_guid,
                        interactive_content=card
                    )

                    if response.status_code == 200 and response.json().get("data"):
                        print("  -> ✅ 个人消息发送成功")
                        has_sent_any = True
                    else:
                        print(f"  -> ❌ 个人消息发送失败: {response.text}")

                except Exception as e:
                    print(f"  -> ❌ 个人消息发送异常: {e}")

            if not has_sent_any and not webhook_urls and not receiver_guids:
                print(f"[Step 5][{project_name}] ⚠️ 未配置 Webhook 且未配置接收人，跳过发送步骤")

        print(f"[Step 5][{project_name}] ✅ 消息分发流程结束")

    except Exception as e:
        print(f"[Step 5] ❌ 发生异常: {e}")
        traceback.print_exc()


# =============================================================================
# 调试输出
# =============================================================================
def build_fact_debug_markdown(fact_items):
    parts = []
    parts.append("# FactItem 调试结果")
    parts.append("")

    for item in fact_items:
        mention_md = mention_to_markdown(item.get("member", {}))
        parts.append(
            f"- `{item.get('date')}` | `{item.get('project_name')}` | "
            f"`{item.get('section')}` | `{item.get('project_source')}` | "
            f"`confidence={item.get('confidence')}` | {mention_md} {item.get('text')}"
        )

    return "\n".join(parts)


def build_grouped_debug_markdown(grouped_projects):
    parts = []
    parts.append("# 项目聚合调试结果")
    parts.append("")

    for project in grouped_projects.get("projects", []):
        parts.append(f"## {project.get('project_name')}")
        parts.append(f"- progress: {len(project.get('progress', []))}")
        parts.append(f"- risk: {len(project.get('risk', []))}")
        parts.append(f"- help: {len(project.get('help', []))}")
        parts.append(f"- next_focus: {len(project.get('next_focus', []))}")
        parts.append(f"- unknown: {len(project.get('unknown', []))}")
        parts.append("")

    return "\n".join(parts)


# =============================================================================
# 主执行流程
# =============================================================================
print("=" * 60)
print(f"开始执行周报工作流（规则主导版 v5）| 项目数: {len(projects)}")
print("=" * 60)

for project in projects:
    project_name = project.get("project_name", "Unknown")
    enable_ai = project.get("enable_weekly_summary", True)

    if not enable_ai:
        print(f"\n⏭ 跳过项目: {project_name} (enable_weekly_summary=False)")
        continue

    print(f"\n▶ 处理项目: {project_name}")

    temp_files = []
    week_info = get_last_week_info()

    try:
        # ---------------------------------------------------------------------
        # Step 1：原始 weekly 聚合 json
        # ---------------------------------------------------------------------
        weekly_json, found, step1_temp_files = step1_weekly_summary_note(project)
        temp_files.extend(step1_temp_files)

        if not found:
            print(f"  ⚠️ 跳过 {project_name}")
            cleanup_temp_files(temp_files, project_name=project_name)
            continue

        users = weekly_json.get("users", [])
        if not users:
            raise Exception("没有找到任何用户数据")

        # ---------------------------------------------------------------------
        # Step 2：代码生成 FactItem
        # ---------------------------------------------------------------------
        fact_items = build_fact_items_from_weekly_json(weekly_json, project)

        if not fact_items:
            print(f"[Step 2][{project_name}] ⚠️ 未生成任何 FactItem，将生成空周报")

        fact_json_path = build_intermediate_json_file(
            project["project_guid"],
            f"{week_info['start_date']}_to_{week_info['end_date']}",
            {"fact_items": fact_items},
            suffix="facts"
        )
        temp_files.append(fact_json_path)

        print(f"[Step 2][{project_name}] 📦 FactItem JSON 已生成: {fact_json_path}")
        print(f"[Step 2][{project_name}] ✅ 共生成 FactItem: {len(fact_items)} 条")

        if project.get("write_debug_note", False):
            fact_debug_md = build_fact_debug_markdown(fact_items)
            write_debug_note_to_worklog_folder(
                project,
                title=f"{project_name} 周报 FactItem 调试",
                markdown_content=fact_debug_md,
                extra_tags=["FactItem"]
            )

        # ---------------------------------------------------------------------
        # Step 3：代码按项目聚合
        # ---------------------------------------------------------------------
        grouped_projects = group_fact_items_by_project(fact_items)

        grouped_json_path = build_intermediate_json_file(
            project["project_guid"],
            f"{week_info['start_date']}_to_{week_info['end_date']}",
            grouped_projects,
            suffix="grouped_projects"
        )
        temp_files.append(grouped_json_path)

        print(f"[Step 3][{project_name}] 📦 项目聚合 JSON 已生成: {grouped_json_path}")
        print(f"[Step 3][{project_name}] ✅ 共识别项目: {len(grouped_projects.get('projects', []))} 个")

        if project.get("write_debug_note", False):
            grouped_debug_md = build_grouped_debug_markdown(grouped_projects)
            write_debug_note_to_worklog_folder(
                project,
                title=f"{project_name} 周报项目聚合调试",
                markdown_content=grouped_debug_md,
                extra_tags=["Grouped"]
            )

        # ---------------------------------------------------------------------
        # Step 3.5：代码渲染周报正文
        # ---------------------------------------------------------------------
        include_low_confidence = project.get("include_low_confidence_facts", True)

        body_markdown = render_grouped_projects_to_markdown(
            grouped_projects,
            include_low_confidence=include_low_confidence
        )

        body_md_path = build_intermediate_markdown_file(
            project["project_guid"],
            f"{week_info['start_date']}_to_{week_info['end_date']}_body",
            body_markdown
        )
        temp_files.append(body_md_path)

        print(f"[Step 3.5][{project_name}] 📝 周报正文 Markdown 已生成: {body_md_path}")

        # ---------------------------------------------------------------------
        # Step 3.6：LLM 只生成团队关键进展
        # ---------------------------------------------------------------------
        progress_content = extract_progress_text_from_grouped_projects(grouped_projects)
        key_summary = generate_key_summary(progress_content, project)

        print(f"[Step 3.6][{project_name}] 🎉 团队关键进展已生成")

        # ---------------------------------------------------------------------
        # Step 3.7：拼最终 Markdown
        # ---------------------------------------------------------------------
        final_weekly_markdown = build_final_markdown_v2(
            weekly_json=weekly_json,
            body_markdown=body_markdown,
            key_summary=key_summary
        )

        if not final_weekly_markdown:
            raise Exception("最终周报内容为空")

        final_md_path = build_intermediate_markdown_file(
            project["project_guid"],
            f"{week_info['start_date']}_to_{week_info['end_date']}_final",
            final_weekly_markdown
        )
        temp_files.append(final_md_path)

        print(f"[Step 3.7][{project_name}] 📄 最终周报 Markdown 已生成: {final_md_path}")

        # ---------------------------------------------------------------------
        # Step 4：创建正式周报笔记
        # ---------------------------------------------------------------------
        note_urls, note_titles = create_final_weekly_note(
            final_weekly_markdown,
            project,
            week_info
        )

        # 源日报入口，最多 5 个
        raw_source_urls_map = weekly_json.get("metadata", {}).get("source_urls", {})

        source_note_entries = [
            {"date": report_date, "url": url}
            for report_date, url in sorted(raw_source_urls_map.items())
        ][:5]

        # ---------------------------------------------------------------------
        # Step 5：发消息
        # ---------------------------------------------------------------------
        step5_send_messages(
            note_urls,
            note_titles,
            project,
            [final_weekly_markdown],
            week_info=week_info,
            source_note_entries=source_note_entries
        )

        cleanup_temp_files(temp_files, project_name=project_name)
        print(f"✅ {project_name} 周报流程结束")

    except Exception as e:
        cleanup_temp_files(temp_files, project_name=project_name)
        print(f"❌ {project_name} 周报流程中断: {e}")
        traceback.print_exc()

print("\n" + "=" * 60)
print("全部周报任务执行完毕")
print("=" * 60)