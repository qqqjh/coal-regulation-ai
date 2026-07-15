import io, json

with io.open('nc_chunks_extracted.json', encoding='utf-8') as f:
    data = json.load(f)

out = []

for label in ['LZY', 'V5', 'V6']:
    chunks = data[label]
    out.append('=' * 70)
    out.append('VERSION: ' + label + '  (' + str(len(chunks)) + ' NC chunks)')
    out.append('=' * 70)
    current_doc = ''
    for c in chunks:
        if c['doc'] != current_doc:
            current_doc = c['doc']
            out.append('')
            out.append('--- DOC: ' + current_doc + ' ---')
        out.append('')
        out.append('[' + c['num'] + '] status=' + c['status'])
        out.append('  level: ' + c['level'])
        out.append('  meta: ' + c['meta'])
        out.append('  content: ' + c['content'][:200].replace('\n', ' '))
        for i, issue in enumerate(c['issues']):
            out.append('  issue' + str(i+1) + ': ' + issue[:150])
        if c['quotes']:
            out.append('  quotes: ' + ' | '.join(c['quotes'][:2]))
    out.append('')

result = '\n'.join(out)
with io.open('nc_details.txt', 'w', encoding='utf-8') as f:
    f.write(result)

print('Written to nc_details.txt, ' + str(len(out)) + ' lines')
