import json
import re
from collections import defaultdict
from types import SimpleNamespace
from zdbase import ZFile
import requests
import pandas as pd

# =========================================================
# 工具函数
# =========================================================
def _try_call(obj, method_name, *args, **kwargs):
    if hasattr(obj, method_name):
        method = getattr(obj, method_name)
        if callable(method):
            try:
                return method(*args, **kwargs)
            except TypeError:
                try:
                    return method()
                except Exception:
                    return None
            except Exception:
                return None
    return None

def _clean_guid(raw_value):
    """清洗 GUID，去除前导单引号，并处理浮点数格式"""
    if pd.isna(raw_value):
        return ""
    s = str(raw_value).strip()
    # 去除前导单引号
    if s.startswith("'"):
        s = s[1:]
    # 处理 Excel 读取的浮点数格式 (如 123.0)
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s

def _get_sheet_rows(data_obj, sheet_name="sheet1"):
    """
    从 ZData / workbook 对象中提取指定 sheet 的行数据
    返回 list[dict]
    """
    raw = getattr(data_obj, "data", data_obj)
    print("DEBUG raw type:", type(raw))
    try:
        print("DEBUG raw attrs:", [x for x in dir(raw) if not x.startswith("_")][:80])
    except Exception:
        pass

    # 1) 如果是 dict，优先找 sheet1
    if isinstance(raw, dict):
        for k, v in raw.items():
            if str(k).lower() == sheet_name.lower():
                return _to_records(v)

        # 没找到 sheet1，就取第一个 sheet
        if raw:
            first_val = next(iter(raw.values()))
            return _to_records(first_val)

    # 2) 如果对象上有 sheet1 / sheets / worksheets 等属性
    for attr in ("sheet1", "Sheet1", "sheets", "worksheets", "sheet", "worksheet"):
        if hasattr(raw, attr):
            val = getattr(raw, attr)

            # 有些是方法
            if callable(val):
                try:
                    # 优先尝试按 sheet 名取
                    try:
                        val = val(sheet_name)
                    except TypeError:
                        val = val()
                except Exception:
                    continue

            # 如果拿到的是 dict，继续找 sheet1
            if isinstance(val, dict):
                for k, v in val.items():
                    if str(k).lower() == sheet_name.lower():
                        return _to_records(v)
                if val:
                    return _to_records(next(iter(val.values())))

            # 如果拿到的是 list/tuple，取第一个
            if isinstance(val, (list, tuple)):
                if len(val) > 0:
                    return _to_records(val[0])
                return []

            # 其它对象直接转
            return _to_records(val)

    # 3) 如果 raw 本身就是 sheet 数据
    return _to_records(raw)

def _dict_to_records(d):
    if not isinstance(d, dict):
        return None

    for key in ("rows", "data", "list", "records", "items", "result"):
        if key in d:
            val = d.get(key)
            rec = _to_records(val)
            if rec is not None:
                return rec

    if d and all(isinstance(v, list) for v in d.values()):
        keys = list(d.keys())
        n = max(len(v) for v in d.values()) if d else 0
        records = []
        for i in range(n):
            row = {}
            for k in keys:
                col = d.get(k, [])
                row[k] = col[i] if i < len(col) else None
            records.append(row)
        return records

    if d and all(isinstance(v, dict) for v in d.values()):
        return list(d.values())

    return [d]

