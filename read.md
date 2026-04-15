可以，叶子层也改成和非叶子层一样的思路最统一：
	•	模型只生成正文
	•	# 部门名
	•	负责人
	•	原日报链接

全部由代码注入。这样叶子和非叶子输出风格就一致了。你现在这版叶子层还是让模型输出整篇，再用 _replace_placeholders(...) 替换，占位符路径和非叶子已经不一致了。 ￼

下面给你直接可替换的修改版。

⸻

1）替换 summarize_dept

把原来的 summarize_dept(...) 整个替换掉：

def summarize_dept(dept_obj, prompt_file_guid):
    """
    叶子部门：模型只负责生成正文部分
    不负责标题、负责人、原日报链接
    """
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


⸻

2）替换 process_leaf_dept

把原来的 process_leaf_dept(...) 整个替换掉：

def process_leaf_dept(dept_id, dept, target_date, dept_daily_note_url_map):
    dept_name = dept.get("dept_name", "Unknown")
    folder_guid = dept.get("output_folder_guid", "")
    project_guid = dept.get("project_guid", "")
    leader_guid = dept.get("leader_guid", "")
    leader_name = dept.get("leader_name", "")
    parent_dept_id = dept.get("parent_dept_id", "")

    print(f"\n  ▶ 处理部门: {dept_name} ({dept_id}) [叶子节点]")

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

    source_link = f"{BASE_URL}/workspace/{note_guid}"
    dept_daily_note_url_map[dept_id] = source_link

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

    dept_list = extracted_json.get("dept", [])
    if not dept_list:
        print(f"    ⚠️ {dept_name} 抽取结果中无 dept 数据，跳过")
        return None

    results = []
    for idx, dept_obj in enumerate(dept_list):
        sub_dept_name = dept_obj.get("dept_name", dept_name)
        print(f"    [4] AI 生成摘要正文: {sub_dept_name} (prompt=叶子)...")

        body_md = summarize_dept(
            dept_obj,
            prompt_file_guid=SUMMARY_PROMPT_FILE_GUID
        )

        header_md = build_summary_header(
            dept_name=dept_name,
            leader_name=leader_name,
            source_note_link=source_link
        )

        summary_md = f"{header_md}\n\n{body_md.strip()}" if body_md.strip() else header_md

        results.append(
            build_summary_result(
                dept_id=dept_id,
                dept_name=dept_name,
                summary_md=summary_md,
                parent_dept_id=parent_dept_id,
                note_guid=note_guid,
                is_leaf=True
            )
        )

    print(f"    ✅ {dept_name} 处理完成")
    return results


⸻

3）_replace_placeholders 可以删掉，或者先留着不用

因为叶子层现在也不再依赖：
	•	[[DEPT_NAME]]
	•	[[DEPT_LEADER_NAME]]
	•	[[SOURCE_NOTE_LINK]]

所以 _replace_placeholders(...) 已经不会再被主流程使用了。
你可以：
	•	先留着不动
	•	或者后面再删

⸻

4）叶子层 prompt 也要改

你原来的叶子 prompt 如果还要求输出整篇完整 Markdown，就要改。
现在建议和非叶子一样，让模型只输出正文。

新的叶子摘要 prompt 建议写成：

请基于以下结构化日报数据，生成部门摘要正文。

要求：
1. 只输出正文部分，不要输出标题，不要输出负责人，不要输出原日报链接。
2. 输出结构固定为以下两个部分：

## 关键进展
- 提炼最重要的进展，避免重复流水账。

## 风险与支持
- 提炼风险、阻塞项、需要协助的事项；如果没有，可以写“暂无”。

输入数据如下：
{{dept_json}}

这样叶子层最终会变成：

# 平台组
负责人：张三 | 原日报链接：[查看源笔记](mentionUrl:...:1:https://.../workspace/note_a_0414)

## 关键进展
- 完成接口联调
- 修复日报解析问题

## 风险与支持
- 需开放测试环境权限以推进测试

而非叶子层会变成：

# 研发中心
负责人：王总 | 原日报链接：[查看源笔记](mentionUrl:...:1:https://.../workspace/研发中心拼接日报guid)

## 综合进展
- 平台组完成接口联调并修复日报解析问题
- 算法组完成模型评测并优化摘要生成prompt

## 风险与支持
- 平台组需开放测试环境权限
- 算法组需补充测试样本

这样两边就统一了：头部都由代码写，模型只管正文。

⸻

5）如果你想顺手再清理一点

你现在这几个函数就可以视情况删掉或废弃：
	•	_replace_placeholders(...)
	•	dept_obj["source_note_link"] = source_link 这段也可以不用了

因为叶子层现在也不再靠占位符替换链接了。 ￼

⸻

6）一句话说清现在的最终结构

到这一步为止，你的代码逻辑就会变成：
	•	叶子原日报 → 抽结构化 JSON → 模型产正文 → 代码拼头部 → 叶子摘要
	•	叶子摘要拼接 → 保存为父层日报 → 父层模型产正文 → 代码拼头部 → 父层摘要

这套结构就很顺了。

如果你要，我下一条直接把把叶子层也改完后的完整整文件最终版再给你一份。