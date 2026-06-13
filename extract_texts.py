#!/usr/bin/env python3
"""Extract original judgment text from findable links and compile into JSON."""
import json
import re
import ssl
import urllib.request
import xml.etree.ElementTree as ET

import certifi

CTX = ssl.create_default_context(cafile=certifi.where())
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

OUT = "/Users/a1234/.verdent/verdent-projects/case-annotation-review/case_texts.json"

# Find Case Law judgments: (case key) -> FCL base URL (full judgment text via /data.xml)
FCL = {
    "Drummond v Keolis Amey Docklands Ltd": {
        "citation": "[2023] EWHC 853 (KB)",
        "url": "https://caselaw.nationalarchives.gov.uk/ewhc/kb/2023/853",
    },
    "JXM v An NHS Trust": {
        "citation": "[2020] EWHC 919 (QB)",
        "url": "https://caselaw.nationalarchives.gov.uk/ewhc/qb/2020/919",
    },
    "Mirza v Farooqui & Anor": {
        "citation": "[2021] EWHC 532 (QB)",
        "url": "https://caselaw.nationalarchives.gov.uk/ewhc/qb/2021/532",
    },
    "NCL v MME": {
        "citation": "[2020] EWHC 2679 (QB)",
        "url": "https://caselaw.nationalarchives.gov.uk/ewhc/qb/2020/2679",
    },
    "Pass v Ministry of Defence": {
        "citation": "[2021] EWHC 243 (QB)",
        "url": "https://caselaw.nationalarchives.gov.uk/ewhc/qb/2021/243",
    },
    "Sivananthan v Vasikaran": {
        "citation": "[2022] EWHC 2938 (KB)",
        "url": "https://caselaw.nationalarchives.gov.uk/ewhc/kb/2022/2938",
    },
    "Spaul & Anor v Southfields Solicitors Ltd": {
        "citation": "[2020] EWHC 1166 (QB)",
        "url": "https://caselaw.nationalarchives.gov.uk/ewhc/qb/2020/1166",
    },
    "Wilson & Ors v Bayer Pharma AG & Ors": {
        "citation": "[2023] EWHC 1282 (KB)",
        "url": "https://caselaw.nationalarchives.gov.uk/ewhc/kb/2023/1282",
    },
}

# PDF judgments (JudiciaryNI)
PDF = {
    "Loughran (Paul) v Piney Rentals Limited & F5 Property Limited": {
        "citation": "[2017] NICty 2",
        "url": "https://www.judiciaryni.uk/sites/judiciary/files/decisions/Loughran%20%28Paul%29%20v%20Piney%20Rentals%20Limited%20%26%20F5%20Property%20Limited.pdf",
        "landing": "https://www.judiciaryni.uk/judicial-decisions/2017-nicty-2",
    },
}

# Cases whose links are paywalled / bot-blocked - text not extractable
BLOCKED = {
    "CDW Ltd v Bird & Anor": {
        "citation": "[2021] EWHC 3665 (QB)",
        "url": "https://www.bailii.org/ew/cases/EWHC/QB/2021/3665.html",
        "reason": "BAILII serves an anti-bot challenge page (Anubis); full text not machine-retrievable. Not on Find Case Law.",
    },
    "Davies v Carter": {
        "citation": "[2021] EWHC 3021 (QB)",
        "url": "https://www.bailii.org/ew/cases/EWHC/QB/2021/3021.html",
        "reason": "BAILII serves an anti-bot challenge page (Anubis); full text not machine-retrievable. Not on Find Case Law.",
    },
    "Mahmood v Liverpool Victoria Insurance Company Ltd": {
        "citation": "[2023] EW Misc 6 (CC)",
        "url": "https://www.casemine.com/judgement/uk/64a709436657545a79eeedd0",
        "reason": "Only available via CaseMine (subscription / 403 to bots). No free full-text source.",
    },
    "Day v Chivers": {
        "citation": "No public neutral citation located",
        "url": "https://www.casemine.com/judgement/uk/5fe970962c94e011bbd49f35",
        "reason": "CaseMine subscription-only (403 to bots); no public neutral citation or free transcript.",
    },
    "Grimshaw v Hudson": {
        "citation": "No public neutral citation located",
        "url": "https://www.casemine.com/judgement/uk/61a0cf0eb50db90e285bb7d7",
        "reason": "CaseMine subscription-only (403 to bots); no public neutral citation or free transcript.",
    },
    "Robinson v Barker & Anor": {
        "citation": "No public neutral citation located",
        "url": "https://www.casemine.com/judgement/uk/5fbb487e2c94e008949a1346",
        "reason": "CaseMine subscription-only (403 to bots); no public neutral citation or free transcript.",
    },
}


def get(url):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=60, context=CTX).read()


def strip_ns(tag):
    return tag.split("}")[-1]


