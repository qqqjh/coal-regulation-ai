import io, re, json

def extract_chunks(path, label):
    with io.open(path, encoding='utf-8') as f:
        content = f.read()

    chunks = []

    # Find doc headers to track which document each chunk belongs to
    doc_headers = []
    for m in re.finditer(r"<div class='doc-header'>(.*?)</div>", content, re.DOTALL):
        doc_headers.append((m.start(), re.sub(r'<[^>]+>', '', m.group(1)).strip()))

    # Split by chunk-card divs - find positions
    chunk_positions = []
    for m in re.finditer(r"<div class='chunk-card'>", content):
        chunk_positions.append(m.start())

    for idx, pos in enumerate(chunk_positions):
        end_pos = chunk_positions[idx+1] if idx+1 < len(chunk_positions) else len(content)
        part = content[pos:end_pos]

        chunk = {}
        chunk['label'] = label

        # which doc
        doc = ''
        for dpos, dname in doc_headers:
            if dpos < pos:
                doc = dname
        chunk['doc'] = doc

        # chunk number/label
        m = re.search(r"<span class='chunk-num'>(.*?)</span>", part)
        chunk['num'] = m.group(1).strip() if m else ''

        # status badge
        m = re.search(r"<span class='status-badge[^']*'>(.*?)</span>", part)
        chunk['status'] = m.group(1).strip() if m else ''

        # level tag
        m = re.search(r"<span class='chunk-level-tag'>(.*?)</span>", part)
        chunk['level'] = re.sub(r'<[^>]+>', '', m.group(1)).strip() if m else ''

        # meta info
        m = re.search(r"<div class='chunk-meta'>(.*?)</div>", part, re.DOTALL)
        chunk['meta'] = re.sub(r'<[^>]+>', '', m.group(1)).strip() if m else ''

        # content text
        m = re.search(r"<div class='content-box'>(.*?)</div>", part, re.DOTALL)
        if m:
            text = re.sub(r'<[^>]+>', '', m.group(1))
            chunk['content'] = text.strip()[:300]
        else:
            chunk['content'] = ''

        # issues
        issue_texts = []
        for im in re.finditer(r"<div class='issue-desc'>(.*?)</div>", part, re.DOTALL):
            issue_texts.append(re.sub(r'<[^>]+>', '', im.group(1)).strip()[:200])
        chunk['issues'] = issue_texts

        # issue types
        issue_types = []
        for im in re.finditer(r"<div class='issue-type'>(.*?)</div>", part):
            issue_types.append(im.group(1).strip())
        chunk['issue_types'] = issue_types

        # issue quotes (the law reference)
        quotes = []
        for im in re.finditer(r"<div class='issue-quote'>(.*?)</div>", part, re.DOTALL):
            quotes.append(re.sub(r'<[^>]+>', '', im.group(1)).strip()[:150])
        chunk['quotes'] = quotes

        # kb refs (what law sections cited)
        kb_titles = []
        for im in re.finditer(r"<div class='kb-ref-title'>(.*?)</div>", part):
            kb_titles.append(im.group(1).strip())
        chunk['kb_refs'] = kb_titles

        chunks.append(chunk)

    return chunks


files = {
    'V5': r'd:/work/project/coal-regulation-ai/review_results/nc_only_20260427/V5_NC_only.html',
    'V6': r'd:/work/project/coal-regulation-ai/review_results/nc_only_20260427/V6_NC_only.html',
    'LZY': r'd:/work/project/coal-regulation-ai/review_results/nc_only_20260427/LZY_v5_NC_only.html',
}

all_data = {}
for label, path in files.items():
    chunks = extract_chunks(path, label)
    all_data[label] = chunks
    print(label + ': ' + str(len(chunks)) + ' chunks')

with io.open('nc_chunks_extracted.json', 'w', encoding='utf-8') as f:
    json.dump(all_data, f, ensure_ascii=False, indent=2)

print('Done. Saved to nc_chunks_extracted.json')
