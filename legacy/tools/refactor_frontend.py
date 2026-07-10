"""
Extract each page div from index.html into frontend/pages/{tabId}.html,
and keep JavaScript split across frontend/js/core.js + frontend/js/pages/*.js.
"""
import os, re

FRONTEND = os.path.join(os.path.dirname(__file__), '..', 'frontend')

# Map human-readable page names (lowercased) to tab IDs
PAGE_MAP = {
    'dashboard':        'dashboard',
    'live monitoring':  'live',
    'vehicle counting': 'counting',
    'alerts':           'alerts',
    'cameras':          'cameras',
    'lane config':      'lanes',
    'jobs':             'jobs',
    'models':           'models',
    'analytics':        'analytics',
    'events':           'events',
    'reports':          'reports',
    'system health':    'health',
    'users':            'users',
    'settings':         'settings',
}

def read(fname):
    with open(os.path.join(FRONTEND, fname)) as f:
        return f.read()

def write(fname, content):
    fpath = os.path.join(FRONTEND, fname)
    os.makedirs(os.path.dirname(fpath), exist_ok=True)
    with open(fpath, 'w') as f:
        f.write(content)

def net_div(line):
    opens = len(re.findall(r'<div\b', line))
    closes = len(re.findall(r'</div\s*>', line))
    return opens - closes

def extract_pages(text):
    """Return list of (tab_id, start_lineno, end_lineno, content)."""
    lines = text.split('\n')
    results = []
    i = 0
    while i < len(lines):
        m = re.match(r'\s*<!-- ===== PAGE: (.+?) ===== -->', lines[i])
        if m:
            raw = m.group(1).strip().lower()
            tab_id = PAGE_MAP.get(raw)
            if not tab_id:
                i += 1; continue
            # Find opening div
            div_start = None
            for j in range(i+1, len(lines)):
                if re.match(r'\s*<div id="page-content-\w+"', lines[j]):
                    div_start = j
                    break
            if div_start is None:
                print(f"  ⚠ No div for page '{tab_id}' after line {i+1}")
                i += 1; continue

            depth = 0
            for j in range(div_start, len(lines)):
                depth += net_div(lines[j])
                if depth <= 0:
                    content = '\n'.join(lines[div_start:j+1])
                    results.append((tab_id, div_start, j, content))
                    i = j  # skip past this page
                    break
        i += 1
    return results

def main():
    text = read('index.html')
    pages = extract_pages(text)
    lines = text.split('\n')
    remove = set()
    for tab_id, s, e, content in pages:
        print(f"  {tab_id:12s}  lines {s+1:4d}-{e+1:4d}  ({len(content)} chars)")
        write(f'pages/{tab_id}.html', content + '\n')
        remove.update(range(s, e+1))

    # Remove page divs bottom-up
    for idx in sorted(remove, reverse=True):
        del lines[idx]
    html = '\n'.join(lines)

    # Remove empty lines before </body>
    html = re.sub(r'\n{3,}', '\n\n', html)

    write('index.html', html)
    print(f"\n✅ Done: {len(pages)} pages, index.html={len(html)} chars")

if __name__ == '__main__':
    main()
