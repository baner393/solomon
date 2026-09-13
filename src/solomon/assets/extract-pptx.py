#!/usr/bin/env python3
"""Extract text from PPTX files without python-pptx.
Usage: python3 extract-pptx.py <file.pptx or directory> [--output FILE]
"""
import zipfile
import xml.etree.ElementTree as ET
import os
import sys
import json

def extract_pptx_text(filepath):
    """Extract all text from a PPTX file without python-pptx."""
    texts = []
    with zipfile.ZipFile(filepath) as z:
        slides = sorted([n for n in z.namelist()
                         if n.startswith("ppt/slides/slide") and n.endswith(".xml")])
        for slide_name in slides:
            with z.open(slide_name) as f:
                tree = ET.parse(f)
                root = tree.getroot()
                ns = {'a': 'http://schemas.openxmlformats.org/drawingml/2006/main'}
                for t in root.findall('.//a:t', ns):
                    if t.text and t.text.strip():
                        texts.append(t.text.strip())
    return "\n".join(texts)

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    
    path = sys.argv[1]
    output = None
    if "--output" in sys.argv:
        idx = sys.argv.index("--output")
        output = sys.argv[idx + 1]
    
    results = {}
    
    if os.path.isdir(path):
        for fname in sorted(os.listdir(path)):
            if fname.endswith(".pptx"):
                fp = os.path.join(path, fname)
                text = extract_pptx_text(fp)
                results[fname] = text
                print(f"✅ {fname}: {len(text)} chars, {text.count(chr(10))+1} lines")
    elif os.path.isfile(path) and path.endswith(".pptx"):
        text = extract_pptx_text(path)
        results[os.path.basename(path)] = text
        print(f"✅ {os.path.basename(path)}: {len(text)} chars")
    else:
        print(f"❌ Not a PPTX file or directory: {path}")
        sys.exit(1)
    
    if output:
        combined = ""
        for fname, text in results.items():
            combined += f"\n\n{'='*60}\n## {fname}\n{'='*60}\n{text}\n"
        with open(output, "w") as f:
            f.write(combined)
        print(f"\n📄 Saved to {output}")
    elif len(results) > 1:
        # Print all to stdout
        for fname, text in results.items():
            print(f"\n{'='*60}\n## {fname}\n{'='*60}")
            print(text[:500])
            if len(text) > 500:
                print(f"... ({len(text)} total chars)")

if __name__ == "__main__":
    main()
