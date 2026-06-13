#!/usr/bin/env python3
"""In-place update of the 16 target cases in cases_with_extracted_output.json.
- Replaces case_text for SUCCESS cases (full text).
- Adds source_url and refetched_at to all 16 targets (incl. FAILED Mahmood, whose
  case_text is left UNCHANGED).
- Touches no other field and no other case.
Writes a backup first and validates the JSON afterwards.
"""
import json, datetime, hashlib, shutil

SRC = 'data/cases_with_extracted_output.json'
BAK = 'data/cases_with_extracted_output.backup_prerefetch.json'

new = json.load(open('refetch_all.json'))
# refetch_all keys are case_id8 prefixes
ts = datetime.datetime.now().astimezone().isoformat(timespec='seconds')

shutil.copyfile(SRC, BAK)
print('Backup ->', BAK)

ds = json.load(open(SRC))
before_hash = {c['case_id']: hashlib.md5((c.get('case_text') or '').encode()).hexdigest() for c in ds}

updated, untouched_targets = [], []
for c in ds:
    cid8 = c['case_id'][:8]
    if cid8 in new:
        rec = new[cid8]
        c['source_url'] = rec['url']
        c['refetched_at'] = ts
        c['refetch_status'] = rec['status']
        c['refetch_source'] = rec['source']
        if rec['status'] == 'SUCCESS' and rec['text'].strip():
            c['case_text'] = rec['text']
            updated.append((cid8, c['case_name'], rec['words']))
        else:
            untouched_targets.append((cid8, c['case_name']))

json.dump(ds, open(SRC, 'w'), ensure_ascii=False, indent=2)

# validate it reloads
chk = json.load(open(SRC))
assert len(chk) == len(ds) == 107, 'length changed!'

# confirm only target case_texts changed
target_ids = set(new.keys())
changed = []
for c in chk:
    h = hashlib.md5((c.get('case_text') or '').encode()).hexdigest()
    if h != before_hash[c['case_id']]:
        changed.append(c['case_id'][:8])
print('\nUpdated case_text for %d cases.' % len(updated))
for u in updated:
    print('   ', u)
print('Target cases with case_text left unchanged (FAILED):', untouched_targets)
print('\ncase_text changed set:', sorted(changed))
non_target_changed = [x for x in changed if x not in target_ids]
print('NON-target case_text changed (must be empty):', non_target_changed)
assert not non_target_changed, 'A non-target case_text was modified!'
print('\nVALIDATION OK: JSON legal, 107 records, only target texts changed.')
