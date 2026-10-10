#!/usr/bin/env python3
"""Build "Buffett Essentials: Selected Letters" as an EPUB for Kindle.

Downloads the Owner's Manual (the current edition, plus the January 1999 and
original June 1996 editions, which still have the "Purchase-Accounting
Adjustments" section and passages later cut) and the 1987, 1989, 2006, 2008
and 2014 shareholder letters from berkshirehathaway.com, turns them into
clean chapters and packages them as EPUB 3 (with an NCX for older readers).

    pip install requests brotli pdfplumber pillow
    python3 build_epub.py [--cache DIR] [--out output/Buffett_Essentials.epub]

The site serves its HTML pages Brotli-compressed, hence `brotli`.
Downloads are cached in --cache (default ~/.cache/buffett-epub), never in
the repository. After packaging, every chapter is compared word by word with
its source and anything missing is reported.
"""
import argparse
import difflib
import functools
import html
import io
import os
import re
import sys
import uuid
import zipfile
from collections import Counter
from datetime import datetime, timezone

import pdfplumber
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
import requests
from PIL import Image, ImageDraw, ImageFont

TITLE = 'Buffett Essentials: Selected Letters'
AUTHOR = 'Warren E. Buffett'
SITE = 'https://www.berkshirehathaway.com/'
CHAPTERS = [  # (slug, chapter title, source URL)
    ('ownman', "The Berkshire Hathaway Owner\u2019s Manual", SITE + 'ownman.pdf'),
    # The current manual no longer has the Introduction and the "Purchase-
    # Accounting Adjustments" section of the earlier editions, and the original
    # has passages found in neither later one, so both earlier editions follow.
    ('owners1999', "The Owner\u2019s Manual: January 1999 Edition", SITE + 'owners.html'),
    ('owners1996', "The Owner\u2019s Manual: Original June 1996 Edition", SITE + '1996ar/manual.html'),
    ('1987', '1987 Shareholder Letter', SITE + 'letters/1987.html'),
    ('1989', '1989 Shareholder Letter', SITE + 'letters/1989.html'),
    ('2006', '2006 Shareholder Letter', SITE + 'letters/2006ltr.pdf'),
    ('2008', '2008 Shareholder Letter', SITE + 'letters/2008ltr.pdf'),
    ('2014', '2014 Shareholder Letter', SITE + 'letters/2014ltr.pdf'),
]
# A short editor's note at the top of a chapter, set apart from the source text.
EDNOTES = {'owners1999': 'This January 1999 edition of the Owner\u2019s Manual is reproduced from '
                         'berkshirehathaway.com/owners.html. It includes the Introduction and the section '
                         '\u201cPurchase-Accounting Adjustments,\u201d which the current edition '
                         '(first chapter) no longer contains.',
           'owners1996': 'This is the original June 1996 edition of the Owner\u2019s Manual, as reproduced in '
                         'Berkshire\u2019s 1996 Annual Report (berkshirehathaway.com/1996ar/manual.html). A few of '
                         'its passages appear in neither later edition, among them the opening paragraph of '
                         '\u201cOwner-Related Business Principles\u201d and a paragraph on look-through earnings '
                         'in \u201cPurchase-Accounting Adjustments.\u201d'}
# Headings that also get their own (nested) entry in the table of contents;
# in ESSAYS they open an essay, which starts on a new page.
SUBTOC = {'owners1999': [r'^PURCHASE-ACCOUNTING ADJUSTMENTS$'],
          'owners1996': [r'^PURCHASE-ACCOUNTING ADJUSTMENTS$'],
          '2014': [r'Past, Present and Future', r'Vice Chairman']}
ESSAYS = {'2014'}
# Descriptions (alt text) of the photographs in the PDFs, by (slug, page).
PHOTOS = {('2014', 22): 'Photograph: Warren Buffett, in a red jacket, with Berkshire\u2019s home-office staff'}
MAX_TABLE_CHARS = 50   # wider tables (in characters), or 5+ columns, become images
MAX_IMAGE_ROWS = 25    # ...unless they are this tall; then an image would be unreadable

FONT_DIRS = ['/usr/share/fonts', '/usr/local/share/fonts', '/Library/Fonts',
             '/System/Library/Fonts', 'C:/Windows/Fonts', os.path.expanduser('~/.fonts')]


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------

def download(url, dest):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    try:
        # The site's firewall rejects the default "python-requests" agent string,
        # so identify the script honestly instead.
        r = requests.get(url, timeout=60, headers={'User-Agent': 'buffett-epub-builder/1.0 (personal ebook build)'})
        r.raise_for_status()
    except requests.RequestException as e:
        sys.exit(f'ERROR: could not download {url}: {e}\nStopping (no workarounds attempted).')
    data = r.content
    if url.endswith('.pdf') and not data.startswith(b'%PDF'):
        sys.exit(f'ERROR: {url} did not return a PDF (got {r.headers.get("content-type")}). Stopping.')
    if url.endswith('.html') and b'<body' not in data[:5000].lower():
        sys.exit(f'ERROR: {url} returned unreadable HTML (content-encoding '
                 f'{r.headers.get("content-encoding")!r}; is the brotli package installed?). Stopping.')
    with open(dest, 'wb') as f:
        f.write(data)
    return dest


# --------------------------------------------------------------------------
# Inline text: a list of (text, style) tokens. Style flags: b=bold, i=italic,
# S=superscript, s=subscript, m=small. 'R:<note id>:<n>' marks a note reference
# and 'br' a forced line break.
# --------------------------------------------------------------------------

def esc(s):
    return html.escape(s, quote=True)


def merge(tokens):
    out = []
    for t, st in tokens:
        if out and out[-1][1] == st and not st.startswith('R:') and st != 'br':
            out[-1] = (out[-1][0] + t, st)
        elif t:
            out.append((t, st))
    return out


def plain(tokens):
    return ''.join(t for t, _ in tokens)


def strip_tokens(tokens):
    tokens = merge(tokens)
    while tokens and not tokens[0][0].strip():
        tokens.pop(0)
    while tokens and not tokens[-1][0].strip():
        tokens.pop()
    if tokens:
        tokens[0] = (tokens[0][0].lstrip(), tokens[0][1])
        tokens[-1] = (tokens[-1][0].rstrip(), tokens[-1][1])
    return tokens


def space_style(a, b):
    """Style for a space between two styled runs: bold/italic/small shared by both."""
    return ''.join(f for f in 'bim' if f in a and f in b)


def to_html(tokens):
    parts = []
    for t, st in merge(tokens):
        if st.startswith('R:'):
            _, fid, n = st.split(':')
            parts.append(f'<a class="nref" epub:type="noteref" id="r-{fid}-{n}" href="#{fid}">{esc(t)}</a>')
            continue
        if st == 'br':
            parts.append('<br/>')
            continue
        h = esc(t)
        for flag, tag in (('S', 'sup'), ('s', 'sub'), ('m', 'small'), ('i', 'i'), ('b', 'b')):
            if flag in st:
                h = f'<{tag}>{h}</{tag}>'
        parts.append(h)
    return ''.join(parts)


VOCAB = Counter()   # words seen in the sources, used to decide line-end hyphens
HYPHEN_LOG = []


def build_vocab(texts):
    for t in texts:
        for line in t.split('\n'):
            line = line.rstrip()
            if line.endswith('-'):
                line = line.rsplit(None, 1)[0] if ' ' in line.strip() else ''
            for w in re.findall(r"[A-Za-z]+(?:-[A-Za-z]+)*", line):
                VOCAB[w.lower()] += 1
                for part in w.split('-'):
                    VOCAB[part.lower()] += 1


def keep_hyphen(left, right):
    """A line ends in 'left-' and the next starts with 'right'. In these sources
    line-end hyphens are nearly always real (giant-company, non-financial), so
    the hyphen is dropped only when the joined word is attested and the
    hyphenated one is not."""
    a = re.search(r'[A-Za-z]+$', left)
    b = re.match(r'[A-Za-z]+', right)
    if not (a and b):
        return True
    hyph, joined = f'{a.group()}-{b.group()}'.lower(), (a.group() + b.group()).lower()
    keep = bool(VOCAB[hyph]) or not VOCAB[joined]
    HYPHEN_LOG.append(f'{a.group()}-{b.group()} -> {"kept" if keep else "joined"}')
    return keep


def join_lines(lines_tokens):
    """Join the token lists of consecutive lines into one paragraph,
    healing line-end hyphenation."""
    out = []
    for lt in lines_tokens:
        lt = strip_tokens(lt)
        if not lt:
            continue
        if out:
            prev = plain(out)
            m = re.search(r'([A-Za-z0-9])-$', prev)
            if m and lt[0][0][:1].isalnum() and not prev.endswith(' -'):
                if not keep_hyphen(prev[:-1], lt[0][0]):
                    t, st = out[-1]
                    out[-1] = (t[:-1], st)
            elif re.search(r'\w[\u2013\u2014]$', prev) and lt[0][0][:1].isalnum():
                pass   # "all\u2013" + "important": a dash used as a hyphen
            else:
                out.append((' ', space_style(out[-1][1], lt[0][1])))
        out.extend(lt)
    return merge(out)


# --------------------------------------------------------------------------
# Blocks shared by both parsers
#   {'t': 'h2'|'p', 'tok': tokens, 'cls': str}
#   {'t': 'li', 'marker': str, 'paras': [tokens, ...]}
#   {'t': 'sep'}   {'t': 'sig', 'date': str, 'lines': [str]}
#   {'t': 'table', ...} (see Table)
# Footnotes: {'marker': '*', 'tok': tokens, 'scope': ...}
# --------------------------------------------------------------------------

UNIT_RE = re.compile(r'\((?:in |dollars in )?\$? ?(?:millions|thousands|billions)\)|\(\d+s omitted\)')
NUM_RE = re.compile(r'^(\$\s?)?\(?\$?\s?[\d,]*\.?\d+\)?%?(\s?\*{1,3})?$|^N/A$|^[\u2014\u2013-]$|^\(\d\)-\(\d\)$|^\(\d\)$')
YEAR_RE = re.compile(r'^(19|20)\d\d\b')


def is_num(text):
    return bool(NUM_RE.match(text.strip().replace(' ', '')))


