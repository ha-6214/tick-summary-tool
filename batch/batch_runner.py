#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
歩み値 一括集計 呼び出し係 v1.0
──────────────────────────────────
集計の計算そのものは一切持たない。原本 tick_value_summary.py を
1銘柄ずつそのまま実行し、その出力を集めて表にするだけの係である。

画面（index.html）からは、次の順に呼ぶ。

  load_engine(原本のテキスト)          … 原本を読み込む
  load_master(銘柄マスタMDのテキスト)   … 銘柄マスタを読み込む
  load_prev([{name, text}, ...])       … 前日データ（JSON/MD）を読み込む
  check([CSVファイル名, ...])          … 事前検証だけを行う
  run_one(CSVファイル名, CSVのテキスト) … 1銘柄を集計する
  finalize()                           … CSV・JSON・MD一式を作る

原本には手を入れない。ルールの改定は原本だけを直す。
"""

import io
import json
import re
import sys
import zipfile
from datetime import datetime, timezone, timedelta

RUNNER_VERSION = "v1.1"
SCHEMA_VERSION = "1.0"

# 集計ルールの互換の区分。判定に使う数値の意味が変わる改定のときだけ、
# ここの記号を変える（例 "A" → "B"）。同じ記号どうしなら前日比を判定する。
RULE_COMPAT = {
    "v1.13": "A",
    "v1.14": "A",
}
DEFAULT_COMPAT = "A"

_ENGINE_SRC = None
_ENGINE_VERSION = ""
_MASTER = {}          # code -> dict
_MASTER_ORDER = []    # 銘柄コード順
_MASTER_VERSION = ""
_PREV = {}            # code -> dict(date, row15, rule_version, compat, source)
_RESULTS = {}         # code -> dict
_TODAY = ""


# ============================================================
# 共通の小道具
# ============================================================
def _num(s, default=None):
    if s is None:
        return default
    t = str(s).strip()
    for ch in [',', '円', '株', '倍', '%', '％', ' ', '　']:
        t = t.replace(ch, '')
    if t in ('', '-', 'ー', '―'):
        return default
    try:
        v = float(t)
    except ValueError:
        return default
    return int(v) if v == int(v) else v


def _norm_date(s):
    if not s:
        return ''
    t = re.sub(r'[年月/.\-]', ' ', str(s)).replace('日', ' ')
    p = [x for x in t.split() if x.isdigit()]
    if len(p) < 3:
        return ''
    return '%04d-%02d-%02d' % (int(p[0]), int(p[1]), int(p[2]))


def _compat_of(rule_version):
    return RULE_COMPAT.get(str(rule_version).strip(), DEFAULT_COMPAT)


def _code_from_name(fname):
    """CSVのファイル名から銘柄コードと日付を取り出す。
    英字を含むコード（194A など）と、連番付きの名前にも対応する。"""
    m = re.search(r'qr[-_]([0-9][0-9A-Za-z]{2,4})[-_](\d{8}|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})',
                  fname, re.IGNORECASE)
    if not m:
        return '', ''
    code = m.group(1)
    d = m.group(2)
    if len(d) == 8 and d.isdigit():
        d = '%s-%s-%s' % (d[0:4], d[4:6], d[6:8])
    return code, _norm_date(d)


# ============================================================
# 原本と銘柄マスタの読み込み
# ============================================================
def load_engine(src_text):
    global _ENGINE_SRC, _ENGINE_VERSION
    _ENGINE_SRC = src_text
    m = re.search(r'RULE_VERSION\s*=\s*["\']([^"\']+)["\']', src_text)
    _ENGINE_VERSION = m.group(1) if m else ''
    return {"rule_version": _ENGINE_VERSION, "compat": _compat_of(_ENGINE_VERSION),
            "runner_version": RUNNER_VERSION}


def load_master(md_text):
    """銘柄マスタMDの表を読み取る。列は位置ではなく見出しの名前で見分ける。"""
    global _MASTER, _MASTER_ORDER, _MASTER_VERSION
    _MASTER, _MASTER_ORDER = {}, []
    m = re.search(r'master_version:\s*(\S+)', md_text)
    _MASTER_VERSION = m.group(1) if m else ''

    alias = {
        '銘柄コード': 'code', 'コード': 'code', '証券コード': 'code',
        '銘柄名称': 'name', '銘柄名': 'name', '名称': 'name',
        '年初来高値': 'year_high',
        '年初来高値日付': 'year_high_date', '年初来高値の日付': 'year_high_date',
        '信用売れ残': 'margin_sell', '信用売残': 'margin_sell',
        '信用買い残': 'margin_buy', '信用買残': 'margin_buy',
        '信用倍率': 'margin_ratio',
        '備考': 'note',
    }

    header, dup = None, []
    for line in md_text.splitlines():
        line = line.strip()
        if not line.startswith('|'):
            continue
        cells = [c.strip() for c in line.strip('|').split('|')]
        if header is None:
            keys = [alias.get(c.replace(' ', '').replace('　', ''), '') for c in cells]
            if 'code' in keys and 'name' in keys:
                header = keys
            continue
        if all(set(c) <= set('-: ') for c in cells):
            continue
        rec = {}
        for k, v in zip(header, cells):
            if k:
                rec[k] = v
        code = str(rec.get('code', '')).strip()
        if not code:
            continue
        if code in _MASTER:
            dup.append(code)
            continue
        _MASTER[code] = {
            'code': code,
            'name': rec.get('name', ''),
            'year_high': _num(rec.get('year_high')),
            'year_high_date': _norm_date(rec.get('year_high_date')),
            'margin_sell': _num(rec.get('margin_sell')),
            'margin_buy': _num(rec.get('margin_buy')),
            'margin_ratio': _num(rec.get('margin_ratio')),
            'note': rec.get('note', ''),
        }
        _MASTER_ORDER.append(code)
    _MASTER_ORDER.sort()
    no_high = [c for c in _MASTER_ORDER if _MASTER[c]['year_high'] is None]
    return {"count": len(_MASTER), "duplicated": dup, "no_year_high": no_high,
            "master_version": _MASTER_VERSION}


# ============================================================
# 前日データの読み込み（JSONを正とし、MDも受け付ける）
# ============================================================
def _prev_from_json(text, source):
    got = {}
    data = json.loads(text)
    rv = data.get('rule_version', '')
    for st in data.get('stocks', []):
        row = st.get('paste1')
        if not row or len(row) != 15:
            continue
        got[str(st.get('code'))] = {
            'date': data.get('date', ''),
            'row15': [float(x) for x in row],
            'rule_version': st.get('rule_version', rv),
            'source': source,
            'kind': 'JSON',
        }
    return got


def _prev_from_md(text, source):
    head = {}
    lines = text.splitlines()
    if lines and lines[0].strip() == '---':
        for line in lines[1:]:
            if line.strip() == '---':
                break
            if ':' in line:
                k, v = line.split(':', 1)
                head[k.strip()] = v.strip().strip('\'"')
    row = None
    for i, line in enumerate(lines):
        if '貼り付け' in line:
            for cand in lines[i + 1:i + 6]:
                parts = cand.replace('\t', ' ').split()
                if len(parts) == 15:
                    try:
                        row = [float(p.replace(',', '')) for p in parts]
                        break
                    except ValueError:
                        pass
        if row:
            break
    code = str(head.get('code') or head.get('ticker') or '').strip()
    if not code:
        m = re.search(r'銘柄[^0-9A-Za-z]{0,8}([0-9][0-9A-Za-z]{3})', text)
        code = m.group(1) if m else ''
    if not code or row is None:
        return {}
    return {code: {'date': _norm_date(head.get('date', '')), 'row15': row,
                   'rule_version': head.get('rule_version', ''),
                   'source': source, 'kind': 'MD'}}


def load_prev(files):
    """files: [{'name': ファイル名, 'text': 中身}, ...]"""
    global _PREV
    _PREV = {}
    conflicts, bad = [], []
    for f in files or []:
        name, text = f.get('name', ''), f.get('text', '')
        try:
            got = _prev_from_json(text, name) if text.lstrip().startswith('{') \
                else _prev_from_md(text, name)
        except Exception as e:                      # noqa: BLE001
            bad.append('%s（%s）' % (name, e))
            continue
        if not got:
            bad.append('%s（前日の値を読み取れませんでした）' % name)
            continue
        for code, v in got.items():
            old = _PREV.get(code)
            if old is None:
                _PREV[code] = v
            elif old['kind'] == 'JSON' and v['kind'] == 'MD':
                if old['row15'] != v['row15']:
                    conflicts.append(code)
            else:
                if old['kind'] == 'MD' and v['kind'] == 'JSON':
                    if old['row15'] != v['row15']:
                        conflicts.append(code)
                    _PREV[code] = v
    return {"count": len(_PREV), "conflicts": sorted(set(conflicts)), "unreadable": bad}


# ============================================================
# 事前検証
# ============================================================
def check(csv_names):
    global _TODAY
    seen, dup, unknown, dates, unmatched = {}, [], [], {}, []
    for n in csv_names:
        code, d = _code_from_name(n)
        if not code:
            unmatched.append(n)
            continue
        if code in seen:
            dup.append(code)
        seen[code] = n
        if d:
            dates.setdefault(d, []).append(code)
        if code not in _MASTER:
            unknown.append(code)
    _TODAY = max(dates.keys()) if dates else ''
    no_prev = [c for c in seen if c not in _PREV]
    mismatch = []
    for c in seen:
        p = _PREV.get(c)
        if p and _compat_of(p.get('rule_version')) != _compat_of(_ENGINE_VERSION):
            mismatch.append(c)
    return {
        "files": len(csv_names),
        "codes": len(seen),
        "duplicated": sorted(set(dup)),
        "not_in_master": sorted(set(unknown)),
        "name_unreadable": unmatched,
        "dates": {k: len(v) for k, v in sorted(dates.items())},
        "today": _TODAY,
        "prev_found": len(seen) - len(no_prev),
        "prev_missing": sorted(no_prev),
        "rule_mismatch": sorted(mismatch),
        "master_count": len(_MASTER),
    }


# ============================================================
# 原本の実行（1銘柄）
# ============================================================
def _run_engine(csv_name, csv_text, info, prev):
    """原本をそのまま実行し、標準出力を受け取る。
    原本は途中で処理を終了させる作りのため、それを受け止めてエラーとして返す。"""
    import os
    os.makedirs('/tmp/tick', exist_ok=True)
    csv_path = '/tmp/tick/' + re.sub(r'[^0-9A-Za-z._-]', '_', csv_name)
    with open(csv_path, 'w', encoding='utf-8') as f:
        f.write(csv_text)

    meta_path = '/tmp/tick/meta.txt'
    meta_lines = [
        '日付: %s' % info.get('date', ''),
        '銘柄コード: %s' % info.get('code', ''),
        '銘柄名称: %s' % info.get('name', ''),
        '年初来高値: %s' % ('' if info.get('year_high') is None else info['year_high']),
        '年初来高値の日付: %s' % info.get('year_high_date', ''),
        '信用売れ残: %s' % ('' if info.get('margin_sell') is None else info['margin_sell']),
        '信用買い残: %s' % ('' if info.get('margin_buy') is None else info['margin_buy']),
        '信用倍率: %s' % ('' if info.get('margin_ratio') is None else info['margin_ratio']),
    ]
    with open(meta_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(meta_lines))

    argv = ['tick_value_summary.py', csv_path, '--meta=' + meta_path,
            '--csv_name=' + csv_name, '--markers']
    if prev:
        argv.append('--prev_row=' + '\t'.join(
            ('%d' % v) if float(v) == int(v) else ('%.1f' % v) for v in prev['row15']))
        if prev.get('date'):
            argv.append('--prev_date=' + prev['date'])
            argv.append('--prev_code=' + info.get('code', ''))
        argv.append('--prev_name=' + str(prev.get('source', '')))

    old_argv, old_stdout = sys.argv, sys.stdout
    buf = io.StringIO()
    sys.argv, sys.stdout = argv, buf
    ok = True
    try:
        ns = {'__name__': '__main__', '__file__': 'tick_value_summary.py'}
        exec(compile(_ENGINE_SRC, 'tick_value_summary.py', 'exec'), ns)   # noqa: S102
    except SystemExit:
        ok = False
    except Exception as e:                                               # noqa: BLE001
        sys.argv, sys.stdout = old_argv, old_stdout
        return False, '', '', '集計の途中で止まりました（%s: %s）' % (type(e).__name__, e)
    finally:
        sys.argv, sys.stdout = old_argv, old_stdout

    out = buf.getvalue()
    # 正常に終わったときだけ、3つの目印が順に出てくる。
    # 改行の違いに左右されないよう、正規表現ではなく位置を探して切り分ける。
    i1 = out.find('<<<FILENAME>>>')
    i2 = out.find('<<<READBACK>>>')
    i3 = out.find('<<<BODY>>>')
    if ok and 0 <= i1 < i2 < i3:
        fname = out[i1 + len('<<<FILENAME>>>'):i2].strip()
        readback = out[i2 + len('<<<READBACK>>>'):i3].strip('\r\n')
        body = out[i3 + len('<<<BODY>>>'):].lstrip('\r\n')
        return True, body, readback, fname
    msg = re.sub(r'<<<[^>]*>>>', ' ', out).strip().splitlines()
    return False, '', '', (' '.join(msg)[:300] if msg else '集計を中止しました')


# ============================================================
# 出力テキストの読み取り（JSON化のため）
# ============================================================
def _pick(pat, text, cast=_num, default=None, group=1):
    m = re.search(pat, text)
    return cast(m.group(group)) if m else default


def _parse_body(text):
    d = {}
    d['basic'] = {
        'total_rows': _pick(r'総約定件数:\s*([\d,]+)', text),
        'opening_price': _pick(r'始値:\s*([\d,.]+)円', text),
        'closing_price': _pick(r'終値:\s*([\d,.]+)円', text),
        'has_closing_auction': '引け約定あり' in text,
        'vwap': _pick(r'日中VWAP:\s*([\d.]+)円', text),
        'volume': _pick(r'出来高合計:\s*([\d,]+)株', text),
        'amount': _pick(r'売買代金合計:\s*([\d,]+)円', text),
        'day_high': _pick(r'当日高値:\s*([\d,.]+)円', text),
        'day_low': _pick(r'当日安値:\s*([\d,.]+)円', text),
    }
    d['session'] = {
        'am_volume': _pick(r'前場出来高:\s*([\d,]+)株', text),
        'am_vwap': _pick(r'前場VWAP:\s*([\d.]+)円', text),
        'pm_volume': _pick(r'後場出来高:\s*([\d,]+)株', text),
        'pm_volume_ratio': _pick(r'後場出来高比率:\s*([\d.]+)%', text),
        'pm_vwap_zaraba': _pick(r'後場VWAP（ザラバのみ）:\s*([\d.]+)円', text),
        'pm_vwap_over_am': bool(re.search(r'後場VWAP > 前場VWAP:\s*はい', text)),
    }
    m = re.search(r'【引け約定[^】]*】\n株数:\s*([\d,]+)株\n金額:\s*([\d,]+)円\n方向:\s*(\S+)', text)
    d['closing_auction'] = {
        'volume': _num(m.group(1)) if m else None,
        'amount': _num(m.group(2)) if m else None,
        'direction': m.group(3) if m else None,
    }
    m = re.search(r'【最大出来高価格帯】\n価格:\s*([\d,.]+)円（([\d,]+)株）', text)
    d['max_volume_band'] = {
        'price': _num(m.group(1)) if m else None,
        'shares': _num(m.group(2)) if m else None,
    }

    def _items(head, pat):
        out = []
        block = re.search(head + r'\n(.*?)\n\n', text, re.S)
        if not block:
            return out
        for line in block.group(1).splitlines():
            m2 = re.match(pat, line)
            if m2:
                out.append({'no': int(m2.group(1)),
                            'hit': m2.group(2) == '該当',
                            'detail': (m2.group(3) or '').strip(),
                            'points': _num(m2.group(4), 0)})
        return out

    d['buy_score'] = {
        'items': _items(r'【買いスコア項目別】',
                        r'項目(\d)（.*?）:\s*(該当|非該当)（(.*?)）→\s*([+\-\d]+)点'),
        'base': _pick(r'【買いスコア合計】基本(\d+)', text),
        'extra': _pick(r'【買いスコア合計】基本\d+ \+ 追加(\d+)', text),
        'counter_total': _pick(r'- 反証(\d+)', text),
        'total': _pick(r'=\s*(-?\d+)/8点', text),
        'max': 8,
        'judgment': _pick(r'【買い判定】(.+)', text, cast=lambda s: s.strip(), default=''),
    }
    d['counters'] = _items(r'【反証条件】',
                           r'反証(\d)（.*?）:\s*(該当|非該当)（(.*?)）→\s*([+\-\d]+)点')
    d['sell_score'] = {
        'items': _items(r'【売りスコア項目別】',
                        r'項目(\d)（.*?）:\s*(該当|非該当)(?:（(.*?)）)?\s*→\s*([+\-\d]+)点'),
        'total': _pick(r'【売りスコア合計】(\d+)/5点', text),
        'max': 5,
        'judgment': _pick(r'【売り判定】(.+)', text, cast=lambda s: s.strip(), default=''),
    }
    inv = {}
    for key, label in [('inst_high', '機関確度・高'), ('inst_mid', '機関確度・中'),
                       ('retail', '個人')]:
        m = re.search(label + r'[^:]*:\s*件数([\d,]+)件 ／ 株数([\d,]+)株 ／ 金額([\d,]+)円'
                              r' ／ 比率([\d.]+)% ／ 買い主導（全体比）([\d.]+)%'
                              r'・売り主導（全体比）([\d.]+)%', text)
        inv[key] = {'count': _num(m.group(1)), 'volume': _num(m.group(2)),
                    'amount': _num(m.group(3)), 'ratio': _num(m.group(4)),
                    'buy_pct': _num(m.group(5)), 'sell_pct': _num(m.group(6))} if m else None
    inv['inst_total'] = {
        'buy_pct': _pick(r'機関500万円以上合算: 買い主導（全体比）([\d.]+)%', text),
        'sell_pct': _pick(r'機関500万円以上合算: 買い主導（全体比）[\d.]+%・売り主導（全体比）([\d.]+)%', text),
    }
    inv['inst_buy_pct_zaraba'] = _pick(
        r'機関500万円以上・買い主導（ザラバのみ・引け約定を除く／全体比）:\s*([\d.]+)%', text)
    d['investors'] = inv

    def _row(head, n):
        m2 = re.search(head + r'.*?\n((?:（.*?\n)?)([-\d.\t ,]+)\n', text, re.S)
        if not m2:
            return None
        parts = m2.group(2).replace('\t', ' ').split()
        if len(parts) != n:
            return None
        return [_num(p) for p in parts]

    d['paste1'] = _row(r'【１．貼り付け用（タブ区切り）】', 15)
    d['paste2'] = _row(r'【２．貼り付け用（タブ区切り）】', 6)
    d['compare_values'] = {
        'inst_buy': _pick(r'機関500万円以上・買い主導（全体比）:\s*([\d.]+)%', text),
        'inst_buy_zaraba': _pick(r'機関500万円以上・買い主導（ザラバのみ・引け除く／全体比）:\s*([\d.]+)%', text),
        'inst_sell': _pick(r'機関500万円以上・売り主導（全体比）:\s*([\d.]+)%', text),
        'ind_buy': _pick(r'個人・買い主導（全体比）:\s*([\d.]+)%', text),
        'ind_sell': _pick(r'個人・売り主導（全体比）:\s*([\d.]+)%', text),
    }
    blocks = []
    b = re.search(r'【30分ブロック別VWAP・出来高（参考）】\n(.*?)\n\n', text, re.S)
    if b:
        for line in b.group(1).splitlines():
            m2 = re.match(r'\s*(\d{2}:\d{2})\s+出来高=([\d,]+)\s+VWAP=([\d.]+)', line)
            if m2:
                blocks.append({'time': m2.group(1), 'volume': _num(m2.group(2)),
                               'vwap': _num(m2.group(3))})
    d['blocks_30min'] = blocks
    d['price_zone'] = {
        'year_high': _pick(r'年初来高値:\s*([\d,]+)円', text),
        'zone_90': _pick(r'年初来高値の90%:\s*([\d,]+)円', text),
        'zone_85': _pick(r'年初来高値の85%:\s*([\d,]+)円', text),
    }
    d['judgments'] = {
        'dip_buy': _pick(r'押し目買い判定：(.+)', text, cast=lambda s: s.strip(), default=''),
        'day_total': _pick(r'総合：(.+)', text, cast=lambda s: s.strip(), default=''),
        'reverse_signal': _pick(r'注意逆シグナル判定：(.+)', text,
                                cast=lambda s: s.strip(), default=''),
    }
    return d


# ============================================================
# 1銘柄の集計
# ============================================================
def run_one(csv_name, csv_text):
    code, date_from_name = _code_from_name(csv_name)
    if not code:
        return {"code": "", "name": "", "status": "エラー",
                "error": "ファイル名から銘柄コードを読み取れませんでした（%s）" % csv_name}
    info = dict(_MASTER.get(code) or {})
    if not info:
        r = {"code": code, "name": "", "status": "エラー",
             "error": "銘柄マスタに登録がありません"}
        _RESULTS[code] = r
        return r
    info['date'] = date_from_name or _TODAY

    prev = _PREV.get(code)
    prev_used, prev_note = prev, None
    if prev and _compat_of(prev.get('rule_version')) != _compat_of(_ENGINE_VERSION):
        prev_used = None
        prev_note = '前日データの集計ルールの版が違うため、前日比の判定は行いませんでした' \
                    '（前日 %s ／ 当日 %s）' % (prev.get('rule_version') or '不明', _ENGINE_VERSION)
    elif not prev:
        prev_note = '前日データがないため、前日比の判定は行いませんでした'

    ok, body, readback, fname = _run_engine(csv_name, csv_text, info, prev_used)
    if not ok:
        r = {"code": code, "name": info.get('name', ''), "status": "エラー", "error": fname}
        _RESULTS[code] = r
        return r

    parsed = _parse_body(body)
    parsed.update({
        'code': code, 'name': info.get('name', ''), 'status': '完了', 'error': None,
        'master': {k: info.get(k) for k in
                   ('year_high', 'year_high_date', 'margin_sell', 'margin_buy', 'margin_ratio')},
        'source': {'csv_file': csv_name, 'date': info['date']},
        'rule_version': _ENGINE_VERSION,
        'prev_source': {
            'available': bool(prev_used),
            'from': (prev_used or {}).get('source'),
            'date': (prev_used or {}).get('date'),
            'kind': (prev_used or {}).get('kind'),
            'rule_version': (prev or {}).get('rule_version'),
            'note': prev_note,
        },
        'md_name': fname or ('summary-%s-%s.md' % (code, info['date'].replace('-', ''))),
        'md_body': body,
        'readback': readback,
    })
    _RESULTS[code] = parsed
    return {"code": code, "name": parsed['name'], "status": "完了", "error": None,
            "dip_buy": parsed['judgments']['dip_buy'],
            "buy_score": parsed['buy_score']['total'],
            "sell_score": parsed['sell_score']['total']}


def reset_results():
    _RESULTS.clear()


# ============================================================
# 書き出し
# ============================================================
_G1 = ['機関高_金額', '機関高_比率', '機関高_買い', '機関高_売り',
       '機関中_金額', '機関中_比率', '機関中_買い', '機関中_売り',
       '個人_金額', '個人_比率', '個人_買い', '個人_売り',
       '合計_金額', '合計_買い', '合計_売り']
_G2 = ['当日終値', '当日高値', '当日安値', '最大出来高価格帯',
       'ザラバ機関買い比率', 'TWAP検出件数']


def _csv_text(codes):
    import csv as _csv
    buf = io.StringIO()
    w = _csv.writer(buf, lineterminator='\n')
    w.writerow(['', '', ''] + ['①機関・個人の売買'] + [''] * (len(_G1) - 1)
               + [''] + ['②価格と執行'] + [''] * (len(_G2) - 1) + [''])
    w.writerow(['日付', '銘柄コード', '銘柄名'] + ['①_' + c for c in _G1]
               + [''] + ['②_' + c for c in _G2] + ['状態'])
    for code in codes:
        r = _RESULTS.get(code)
        if not r:
            continue
        head = [_TODAY, code, r.get('name', '')]
        if r.get('status') != '完了':
            w.writerow(head + [''] * 15 + [''] + [''] * 6
                       + ['エラー: ' + str(r.get('error', ''))])
            continue
        p1 = r.get('paste1') or [''] * 15
        p2 = r.get('paste2') or [''] * 6
        w.writerow(head + list(p1) + [''] + list(p2) + ['完了'])
    return buf.getvalue()


def finalize():
    codes = sorted(_RESULTS.keys())
    done = [c for c in codes if _RESULTS[c].get('status') == '完了']
    err = [c for c in codes if _RESULTS[c].get('status') != '完了']
    jst = timezone(timedelta(hours=9))

    stocks = []
    for c in codes:
        r = dict(_RESULTS[c])
        r.pop('md_body', None)
        r.pop('readback', None)
        stocks.append(r)

    payload = {
        'schema_version': SCHEMA_VERSION,
        'runner_version': RUNNER_VERSION,
        'rule_version': _ENGINE_VERSION,
        'rule_compat_key': _compat_of(_ENGINE_VERSION),
        'date': _TODAY,
        'generated_at': datetime.now(jst).isoformat(timespec='seconds'),
        'master_version': _MASTER_VERSION,
        'stock_count': len(codes),
        'codes': codes,
        'stocks': stocks,
        'summary': {
            'target': len(codes),
            'done': len(done),
            'error': len(err),
            'error_codes': err,
            'dip_buy_candidates': [c for c in done
                                   if '構図あり' in (_RESULTS[c]['judgments']['dip_buy'] or '')],
            'reverse_signal': [c for c in done
                               if '該当なし' not in (_RESULTS[c]['judgments']['reverse_signal'] or '')],
            'prev_missing': [c for c in done if not _RESULTS[c]['prev_source']['available']],
        },
    }
    day = _TODAY.replace('-', '')
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, 'w', zipfile.ZIP_DEFLATED) as z:
        for c in done:
            z.writestr(_RESULTS[c]['md_name'], _RESULTS[c]['md_body'])
    return {
        'csv_name': 'paste-%s.csv' % day,
        'csv': _csv_text(codes),
        'json_name': 'summary-%s.json' % day,
        'json': json.dumps(payload, ensure_ascii=False, indent=1),
        'zip_name': 'md-%s.zip' % day,
        'zip_b64': __import__('base64').b64encode(mem.getvalue()).decode('ascii'),
        'summary': payload['summary'],
    }
