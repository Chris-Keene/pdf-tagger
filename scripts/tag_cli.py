#!/usr/bin/env python3
"""Headless tagging, for batch tests and for people who prefer a terminal.

    tag_cli.py in.pdf -o out.pdf [--alt "text"] [--lang en-GB] [--report]
"""
import argparse, json, os, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.analyse import analyse, normalise_headings, heading_warnings
from app.tagger import build


def verapdf(path):
    exe = None
    for c in ("verapdf", "/opt/homebrew/bin/verapdf"):
        try:
            subprocess.run([c, "--version"], capture_output=True, timeout=60)
            exe = c
            break
        except Exception:
            continue
    if not exe:
        return None
    # verapdf exits non-zero on FAIL, so never chain it with &&
    p = subprocess.run([exe, "-f", "ua1", "--format", "xml", path],
                       capture_output=True, text=True, timeout=300)
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(p.stdout)
    except Exception:
        return None
    d = root.find(".//details")
    vr = root.find(".//validationReport")
    if d is None or vr is None:
        return None
    fails = []
    for rule in root.iter("rule"):
        if rule.get("status") == "failed":
            fails.append({"clause": rule.get("clause"),
                          "test": rule.get("testNumber"),
                          "checks": int(rule.get("failedChecks", 0)),
                          "description": (rule.findtext("description") or "").strip()})
    return {"compliant": vr.get("isCompliant") == "true",
            "passed": int(d.get("passedRules", 0)),
            "failed": int(d.get("failedRules", 0)),
            "failedChecks": int(d.get("failedChecks", 0)),
            "rules": fails}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out")
    ap.add_argument("--alt", default="", help="placeholder alt for every figure")
    ap.add_argument("--lang", default="en-GB")
    ap.add_argument("--title", default="")
    ap.add_argument("--report", action="store_true", help="validate the result")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    out = a.out or os.path.splitext(a.pdf)[0] + "-tagged.pdf"
    model = normalise_headings(analyse(a.pdf, lang=a.lang))
    if a.title:
        model.title = a.title
    figs = 0
    for p in model.pages:
        for e in p.elements:
            if e.role == "Figure":
                figs += 1
                if a.alt:
                    e.alt = a.alt
    build(model, out)

    counts = {}
    for p in model.pages:
        for e in p.elements:
            counts[e.role] = counts.get(e.role, 0) + 1
    result = {"in": a.pdf, "out": out, "pages": len(model.pages),
              "was_tagged": model.was_tagged, "title": model.title,
              "roles": counts, "figures": figs,
              "heading_warnings": heading_warnings(model)}
    if a.report:
        result["verapdf"] = verapdf(out)
    if a.json:
        print(json.dumps(result, indent=2))
    else:
        v = result.get("verapdf")
        tag = "COMPLIANT" if v and v["compliant"] else (
            f"FAIL {v['failed']} rules" if v else "not validated")
        print(f"{os.path.basename(a.pdf)[:44]:46} {result['pages']:3}pp  "
              f"{tag:16} {dict(sorted(counts.items()))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