class Table:
    """A table: header rows of (tokens, first col, last col) cells and body rows
    of per-column token lists. `raw` describes how to draw it as an image."""

    def __init__(self, header, rows, ncols, indents=None):
        self.header, self.rows, self.ncols = header, rows, ncols
        self.indents = indents or [0] * len(rows)
        # right-aligned figure columns; a column of years reads better as text
        self.numeric = [sum(is_num(plain(r[c])) and not YEAR_RE.match(plain(r[c]).strip())
                            for r in rows if plain(r[c]).strip()) >
                        sum(1 for r in rows if plain(r[c]).strip()) / 2 for c in range(ncols)]
        self.raw = None      # for the image: ('pdf', path, page, crop box) or ('text', styled lines)
        self.notes = []      # footnotes that belong to this table
        self.rules = {}      # row -> {column: 1 or 2}: single or double rule below the cell
        self.gaps = set()    # rows set off from the one above by extra space

    def mark_layout(self, spans, cols, rules):
        """From a PDF table's drawing: the rules below figures (a subtotal or
        total follows; totals are often double-ruled) and the extra space that
        groups rows. spans: (top, base) of each row; cols: column extents."""
        lo, hi = min(a for a, _ in cols) - 12, max(b for _, b in cols) + 12
        found = {}
        for x0, top, x1, bottom in rules:
            y = (top + bottom) / 2
            if bottom - top > 2.5 or x1 - x0 < 8 or x0 < lo or x1 > hi:
                continue                    # boxes, specks, rules beside the table
            above = [i for i, (t, b) in enumerate(spans) if (t + b) / 2 < y]
            if not above or y > spans[above[-1]][1] + 12:
                continue                    # rules under the headings or well below the table
            for c, (a, b) in enumerate(cols):
                if min(x1, b) - max(x0, a) > 0.5 * (b - a):
                    found.setdefault((above[-1], c), []).append(y)
        for (i, c), ys in found.items():
            ys.sort()
            self.rules.setdefault(i, {})[c] = 1 + any(b - a > 1.2 for a, b in zip(ys, ys[1:]))
        if len(spans) > 2:
            space = sorted(spans[i][0] - spans[i - 1][1] for i in range(1, len(spans)))
            line = sorted(b - t for t, b in spans)[len(spans) // 2]
            usual = space[len(space) // 2] + 0.3 * line
            # A single rule (a subtotal follows) brings space of its own, so only
            # more than that sets off a group; a double rule closes one anyway.
            self.gaps = {i for i in range(1, len(spans)) if spans[i][0] - spans[i - 1][1] >
                         usual + (0.45 * line if set(self.rules.get(i - 1, {}).values()) == {1} else 0)}

    def text_width(self):
        """Rough width in characters when wrapping text columns but not numbers."""
        total = 0
        for c in range(self.ncols):
            cells = [plain(r[c]).strip() for r in self.rows]
            longest = max((len(x) for x in cells), default=0)
            if self.numeric[c]:
                w = longest
            else:
                words = max((len(w) for x in cells for w in x.split()), default=0)
                w = max(min(longest, 14), words)
            for toks, c0, c1 in (h for row in self.header for h in row):
                if c0 == c1 == c:
                    w = max(w, max((len(x) for x in plain(toks).split()), default=0))
            total += w + 2
        return total - 2

    @property
    def wide(self):
        too_wide = self.text_width() > MAX_TABLE_CHARS or self.ncols >= 5
        return too_wide and len(self.rows) <= MAX_IMAGE_ROWS

    def plain_text(self):
        parts = [plain(t) for row in self.header for t, _, _ in row]
        parts += [' '.join(plain(c) for c in r if plain(c).strip()) for r in self.rows]
        return ' / '.join(p.strip() for p in parts if p.strip())


def build_columns(rows, gap):
    """rows: lists of segments (x0, x1, tokens). Numbers are right-aligned, so
    numeric segments that overlap (within `gap`) form a column; text segments
    either fall inside such a column or fill the slot between two of them.
    Returns (column extents, rows of per-column token lists)."""
    numcols = []
    for a, b in sorted((s[0], s[1]) for r in rows for s in r if is_num(plain(s[2]))):
        if numcols and a <= numcols[-1][1] + gap:
            numcols[-1][1] = max(numcols[-1][1], b)
        else:
            numcols.append([a, b])

    def home(seg):
        a, b, toks = seg
        for k, (c0, c1) in enumerate(numcols):
            if min(b, c1) - max(a, c0) > 0:
                return ('n', k)
        return ('t', sum(1 for c0, c1 in numcols if c1 <= a))

    keys = {}
    for r in rows:
        for seg in r:
            k = home(seg)
            lo, hi = keys.get(k, (seg[0], seg[1]))
            keys[k] = (min(lo, seg[0]), max(hi, seg[1]))
    order = sorted(keys, key=lambda k: (k[1], 0 if k[0] == 't' else 1))
    index = {k: i for i, k in enumerate(order)}
    out = []
    for r in rows:
        cells = [[] for _ in order]
        for seg in sorted(r, key=lambda s: s[0]):
            c = cells[index[home(seg)]]
            if c:
                c.append((' ', ''))
            c.extend(seg[2])
        out.append([merge(c) for c in cells])
    return [keys[k] for k in order], out


def header_rows(fragments, colspans):
    """fragments: (x0, x1, top, tokens) of header text; colspans: (x0, x1) of each
    column. Returns header rows of (tokens, c0, c1): the widest headings on top,
    single-column ones at the bottom; stacked fragments are joined."""
    n = len(colspans)
    bounds = []   # each column widened to the middle of the gap next to it
    for c, (a, b) in enumerate(colspans):
        lo = (colspans[c - 1][1] + a) / 2 if c else -1e9
        hi = (b + colspans[c + 1][0]) / 2 if c + 1 < n else 1e9
        bounds.append((lo, hi))

    def hits(x0, x1):
        h = [c for c, (lo, hi) in enumerate(bounds) if min(x1, hi) - max(x0, lo) > 0.25 * min(x1 - x0, hi - lo)]
        return h or [min(range(n), key=lambda c: abs((x0 + x1) / 2 - sum(colspans[c]) / 2))]
    # fragments per line, not counting the stub (first) column
    per_line = Counter(f[2] for f in fragments if hits(f[0], f[1]) != [0])
    top_line = min((f[2] for f in fragments), default=None)
    groups = {}
    for x0, x1, top, toks in sorted(fragments, key=lambda f: (f[2], f[0])):
        hit = hits(x0, x1)
        lone = sum(f[2] == top for f in fragments) == 1
        if lone and top == top_line and hit == [0] and len(set(f[2] for f in fragments)) > 1:
            hit = list(range(n))   # a caption above the column headings
        elif per_line[top] == 1 and hit != [0] and (len(hit) > 1 or x0 < bounds[hit[0]][0] or x1 > bounds[hit[0]][1]
                                                     or UNIT_RE.fullmatch(plain(toks).strip())):
            # a heading alone over the figures that does not fit one column spans
            # the widest group of columns it is centred over; so does a unit
            # caption like "(in millions)", even when it fits under one heading
            mid = (x0 + x1) / 2
            for a, b in sorted(((a, b) for a in range(n) for b in range(a + 1, n)), key=lambda g: g[0] - g[1]):
                g0, g1 = colspans[a][0], colspans[b][1]
                if (a <= hit[0] and hit[-1] <= b and abs(mid - (g0 + g1) / 2) <= 0.1 * (g1 - g0) + 1
                        and x1 - x0 <= 1.2 * (g1 - g0)):
                    hit = list(range(a, b + 1))
                    break
        groups.setdefault((hit[0], hit[-1]), []).append(toks)
    levels = {}   # span width -> non-overlapping spans
    for (c0, c1), toks in sorted(groups.items()):
        lv = levels.setdefault(c1 - c0, [])
        if lv and c0 <= lv[-1][1]:
            lv[-1] = (lv[-1][0], max(c1, lv[-1][1]), lv[-1][2] + toks)
        else:
            lv.append((c0, c1, list(toks)))
    rows = []
    for width in sorted(levels, reverse=True):
        if width == 0:   # one cell per column
            have = {c0: join_frag(toks) for c0, _, toks in levels[0]}
            rows.append([(have.get(c, []), c, c) for c in range(n)])
            continue
        row, c = [], 0
        for c0, c1, toks in levels[width]:
            if c0 > c:
                row.append(([], c, c0 - 1))
            row.append((join_frag(toks), c0, c1))
            c = c1 + 1
        if c < n:
            row.append(([], c, n - 1))
        rows.append(row)
    return rows


def join_frag(tok_lists):
    out = []
    for t in tok_lists:
        t = strip_tokens(t)
        if out and t:
            out.append((' ', space_style(out[-1][1], t[0][1])))
        out.extend(t)
    return merge(out)


# --------------------------------------------------------------------------
# 1987 / 1989: HTML pages whose letter is one big <PRE> block
# --------------------------------------------------------------------------

def pre_styled_lines(pre):
    """Split <PRE> content into lines of (char, style) pairs. <U> (typewriter
    underlining of titles) becomes italics; <FONT SIZE=-1> becomes small."""
    lines, cur, st = [], [], set()
    for part in re.split(r'(<[^>]*>)', pre):
        if part.startswith('<'):
            m = re.match(r'<\s*(/?)\s*([A-Za-z]+)', part)
            flag = m and {'B': 'b', 'I': 'i', 'U': 'i', 'FONT': 'm'}.get(m.group(2).upper())
            if flag:
                (st.discard if m.group(1) else st.add)(flag)
            continue
        for ch in html.unescape(part):
            if ch == '\n':
                lines.append(cur)
                cur = []
            elif ch == '\t':
                cur.extend([(' ', '')] * (8 - len(cur) % 8))
            elif ch != '\r':
                cur.append((ch, ''.join(sorted(st))))
    lines.append(cur)
    return lines


def ltext(line):
    return ''.join(c for c, _ in line).rstrip()


def chars_tokens(chars):
    """(char, style) pairs -> tokens, collapsing runs of spaces."""
    toks, pending, prev = [], False, ''
    for ch, st in chars:
        if ch == ' ':
            pending = bool(toks)
            continue
        if pending:
            toks.append((' ', space_style(prev, st)))
            pending = False
        toks.append((ch, st))
        prev = st
    return merge(toks)


def pre_bullet(line):
    m = re.match(r'(\s*)o(\s+)\S', ltext(line))
    return m if m and 'b' in line[len(m.group(1))][1] else None


def pre_numbered(line):
    return re.match(r'\s{3,6}(\(\d+\))\s+\S', ltext(line))


def pre_tabular(line):
    t = ltext(line)
    if pre_bullet(line):
        return False
    return bool(re.search(r'\.{4,}|-{4,}|={4,}', t) or re.search(r'\S {3,}\S', t.strip())
                or len(t) - len(t.lstrip()) >= 12)


def parse_pre(raw):
    page = raw.decode('latin-1')
    pre = re.search(r'<PRE>(.*)</PRE>', page, re.S | re.I).group(1)
    blocks, notes = [], []
    head = re.search(r'<P ALIGN=CENTER>(.*?)</P>', page, re.S | re.I)
    if head:
        hl = [c for ln in pre_styled_lines(head.group(1)) for c in ln + [(' ', '')]]
        blocks.append({'t': 'p', 'cls': 'center', 'tok': strip_tokens(chars_tokens(hl))})

    chunks, cur = [], []
    for ln in pre_styled_lines(pre):
        if ltext(ln).strip():
            cur.append(ln)
        elif cur:
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)

    for chunk in chunks:
        texts = [ltext(ln) for ln in chunk]
        first = texts[0].strip()
        if all(re.fullmatch(r'\s*(\*\s*){3,}', t) for t in texts):
            blocks.append({'t': 'sep'})
        elif any(re.search(r'Warren E\. Buffett|Chairman of the Board', t) for t in texts) and \
                any(len(t) - len(t.lstrip()) >= 20 for t in texts):
            left = [t[:30].strip() for t in texts if t[:30].strip()]
            right = [t[30:].strip() for t in texts if t[30:].strip()]
            blocks.append({'t': 'sig', 'date': ' '.join(left), 'lines': right})
        elif first.startswith('*'):
            tok = join_lines([chars_tokens(ln) for ln in chunk])
            m = re.match(r'(\*+)\s*', plain(tok))
            scope = blocks[-1] if blocks and blocks[-1]['t'] == 'table' else None
            notes.append({'marker': m.group(1), 'tok': strip_tokens(cut_prefix(tok, m.end())), 'scope': scope})
        elif sum(map(pre_tabular, chunk)) >= max(1, len(chunk) / 2) or \
                any(re.fullmatch(r'\s*[-=][-= ]{3,}', t) for t in texts):
            if blocks and blocks[-1]['t'] == 'table' and blocks[-1].get('src_lines'):
                blocks[-1]['src_lines'] += [[]] + chunk
            else:
                blocks.append({'t': 'table', 'src_lines': list(chunk)})
        elif len(chunk) == 1 and not texts[0].startswith(' ') and \
                all('b' in st for c, st in chunk[0] if c.strip()):
            tok = strip_tokens(chars_tokens(chunk[0]))
            if first.endswith(':'):
                blocks.append({'t': 'p', 'cls': 'noindent', 'tok': tok})
            else:
                blocks.append({'t': 'h2', 'tok': [(t, st.replace('b', '')) for t, st in tok]})
        else:
            blocks.extend(pre_paragraphs(chunk))

    for b in blocks:
        if b['t'] == 'table':
            b.update(t='table', table=pre_table(b.pop('src_lines')))
    # a "Source: ..." line right below a table is a table caption, not body text
    for i, b in enumerate(blocks):
        if b['t'] == 'p' and i and blocks[i - 1]['t'] == 'table' and plain(b['tok']).startswith('Source:'):
            b['cls'] = 'tsrc'
    return blocks, notes


def cut_prefix(tokens, n):
    """Drop the first n characters of a token list."""
    out = []
    for t, st in tokens:
        if n >= len(t):
            n -= len(t)
            continue
        out.append((t[n:], st))
        n = 0
    return out


def pre_paragraphs(chunk):
    paras = []
    for ln in chunk:
        lead = len(ltext(ln)) - len(ltext(ln).lstrip())
        if not paras or pre_bullet(ln) or pre_numbered(ln) or 3 <= lead <= 6:
            paras.append([ln])
        else:
            paras[-1].append(ln)
    out = []
    for para in paras:
        b, n = pre_bullet(para[0]), pre_numbered(para[0])
        if b:
            skip = b.end() - 1
            tok = join_lines([chars_tokens(para[0][skip:])] + [chars_tokens(l) for l in para[1:]])
            out.append({'t': 'li', 'marker': '\u2022', 'paras': [tok]})
        elif n:
            tok = join_lines([chars_tokens(para[0][n.end() - 1:])] + [chars_tokens(l) for l in para[1:]])
            out.append({'t': 'li', 'marker': n.group(1), 'paras': [tok]})
        else:
            lead = len(ltext(para[0])) - len(ltext(para[0]).lstrip())
            tok = join_lines([chars_tokens(l) for l in para])
            out.append({'t': 'p', 'cls': '' if lead else 'noindent', 'tok': tok})
    return out


# --------------------------------------------------------------------------
# January 1999 Owner's Manual: a plain HTML page of <P> paragraphs, an <OL>
# of principles (each in italics, followed by its commentary) and a <PRE>
# signature
# --------------------------------------------------------------------------

def is_pre_letter(raw):
    """1987/1989: the whole letter is typed into one <PRE> block."""
    page = raw.decode('latin-1')
    m = re.search(r'<PRE>(.*)</PRE>', page, re.S | re.I)
    return bool(m) and len(m.group(1)) > len(page) / 2


def html_units(page):
    """Split the body of a simple HTML page into units ('p', 'li' or 'pre'),
    each with the list item it belongs to and its source lines of (char,
    style) pairs; None stands for a <BR>."""
    body = re.sub(r'(?is)^.*?<body[^>]*>|</body>.*$', '', page)
    body = re.sub(r'(?s)<!--.*?-->|<![^>]*>', '', body)   # incl. markers like <! 1. >
    units, st, item, count = [], set(), 0, 0

    def new(kind, center=False):
        units.append({'kind': kind, 'center': center, 'item': item, 'lines': [[]]})
    new('p')
    for part in re.split(r'(<[^>]*>)', body):
        if not part.startswith('<'):
            for ch in html.unescape(part):
                if ch == '\n':
                    units[-1]['lines'].append([])
                elif ch != '\r':
                    units[-1]['lines'][-1].append((' ' if ch == '\t' else ch, ''.join(sorted(st))))
            continue
        m = re.match(r'<\s*(/?)\s*([A-Za-z]+)', part)
        if not m:
            continue
        close, tag = m.group(1), m.group(2).upper()
        flag = {'B': 'b', 'I': 'i', 'U': 'i', 'FONT': 'm'}.get(tag)
        if flag:
            (st.discard if close else st.add)(flag)
        elif tag == 'BR':
            units[-1]['lines'] += [None, []]
        elif tag == 'OL':
            item, count = 0, 0
            new('p')
        elif tag == 'LI' and not close:
            count += 1
            item = count
            new('li')
        elif tag == 'P' and not close:
            new('p', bool(re.search(r'(?i)align\s*=\s*"?center', part)))
        elif tag in ('P', 'HR', 'PRE'):
            new('pre' if tag == 'PRE' and not close else 'p')
    return [u for u in units if any(ln and any(c.strip() for c, _ in ln) for ln in u['lines'])]


def unit_tokens(lines):
    """A unit's lines -> tokens: lines are joined (healing line-end hyphens),
    a <BR> becomes a forced line break."""
    runs, cur = [], []
    for ln in lines:
        if ln is None:
            runs.append(cur)
            cur = []
        else:
            cur.append(chars_tokens(ln))
    runs.append(cur)
    tok = []
    for run in runs:
        joined = join_lines(run)
        if joined:
            if tok:
                tok.append(('\n', 'br'))
            tok += joined
    return strip_tokens(tok)


def parse_html(raw):
    # \x9b is a cent sign typed in code page 437 ("assets for 80¢");
    # \x92 a Windows apostrophe
    page = raw.decode('latin-1').replace('\x9b', '¢').replace('\x92', '’')
    blocks, notes = [], []
    for u in html_units(page):
        if u['kind'] == 'pre':
            lines = [ltext(ln).strip() for ln in u['lines'] if ln and ltext(ln).strip()]
            blocks.append({'t': 'sig', 'date': '', 'lines': lines})
            continue
        tok = unit_tokens(u['lines'])
        text = plain(tok)
        if u['center']:
            blocks.append({'t': 'p', 'cls': 'center', 'tok': tok})
        elif 'm' in tok[0][1] and text.startswith('*'):   # the small-print copyright note
            m = re.match(r'(\*+)\s*', text)
            tok = [(t, st.replace('m', '')) for t, st in cut_prefix(tok, m.end())]
            notes.append({'marker': m.group(1), 'tok': strip_tokens(tok), 'scope': None})
        elif text.isupper() and re.search(r'[A-Z]{3}', text) and len(text) <= 60 and not text.endswith('.'):
            blocks.append({'t': 'h2', 'tok': tok})
        elif u['kind'] == 'li':
            blocks.append({'t': 'li', 'marker': f'{u["item"]}.', 'paras': [tok]})
        elif u['item'] and blocks and blocks[-1]['t'] == 'li':   # commentary on a principle
            blocks[-1]['paras'].append(tok)
        else:
            blocks.append({'t': 'p', 'cls': '', 'tok': tok})
    return blocks, notes


def pre_table(lines):
    texts = [ltext(ln) for ln in lines]
    rule = [bool(re.fullmatch(r'\s*[-=][-= ]*', t)) for t in texts]
    italic = [bool(t.strip()) and all('i' in st for c, st in ln if c.strip()) for t, ln in zip(texts, lines)]
    # The letters set table headings in italics: the header is everything before
    # the first plain (non-italic, non-rule) line.
    first_data = next((k for k, t in enumerate(texts) if t.strip() and not rule[k] and not italic[k]), 0)
    if not any(italic[:first_data]):
        first_data = next((k for k, t in enumerate(texts) if re.search(r'\.{3,}|\d,\d{3}', t)), 0)
    clean = [re.sub(r'(?<= )\.{2,}', lambda m: ' ' * len(m.group()), t) for t in texts]

    def segments(k):
        segs = []
        for m in re.finditer(r'\S+(?: \S+)*', clean[k]):
            chars = [(ch, st) for i, (ch, st) in enumerate(lines[k]) if m.start() <= i < m.end()]
            segs.append((m.start(), m.end(), strip_tokens(chars_tokens(chars))))
        return tidy_segments(segs)

    data = [segments(k) for k in range(first_data, len(lines)) if texts[k].strip() and not rule[k]]
    frags = [(a, b, k, toks) for k in range(first_data) if not rule[k] for a, b, toks in segments(k)]
    t = assemble_table(data, frags, gap=0, indent_unit=2)
    # image source: the table exactly as typed, minus common indentation
    keep = list(lines)
    while keep and not ltext(keep[-1]).strip():
        keep.pop()
    lead = min(len(ltext(l)) - len(ltext(l).lstrip()) for l in keep if ltext(l).strip())
    t.raw = ('text', [l[lead:] for l in keep])
    return t


def tidy_segments(segs):
    """A lone '$' belongs to the number after it; a lone '*'/'**' to the cell before it."""
    out = []
    for s in segs:
        txt = plain(s[2]).strip()
        if out and plain(out[-1][2]).strip() == '$':
            p = out.pop()
            s = (p[0], s[1], [('$', '')] + strip_tokens(s[2]))
        elif out and re.fullmatch(r'\*{1,3}', txt):
            p = out.pop()
            s = (p[0], s[1], p[2] + [(' ' + txt, '')])
        out.append(s)
    return out


def assemble_table(seg_rows, header_frags, gap, indent_unit, pos=None, rules=None):
    """Turn rows of (x0, x1, tokens) segments into a Table. A label-only line
    that does not end in ':' is the first half of a wrapped label. With the
    (top, base) of each line and the page's rules, PDF tables also get their
    subtotal/total rules and row grouping."""
    rows, leads, spans, carry = [], [], [], None
    pos = pos or [(0, 0)] * len(seg_rows)
    for k, segs in enumerate(seg_rows):
        if not segs:
            continue
        lead, span = segs[0][0], pos[k]
        if carry:
            if not is_num(plain(segs[0][2])):
                segs = [(segs[0][0], segs[0][1], join_frag([carry[1][0][2], segs[0][2]]))] + segs[1:]
                lead, span = carry[0], (carry[2][0], span[1])
            else:
                rows.append(carry[1])
                leads.append(carry[0])
                spans.append(carry[2])
            carry = None
        numeric = any(is_num(plain(s[2])) for s in segs)
        if (not numeric and len(segs) == 1 and k < len(seg_rows) - 1
                and not plain(segs[0][2]).rstrip().endswith(':')):
            carry = (lead, segs, span)
            continue
        rows.append(segs)
        leads.append(lead)
        spans.append(span)
    if carry:
        rows.append(carry[1])
        leads.append(carry[0])
        spans.append(carry[2])
    cols, cells = build_columns(rows, gap)
    text_leads = [l for r, l in zip(rows, leads) if not is_num(plain(r[0][2]))]
    base = min(text_leads, default=0)
    indents = [min(3, round((l - base) / indent_unit)) if not is_num(plain(r[0][2])) else 0
               for r, l in zip(rows, leads)]
    t = Table(header_rows(header_frags, cols), cells, len(cols), indents)
    if rules is not None:
        t.mark_layout(spans, cols, rules)
    return t


# --------------------------------------------------------------------------
# PDFs: Owner's Manual, 2006, 2008, 2014
# --------------------------------------------------------------------------

BULLET = '•'
MARKER_RE = re.compile(r'^(\*{1,3}|†|‡)')
SEP_RE = re.compile(r'^(\*\s*){3,}$')
LISTMARK_RE = re.compile(r'^\(?(\d{1,2}|[ivx]{1,5})[.)]$')
DATE_RE = re.compile(r'^(January|February|March|April|May|June|July|August|September|October|'
                     r'November|December) \d{1,2}, \d{4}$')
LETTERHEAD_RE = re.compile(r'^BERKSHIRE HATHAWAY INC\.?$')
FRACTIONS = {'1/2': '½', '1/4': '¼', '3/4': '¾', '1/3': '⅓', '2/3': '⅔'}
ABBREV_RE = re.compile(r'\b(Inc|Co|Corp|Cos|Ltd|Bros|Mfg|Jr|Sr)$')


def pdf_word(w):
    font = w['fontname'].split('+')[-1]
    text = w['text'].replace('(cid:129)', BULLET).replace('\u00a0', ' ')
    if font.startswith('IIISpecialChar') and text == '‹':
        text = BULLET   # this font draws its '‹' glyph as a bullet
    st = ('b' if 'Bold' in font else '') + ('i' if 'Italic' in font or 'Oblique' in font else '')
    return dict(text=text, x0=w['x0'], x1=w['x1'], top=w['top'], bottom=w['bottom'], size=w['size'], st=st)


def page_lines(page, body):
    """Words grouped into lines by baseline; small raised or lowered words
    become super/subscripts of the line they sit on."""
    words = [pdf_word(w) for w in page.extract_words(x_tolerance=1.5, y_tolerance=2,
                                                     extra_attrs=['fontname', 'size'])]
    words = [w for w in words if w['text'].strip()]
    lines = []
    for w in sorted((w for w in words if w['size'] >= 0.8 * body), key=lambda w: w['bottom']):
        if lines and w['bottom'] - lines[-1]['base'] <= 3.0:
            lines[-1]['words'].append(w)
        else:
            lines.append({'base': w['bottom'], 'words': [w]})
    for L in lines:   # the baseline most of the line's text sits on
        L['base'] = Counter(round(w['bottom'], 1) for w in L['words'] for _ in w['text']).most_common(1)[0][0]
    for w in (w for w in words if w['size'] < 0.8 * body):
        near = [L for L in lines if L['base'] - 1.2 * body <= w['bottom'] <= L['base'] + 0.5 * body]
        if not near:
            lines.append({'base': w['bottom'], 'words': [w]})
            continue
        L = min(near, key=lambda L: abs(L['base'] - w['bottom']))
        if w['text'] != BULLET:
            if w['bottom'] < L['base'] - 1.5:
                w['st'] += 'S'
            elif w['bottom'] > L['base'] + 0.8:
                w['st'] += 's'
        L['words'].append(w)
    for L in lines:
        L['words'].sort(key=lambda w: w['x0'])
        main = [w for w in L['words'] if not set('Ss') & set(w['st'])] or L['words']
        L['x0'], L['x1'] = L['words'][0]['x0'], L['words'][-1]['x1']
        L['top'] = min(w['top'] for w in main)
        L['size'] = Counter(round(w['size'], 1) for w in main for _ in w['text']).most_common(1)[0][0]
        L['text'] = ' '.join(w['text'] for w in L['words'])
    return sorted(lines, key=lambda L: L['base'])


def words_tokens(words):
    """Words of one line -> tokens. A space is inserted wherever the PDF leaves
    a gap; superscript fractions (1⁄2) become single characters."""
    ws, k = [], 0
    while k < len(words):
        w = words[k]
        if (k + 2 < len(words) and words[k + 1]['text'] == '⁄' and 'S' in w['st']
                and w['text'].isdigit() and words[k + 2]['text'].isdigit()):
            frac = f"{w['text']}/{words[k + 2]['text']}"
            w = dict(w, text=FRACTIONS.get(frac, frac.replace('/', '⁄')), x1=words[k + 2]['x1'],
                     st=w['st'].replace('S', ''), glue=bool(ws) and w['x0'] - ws[-1]['x1'] < 3)
            k += 2
        ws.append(w)
        k += 1
    toks, prev = [], None
    for w in ws:
        if prev is not None and w['x0'] - prev['x1'] > 0.8 and not w.get('glue'):
            toks.append((' ', space_style(prev['st'], w['st'])))
        toks.append((w['text'], w['st']))
        prev = w
    return merge(toks)


def has_leaders(line):
    run = best = 0
    for w in line['words']:
        if re.fullmatch(r'\.+', w['text']):
            run += len(w['text'])
            best = max(best, run)
        else:
            run = 0
        if re.search(r'\.{5,}$', w['text']):
            best = max(best, 5)
    return best >= 4


def pdf_segments(line, gap=9.0):
    """Split a table line into cells at wide gaps and at dot leaders."""
    segs, cur = [], []

    def flush():
        if cur:
            segs.append((cur[0]['x0'], cur[-1]['x1'], words_tokens(cur)))
            cur.clear()
    for w in line['words']:
        if re.fullmatch(r'\.+', w['text']) and has_leaders(line):
            flush()
            continue
        m = re.match(r'(.*?[^.])\.{3,}$', w['text'])
        if m:   # "Primary......" -> "Primary", but keep the period of "Inc."
            text = m.group(1) + ('.' if ABBREV_RE.search(m.group(1)) else '')
            w = dict(w, text=text, x1=w['x0'] + (w['x1'] - w['x0']) * len(text) / len(w['text']))
        if cur and w['x0'] - cur[-1]['x1'] > gap:
            flush()
        cur.append(w)
        if m:
            flush()
    flush()
    return tidy_segments(segs)


def is_row(line):
    if has_leaders(line):
        return True
    segs = pdf_segments(line)
    rest = [plain(s[2]) for s in segs[1:]]
    return len(segs) >= 2 and any(map(is_num, rest)) and all(is_num(t) or len(t.split()) <= 5 for t in rest)


def headerish(line, L, R):
    """Could this line be part of a table heading?"""
    t = line['text'].strip()
    if len(pdf_segments(line)) >= 2:
        return True
    if t.endswith(':') or len(t.split()) > 8 or all('b' in w['st'] for w in line['words']):
        return False
    if all('i' in w['st'] for w in line['words']):
        return True
    return line['x1'] - line['x0'] < 0.45 * (R - L) and line['x0'] > L + 45


def starts_item(chunk, k):
    """Is line k of the chunk the start of a bulleted or numbered item?"""
    ws = chunk[k]['words']
    if ws[0]['text'] == BULLET:
        return True
    if len(ws) < 2 or not LISTMARK_RE.match(ws[0]['text']):
        return False
    hanging = k + 1 < len(chunk) and abs(chunk[k + 1]['x0'] - ws[1]['x0']) < 3
    return ws[1]['x0'] - ws[0]['x1'] >= 4 or hanging


def doc_layout(pages, body):
    """Left margin (document-wide), right margin (per page) and the vertical
    gap that separates paragraphs."""
    rights = []
    for lines in pages:
        xs = Counter(round(L['x1']) for L in lines if abs(L['size'] - body) < 0.6
                     and len(L['words']) >= 8 and not has_leaders(L))
        rights.append(xs.most_common(1)[0][0] if xs else None)
    default_r = Counter(r for r in rights if r).most_common(1)[0][0]
    rights = [r or default_r for r in rights]
    full = Counter(round(L['x0']) for lines, r in zip(pages, rights) for L in lines
                   if L['x1'] >= r - 3 and abs(L['size'] - body) < 0.6)
    left = min(x for x, n in full.items() if n >= 0.1 * sum(full.values()))
    gaps = Counter(round((b['top'] - a['base']) * 2) / 2 for lines in pages for a, b in zip(lines, lines[1:])
                   if abs(a['size'] - body) < 0.6 and abs(b['size'] - body) < 0.6 and b['top'] > a['base'])
    return left, rights, gaps.most_common(1)[0][0] + 2.0


def strip_furniture(pages, heights):
    """Drop page numbers and running headers/footers."""
    for lines, h in zip(pages, heights):
        lines[:] = [L for L in lines if not (re.fullmatch(r'\d{1,3}', L['text'].strip())
                                             and (L['top'] > 0.85 * h or L['base'] < 0.08 * h))]
    for pos in (0, -1):
        keyed = Counter(re.sub(r'\d+', '#', lines[pos]['text']) for lines in pages if lines)
        for lines in pages:
            if lines and len(pages) >= 4 and keyed[re.sub(r'\d+', '#', lines[pos]['text'])] >= 3:
                lines.pop(pos)


def chunks_of(lines, thr):
    """Split a page into runs of lines without a paragraph-sized gap between
    them; a '* * *' separator is always a chunk of its own."""
    chunks = []
    for L in lines:
        sep = SEP_RE.match(L['text'])
        if (chunks and not sep and not SEP_RE.match(chunks[-1][-1]['text'])
                and L['top'] - chunks[-1][-1]['base'] <= thr and abs(L['size'] - chunks[-1][-1]['size']) < 0.6):
            chunks[-1].append(L)
        else:
            chunks.append([L])
    return chunks


def page_items(chunks, L, R):
    """Classify a page's chunks and group table headings, rows and notes."""
    kinds = []
    for c in chunks:
        rows = [is_row(l) for l in c]
        if all(SEP_RE.match(l['text']) for l in c):
            kinds.append('sep')
        elif MARKER_RE.match(c[0]['text']):
            kinds.append('note')
        elif sum(rows) >= 2 and 2 * sum(rows) >= len(c) or \
                sum(rows) >= 1 and all(r or headerish(l, L, R) for r, l in zip(rows, c)):
            kinds.append('table')
        else:
            kinds.append('text')
    for i in range(len(chunks) - 2, -1, -1):
        if (kinds[i] in ('text', 'table') and kinds[i + 1] in ('table', 'thead')
                and all(headerish(l, L, R) and not (kinds[i] == 'table' and data_row(l)) for l in chunks[i])
                and chunks[i + 1][0]['top'] - chunks[i][-1]['base'] < 30):
            kinds[i] = 'thead'
    items, i = [], 0
    while i < len(chunks):
        if kinds[i] not in ('thead', 'table'):
            items.append((kinds[i], chunks[i]))
            i += 1
            continue
        head, rows, notes = [], [], []
        while i < len(chunks) and kinds[i] == 'thead':
            head += chunks[i]
            i += 1
        while i < len(chunks) and kinds[i] == 'table' and (not rows or chunks[i][0]['top'] - rows[-1]['base'] < 30):
            rows += chunks[i]
            i += 1
        while i < len(chunks) and kinds[i] == 'note' and chunks[i][0]['top'] - (notes or rows or head)[-1]['base'] < 25:
            notes += chunks[i]
            i += 1
        lines = head + rows
        intro = []   # a sentence introducing the table can share its chunk
        while lines and not is_row(lines[0]) and not headerish(lines[0], L, R):
            intro.append(lines.pop(0))
        if intro:
            items.append(('text', intro))
        tail = []    # so can notes ("* Includes ...") right below it
        while lines and MARKER_RE.match(lines[-1]['text']) and not SEP_RE.match(lines[-1]['text']):
            tail.insert(0, lines.pop())
        while lines and tail and not MARKER_RE.match(tail[0]['text']):
            tail.insert(0, lines.pop())   # a note's own continuation lines
        notes = tail + notes
        if any(map(is_row, lines)):
            items.append(('table', lines, notes))
        elif lines:
            items.append(('text', lines))
    return items


def data_row(l):
    """Leaders or real figures; '(1)', years and italic captions are headings."""
    if has_leaders(l):
        return True
    if all('i' in w['st'] or 'b' in w['st'] for w in l['words']):
        return False
    return any(is_num(t) and not re.fullmatch(r'\(\d\)(-\(\d\))?|(19|20)\d\d\*{0,3}', t)
               for t in (plain(s[2]).strip() for s in pdf_segments(l)))


def pdf_table(lines, rules=()):
    first = next((k for k, l in enumerate(lines) if data_row(l)), 0)
    head, rows = lines[:first], lines[first:]
    frags = [(a, b, l['top'], toks) for l in head for a, b, toks in pdf_segments(l, gap=8)]
    return assemble_table([pdf_segments(l) for l in rows], frags, gap=2, indent_unit=6,
                          pos=[(l['top'], l['base']) for l in rows], rules=list(rules))


def crop_box(tlines, page, rules, height):
    """Where a table sits on its page: its text and ruling lines, but never
    the text just above or below it (an introducing sentence, its notes)."""
    x0, x1 = min(l['x0'] for l in tlines), max(l['x1'] for l in tlines)
    top, bot = min(l['top'] for l in tlines), max(l['base'] for l in tlines)
    for gx0, gtop, gx1, gbot in rules:
        if top - 8 <= gtop and gbot <= bot + 8 and gx1 > x0 and gx0 < x1:
            x0, top, x1, bot = min(x0, gx0), min(top, gtop), max(x1, gx1), max(bot, gbot)
    inside = set(map(id, tlines))
    above = max((l['base'] for l in page if id(l) not in inside and l['base'] <= tlines[0]['top'] + 1), default=0)
    below = min((l['top'] for l in page if id(l) not in inside and l['top'] >= tlines[-1]['base'] - 1),
                default=height)
    return x0 - 6, max(top - 5, above + 0.5), x1 + 6, min(bot + 5, below - 0.5)


def note_items(lines, scope, R):
    notes = []
    for l in lines:
        m = MARKER_RE.match(l['words'][0]['text'])
        if m:
            w0 = l['words'][0]
            rest = w0['text'][len(m.group(1)):]
            words = ([dict(w0, text=rest)] if rest else []) + l['words'][1:]
            notes.append({'marker': m.group(1), 'scope': scope, 'lines': [dict(l, words=words)]})
        elif notes:
            notes[-1]['lines'].append(l)
    for n in notes:   # keep the note's own line breaks where a line stops short
        tok, run = [], []
        for k, l in enumerate(n['lines']):
            run.append(words_tokens(l['words']))
            if k + 1 < len(n['lines']) and l['x1'] < R - 60:
                tok += join_lines(run) + [('\n', 'br')]
                run = []
        n['tok'] = strip_tokens(tok + join_lines(run))
        del n['lines']
    return notes


def pdf_photos(path):
    """Per page, (top, index) of each embedded image big enough to be a
    photograph (rules and specks are drawn otherwise or are tiny)."""
    out = []
    doc = pdfium.PdfDocument(path)
    for pno in range(len(doc)):
        page = doc[pno]
        h, found = page.get_height(), []
        for k, obj in enumerate(page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE])):
            x0, y0, x1, y1 = obj.get_bounds()
            if x1 - x0 >= 36 and y1 - y0 >= 36:   # at least half an inch each way
                found.append((h - y1, k))
        out.append(found)
        page.close()
    doc.close()
    return out


