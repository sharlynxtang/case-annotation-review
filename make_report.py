#!/usr/bin/env python3
import json, datetime, os
ds = json.load(open('data/cases_with_extracted_output.json'))
orig = json.load(open('data/cases_with_extracted_output.backup_prerefetch.json'))
new = json.load(open('refetch_all.json'))

owc = {c['case_id'][:8]: len((c.get('case_text') or '').split()) for c in orig}
nm  = {c['case_id'][:8]: c['case_name'] for c in orig}

# task-defined display order (truncation buckets)
order = [
 ('A. Severe truncation', ['e46aed3c','4e559ebd','7b6dafa3','9cdc7239','45ab8e9e']),
 ('B. Partial truncation', ['17474a77','73ec464f','a6ce0dd1','cb4c2364','ce565378',
                            '7fec9d3a','7b752279','c8f5df2c','d1ee43c5','ef17f0c1','5da8b5f5']),
]
ts = datetime.datetime.now().astimezone().isoformat(timespec='seconds')

succ = sum(1 for v in new.values() if v['status'] == 'SUCCESS')
fail = sum(1 for v in new.values() if v['status'] == 'FAILED')

L = []
L.append('# Case Re-fetch Report')
L.append('')
L.append('Generated: %s' % ts)
L.append('')
L.append('Dataset: `data/cases_with_extracted_output.json` (107 records; 16 targets re-fetched in place).')
L.append('')
L.append('**Result: %d/16 SUCCESS, %d FAILED.** (Acceptance threshold: >=12 SUCCESS.)' % (succ, fail))
L.append('')
L.append('| # | Case | Orig words | New words | x | Status | Source | URL |')
L.append('|---|------|-----------:|----------:|---:|--------|--------|-----|')
i = 0
for bucket, ids in order:
    for cid in ids:
        i += 1
        v = new[cid]
        o = owc[cid]; n = v['words']
        ratio = ('%.1fx' % (n / o)) if o else '-'
        url = v['url']
        L.append('| %d | %s | %d | %d | %s | %s | %s | %s |' % (
            i, nm[cid].replace('|', '/'), o, n, ratio, v['status'], v['source'], url))

L.append('')
L.append('## Notes')
L.append('')
L.append('- **Primary source** for 11 cases: National Archives *Find Case Law* '
         '(`caselaw.nationalarchives.gov.uk`), parsed from the `.judgment-body` element.')
L.append('- **BAILII** is now behind an "Anubis" JavaScript anti-bot challenge and could not be '
         'fetched directly. For BAILII-only cases (Thorley, CDW v Bird, Davies v Carter) the '
         'archived BAILII pages were retrieved via the **Wayback Machine** '
         '(`web.archive.org/.../2id_/...`), which serves the original full HTML/PDF.')
L.append('- **Lord St Davids** full judgment taken from the official **Courts & Tribunals '
         'Judiciary** PDF (`judiciary.uk`). New word count (2,439) is lower than the original '
         'truncated count (2,715) because the prior capture included BAILII page furniture/headnote; '
         'the judiciary.uk text is the complete verbatim judgment (34 numbered paragraphs, ending '
         'with the conviction disposal and the judge\'s signature).')
L.append('- **Doughty v Kazmierski** (paginated source): the complete judgment body from National '
         'Archives is 13,558 words and ends with a proper closing paragraph; the original 14,091 '
         'count was inflated by repeated page furniture across the paginated BAILII capture.')
L.append('- **Mahmood v Liverpool Victoria Insurance Co Ltd [2023] EW Misc 6 (CC)** — **FAILED**. '
         'This unreported County Court judgment (HHJ Malek) is not on National Archives (404), was '
         'never archived on the Wayback Machine (CDX returns no snapshot), has no vLex entry, and '
         'CaseMine only exposes a ~380-word login-gated AI summary. BAILII itself is JS-blocked. '
         'No open verbatim full-text source exists; its `case_text` was left **unchanged** and '
         'flagged `refetch_status=FAILED` for manual handling.')
L.append('')
L.append('## Completeness verification')
L.append('')
L.append('Every SUCCESS case ends in a proper judicial conclusion (e.g. "the claim must be '
         'dismissed", "That is my judgment", a dated order, or a judge\'s signature) rather than a '
         'mid-sentence cut. All but two SUCCESS cases exceed 2x the original word count; the two '
         'exceptions (Lord St Davids, Doughty) are explained above and verified complete by their '
         'closing paragraphs.')
L.append('')
L.append('## Fields written to each target record')
L.append('')
L.append('- `case_text` — replaced with full text (SUCCESS only).')
L.append('- `source_url` — authoritative source URL.')
L.append('- `refetched_at` — ISO timestamp (%s).' % ts)
L.append('- `refetch_status` — SUCCESS / FAILED.')
L.append('- `refetch_source` — provenance label.')
L.append('')
L.append('All other fields (`case_id`, `extracted_output`, annotations, etc.) and the remaining '
         '91 cases are untouched. A backup of the pre-refetch dataset is at '
         '`data/cases_with_extracted_output.backup_prerefetch.json`.')

out = os.path.expanduser('~/Desktop/refetch_report.md')
open(out, 'w').write('\n'.join(L) + '\n')
print('Wrote', out)
print('\n'.join(L[:20]))
