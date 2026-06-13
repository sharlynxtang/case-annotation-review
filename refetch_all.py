#!/usr/bin/env python3
"""Consolidate full-text refetch for the 16 target cases.
Sources:
  - 11 cases: National Archives Find Case Law (already in refetch_tna_out.json)
  - Thorley, CDW: Wayback Machine archived BAILII HTML
  - Davies v Carter, Lord St Davids: PDF (Wayback BAILII PDF / judiciary.uk PDF)
  - Mahmood: FAILED (no open full-text source)
Outputs refetch_all.json mapping case_id8 -> {url, text, words, status, source}
"""
import requests, time, json, re, io
import bs4, pypdf

H = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/124.0 Safari/537.36',
     'Accept-Language': 'en-GB,en;q=0.9'}


def get(url, tries=3, timeout=50):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=H, timeout=timeout)
            if r.status_code == 200 and len(r.content) > 1500:
                return r
            last = 'HTTP %s len %s' % (r.status_code, len(r.content))
        except Exception as e:
            last = str(e)
        time.sleep(3 + i)
    raise RuntimeError('GET failed %s :: %s' % (url, last))


def clean_lines(text):
    lines = [ln.rstrip() for ln in text.split('\n')]
    out, blank = [], 0
    for ln in lines:
        s = ln.strip()
        if s == '':
            blank += 1
            if blank <= 1:
                out.append('')
        else:
            blank = 0
            out.append(s)
    return '\n'.join(out).strip()


def fix_enc(t):
    # BAILII pages sometimes mis-serve cp1252 pound sign as U+FFFD / mojibake
    return (t.replace('\ufffd', '£')
             .replace('\u00a3', '£'))


def bailii_from_wayback(neutral_path):
    """neutral_path like 'EWHC/QB/2021/2604' -> full judgment text from Wayback BAILII HTML."""
    url = 'http://web.archive.org/web/2id_/http://www.bailii.org/ew/cases/%s.html' % neutral_path
    r = get(url, tries=4, timeout=60)
    r.encoding = 'utf-8'
    soup = bs4.BeautifulSoup(r.text, 'lxml')
    for t in soup(['script', 'style']):
        t.decompose()
    full = soup.get_text('\n')
    full = fix_enc(full)
    # Trim BAILII chrome: start at 'Neutral Citation', end before the BAILII footer block.
    m = re.search(r'Neutral Citation Number', full)
    start = m.start() if m else 0
    # footer marker
    fm = re.search(r'\n\s*BAILII:\s*\n', full)
    end = fm.start() if fm else len(full)
    body = full[start:end]
    canon = 'https://www.bailii.org/ew/cases/%s.html' % neutral_path
    return clean_lines(body), canon


def pdf_text(path_or_bytes):
    if isinstance(path_or_bytes, (bytes, bytearray)):
        rd = pypdf.PdfReader(io.BytesIO(path_or_bytes))
    else:
        rd = pypdf.PdfReader(path_or_bytes)
    txt = '\n'.join((p.extract_text() or '') for p in rd.pages)
    return clean_lines(fix_enc(txt))


results = {}

# 1) load the 11 TNA results
tna = json.load(open('refetch_tna_out.json'))
for cid, v in tna.items():
    results[cid] = {'url': v['url'], 'text': v['text'], 'words': v['words'],
                    'status': 'SUCCESS', 'source': 'National Archives Find Case Law'}

# 2) Thorley (EWHC QB 2021 2604) via Wayback BAILII
print('Thorley...')
txt, canon = bailii_from_wayback('EWHC/QB/2021/2604')
results['17474a77'] = {'url': canon, 'text': txt, 'words': len(txt.split()),
                       'status': 'SUCCESS', 'source': 'BAILII (via Wayback Machine)'}
print('  ', len(txt.split()), 'words; tail:', repr(txt[-90:]))
time.sleep(1.5)

# 3) CDW (EWHC QB 2021 3665) via Wayback BAILII
print('CDW...')
txt, canon = bailii_from_wayback('EWHC/QB/2021/3665')
results['9cdc7239'] = {'url': canon, 'text': txt, 'words': len(txt.split()),
                       'status': 'SUCCESS', 'source': 'BAILII (via Wayback Machine)'}
print('  ', len(txt.split()), 'words; tail:', repr(txt[-90:]))
time.sleep(1.5)

# 4) Davies v Carter (EWHC QB 2021 3021) via Wayback BAILII PDF
print('Davies...')
r = get('https://web.archive.org/web/20240724143152id_/http://www.bailii.org/ew/cases/EWHC/QB/2021/3021.pdf',
        tries=4, timeout=70)
dtxt = pdf_text(r.content)
results['73ec464f'] = {'url': 'https://www.bailii.org/ew/cases/EWHC/QB/2021/3021.pdf',
                       'text': dtxt, 'words': len(dtxt.split()),
                       'status': 'SUCCESS', 'source': 'BAILII PDF (via Wayback Machine)'}
print('  ', len(dtxt.split()), 'words; tail:', repr(dtxt[-90:]))
time.sleep(1.5)

# 5) Lord St Davids via judiciary.uk PDF
print('Lord St Davids...')
r = get('https://www.judiciary.uk/wp-content/uploads/2017/07/r-v-lord-st-davids-20170714-judgment.pdf',
        tries=3, timeout=60)
stxt = pdf_text(r.content)
results['7fec9d3a'] = {'url': 'https://www.judiciary.uk/wp-content/uploads/2017/07/r-v-lord-st-davids-20170714-judgment.pdf',
                       'text': stxt, 'words': len(stxt.split()),
                       'status': 'SUCCESS', 'source': 'Courts & Tribunals Judiciary (judiciary.uk PDF)'}
print('  ', len(stxt.split()), 'words; tail:', repr(stxt[-90:]))

# 6) Mahmood: FAILED - no open full-text source
results['e46aed3c'] = {'url': 'https://www.bailii.org/ew/cases/Misc/2023/6.html',
                       'text': '', 'words': 0, 'status': 'FAILED',
                       'source': 'NOT AVAILABLE (BAILII Anubis-blocked; no Wayback snapshot; not on National Archives; CaseMine login-gated)'}

json.dump(results, open('refetch_all.json', 'w'), ensure_ascii=False)
print('\nSaved refetch_all.json with', len(results), 'entries')