def photo_bytes(path, pno, k):
    """An embedded photograph at its own resolution, as an RGB JPEG: print
    PDFs carry CMYK JPEGs, which many e-readers cannot show."""
    doc = pdfium.PdfDocument(path)
    page = doc[pno]
    obj = list(page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE]))[k]
    img = obj.get_bitmap(render=False).to_pil().convert('RGB')
    page.close()
    doc.close()
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=90, optimize=True)
    return buf.getvalue()


def parse_pdf(path):
    with pdfplumber.open(path) as pdf:
        body = Counter(round(c['size'], 1) for p in pdf.pages for c in p.chars).most_common(1)[0][0]
        pages = [page_lines(p, body) for p in pdf.pages]
        heights = [p.height for p in pdf.pages]
        rules = [[(o['x0'], o['top'], o['x1'], o['bottom']) for o in p.lines + p.rects] for p in pdf.pages]
    strip_furniture(pages, heights)
    L, rights, thr = doc_layout(pages, body)
    pics = pdf_photos(path)
    blocks, notes = [], []

    def text_block(chunk, pno, R):
        """A chunk of prose lines -> paragraphs, list items, headings, signatures."""
        t = ' '.join(l['text'] for l in chunk).strip()
        center = (L + R) / 2
        one = len(chunk) == 1
        x0, x1 = min(l['x0'] for l in chunk), max(l['x1'] for l in chunk)
        centered = abs((x0 + x1) / 2 - center) < 20 and x1 - x0 < 0.8 * (R - L)
        bold = all('b' in w['st'] or not re.search(r'\w', w['text']) for l in chunk for w in l['words'])
        caps = t.upper() == t and re.search(r'[A-Z]{3}', t)
        small = 'small' if chunk[0]['size'] < body - 0.4 else ''
        if one and LETTERHEAD_RE.match(t):
            return [{'t': 'p', 'cls': 'center', 'lines': chunk, 'pages': {pno}}]
        if one and (bold or caps) and len(t) <= 80 and not t.endswith(':') and not t.endswith('.'):
            return [{'t': 'h2', 'cls': 'center' if centered and x0 > L + 20 else '', 'lines': chunk, 'pages': {pno}}]
        right = L + 0.4 * (R - L)

        def sig_line(l):
            segs = pdf_segments(l, 30)
            first = plain(segs[0][2]).strip()
            return l['x0'] > right or (DATE_RE.match(first) and (len(segs) == 1 or segs[1][0] > right))
        if len(chunk) <= 4 and all(map(sig_line, chunk)) and any(l['x1'] > right + 40 for l in chunk):
            date, names = [], []
            for l in chunk:
                segs = pdf_segments(l, 30)
                if l['x0'] <= right:
                    date.append(plain(segs[0][2]))
                    segs = segs[1:]
                names += [plain(s[2]) for s in segs]
            return [{'t': 'sig', 'date': ' '.join(date), 'lines': names}]
        if centered and len(chunk) <= 3 and x0 > L + 20:
            return [{'t': 'p', 'cls': 'center', 'lines': chunk, 'pages': {pno}, 'br': True}]
        out = []
        paras = [[0]]
        for k in range(1, len(chunk)):
            prev, l = chunk[k - 1], chunk[k]
            if starts_item(chunk, k) or (l['x0'] > prev['x0'] + 12 and prev['x1'] < R - 25
                                         and not starts_item(chunk, k - 1)):
                paras.append([k])
            else:
                paras[-1].append(k)
        for idx in paras:
            ls = [chunk[k] for k in idx]
            if starts_item(chunk, idx[0]):
                first = ls[0]
                mark = first['words'][0]['text']
                ls = [dict(first, words=first['words'][1:], x0=first['words'][1]['x0'])] + ls[1:]
                out.append({'t': 'li', 'marker': mark, 'paras': [ls], 'indent': ls[0]['x0'], 'pages': {pno},
                            'mx': first['x0']})
                continue
            indented = ls[0]['x0'] > (ls[1]['x0'] if len(ls) > 1 else L) + 8
            out.append({'t': 'p', 'cls': ' '.join(c for c in ('' if indented else 'noindent', small) if c),
                        'lines': ls, 'pages': {pno}, 'x0': min(l['x0'] for l in ls[1:] or ls)})
        return out

    for pno, lines in enumerate(pages):
        R = rights[pno]
        items = page_items(chunks_of(lines, thr), L, R)
        # photographs go where they are on the page: before the first item below them
        flow = sorted([(min(l['top'] for l in it[1]), 1, n) for n, it in enumerate(items)] +
                      [(top, 0, k) for top, k in pics[pno]])
        for _, is_item, n in flow:
            if not is_item:
                blocks.append({'t': 'img', 'src': (path, pno, n), 'page': pno})
                continue
            item = items[n]
            kind = item[0]
            if kind == 'sep':
                blocks.append({'t': 'sep'})
            elif kind == 'note':
                notes += note_items(item[1], ('page', pno), R)
            elif kind == 'table':
                _, tlines, tnotes = item
                b = {'t': 'table', 'table': pdf_table(tlines, rules[pno]), 'pages': {pno}}
                b['table'].raw = ('pdf', path, pno, crop_box(tlines, lines, rules[pno], heights[pno]))
                blocks.append(b)
                notes += note_items(tnotes, b, R)
            else:
                new = text_block(item[1], pno, R)
                # (a photograph does not interrupt a paragraph)
                last = next((b for b in reversed(blocks) if b['t'] != 'img'), None)
                for b in new:
                    # a paragraph cut by the page break continues on this page
                    if (n == 0 and b is new[0] and b['t'] == 'p' and last and last['t'] in ('p', 'li')
                            and last.get('open') and 'noindent' in b['cls']):
                        target = last['lines'] if last['t'] == 'p' else last['paras'][-1]
                        target += b['lines']
                        last['pages'].add(pno)
                        continue
                    # an indented paragraph right after a list item belongs to that item;
                    # indented well beyond the item's text, it is a quotation
                    x0 = min(b.get('x0', 0), b['lines'][0]['x0']) if b['t'] == 'p' else 0
                    if b['t'] == 'p' and blocks and blocks[-1]['t'] == 'li' and x0 >= blocks[-1]['indent'] - 4:
                        blocks[-1]['paras'].append(b['lines'])
                        if x0 >= blocks[-1]['indent'] + 20:
                            blocks[-1].setdefault('quotes', set()).add(len(blocks[-1]['paras']) - 1)
                        blocks[-1]['pages'].add(pno)
                        continue
                    if b['t'] == 'sig' and blocks and blocks[-1]['t'] == 'p' and not b['date'] and \
                            len(blocks[-1]['lines']) == 1 and DATE_RE.match(blocks[-1]['lines'][0]['text']):
                        b['date'] = blocks.pop()['lines'][0]['text']
                    if b['t'] == 'sig' and blocks and blocks[-1]['t'] == 'sig':   # "Sincerely," + name
                        blocks[-1]['lines'] += b['lines']
                        continue
                    blocks.append(b)
        # Does the page end in the middle of a paragraph? Only then is its last
        # line justified, i.e. flush with the right margin.
        for b in blocks:
            b.pop('open', None)
        text = [b for b in blocks if b['t'] != 'img']
        if text and text[-1]['t'] in ('p', 'li'):
            last_line = (text[-1]['lines'] if text[-1]['t'] == 'p' else text[-1]['paras'][-1])[-1]
            text[-1]['open'] = last_line['x1'] >= R - 1.0 or last_line['text'].endswith('-')

    stack = []   # nesting of list items, from the marker positions
    for b in blocks:
        if b['t'] != 'li':
            stack = []
            continue
        while stack and stack[-1] > b['mx'] + 8:
            stack.pop()
        if not stack or b['mx'] > stack[-1] + 8:
            stack.append(b['mx'])
        b['level'] = len(stack) - 1
    for b in blocks:   # lines -> tokens
        b.pop('open', None)
        if b['t'] in ('p', 'h2'):
            if b.pop('br', False):
                tok = []
                for l in b.pop('lines'):
                    if tok:
                        tok.append(('\n', 'br'))
                    tok += strip_tokens(words_tokens(l['words']))
                b['tok'] = tok
            else:
                b['tok'] = join_lines([words_tokens(l['words']) for l in b.pop('lines')])
            if b['t'] == 'h2':
                b['tok'] = [(t, st.replace('b', '')) for t, st in b['tok']]
        elif b['t'] == 'li':
            b['paras'] = [join_lines([words_tokens(l['words']) for l in ls]) for ls in b['paras']]
    return blocks, notes


