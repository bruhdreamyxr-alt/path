"""Audit WCAG contrast across every palette in ui.py.

Static: parses COLOR_THEMES with ast (no GUI, no ctk import), so it is safe
to run anywhere and cannot leave fingerprints.
"""
import ast
import sys

themes = None
tree = ast.parse(open('ui.py', encoding='utf-8').read())
for node in tree.body:
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == 'COLOR_THEMES':
                themes = ast.literal_eval(node.value)
if not themes:
    sys.exit('COLOR_THEMES not found')


def lum(col):
    col = str(col).lstrip('#')
    if len(col) == 3:
        col = ''.join(c * 2 for c in col)
    parts = []
    for i in (0, 2, 4):
        c = int(col[i:i + 2], 16) / 255
        parts.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = parts
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a, b):
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# min: 4.5 = body text (WCAG 1.4.3), 3.0 = large text/icons/non-text UI
# (1.4.11), 1.08-1.15 = a surface that only has to be *distinguishable* from
# its neighbour (hairline edges, hover fills, active pills). The hairlines are
# not held to a text bar on purpose: they are one pixel of `hover` between a
# card and the window, and several palettes place that step less than 0.15 from
# their own `sidebar_active` - nudging it further would collide with the tone it
# is supposed to sit beside.
PAIRS = [
    ('text', 'bg', 4.5), ('text', 'surface', 4.5), ('text', 'sidebar', 4.5),
    ('text', 'sidebar_active', 4.5),
    ('sub', 'bg', 4.5), ('sub', 'surface', 4.5), ('sub', 'sidebar', 4.5),
    ('sub', 'sidebar_active', 4.5),
    ('accent', 'surface', 3.0), ('accent', 'bg', 3.0), ('accent', 'sidebar', 3.0),
    ('success', 'surface', 3.0), ('warning', 'surface', 3.0),
    ('danger', 'surface', 3.0), ('purple', 'surface', 3.0),
    ('success', 'sidebar', 3.0), ('warning', 'sidebar', 3.0),
    ('danger', 'sidebar', 3.0),
    ('hover', 'surface', 1.08), ('hover', 'sidebar', 1.08),
    ('sidebar_active', 'sidebar', 1.05),
    ('accent_hover', 'accent', 1.05),
]

fails = []
for name, pal in themes.items():
    for fg, bkey, need in PAIRS:
        if fg not in pal or bkey not in pal:
            fails.append((name, fg, bkey, need, -1.0))
            continue
        got = ratio(pal[fg], pal[bkey])
        if got < need:
            fails.append((name, fg, bkey, need, got))

print(f"{len(themes)} palettes, {len(PAIRS)} pairs each "
      f"({len(themes) * len(PAIRS)} checks): {len(fails)} below target")
for name, fg, bkey, need, got in sorted(fails, key=lambda r: r[4]):
    print(f"  {name:<18} {fg} on {bkey}: {got:.2f} (want {need}) "
          f"{themes[name].get(fg)} on {themes[name].get(bkey)}")
