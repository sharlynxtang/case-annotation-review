#!/usr/bin/env python3
# Fetch the 11 cases available on National Archives Find Case Law.
import requests, time, json, re, sys
import bs4

H = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
     'Accept-Language': 'en-GB,en;q=0.9'}

# case_id8 -> TNA url
TNA = {
 '4e559ebd': 'https://caselaw.nationalarchives.gov.uk/ewhc/qb/2021/425',   # Grimshaw v Hudson
 '7b6dafa3': 'https://caselaw.nationalarchives.gov.uk/ewhc/qb/2021/432',   # Walker v Smith
 '45ab8e9e': 'https://caselaw.nationalarchives.gov.uk/ewhc/qb/2020/3522',  # Day v Chivers
 'a6ce0dd1': 'https://caselaw.nationalarchives.gov.uk/ewhc/qb/2020/2154',  # Chichester
 'cb4c2364': 'https://caselaw.nationalarchives.gov.uk/ewhc/qb/2022/2436',  # Evans
 'ce565378': 'https://caselaw.nationalarchives.gov.uk/ewhc/kb/2022/3081',  # Salzer
 '7b752279': 'https://caselaw.nationalarchives.gov.uk/ewhc/qb/2022/1960',  # Hodson
 'c8f5df2c': 'https://caselaw.nationalarchives.gov.uk/ewhc/kb/2023/1220',  # Sestras
 'd1ee43c5': 'https://caselaw.nationalarchives.gov.uk/ewhc/kb/2023/112',   # Lewin v Gray
 'ef17f0c1': 'https://caselaw.nationalarchives.gov.uk/ewhc/kb/2023/1671',  # FLR v Chandran
 '5da8b5f5': 'https://caselaw.nationalarchives.gov.uk/ewhc/kb/2024/1393',  # Doughty (14k, paginated source)
}

def fetch(url, tries=3):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=H, timeout=45)
            if r.status_code == 200:
                r.encoding = 'utf-8'
                return r.text
            last = 'HTTP %s' % r.status_code
        except Exception as e:
            last = str(e)
        time.sleep(2 + i)
    raise RuntimeError('fetch failed %s: %s' % (url, last))

def extract(html):
    soup = bs4.BeautifulSoup(html, 'lxml')
    el = soup.select_one('.judgment-body') or soup.select_one('article') or soup.select_one('main')
    if not el:
        return ''
    # remove scripts/styles
    for t in el(['script', 'style']):
        t.decompose()
    text = el.get_text('\n')
    # collapse excessive blank lines
    lines = [ln.rstrip() for ln in text.split('\n')]
    out = []
    blank = 0
    for ln in lines:
        if ln.strip() == '':
            blank += 1
            if blank <= 1:
                out.append('')
        else:
            blank = 0
            out.append(ln.strip())
    return '\n'.join(out).strip()

results = {}
for cid, url in TNA.items():
    try:
        html = fetch(url)
        txt = extract(html)
        wc = len(txt.split())
        results[cid] = {'url': url, 'text': txt, 'words': wc, 'status': 'OK'}
        print('%s  %5d words  %s' % (cid, wc, url))
        print('   TAIL:', repr(txt[-160:]))
    except Exception as e:
        results[cid] = {'url': url, 'text': '', 'words': 0, 'status': 'FETCH_FAIL', 'err': str(e)}
        print('%s  FAIL %s' % (cid, e))
    time.sleep(1.5)

json.dump(results, open('refetch_tna_out.json', 'w'), ensure_ascii=False)
print('\nSaved refetch_tna_out.json')