# --------------------------------------------------------------------------
# Footnotes: link each note to its marker(s) in the text
# --------------------------------------------------------------------------

def link_marker(tokens, marker, fid, refs):
    """Turn occurrences of `marker` attached to text into note references."""
    out, found = [], 0
    pat = re.compile(r'(?<![*\s])\s?' + re.escape(marker) + r'(?!\*)')
    for t, st in tokens:
        if st.startswith('R:') or st == 'br':
            out.append((t, st))
            continue
        pos = 0
        for m in pat.finditer(t):
            if m.start() == 0 and not out:
                continue
            refs.append(fid)
            out.append((t[pos:m.start() + len(m.group()) - len(marker)], st))
            out.append((marker, f'R:{fid}:{len(refs)}'))
            pos = m.end()
            found += 1
        out.append((t[pos:], st))
    return merge(out), found


def link_notes(slug, blocks, notes):
    def fields(b):
        if b['t'] in ('p', 'h2'):
            yield b, 'tok'
        elif b['t'] == 'li':
            for i in range(len(b['paras'])):
                yield b['paras'], i
        elif b['t'] == 'table':
            t = b['table']
            for row in t.rows:
                for i in range(len(row)):
                    yield row, i
            for row in t.header:
                for i, (toks, c0, c1) in enumerate(row):
                    yield row, (i, c0, c1)

    def apply(b, marker, fid, refs, once):
        n = 0
        for holder, key in fields(b):
            if isinstance(key, tuple):
                i, c0, c1 = key
                new, k = link_marker(holder[i][0], marker, fid, refs)
                holder[i] = (new, c0, c1)
            else:
                new, k = link_marker(holder[key], marker, fid, refs)
                holder[key] = new
            n += k
            if once and n:
                break
        return n

    # table notes first, so page notes do not grab a table's asterisks
    order = sorted(range(len(notes)), key=lambda k: not isinstance(notes[k]['scope'], dict))
    for k in order:
        n = notes[k]
        n['id'] = f'n-{slug}-{k + 1}'
        n['refs'] = []
        scope = n['scope']
        if isinstance(scope, dict):
            scope['table'].notes.append(n)
            if not scope['table'].wide:   # image tables get their links below the image
                apply(scope, n['marker'], n['id'], n['refs'], once=False)
            continue
        cands = [b for b in blocks if b['t'] in ('p', 'h2', 'li', 'table')
                 and not (b['t'] == 'table' and b['table'].wide)
                 and (scope is None or scope[1] in b.get('pages', ()))]
        for b in cands:
            if apply(b, n['marker'], n['id'], n['refs'], once=True):
                break
        else:
            print(f'  warning: no reference found for note {n["marker"]} {plain(n["tok"])[:50]!r}')


