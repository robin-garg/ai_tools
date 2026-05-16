import os
import re
import shutil

def extract_date(filename):
    # Match first YYYYMMDD (8 consecutive digits)
    m = re.search(r'(\d{8})', filename)
    if m:
        d = m.group(1)
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    # Fallback: match YYYY-MM-DD with hyphens
    m = re.search(r'(\d{4}-\d{2}-\d{2})', filename)
    if m:
        return m.group(1)
    return None

def organize(directory):
    moved = []
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        date = extract_date(name)
        dest_folder = os.path.join(directory, date if date else "misc")
        os.makedirs(dest_folder, exist_ok=True)
        shutil.move(path, os.path.join(dest_folder, name))
        moved.append((name, date or "misc"))
    return moved

base = os.path.join(os.path.dirname(__file__), "..")

print("=== LOGS ===")
for name, date in organize(os.path.join(base, "logs")):
    print(f"  {name}  →  {date}/")

print("\n=== REPORTS ===")
for name, date in organize(os.path.join(base, "reports")):
    print(f"  {name}  →  {date}/")

print("\nDone!")
