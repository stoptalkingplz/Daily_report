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