# --------------------------------------------------------------------------
# Images: wide tables and the cover
# --------------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def find_font(*names):
    for d in FONT_DIRS:
        for root, _, files in os.walk(d):
            for n in names:
                if n in files:
                    return os.path.join(root, n)
    return None


def font(names, size):
    path = find_font(*names)
    return ImageFont.truetype(path, size) if path else ImageFont.load_default(size=size)


def png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, 'PNG', optimize=True)
    return buf.getvalue()


def table_image(t):
    """Render a wide table: PDF tables are cropped from the page itself, the
    typewritten tables of 1987/1989 are redrawn in a monospaced font."""
    if t.raw[0] == 'pdf':
        _, path, pno, (x0, top, x1, bot) = t.raw
        scale = 3.0
        doc = pdfium.PdfDocument(path)
        page = doc[pno]
        img = page.render(scale=scale).to_pil().convert('L')
        page.close()
        doc.close()
        return img.crop((int(x0 * scale), int(top * scale), int(x1 * scale) + 1, int(bot * scale) + 1))
    lines = t.raw[1]
    size, pad = 30, 24
    faces = {'': font(['LiberationMono-Regular.ttf', 'DejaVuSansMono.ttf', 'FreeMono.ttf'], size),
             'i': font(['LiberationMono-Italic.ttf', 'DejaVuSansMono-Oblique.ttf', 'FreeMonoOblique.ttf'], size),
             'b': font(['LiberationMono-Bold.ttf', 'DejaVuSansMono-Bold.ttf', 'FreeMonoBold.ttf'], size)}
    adv = faces[''].getlength('M')
    width = max(len(ltext(l)) for l in lines)
    img = Image.new('L', (int(2 * pad + width * adv), int(2 * pad + len(lines) * size * 1.3)), 255)
    draw = ImageDraw.Draw(img)
    for row, line in enumerate(lines):
        for col, (ch, st) in enumerate(line):
            if ch.strip():
                face = faces['b' if 'b' in st else 'i' if 'i' in st else '']
                draw.text((pad + col * adv, pad + row * size * 1.3), ch, font=face, fill=0)
    return img