def _to_records(data):
    if data is None:
        return []

    if isinstance(data, str):
        s = data.strip()
        if not s:
            return []
        try:
            obj = json.loads(s)
            return _to_records(obj)
        except Exception:
            raise TypeError("data 是字符串，但不是有效 JSON")

    if hasattr(data, "to_dict"):
        try:
            d = data.to_dict(orient="records")
            if isinstance(d, list):
                return d
        except Exception:
            pass

        try:
            d = data.to_dict()
            rec = _to_records(d)
            if rec is not None:
                return rec
        except Exception:
            pass

    for m in ("to_pandas", "to_df", "to_dataframe", "to_dict", "to_json", "to_list", "to_records", "get_data", "get_rows", "get_records"):
        val = _try_call(data, m)
        if val is not None:
            rec = _to_records(val)
            if rec is not None:
                return rec

    for attr in ("data", "rows", "records", "list", "result", "value", "values", "_data", "_rows", "_records", "_list", "_result", "_value"):
        if hasattr(data, attr):
            try:
                val = getattr(data, attr)
                if callable(val):
                    val = val()
                rec = _to_records(val)
                if rec is not None:
                    return rec
            except Exception:
                pass

    if isinstance(data, dict):
        rec = _dict_to_records(data)
        if rec is not None:
            return rec

    if isinstance(data, (list, tuple)):
        if len(data) == 0:
            return []
        if isinstance(data[0], dict):
            return list(data)

        records = []
        for item in data:
            if isinstance(item, dict):
                records.append(item)
            elif hasattr(item, "to_dict"):
                try:
                    records.append(item.to_dict())
                except Exception:
                    records.append({"value": str(item)})
            elif hasattr(item, "__dict__"):
                d = {k: v for k, v in vars(item).items() if not k.startswith("_") and not callable(v)}
                records.append(d if d else {"value": str(item)})
            else:
                records.append({"value": str(item)})
        return records

    try:
        if hasattr(data, "__iter__") and not isinstance(data, (bytes, bytearray)):
            items = list(data)
            if items:
                return _to_records(items)
    except Exception:
        pass

    if hasattr(data, "__dict__"):
        d = {k: v for k, v in vars(data).items() if not k.startswith("_") and not callable(v)}
        if d:
            return [d]

    raise TypeError(f"无法识别 data 类型：{type(data)}")

def _clean_parent(v):
    if v is None:
        return ""
    s = str(v).strip()
    if s in {"", "-", "None", "null", "NULL"}:
        return ""
    return s

def _clean_leader_name(v):
    if v is None:
        return ""
    s = str(v).strip()
    if s.startswith("@"):
        s = s[1:].strip()
    return s

def _parse_depth(level):
    if level is None:
        return None
    s = str(level).strip()
    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else None

def _clean_text(v):
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except Exception:
        pass

    s = str(v).strip()
    if s in {"", "-", "None", "null", "NULL", "nan", "NaN"}:
        return ""

    # 处理 Excel 里读出来的 123.0
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]

    return s

def get_leader_guid(leader_name, login_name, user_map_df):

    if not leader_name or not login_name or user_map_df is None:
        return ""
    
    try:
        # 强制清洗列名 (防止列名带有空格)
        if len(user_map_df.columns) >= 3:
            user_map_df.columns = ['eid', 'login_name', 'name'] + [f'col_{i}' for i in range(3, len(user_map_df.columns))]
        
        # 清洗数据
        for col in ['login_name', 'name']:
            if col in user_map_df.columns:
                user_map_df[col] = user_map_df[col].astype(str).str.strip()
        
        # 匹配
        match = user_map_df[
            (user_map_df['name'] == str(leader_name).strip()) & 
            (user_map_df['login_name'] == str(login_name).strip())
        ]
        
        if match.empty:
            return ""

        target_eid = match.iloc[0]['eid']
        
        # 调用 API
        resp = requests.get(
            "https://workspace.cxmt.com/api/user/platform/getUserGuidById",
            params={"id": target_eid},
            timeout=10
        )
        
        api_resp = resp.json()
        guid_val = api_resp.get('data')
        
        if guid_val is not None:
            return str(guid_val)
        return ""
            
    except Exception as e:
        print(f"❌ GUID Error: {e}")
        return ""

# =========================================================
# 主逻辑 (增加接收端调试)
# =========================================================
print("📥 开始生成组织配置文件 config_org.json ...")

# --- A. 加载映射表 ---
user_map_df = None
if user_map is not None and hasattr(user_map, 'path'):
    try:
        df = pd.read_excel(user_map.path, engine='openpyxl')
        if len(df.columns) >= 3:
            df.columns = ['eid', 'login_name', 'name', 'extra']
        user_map_df = df 
        print(f"✅ 映射表加载成功，共 {len(df)} 行")
    except Exception as e:
        print(f"❌ 映射表读取失败: {e}")