def fcl_fulltext(base_url):
    """Fetch the Akoma Ntoso XML from Find Case Law and return plain judgment text + metadata."""
    xml = get(base_url + "/data.xml")
    root = ET.fromstring(xml)

    # metadata
    meta = {}
    for el in root.iter():
        t = strip_ns(el.tag)
        if t == "FRBRdate" and "date" in el.attrib and "date" not in meta:
            # prefer judgment/decision date
            if el.attrib.get("name", "") in ("judgment", "decision") or "date" not in meta:
                meta["date"] = el.attrib["date"]
        if t == "FRBRname" and "value" in el.attrib and "name" not in meta:
            meta["name"] = el.attrib["value"]
        if t == "TLCOrganization" and "court" not in meta:
            sn = el.attrib.get("showAs", "")
            if "Court" in sn or "Division" in sn:
                meta["court"] = sn

    # judgment body text: collect text from <judgmentBody>
    parts = []
    for el in root.iter():
        if strip_ns(el.tag) == "judgmentBody":
            for sub in el.iter():
                if strip_ns(sub.tag) in ("p", "span") and sub.text:
                    txt = "".join(sub.itertext())
                    txt = re.sub(r"\s+", " ", txt).strip()
                    if txt:
                        parts.append(txt)
            break

    # de-dup consecutive (span nested in p causes repeats): use p-level only
    seen = set()
    clean = []
    for el in root.iter():
        if strip_ns(el.tag) == "judgmentBody":
            for p in el.iter():
                if strip_ns(p.tag) == "p":
                    txt = re.sub(r"\s+", " ", "".join(p.itertext())).strip()
                    if txt and txt not in seen:
                        seen.add(txt)
                        clean.append(txt)
            break

    full = "\n\n".join(clean) if clean else "\n\n".join(parts)

    # header (judge / neutral citation) from <header>
    header_parts = []
    for el in root.iter():
        if strip_ns(el.tag) == "header":
            for p in el.iter():
                if strip_ns(p.tag) == "p":
                    txt = re.sub(r"\s+", " ", "".join(p.itertext())).strip()
                    if txt:
                        header_parts.append(txt)
            break
    header = "\n".join(header_parts)

    return meta, header, full


def pdf_fulltext(url):
    data = get(url)
    tmp = "/tmp/_judgment.pdf"
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        from pypdf import PdfReader
    except ImportError:
        import subprocess
        subprocess.run(["/Users/a1234/.verdent/verdent-projects/case-annotation-review/.venv/bin/pip",
                        "install", "pypdf", "-q"], check=True)
        from pypdf import PdfReader
    reader = PdfReader(tmp)
    pages = [pg.extract_text() or "" for pg in reader.pages]
    return "\n\n".join(pages).strip()


def main():
    result = {
        "source_note": (
            "Original judgment texts extracted from publicly accessible sources. "
            "Find Case Law (The National Archives) full text retrieved via its Akoma Ntoso XML API. "
            "Loughran retrieved from the Judiciary NI official PDF. "
            "Cases marked 'text_not_extracted' have only paywalled (CaseMine) or bot-blocked (BAILII) "
            "sources with no free machine-readable transcript."
        ),
        "extracted": [],
        "text_not_extracted": [],
    }

    for name, info in FCL.items():
        print("Fetching FCL:", name)
        try:
            meta, header, full = fcl_fulltext(info["url"])
            result["extracted"].append({
                "case": name,
                "citation": info["citation"],
                "source": "Find Case Law (The National Archives)",
                "url": info["url"],
                "court": meta.get("court", ""),
                "date": meta.get("date", ""),
                "full_case_name": meta.get("name", ""),
                "header": header,
                "judgment_text": full,
                "char_count": len(full),
            })
            print("   OK chars:", len(full))
        except Exception as e:
            print("   ERROR:", e)
            result["text_not_extracted"].append({
                "case": name, "citation": info["citation"], "url": info["url"],
                "reason": f"Fetch/parse error: {e}",
            })

    for name, info in PDF.items():
        print("Fetching PDF:", name)
        try:
            full = pdf_fulltext(info["url"])
            result["extracted"].append({
                "case": name,
                "citation": info["citation"],
                "source": "Judiciary NI (official PDF)",
                "url": info.get("landing", info["url"]),
                "pdf_url": info["url"],
                "court": "County Court for Northern Ireland",
                "date": "",
                "full_case_name": name,
                "header": "",
                "judgment_text": full,
                "char_count": len(full),
            })
            print("   OK chars:", len(full))
        except Exception as e:
            print("   ERROR:", e)
            result["text_not_extracted"].append({
                "case": name, "citation": info["citation"], "url": info["url"],
                "reason": f"PDF fetch/parse error: {e}",
            })

    for name, info in BLOCKED.items():
        result["text_not_extracted"].append({
            "case": name,
            "citation": info["citation"],
            "url": info["url"],
            "reason": info["reason"],
        })

    result["summary"] = {
        "extracted_count": len(result["extracted"]),
        "not_extracted_count": len(result["text_not_extracted"]),
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("\nSaved:", OUT)
    print("Extracted:", len(result["extracted"]), "| Not extracted:", len(result["text_not_extracted"]))


if __name__ == "__main__":
    main()