def cover_image():
    W, H = 1600, 2560
    img = Image.new('L', (W, H), 250)
    draw = ImageDraw.Draw(img)
    serif = ['LiberationSerif-Regular.ttf', 'DejaVuSerif.ttf', 'FreeSerif.ttf']
    serif_b = ['LiberationSerif-Bold.ttf', 'DejaVuSerif-Bold.ttf', 'FreeSerifBold.ttf']
    serif_i = ['LiberationSerif-Italic.ttf', 'DejaVuSerif-Italic.ttf', 'FreeSerifItalic.ttf']

    def centred(text, y, f, fill=20):
        w = draw.textlength(text, font=f)
        draw.text(((W - w) / 2, y), text, font=f, fill=fill)
    centred('BUFFETT', 640, font(serif_b, 190))
    centred('ESSENTIALS', 860, font(serif_b, 190))
    draw.line((W / 2 - 260, 1150, W / 2 + 260, 1150), fill=60, width=4)
    centred('Selected Letters', 1210, font(serif_i, 110))
    centred('Warren E. Buffett', 1900, font(serif, 100))
    centred('Owner’s Manual · 1987 · 1989 · 2006 · 2008 · 2014', 2080, font(serif, 52), 90)
    return img


# --------------------------------------------------------------------------
# XHTML
# --------------------------------------------------------------------------

