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
    
    
    
    
def insert_markdown_to_note(user_guid, note_guid, markdown_content):
    def downgrade_product_mentions_in_headers(content):
        """
        仅将三级标题（### ...）中的 product mention 降级为纯文本，
        避免在写回笔记时被系统错误解析成“不存在的账号”。

        例如：
        ### [@G5B Platform](mention:uid:id) & [@V1+ Platform](mention:uid:id)
        ->
        ### G5B Platform & V1+ Platform
        """
        lines = content.split("\n")
        new_lines = []

        for line in lines:
            if line.startswith("### "):
                line = re.sub(
                    r"\[@([^\]]+)\]\(mention:[^:]+:[^)]+\)",
                    r"\1",
                    line
                )
            new_lines.append(line)

        return "\n".join(new_lines)

    clean_content = strip_markdown_wrapper(markdown_content)

    # 先把 product header 里的 mention 降级成纯文本
    clean_content = downgrade_product_mentions_in_headers(clean_content)

    # 再做正常 special node 转换（人员 mention 仍然保留）
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