# --- B. 加载部门数据并构建节点 ---
nodes = {}
parent_map = {}
children_map = defaultdict(list)
name_map = {}

try:
    # 使用 _get_sheet_rows 读取数据 (因为直接读取 ZData 失败了)
    rows = _get_sheet_rows(data, "sheet1")
    # print(f"📄 读取到部门数据 {len(rows)} 行")

    for idx, row in enumerate(rows, start=1):
        # 1. 提取基础字段 (部门ID, 名称, 父ID)
        dept_id = str(row.get("部门ID", row.get("dept_id", ""))).strip()
        if not dept_id:
            continue
        dept_name = str(row.get("部门名称", "")).strip()
        parent_id = str(row.get("父级部门ID", row.get("parent_dept_id", ""))).strip()

        leader_name = str(row.get("部门负责人", "")).strip()
        login_name = str(row.get("负责人id", "")).strip()

        # Project_guid
        raw_project_guid = row.get("Project_guid", "")
        project_guid = _clean_guid(raw_project_guid)
        
        raw_ai_guid = row.get("AI_Log_guid", "")
        output_folder_guid = _clean_guid(raw_ai_guid)

        webhook = _clean_text(row.get("webhook", row.get("Webhook", "")))
        depth = _parse_depth(row.get("层级", row.get("depth", None)))

        leader_guid = get_leader_guid(leader_name, login_name, user_map_df)

        # 6. 打印调试日志 (保留原样)
        # print(f"🔗 第{idx}行 [{dept_id}]: output_folder_guid={output_folder_guid}")

        # 6. 构建节点
        nodes[dept_id] = {
            "dept_id": dept_id,
            "dept_name": dept_name,
            "parent_dept_id": parent_id,
            "leader_name": leader_name,
            "leader_guid": leader_guid,
            "output_folder_guid": output_folder_guid,
            "project_guid": project_guid,   # 文本形式保存
            "webhook": webhook,
            "children": [],
            "is_leaf": False,
            "is_root": False,
            "depth": depth,
        }
        parent_map[dept_id] = parent_id
        name_map[dept_id] = dept_name

    # --- C. 构建树结构 ---
    for dept_id, node in nodes.items():
        p_id = node["parent_dept_id"]
        if p_id and p_id in nodes:
            children_map[p_id].append(dept_id)
        else:
            node["parent_dept_id"] = ""
            parent_map[dept_id] = ""

    for dept_id, node in nodes.items():
        node["children"] = children_map.get(dept_id, [])
        node["is_leaf"] = len(node["children"]) == 0
        node["is_root"] = node["parent_dept_id"] == ""

    root_nodes = [d for d, n in nodes.items() if n["is_root"]]
    leaf_nodes = [d for d, n in nodes.items() if n["is_leaf"]]

    # --- D. 写入文件 ---
    config_data = {
        "ak": ak,
        "sk": sk,
        "org_guid": str(org_id),
        "daily_extract_prompt_file_guid": daily_extract_prompt_file_guid,
        "summary_prompt_file_guid": summary_prompt_file_guid,
        "card_prompt_file_guid": card_prompt_file_guid,
        "non_leaf_summary_prompt_file_guid": non_leaf_summary_prompt_file_guid,
        "message_receiver_guids": message_receiver_guids, 
        "message_sender_guid": message_sender_guid,
        "max_concurrent_llm": max_concurrent_llm,
        "max_concurrent_dept": max_concurrent_dept,
        "user_guid": user_guid,    
        "org_config": {
            "nodes": nodes,
            "root_nodes": root_nodes,
            "leaf_nodes": leaf_nodes,
            "parent_map": parent_map,
            "children_map": dict(children_map),
            "name_map": name_map,
        }
    }
    
    output_path = "/tmp/config_org.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(config_data, f, ensure_ascii=False, indent=2)
        
    print(f"✅ 文件已生成: {output_path}")
    config_org_file = ZFile(output_path, "config_org.json")

except Exception as e:
    print(f"❌ 生成失败：{e}")
    import traceback
    traceback.print_exc()
    config_org_file = SimpleNamespace(path="", source_name="config_org.json")