CSS = """
body { margin: 0; padding: 0; }
h1 { font-size: 1.5em; text-align: center; margin: 1.5em 0 1.2em 0; page-break-before: always; }
h2 { font-size: 1.12em; margin: 1.4em 0 0.6em 0; page-break-after: avoid; }
h2.center, h2.essay { text-align: center; }
h2.essay { font-size: 1.3em; margin-top: 2.5em; page-break-before: always; }
p { margin: 0 0 0.5em 0; text-indent: 1.3em; }
p.noindent, p.center, p.tsrc, p.tnotes, p.sep, .sig p, .li p, .notes p, nav p { text-indent: 0; }
p.center { text-align: center; }
p.small, p.tsrc, p.tnotes { font-size: 0.88em; }
p.sep { text-align: center; margin: 1em 0; }
p.ednote { text-indent: 0; font-style: italic; font-size: 0.88em; margin: 0 1.5em 1.5em 1.5em; }
.li { margin: 0 0 0.5em 2em; }
.li.lv1 { margin-left: 4em; }
.li p.first { text-indent: -2em; }
.li p.first span.mk { display: inline-block; width: 2em; text-indent: 0; }
.li p.quote { margin-left: 2em; }
.sig { margin: 1.5em 0 1em 0; }
.sig p.name { text-align: right; margin: 0; }
table.data { border-collapse: collapse; margin: 0.8em auto 1em auto; font-size: 0.85em; }
table.data th, table.data td { padding: 0.1em 0.45em; vertical-align: bottom; }
table.data th { font-weight: normal; text-align: center; }
table.data th.last { border-bottom: 1px solid #777; }
table.data th.stub { text-align: left; }
table.data td.num { text-align: right; }
table.data td.nw { white-space: nowrap; }
table.data td.ind1 { padding-left: 1.2em; }
table.data td.ind2 { padding-left: 2em; }
table.data td.ind3 { padding-left: 2.8em; }
table.data td.rule1 { border-bottom: 1px solid #555; }
table.data td.rule2 { border-bottom: 3px double #555; }
table.data tr.gap td { padding-top: 0.7em; }
div.tableimg { text-align: center; margin: 0.8em 0; }
div.tableimg img { max-width: 100%; }
div.photo { text-align: center; margin: 1.2em 0; page-break-inside: avoid; }
div.photo img { max-width: 100%; }
a.nref { text-decoration: none; }
.notes { margin-top: 2em; border-top: 1px solid #777; padding-top: 0.5em; }
.notes h2 { font-size: 1em; margin-top: 0.5em; }
.notes p { font-size: 0.9em; margin-bottom: 0.6em; }
div.cover { text-align: center; margin: 0; padding: 0; }
div.cover img { height: 97%; max-width: 100%; }
nav ol { list-style-type: none; margin-left: 0; padding-left: 0; }
nav ol ol { margin-left: 1.5em; }
nav li { margin: 0.4em 0; }
"""


def xhtml(title, body, css='style.css'):
    return ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
            'xml:lang="en" lang="en">\n<head>\n<meta charset="utf-8"/>\n'
            f'<title>{esc(title)}</title>\n<link rel="stylesheet" type="text/css" href="{css}"/>\n'
            f'</head>\n<body>\n{body}\n</body>\n</html>\n')


def table_html(t):
    out = ['<table class="data">']
    if t.header:
        out.append('<thead>')
        for k, row in enumerate(t.header):
            cells = []
            for toks, c0, c1 in row:
                span = f' colspan="{c1 - c0 + 1}"' if c1 > c0 else ''
                cls = (['stub'] if c0 == 0 and c1 == 0 and not t.numeric[0] else []) + \
                      (['last'] if k == len(t.header) - 1 and plain(toks).strip() else [])   # rule under headings only
                cls = f' class="{" ".join(cls)}"' if cls else ''
                cells.append(f'<th{span}{cls}>{to_html(toks)}</th>')
            out.append('<tr>' + ''.join(cells) + '</tr>')
        out.append('</thead>')
    out.append('<tbody>')
    for r, (row, ind) in enumerate(zip(t.rows, t.indents)):
        cells = []
        for c, toks in enumerate(row):
            rule = t.rules.get(r, {}).get(c)
            cls = (['num'] if t.numeric[c] else []) + (['nw'] if is_num(plain(toks)) else []) + \
                  ([f'ind{ind}'] if c == 0 and ind else []) + ([f'rule{rule}'] if rule else [])
            attr = f' class="{" ".join(cls)}"' if cls else ''
            if is_num(plain(toks)):   # "$   965" is aligned type; in a cell it is "$965"
                toks = merge([(re.sub(r'^\$\s+', '$', x) if k == 0 else x, st) for k, (x, st) in enumerate(toks)])
            cells.append(f'<td{attr}>{to_html(toks)}</td>')
        out.append(('<tr class="gap">' if r in t.gaps else '<tr>') + ''.join(cells) + '</tr>')
    out.append('</tbody>\n</table>')
    return '\n'.join(out)


def chapter_html(ch, images):
    """Render one chapter; wide tables are added to `images` as PNGs."""
    slug, parts, sub = ch['slug'], [f'<section epub:type="chapter" id="c-{ch["slug"]}">',
                                     f'<h1>{esc(ch["title"])}</h1>'], []
    if slug in EDNOTES:
        parts.append(f'<p class="ednote">{esc(EDNOTES[slug])}</p>')
    for k, b in enumerate(ch['blocks']):
        t = b['t']
        if t == 'p':
            cls = f' class="{b["cls"]}"' if b.get('cls') else ''
            parts.append(f'<p{cls}>{to_html(b["tok"])}</p>')
        elif t == 'h2':
            hid = f'h-{slug}-{k}'
            text = plain(b['tok'])
            cls = b.get('cls', '')
            if any(re.search(p, text) for p in SUBTOC.get(slug, [])):
                sub.append((hid, text.title() if text.isupper() else text))
                if slug in ESSAYS:
                    cls = 'essay'
            cls = f' class="{cls}"' if cls else ''
            parts.append(f'<h2 id="{hid}"{cls}>{to_html(b["tok"])}</h2>')
        elif t == 'li':
            paras = [f'<p class="first"><span class="mk">{esc(b["marker"])} </span>{to_html(b["paras"][0])}</p>']
            paras += [f'<p class="quote">{to_html(p)}</p>' if i in b.get('quotes', ()) else f'<p>{to_html(p)}</p>'
                      for i, p in enumerate(b['paras'][1:], 1)]
            parts.append(f'<div class="li lv{min(b.get("level", 0), 1)}">' + ''.join(paras) + '</div>')
        elif t == 'sep':
            parts.append('<p class="sep">*\u2003*\u2003*</p>')
        elif t == 'img':
            name = f'{slug}-photo{sum(n.startswith(slug + "-photo") for n in images) + 1}.jpg'
            images[name] = photo_bytes(*b['src'])
            alt = PHOTOS.get((slug, b['page'] + 1), 'Photograph')
            parts.append(f'<div class="photo"><img src="../images/{name}" alt="{esc(alt)}"/></div>')
        elif t == 'sig':
            lines = '<br/>'.join(esc(x) for x in b['lines'])
            date = f'<p class="date">{esc(b["date"])}</p>' if b['date'] else ''
            parts.append(f'<div class="sig">{date}<p class="name">{lines}</p></div>')
        elif t == 'table':
            tb = b['table']
            if tb.wide:
                name = f'{slug}-table{sum("-table" in n for n in images) + 1}.png'
                images[name] = png_bytes(table_image(tb))
                parts.append(f'<div class="tableimg"><img src="../images/{name}" alt="{esc("Table: " + tb.plain_text())}"/></div>')
                if tb.notes:   # the image cannot link its asterisks, so link them here
                    refs = []
                    for n in tb.notes:
                        n['refs'].append(n['id'])
                        refs.append(f'<a class="nref" epub:type="noteref" id="r-{n["id"]}-{len(n["refs"])}" '
                                    f'href="#{n["id"]}">{esc(n["marker"])}</a>')
                    parts.append(f'<p class="tnotes">Table notes: {" ".join(refs)}</p>')
            else:
                parts.append(table_html(tb))
    if ch['notes']:
        parts.append('<section class="notes">\n<h2>Notes</h2>')
        for n in ch['notes']:
            back = f'<a href="#r-{n["id"]}-1">{esc(n["marker"])}</a>' if n['refs'] else esc(n['marker'])
            parts.append(f'<p id="{n["id"]}">{back} {to_html(n["tok"])}</p>')
        parts.append('</section>')
    parts.append('</section>')
    return xhtml(ch['title'], '\n'.join(parts), '../style.css'), sub


# --------------------------------------------------------------------------
# EPUB packaging
# --------------------------------------------------------------------------

def write_epub(chapters, out_path):
    images, docs, toc = {}, [], []
    for i, ch in enumerate(chapters, 1):
        body, sub = chapter_html(ch, images)
        docs.append((f'text/ch{i}.xhtml', body))
        toc.append((f'text/ch{i}.xhtml', ch['title'], sub))
    book_id = 'urn:uuid:' + str(uuid.uuid5(uuid.NAMESPACE_URL, SITE + '#' + TITLE))
    modified = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    items = []
    for href, title, sub in toc:
        inner = ''.join(f'<li><a href="{href}#{hid}">{esc(t)}</a></li>' for hid, t in sub)
        items.append(f'<li><a href="{href}">{esc(title)}</a>' + (f'<ol>{inner}</ol>' if inner else '') + '</li>')
    nav = xhtml('Contents', '<nav epub:type="toc" id="toc">\n<h1>Contents</h1>\n<ol>\n' + '\n'.join(items) +
                '\n</ol>\n</nav>\n<nav epub:type="landmarks" hidden="hidden">\n<ol>\n'
                '<li><a epub:type="cover" href="cover.xhtml">Cover</a></li>\n'
                '<li><a epub:type="toc" href="nav.xhtml#toc">Contents</a></li>\n'
                f'<li><a epub:type="bodymatter" href="{docs[0][0]}">Start</a></li>\n</ol>\n</nav>')
    points, order = [], 0
    for href, title, sub in toc:
        order += 1
        kids = ''
        for hid, t in sub:
            order += 1
            kids += (f'<navPoint id="np{order}" playOrder="{order}"><navLabel><text>{esc(t)}</text></navLabel>'
                     f'<content src="{href}#{hid}"/></navPoint>')
        points.append(f'<navPoint id="np{order - len(sub)}" playOrder="{order - len(sub)}"><navLabel><text>'
                      f'{esc(title)}</text></navLabel><content src="{href}"/>{kids}</navPoint>')
    ncx = ('<?xml version="1.0" encoding="utf-8"?>\n<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
           f'<head><meta name="dtb:uid" content="{book_id}"/><meta name="dtb:depth" content="2"/>'
           '<meta name="dtb:totalPageCount" content="0"/><meta name="dtb:maxPageNumber" content="0"/></head>\n'
           f'<docTitle><text>{esc(TITLE)}</text></docTitle>\n<navMap>\n' + '\n'.join(points) + '\n</navMap>\n</ncx>\n')
    cover = xhtml('Cover', f'<div class="cover"><img src="images/cover.png" alt="{esc(TITLE)} by {esc(AUTHOR)}"/></div>')

    manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
                '<item id="css" href="style.css" media-type="text/css"/>',
                '<item id="cover-image" href="images/cover.png" media-type="image/png" properties="cover-image"/>',
                '<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>']
    manifest += [f'<item id="ch{i}" href="{href}" media-type="application/xhtml+xml"/>'
                 for i, (href, _) in enumerate(docs, 1)]
    manifest += [f'<item id="img{i}" href="images/{name}" media-type="image/'
                 f'{"jpeg" if name.endswith(".jpg") else "png"}"/>' for i, name in enumerate(images, 1)]
    spine = ['<itemref idref="cover" linear="yes"/>', '<itemref idref="nav"/>']
    spine += [f'<itemref idref="ch{i}"/>' for i in range(1, len(docs) + 1)]
    opf = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="en">\n'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
           f'<dc:identifier id="bookid">{book_id}</dc:identifier>\n'
           f'<dc:title>{esc(TITLE)}</dc:title>\n'
           f'<dc:creator id="author">{esc(AUTHOR)}</dc:creator>\n'
           '<meta refines="#author" property="role" scheme="marc:relators">aut</meta>\n'
           '<meta refines="#author" property="file-as">Buffett, Warren E.</meta>\n'
           '<dc:language>en</dc:language>\n'
           f'<dc:source>{SITE}</dc:source>\n'
           f'<meta property="dcterms:modified">{modified}</meta>\n'
           '<meta name="cover" content="cover-image"/>\n'
           '</metadata>\n<manifest>\n' + '\n'.join(manifest) + '\n</manifest>\n'
           '<spine toc="ncx">\n' + '\n'.join(spine) + '\n</spine>\n'
           '<guide>\n<reference type="cover" title="Cover" href="cover.xhtml"/>\n'
           '<reference type="toc" title="Contents" href="nav.xhtml"/>\n'
           f'<reference type="text" title="Start" href="{docs[0][0]}"/>\n</guide>\n</package>\n')
    container = ('<?xml version="1.0" encoding="utf-8"?>\n'
                 '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
                 '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                 '</rootfiles>\n</container>\n')

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with zipfile.ZipFile(out_path, 'w') as z:
        z.writestr(zipfile.ZipInfo('mimetype'), 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        files = [('META-INF/container.xml', container), ('OEBPS/content.opf', opf), ('OEBPS/toc.ncx', ncx),
                 ('OEBPS/nav.xhtml', nav), ('OEBPS/style.css', CSS.lstrip()), ('OEBPS/cover.xhtml', cover),
                 ('OEBPS/images/cover.png', png_bytes(cover_image()))]
        files += [('OEBPS/' + href, body) for href, body in docs]
        files += [('OEBPS/images/' + name, data) for name, data in images.items()]
        for name, data in files:
            z.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
    return docs, images


# --------------------------------------------------------------------------
# Completeness check: every word of each source must appear in its chapter
# --------------------------------------------------------------------------

def source_text(path):
    if path.endswith('.html'):
        page = open(path, 'rb').read().decode('latin-1')
        page = re.sub(r'(?is)<script.*?</script>|<!--.*?-->|<title>.*?</title>', ' ', page)
        return html.unescape(re.sub(r'<[^>]+>', ' ', page))
    pages = []
    with pdfplumber.open(path) as pdf:   # all words except page numbers
        for p in pdf.pages:
            ws = p.extract_words(x_tolerance=1.5)
            pages.append(' '.join(w['text'].replace('(cid:129)', BULLET) for w in ws if not (
                w['text'].isdigit() and w['top'] > 0.85 * p.height
                and not any(v is not w and abs(v['bottom'] - w['bottom']) < 3 for v in ws))))
    return '\n'.join(pages)


def tokens_of(text):
    for frac, ch in FRACTIONS.items():
        text = text.replace(ch, ' ' + frac + ' ')
    return re.findall(r'[a-z0-9]+', text.lower())


def chapter_text(doc):
    """Visible text of a chapter, with each image's alt text where the image is."""
    doc = re.sub(r'(?s)<head>.*?</head>', ' ', doc)
    doc = re.sub(r'<img [^>]*alt="([^"]*)"[^>]*/>', lambda m: ' ' + m.group(1) + ' ', doc)
    doc = re.sub(r'</?(sup|sub|small|i|b|a|span)\b[^>]*>', '', doc)   # inline tags: no word break
    return html.unescape(re.sub(r'<[^>]+>', ' ', doc))


def source_photos(path):
    """How many pictures the source shows (counted independently of the
    converter: pdfplumber for PDFs, <IMG> tags for HTML pages)."""
    if path.endswith('.html'):
        return len(re.findall(rb'(?i)<img\b', open(path, 'rb').read()))
    with pdfplumber.open(path) as pdf:
        return sum(im['x1'] - im['x0'] >= 36 and im['bottom'] - im['top'] >= 36
                   for p in pdf.pages for im in p.images)


def check(chapters, docs, sources, paths):
    """Diff each chapter's words against its source. Text that only moved
    (footnotes to the chapter end, stacked table headings) is not a gap.
    Every picture of the source must be in the chapter too."""
    print('\nCompleteness check (source words and pictures missing from the EPUB):')
    ok = True
    for ch, (_, doc) in zip(chapters, docs):
        src = tokens_of(sources[ch['slug']])
        out = tokens_of(chapter_text(doc))
        ops = difflib.SequenceMatcher(None, src, out, autojunk=False).get_opcodes()
        o_join = ' ' + ' '.join(out) + ' '
        added = [(j, out[j]) for op, i1, i2, j1, j2 in ops if op in ('insert', 'replace') for j in range(j1, j2)]
        missing, moved = [], 0
        for op, i1, i2, j1, j2 in ops:
            run = src[i1:i2]
            if op == 'equal' or not run or ''.join(run) == ''.join(out[j1:j2]):
                continue                       # identical, or the same letters split differently
            if ' ' + ' '.join(run) + ' ' not in o_join and \
                    Counter(run) - Counter(w for j, w in added if abs(j - j1) < 150):
                missing.append(' '.join(run))
            else:
                moved += len(run)              # footnotes, stacked table headings
        photos, shown = source_photos(paths[ch['slug']]), doc.count('<div class="photo">')
        if shown < photos:
            missing.append(f'{photos - shown} of {photos} photograph(s)')
        print(f'  {ch["title"]}: {len(src)} source words, {moved} moved, {photos} photograph(s), '
              f'{"OK" if not missing else f"{len(missing)} gap(s)"}')
        for m in missing:
            ok = False
            print(f'      missing: {m[:150]}')
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--cache', default=os.path.expanduser('~/.cache/buffett-epub'),
                    help='where downloaded sources are kept (outside the repository)')
    ap.add_argument('--out', default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  'output', 'Buffett_Essentials.epub'))
    args = ap.parse_args()
    os.makedirs(args.cache, exist_ok=True)
    paths, sources = {}, {}
    for slug, title, url in CHAPTERS:
        paths[slug] = download(url, os.path.join(args.cache, slug + os.path.splitext(url)[1]))
        sources[slug] = source_text(paths[slug])
    build_vocab(sources.values())

    chapters = []
    for slug, title, url in CHAPTERS:
        print(f'Converting {title} ...')
        path = paths[slug]
        if path.endswith('.pdf'):
            blocks, notes = parse_pdf(path)
        else:
            raw = open(path, 'rb').read()
            blocks, notes = parse_pre(raw) if is_pre_letter(raw) else parse_html(raw)
        link_notes(slug, blocks, notes)
        chapters.append({'slug': slug, 'title': title, 'blocks': blocks, 'notes': notes})
    docs, images = write_epub(chapters, args.out)
    tables = [b['table'] for ch in chapters for b in ch['blocks'] if b['t'] == 'table']
    print(f'Wrote {args.out}: {len(chapters)} chapters, {sum(not t.wide for t in tables)} HTML tables, '
          f'{sum("-table" in n for n in images)} tables as images, '
          f'{sum("-photo" in n for n in images)} photograph(s), {sum(len(ch["notes"]) for ch in chapters)} footnotes')
    joined = [h for h in HYPHEN_LOG if h.endswith('joined')]
    print(f'Line-end hyphens: {len(HYPHEN_LOG)} seen, {len(joined)} removed {joined}')
    if not check(chapters, docs, sources, paths):
        sys.exit('Some source text is missing from the EPUB (see above).')


if __name__ == '__main__':
    main()
