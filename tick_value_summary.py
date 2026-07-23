#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
歩み値CSV 固定集計スクリプト v1.6（需給構造判定つき）
──────────────────────────────────
入力: 歩み値CSV（値段,株数,金額,時刻）
出力: 集計結果テキスト（分析推論プロンプトへの入力用）
      買いスコア・売りスコアに加え、需給構造判定（押し目買い判定・当日数値判定・
      注意逆シグナル判定）まで確定値として出力する。

使い方（コードは一切変更せず、引数で値を渡す）:
  python3 tick_value_summary.py <CSVファイルパス> <年初来高値> [オプション]

  必須の位置引数:
    第1引数: CSVファイルパス
    第2引数: 年初来高値（円・数値のみ。--yearhigh= や --meta= で渡す場合は省略可）

  任意のオプション引数（--キー=値 の形式。順不同・省略可）:
    --date=2026年7月23日           日付（需給構造判定に必要）
    --code=7011                    銘柄コード（需給構造判定に必要）
    --name=三菱重工業               銘柄名称
    --yearhigh_date=2026年3月2日   年初来高値の日付
    --margin_sell=1129800          信用売れ残
    --margin_buy=24753100          信用買い残
    --margin_ratio=21.91           信用倍率
    --threshold=5000000            機関／個人を分ける金額（省略時500万円）

  前日データ（前日比が必要な判定に使用。省略すると「判定不能」になる）:
    --prev=前日の集計結果.md        前日のファイルを読み込む
    --prev_row="15項目の貼り付け行"  貼り付け行を直接渡す
    --prev_date=2026-07-22         前日の日付（照合用・--prev_row と併用）
    --prev_code=7011               前日の銘柄コード（照合用・--prev_row と併用）

  銘柄情報をまとめて渡す場合:
    --meta=銘柄情報.txt             「日付: ...」形式のテキストファイルを読み込む

  実行例:
    python3 tick_value_summary.py qr-7011-20260723.csv 5208 \
      --date=2026年7月23日 --code=7011 --name=三菱重工業 \
      --yearhigh_date=2026年3月2日 \
      --margin_sell=1129800 --margin_buy=24753100 --margin_ratio=21.91 \
      --prev=summary-7011-20260722.md

  注意:
    --markers はブラウザ版ツールが内部で使う指定です。手で実行するときは付けません。
"""

import csv
import sys
import os
import re
from collections import defaultdict

# ============================================================
# ルール版番号（出力・ファイル名の照合に使用）
# ============================================================
RULE_VERSION = "v1.8"

# ============================================================
# 境界の定義（プロンプトの文言をそのまま数式にしたもの。変更しないこと）
#   「以上」→ ≧ / 「超」→ > / 「以下」→ ≦
#   「増加」→ 差 > 0 / 「低下または横ばい」→ 差 ≦ 0（0は横ばい側）
#   比較はすべて小数第1位に丸めた値で行う（貼り付け行の桁にそろえる）
# ============================================================
TH_DIP_INST_BUY_PT = 5.0    # 押し目買い 条件1：機関買い主導の前日比（pt・以上）
TH_DAY_INST_BUY    = 5.0    # 当日数値 条件1：機関買い主導（%・以上）
TH_DAY_IND_DIFF_PT = 8.0    # 当日数値 条件2：個人売り−買い（pt・以上）
TH_REV_INST_BUY    = 2.0    # 逆シグナル 条件1：機関買い主導（%・以下）
TH_REV_IND_BUY     = 30.0   # 逆シグナル 条件2：個人買い主導（%・超）


# ブラウザ版から呼ばれたときだけ、出力に目印を付ける。
# コマンドラインで直接動かす場合（生成AIに渡して実行する場合）は素のテキストを出力する。
MARKERS = (('--markers' in sys.argv)
           or any(a.startswith('--meta=') for a in sys.argv)) and ('--plain' not in sys.argv)


def die(msg):
    """処理を止めて理由を返す"""
    print("<<<ERROR>>>" if MARKERS else "【エラー】集計を中止しました")
    print(msg)
    sys.exit(1)


def r1(x):
    """小数第1位に丸める"""
    return round(float(x) + 0.0, 1)


def norm_date(s):
    """日付文字列を (YYYY, MM, DD) に正規化。失敗時は None"""
    if not s:
        return None
    t = str(s).strip()
    for ch in ['年', '月', '/', '-', '.']:
        t = t.replace(ch, ' ')
    t = t.replace('日', ' ')
    parts = [p for p in t.split() if p.isdigit()]
    if len(parts) < 3:
        return None
    y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    if not (1900 < y < 2200 and 1 <= m <= 12 and 1 <= d <= 31):
        return None
    return (y, m, d)


def date_iso(t):
    return "%04d-%02d-%02d" % t


def date_compact(t):
    return "%04d%02d%02d" % t


def clean_num(s):
    """カンマ・単位・空白を除いた数値文字列を返す"""
    if s is None:
        return ''
    t = str(s).strip()
    for ch in [',', '円', '株', '倍', '％', '%', ' ', '　']:
        t = t.replace(ch, '')
    return t


# --- 銘柄情報テキストの項目名（確定した8項目。取得時刻などは読み飛ばす） ---
META_KEYS = {
    '日付': 'date',
    '銘柄コード': 'code',
    '銘柄名称': 'name',
    '年初来高値': 'yearhigh',
    '年初来高値の日付': 'yearhigh_date',
    '信用売れ残': 'margin_sell',
    '信用買い残': 'margin_buy',
    '信用倍率': 'margin_ratio',
}


def parse_meta_text(text):
    """銘柄情報テキストを読み取る。戻り値は (値の辞書, 無視した項目名, 重複した項目名)"""
    got, ignored, dup = {}, [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or line.startswith('```'):
            continue
        sep = None
        for ch in [':', '：']:
            if ch in line:
                sep = ch
                break
        if sep is None:
            continue
        key, val = line.split(sep, 1)
        key = key.strip().lstrip('-・*＊ ').replace('*', '').strip()
        val = val.strip()
        if key not in META_KEYS:
            if key:
                ignored.append(key)
            continue
        k = META_KEYS[key]
        # 「年初来高値」の行に日付が書かれている場合は、日付として扱う
        if k == 'yearhigh' and not clean_num(val).replace('.', '', 1).isdigit() \
                and norm_date(val) is not None:
            k = 'yearhigh_date'
        if k in got:
            dup.append(key)
            continue
        got[k] = val
    return got, ignored, dup


def find_paste_row(lines):
    """15項目の貼り付け行を探す。見出しの有無にかかわらず読み取る"""
    def as_nums(line):
        parts = line.replace('\t', ' ').split()
        if len(parts) != 15:
            return None
        try:
            return [float(p.replace(',', '')) for p in parts]
        except ValueError:
            return None
    for i, line in enumerate(lines):
        if '貼り付け' in line:
            for cand in lines[i + 1:i + 6]:
                nums = as_nums(cand)
                if nums:
                    return nums
    for line in lines:
        nums = as_nums(line)
        if nums:
            return nums
    return None


def find_prev_date(text):
    """本文から日付を推定する（見出し部分がない前日ファイル向け）"""
    m = re.search(r'日付[^0-9]{0,6}(\d{4})[年/\-.](\d{1,2})[月/\-.](\d{1,2})', text)
    if not m:
        m = re.search(r'(\d{4})[年/\-.](\d{1,2})[月/\-.](\d{1,2})', text)
    if not m:
        return None
    return norm_date('%s-%s-%s' % m.groups())


def find_prev_code(text):
    """本文から銘柄コードを推定する（見出し部分がない前日ファイル向け）"""
    m = re.search(r'銘柄[^0-9A-Za-z]{0,8}([0-9][0-9A-Za-z]{3})', text)
    return m.group(1) if m else ''


def parse_prev_md(text):
    """前日データを読み取る。戻り値は (見出し部分の辞書, 貼り付け行の15数値)"""
    head = {}
    lines = text.splitlines()
    if lines and lines[0].strip() == '---':
        for line in lines[1:]:
            if line.strip() == '---':
                break
            if ':' in line:
                k, v = line.split(':', 1)
                head[k.strip()] = v.strip()
    if not head.get('date'):
        d = find_prev_date(text)
        if d:
            head['date'] = date_iso(d)
    if not head.get('code'):
        head['code'] = str(head.get('ticker', '')).strip()
    if not head.get('code'):
        head['code'] = find_prev_code(text)
    return head, find_paste_row(lines)


if len(sys.argv) < 2:
    die("使い方: python3 tick_value_summary.py <CSVファイルパス> [<年初来高値>] [--キー=値 ...]")

CSV_PATH = sys.argv[1]                                  # CSVファイルパス

# --- 引数の解析（第2引数の年初来高値は省略可。--meta で渡す場合に対応） ---
opts = {}
pos = []
for arg in sys.argv[2:]:
    if arg.startswith('--'):
        if '=' in arg:
            key, val = arg[2:].split('=', 1)
            opts[key] = val
        else:
            opts[arg[2:]] = '1'          # --plain や --markers のような値なしの指定
    else:
        pos.append(arg)

# --- 銘柄情報テキスト（ファイル指定）の読み取り ---
META_IGNORED = []
META_DUP = []
if opts.get('meta'):
    try:
        with open(opts['meta'], encoding='utf-8-sig') as f:
            meta_text = f.read()
    except OSError:
        die("銘柄情報のテキストを読み取れませんでした。")
    meta_got, META_IGNORED, META_DUP = parse_meta_text(meta_text)
    for k, v in meta_got.items():
        opts.setdefault(k, v)

# --- 年初来高値（第2位置引数 → --yearhigh の順で採用） ---
_yh = pos[0] if pos else opts.get('yearhigh', '')
_yh = clean_num(_yh)
if not _yh.replace('.', '', 1).isdigit():
    _seen = opts.get('yearhigh', '')
    die("年初来高値を数値として読み取れませんでした（読み取った内容: 「%s」）。\n"
        "銘柄情報の「年初来高値」の行に、数値だけが書かれているか確認してください。\n"
        "日付を書く行の項目名は「年初来高値の日付」です。" % (_seen if _seen else '空欄'))
YEAR_HIGH = int(float(_yh))                             # 年初来高値（円）

THRESHOLD_AMOUNT = int(opts.get('threshold', 5_000_000))  # 機関/個人の閾値（円）

# --- 基本情報の付帯項目（未指定は空欄表示） ---
INFO_DATE          = opts.get('date', '')            # 日付
INFO_CODE          = opts.get('code', '')            # 銘柄コード
INFO_NAME          = opts.get('name', '')            # 銘柄名称
INFO_YEARHIGH_DATE = opts.get('yearhigh_date', '')   # 年初来高値の日付
INFO_MARGIN_SELL   = opts.get('margin_sell', '')     # 信用売れ残
INFO_MARGIN_BUY    = opts.get('margin_buy', '')      # 信用買い残
INFO_MARGIN_RATIO  = opts.get('margin_ratio', '')    # 信用倍率

TODAY_DT = norm_date(INFO_DATE)
if TODAY_DT is None:
    die("日付を読み取れませんでした。銘柄情報に「日付: 2026年7月22日」の行があるか確認してください。")
if not INFO_CODE:
    die("銘柄コードを読み取れませんでした。銘柄情報に「銘柄コード: 5802」の行があるか確認してください。")

# ============================================================
# 前日データ（前日のMDファイル）の読み取りと照合
#   値の正は本文の貼り付け行。見出し部分は照合だけに使う。
#   銘柄コードまたは日付が合わない場合は集計を止める。
# ============================================================
PREV = None
PREV_WARN = []
PREV_NAME = opts.get('prev_name', '')
prev_text = None
if opts.get('prev'):
    try:
        with open(opts['prev'], encoding='utf-8-sig') as f:
            prev_text = f.read()
    except OSError:
        die("前日データを読み取れませんでした。")
elif opts.get('prev_row'):
    # 貼り付け行（15項目）を直接受け取る場合
    prev_text = '【貼り付け用】\n' + opts['prev_row']
    if opts.get('prev_date'):
        prev_text = '---\ndate: %s\ncode: %s\n---\n' % (
            opts['prev_date'], opts.get('prev_code', '')) + prev_text
if prev_text is not None:
    prev_head, prev_nums = parse_prev_md(prev_text)
    if prev_nums is None:
        die("前日データの中に貼り付け行（15項目）が見つかりませんでした。数値が15個そろっているか確認してください。")
    prev_code = str(prev_head.get('code', '')).strip()
    if prev_code and prev_code != str(INFO_CODE).strip():
        die("前日データの銘柄コードが違います（前日 %s / 当日 %s）。集計を中止しました。"
            % (prev_code, INFO_CODE))
    prev_dt = norm_date(prev_head.get('date', ''))
    if prev_dt is not None and prev_dt >= TODAY_DT:
        die("前日データの日付が当日以降です（前日 %s / 当日 %s）。集計を中止しました。"
            % (date_iso(prev_dt), date_iso(TODAY_DT)))
    if prev_dt is None:
        PREV_WARN.append("前日データの日付を読み取れませんでした（照合できていません）")
    if not prev_code:
        PREV_WARN.append("前日データの銘柄コードを読み取れませんでした（照合できていません）")
    PREV = {
        'date': date_iso(prev_dt) if prev_dt else '日付不明',
        'inst_buy':  r1(prev_nums[2] + prev_nums[6]),
        'inst_sell': r1(prev_nums[3] + prev_nums[7]),
        'ind_buy':   r1(prev_nums[10]),
        'ind_sell':  r1(prev_nums[11]),
    }

# ============================================================
# ユーティリティ関数
# ============================================================
def to_seconds(t):
    """時刻文字列 HH:MM:SS → 秒数"""
    h, m, s = map(int, t.split(':'))
    return h * 3600 + m * 60 + s

def to_num(s):
    """数値文字列を数に変換する。小数の値段（0.5円刻みなど）にも対応し、
    整数で表せる場合は整数として返す"""
    v = float(str(s).replace(',', '').replace('　', '').strip())
    iv = int(v)
    return iv if v == iv else v


def format_number(n):
    """数値をカンマ区切り文字列に"""
    return f"{n:,}"

# ============================================================
# メイン処理
# ============================================================
def main():
    # --- CSV読み込み ---
    raw_rows = []
    with open(CSV_PATH, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            die("CSVの中身が空でした。")
        need = ['値段', '株数', '金額', '時刻']
        missing = [c for c in need if c not in reader.fieldnames]
        if missing:
            die("CSVの見出しに次の項目が見つかりません: %s\n読み取った見出し: %s"
                % ('、'.join(missing), '、'.join([c for c in reader.fieldnames if c])))
        for i, r in enumerate(reader, start=2):
            try:
                raw_rows.append({
                    'price':  to_num(r['値段']),
                    'volume': to_num(r['株数']),
                    'amount': to_num(r['金額']),
                    'time':   r['時刻'].strip()
                })
            except (ValueError, TypeError, AttributeError):
                die("CSVの%d行目を数値として読み取れませんでした。\n"
                    "値段=「%s」 株数=「%s」 金額=「%s」 時刻=「%s」"
                    % (i, r.get('値段'), r.get('株数'), r.get('金額'), r.get('時刻')))
    if not raw_rows:
        die("CSVに約定データの行がありませんでした。")

    # 時系列順に並び替え（CSVは降順の場合がある）
    # 最初の行と最後の行の時刻を比較
    if to_seconds(raw_rows[0]['time']) > to_seconds(raw_rows[-1]['time']):
        raw_rows.reverse()

    rows = raw_rows
    total_rows = len(rows)

    # ============================================================
    # 1. 基本情報
    # ============================================================
    total_volume = sum(r['volume'] for r in rows)
    total_amount = sum(r['amount'] for r in rows)
    day_vwap = total_amount / total_volume if total_volume > 0 else 0

    # 始値: 最も早い時刻の約定価格
    opening_price = rows[0]['price']

    # 終値: 15:30:00 の引け約定価格
    closing_rows = [r for r in rows if r['time'] == '15:30:00']
    has_closing = len(closing_rows) > 0
    if has_closing:
        closing_price = closing_rows[-1]['price']
        closing_volume = sum(r['volume'] for r in closing_rows)
        closing_amount = sum(r['amount'] for r in closing_rows)
    else:
        closing_price = rows[-1]['price']
        closing_volume = 0
        closing_amount = 0

    # ============================================================
    # 2. ティックルール（売買方向の推定）
    # ============================================================
    directions = []
    prev_price = None
    prev_dir = None
    for i, r in enumerate(rows):
        if i == 0:
            directions.append(None)  # 寄り付き: 方向なし
            prev_price = r['price']
            continue
        if r['price'] > prev_price:
            d = 'buy'
        elif r['price'] < prev_price:
            d = 'sell'
        else:
            d = prev_dir  # 同値: 直前の方向を引き継ぐ
        directions.append(d)
        if d is not None:
            prev_dir = d
        prev_price = r['price']

    # ============================================================
    # 3. 前場・後場の集計（引け約定の扱い分け）
    # ============================================================
    am_vol = 0; am_amt = 0
    pm_vol = 0; pm_amt = 0          # 引け約定を含む
    pm_zaraba_vol = 0; pm_zaraba_amt = 0  # 引け約定を除く（VWAP用）
    closing_auction_vol = 0; closing_auction_amt = 0

    for r in rows:
        secs = to_seconds(r['time'])
        if r['time'] == '15:30:00':
            pm_vol += r['volume']
            pm_amt += r['amount']
            closing_auction_vol += r['volume']
            closing_auction_amt += r['amount']
        elif 9 * 3600 <= secs <= 11 * 3600 + 30 * 60:
            am_vol += r['volume']
            am_amt += r['amount']
        elif 12 * 3600 + 30 * 60 <= secs < 15 * 3600 + 30 * 60:
            pm_vol += r['volume']
            pm_amt += r['amount']
            pm_zaraba_vol += r['volume']
            pm_zaraba_amt += r['amount']

    pm_ratio = pm_vol / total_volume * 100 if total_volume > 0 else 0
    am_vwap = am_amt / am_vol if am_vol > 0 else 0
    pm_vwap = pm_zaraba_amt / pm_zaraba_vol if pm_zaraba_vol > 0 else 0

    # ============================================================
    # 4. 最大出来高価格帯
    # ============================================================
    price_volume = defaultdict(int)
    for r in rows:
        price_volume[r['price']] += r['volume']
    max_vol_price = max(price_volume, key=price_volume.get)
    max_vol_shares = price_volume[max_vol_price]

    # ============================================================
    # 5. 買いスコア各項目
    # ============================================================
    # 項目1: 後場出来高比率65%超 AND 後場VWAP > 前場VWAP
    buy_item1 = pm_ratio > 65 and pm_vwap > am_vwap

    # 項目2: 1000株以上の約定比率（件数ベース）50%超
    large_count = sum(1 for r in rows if r['volume'] >= 1000)
    large_ratio = large_count / total_rows * 100 if total_rows > 0 else 0
    buy_item2 = large_ratio > 50

    # 項目3: 終値 > 始値
    buy_item3 = closing_price > opening_price if has_closing else False

    # 項目4: 14:30〜15:00に5000株以上が3件以上
    big_1430 = sum(1 for r in rows
                   if to_seconds(r['time']) >= 14 * 3600 + 30 * 60
                   and to_seconds(r['time']) < 15 * 3600
                   and r['volume'] >= 5000)
    buy_item4 = big_1430 >= 3

    # 項目5: TWAP型の大口が後場に観察される
    # 定義: 1000〜5000株、500万円以上、5分間隔(±1分)、同程度株数(±20%)、同一方向、3回以上
    twap_candidates = []
    for i, r in enumerate(rows):
        if r['time'] == '15:30:00':
            continue
        secs = to_seconds(r['time'])
        if secs >= 12 * 3600 + 30 * 60 and secs < 15 * 3600 + 25 * 60:
            if 1000 <= r['volume'] <= 5000 and r['amount'] >= THRESHOLD_AMOUNT:
                twap_candidates.append((i, secs, r['volume'], directions[i]))

    twap_indices = set()
    for si in range(len(twap_candidates)):
        idx_s, secs_s, vol_s, dir_s = twap_candidates[si]
        if dir_s is None:
            continue
        seq = [(idx_s, secs_s, vol_s)]
        for ni in range(si + 1, len(twap_candidates)):
            idx_n, secs_n, vol_n, dir_n = twap_candidates[ni]
            if dir_n != dir_s:
                continue
            last_secs = seq[-1][1]
            dt = abs(secs_n - last_secs)
            if 240 <= dt <= 360:  # 4〜6分（5分±1分）
                if vol_s > 0 and abs(vol_n - vol_s) / vol_s <= 0.2:
                    seq.append((idx_n, secs_n, vol_n))
        if len(seq) >= 3:
            for s in seq:
                twap_indices.add(s[0])

    buy_item5_twap = len(twap_indices) > 0

    # 項目6: 終値 > 最大出来高価格帯
    buy_item6_maxvol = closing_price > max_vol_price if has_closing else False

    # ============================================================
    # 6. 反証条件
    # ============================================================
    # 反証1: 9:00〜9:15に大口注文が全体の30%超
    total_large_vol = sum(r['volume'] for r in rows if r['volume'] >= 1000)
    early_large_vol = sum(r['volume'] for r in rows
                         if r['volume'] >= 1000
                         and to_seconds(r['time']) <= 9 * 3600 + 15 * 60)
    early_ratio = early_large_vol / total_large_vol * 100 if total_large_vol > 0 else 0
    counter1 = early_ratio > 30

    # 反証2: 年初来高値の90%以上で大口買い集中
    zone_90 = YEAR_HIGH * 0.9 if YEAR_HIGH > 0 else 999999
    large_buy_zone90 = sum(r['volume'] for i, r in enumerate(rows)
                          if r['volume'] >= 1000 and directions[i] == 'buy'
                          and r['price'] >= zone_90)
    total_large_buy_vol = sum(r['volume'] for i, r in enumerate(rows)
                             if r['volume'] >= 1000 and directions[i] == 'buy')
    zone90_ratio = large_buy_zone90 / total_large_buy_vol * 100 if total_large_buy_vol > 0 else 0
    # 「集中」= 大口買いの過半数が90%ゾーンにある場合
    counter2 = zone90_ratio > 50

    # 反証3: 同一秒に1000株以上が2件以上ある秒が5秒超
    time_large_counts = defaultdict(int)
    for r in rows:
        if r['volume'] >= 1000 and r['time'] != '15:30:00' and r['time'] != '09:00:00':
            time_large_counts[r['time']] += 1
    same_sec_count = sum(1 for c in time_large_counts.values() if c >= 2)
    counter3 = same_sec_count > 5

    # ============================================================
    # 7. 買いスコア算出
    # ============================================================
    base_buy = (2 if buy_item1 else 0) + (2 if buy_item2 else 0) + \
               (1 if buy_item3 else 0) + (1 if buy_item4 else 0)
    extra_buy = (1 if buy_item5_twap else 0) + (1 if buy_item6_maxvol else 0)
    counter_total = (1 if counter1 else 0) + (1 if counter2 else 0) + (1 if counter3 else 0)
    buy_score = max(0, base_buy + extra_buy - counter_total)

    # 買い判定
    if not has_closing:
        buy_judgment = "判定不能（引け約定なし）"
    elif buy_score >= 6:
        buy_judgment = "買い初動の可能性が高い"
    elif buy_score >= 3:
        buy_judgment = "要継続観察"
    else:
        buy_judgment = "シグナルなし"

    # ============================================================
    # 8. 売りスコア各項目
    # ============================================================
    # 項目1: 後場VWAP < 前場VWAP
    sell_item1 = pm_vwap < am_vwap

    # 項目2: VWAP比マイナス乖離＋出来高増加
    # 30分ブロック単位で、VWAP下回り＋前ブロックより出来高増加を検出
    block_data = defaultdict(lambda: {'vol': 0, 'amt': 0})
    for r in rows:
        secs = to_seconds(r['time'])
        b = (secs // 1800) * 1800
        block_data[b]['vol'] += r['volume']
        block_data[b]['amt'] += r['amount']

    sell_item2 = False
    prev_block_vol = None
    for b in sorted(block_data.keys()):
        if b < 12 * 3600 + 30 * 60:
            prev_block_vol = block_data[b]['vol']
            continue
        bvol = block_data[b]['vol']
        bvwap = block_data[b]['amt'] / bvol if bvol > 0 else 0
        if bvwap < day_vwap and prev_block_vol is not None and bvol > prev_block_vol:
            sell_item2 = True
        prev_block_vol = bvol

    # 項目3: 年初来高値85%以上で大口売りが後場に集中 → +2点
    zone_85 = YEAR_HIGH * 0.85 if YEAR_HIGH > 0 else 999999
    large_sell_pm_zone85 = sum(r['volume'] for i, r in enumerate(rows)
                              if r['volume'] >= 1000
                              and directions[i] == 'sell'
                              and r['price'] >= zone_85
                              and to_seconds(r['time']) >= 12 * 3600 + 30 * 60)
    total_large_sell_pm = sum(r['volume'] for i, r in enumerate(rows)
                             if r['volume'] >= 1000
                             and directions[i] == 'sell'
                             and to_seconds(r['time']) >= 12 * 3600 + 30 * 60)
    zone85_sell_ratio = large_sell_pm_zone85 / total_large_sell_pm * 100 if total_large_sell_pm > 0 else 0
    # 「集中」= 後場大口売りの過半数が85%ゾーンにある場合
    sell_item3 = zone85_sell_ratio > 50

    # 項目4: 終値 < 最大出来高価格帯
    sell_item4 = closing_price < max_vol_price if has_closing else False

    sell_score = (1 if sell_item1 else 0) + (1 if sell_item2 else 0) + \
                 (2 if sell_item3 else 0) + (1 if sell_item4 else 0)

    # 売り判定
    if sell_score >= 3:
        sell_judgment = "売り初動の可能性あり"
    elif sell_score == 2:
        sell_judgment = "要注意"
    else:
        sell_judgment = "売りシグナルなし"

    # ============================================================
    # 9. 重点時間帯の判定
    # ============================================================
    time_bins = defaultdict(int)
    for i, r in enumerate(rows):
        if r['volume'] >= 1000:
            secs = to_seconds(r['time'])
            bin_start = (secs // 1800) * 1800
            h = bin_start // 3600
            m = (bin_start % 3600) // 60
            time_bins[f"{h:02d}:{m:02d}"] += r['amount']

    total_large_amount = sum(time_bins.values())
    heavy_bands = []
    for tb in sorted(time_bins.keys()):
        pct = time_bins[tb] / total_large_amount * 100 if total_large_amount > 0 else 0
        if pct >= 25:
            # 30分区切りの終了時刻を算出
            parts = tb.split(':')
            end_h = int(parts[0])
            end_m = int(parts[1]) + 30
            if end_m >= 60:
                end_h += 1
                end_m -= 60
            heavy_bands.append(f"{tb}〜{end_h:02d}:{end_m:02d}")

    # ============================================================
    # 10. 機関・個人の売買分類
    # ============================================================
    # 10a. 執行パターン判定用のインデックスセット構築

    # ③ 同一方向・同水準の大口が同じ30分区切りで3件以上連続
    is_closing_flag = [r['time'] == '15:30:00' for r in rows]
    is_opening_flag = [r['time'] == '09:00:00' and i == 0 for i, r in enumerate(rows)]
    # 寄り付き09:00:00の複数約定も含めるため、最初の行だけでなく09:00:00全体を確認
    # ただし寄り付き板寄せは複数行に分かれることがあるため、09:00:00の行は開始処理として扱う

    consec_set = set()
    bin_dir_orders = defaultdict(list)
    for i, r in enumerate(rows):
        if r['amount'] >= THRESHOLD_AMOUNT and not is_closing_flag[i]:
            secs = to_seconds(r['time'])
            b = secs // 1800
            d = directions[i]
            if d is not None:
                bin_dir_orders[(b, d)].append(i)

    for (b, d), indices in bin_dir_orders.items():
        if len(indices) >= 3:
            for idx in indices:
                consec_set.add(idx)

    # ① TWAP型（既に twap_indices で算出済み）
    # ② 引け約定
    closing_set = set()
    for i, r in enumerate(rows):
        if r['time'] == '15:30:00' and r['amount'] >= THRESHOLD_AMOUNT:
            closing_set.add(i)

    inst_high_set = consec_set | twap_indices | closing_set

    # 10b. 分類集計
    ih_count = 0; ih_vol = 0; ih_amt = 0; ih_buy_amt = 0; ih_sell_amt = 0
    im_count = 0; im_vol = 0; im_amt = 0; im_buy_amt = 0; im_sell_amt = 0
    rt_count = 0; rt_vol = 0; rt_amt = 0; rt_buy_amt = 0; rt_sell_amt = 0

    for i, r in enumerate(rows):
        d = directions[i]
        if r['amount'] >= THRESHOLD_AMOUNT:
            if i in inst_high_set:
                ih_count += 1; ih_vol += r['volume']; ih_amt += r['amount']
                if d == 'buy':   ih_buy_amt += r['amount']
                elif d == 'sell': ih_sell_amt += r['amount']
            else:
                im_count += 1; im_vol += r['volume']; im_amt += r['amount']
                if d == 'buy':   im_buy_amt += r['amount']
                elif d == 'sell': im_sell_amt += r['amount']
        else:
            rt_count += 1; rt_vol += r['volume']; rt_amt += r['amount']
            if d == 'buy':   rt_buy_amt += r['amount']
            elif d == 'sell': rt_sell_amt += r['amount']

    ta = total_amount  # 短縮
    ih_ratio = ih_amt / ta * 100 if ta > 0 else 0
    im_ratio = im_amt / ta * 100 if ta > 0 else 0
    rt_ratio = rt_amt / ta * 100 if ta > 0 else 0

    ih_buy_pct  = ih_buy_amt / ta * 100 if ta > 0 else 0
    ih_sell_pct = ih_sell_amt / ta * 100 if ta > 0 else 0
    im_buy_pct  = im_buy_amt / ta * 100 if ta > 0 else 0
    im_sell_pct = im_sell_amt / ta * 100 if ta > 0 else 0
    rt_buy_pct  = rt_buy_amt / ta * 100 if ta > 0 else 0
    rt_sell_pct = rt_sell_amt / ta * 100 if ta > 0 else 0

    # 機関500万円以上合算
    inst_buy_pct  = ih_buy_pct + im_buy_pct
    inst_sell_pct = ih_sell_pct + im_sell_pct

    total_buy_pct  = ih_buy_pct + im_buy_pct + rt_buy_pct
    total_sell_pct = ih_sell_pct + im_sell_pct + rt_sell_pct

    # ============================================================
    # 11. 30分ブロック別の詳細（参考）
    # ============================================================
    block_details = []
    for b in sorted(block_data.keys()):
        h = b // 3600
        m = (b % 3600) // 60
        bvol = block_data[b]['vol']
        bvwap = block_data[b]['amt'] / bvol if bvol > 0 else 0
        block_details.append(f"  {h:02d}:{m:02d}  出来高={format_number(bvol)}  VWAP={bvwap:.1f}")

    # ============================================================
    # 12. 引け約定の方向
    # ============================================================
    closing_direction = "不明"
    if has_closing:
        # 引け約定の直前のティックを探す
        closing_idx = None
        for i, r in enumerate(rows):
            if r['time'] == '15:30:00':
                closing_idx = i
                break
        if closing_idx is not None and closing_idx > 0:
            prev_tick_price = rows[closing_idx - 1]['price']
            if closing_price > prev_tick_price:
                closing_direction = "買い主導"
            elif closing_price < prev_tick_price:
                closing_direction = "売り主導"
            else:
                closing_direction = "同値（直前方向引き継ぎ）"

    # ============================================================
    # 出力
    # ============================================================
    output = []
    output.append("=" * 60)
    output.append("歩み値CSV 集計結果")
    output.append("=" * 60)

    output.append("")
    output.append("【基本情報】")
    output.append(f"日付：{INFO_DATE}")
    output.append(f"銘柄：{INFO_CODE}／{INFO_NAME}")
    if INFO_YEARHIGH_DATE:
        output.append(f"年初来高値（{INFO_YEARHIGH_DATE}）：{format_number(YEAR_HIGH)}")
    else:
        output.append(f"年初来高値：{format_number(YEAR_HIGH)}")
    output.append(f"信用売れ残：{INFO_MARGIN_SELL}")
    output.append(f"信用買い残：{INFO_MARGIN_BUY}")
    output.append(f"信用倍率：{INFO_MARGIN_RATIO}")
    output.append(f"総約定件数: {format_number(total_rows)}")
    output.append(f"始値: {format_number(opening_price)}円")
    output.append(f"終値: {format_number(closing_price)}円（引け約定{'あり' if has_closing else 'なし'}）")
    output.append(f"日中VWAP: {day_vwap:.2f}円")
    output.append(f"出来高合計: {format_number(total_volume)}株")
    output.append(f"売買代金合計: {format_number(total_amount)}円")

    output.append("")
    output.append("【前場・後場】")
    output.append(f"前場出来高: {format_number(am_vol)}株 ／ 前場VWAP: {am_vwap:.2f}円")
    output.append(f"後場出来高: {format_number(pm_vol)}株（引け約定{format_number(closing_auction_vol)}株を含む）")
    output.append(f"後場出来高比率: {pm_ratio:.1f}%")
    output.append(f"後場VWAP（ザラバのみ）: {pm_vwap:.2f}円")
    output.append(f"後場VWAP > 前場VWAP: {'はい' if pm_vwap > am_vwap else 'いいえ'}")

    output.append("")
    output.append("【引け約定（クロージング・オークション）】")
    output.append(f"株数: {format_number(closing_volume)}株")
    output.append(f"金額: {format_number(closing_amount)}円")
    output.append(f"方向: {closing_direction}")

    output.append("")
    output.append("【最大出来高価格帯】")
    output.append(f"価格: {format_number(max_vol_price)}円（{format_number(max_vol_shares)}株）")
    output.append(f"終値との比較: 終値{closing_price} {'>' if closing_price > max_vol_price else '=' if closing_price == max_vol_price else '<'} 最大出来高価格帯{max_vol_price}")

    output.append("")
    output.append("【買いスコア項目別】")
    output.append(f"項目1（後場出来高65%超＋後場VWAP>前場VWAP）: {'該当' if buy_item1 else '非該当'}（後場{pm_ratio:.1f}%、後場VWAP{pm_vwap:.2f} vs 前場VWAP{am_vwap:.2f}）→ {'+2' if buy_item1 else '0'}点")
    output.append(f"項目2（大口比率50%超）: {'該当' if buy_item2 else '非該当'}（{large_ratio:.1f}%、{large_count}/{total_rows}件）→ {'+2' if buy_item2 else '0'}点")
    output.append(f"項目3（終値>始値）: {'該当' if buy_item3 else '非該当'}（終値{closing_price} vs 始値{opening_price}）→ {'+1' if buy_item3 else '0'}点")
    output.append(f"項目4（14:30-15:00 ≥5000株 ≥3件）: {'該当' if buy_item4 else '非該当'}（{big_1430}件）→ {'+1' if buy_item4 else '0'}点")
    output.append(f"項目5（後場TWAP検出）: {'該当' if buy_item5_twap else '非該当'}（TWAP該当約定{len(twap_indices)}件）→ {'+1' if buy_item5_twap else '0'}点")
    output.append(f"項目6（終値>最大出来高価格帯）: {'該当' if buy_item6_maxvol else '非該当'}（終値{closing_price} vs {max_vol_price}）→ {'+1' if buy_item6_maxvol else '0'}点")

    output.append("")
    output.append("【反証条件】")
    output.append(f"反証1（9:00-9:15 大口30%超集中）: {'該当' if counter1 else '非該当'}（{early_ratio:.1f}%）→ {'-1' if counter1 else '0'}点")
    output.append(f"反証2（年初来高値90%以上で大口買い集中）: {'該当' if counter2 else '非該当'}（90%ライン={zone_90:.0f}円、ゾーン内比率{zone90_ratio:.1f}%）→ {'-1' if counter2 else '0'}点")
    output.append(f"反証3（同一秒大口混在>5秒）: {'該当' if counter3 else '非該当'}（{same_sec_count}秒）→ {'-1' if counter3 else '0'}点")

    output.append("")
    output.append(f"【買いスコア合計】基本{base_buy} + 追加{extra_buy} - 反証{counter_total} = {buy_score}/8点")
    output.append(f"【買い判定】{buy_judgment}")

    output.append("")
    output.append("【売りスコア項目別】")
    output.append(f"項目1（後場VWAP<前場VWAP）: {'該当' if sell_item1 else '非該当'}（後場{pm_vwap:.2f} vs 前場{am_vwap:.2f}）→ {'+1' if sell_item1 else '0'}点")
    output.append(f"項目2（VWAPマイナス乖離＋出来高増加）: {'該当' if sell_item2 else '非該当'} → {'+1' if sell_item2 else '0'}点")
    output.append(f"項目3（85%以上で大口売り後場集中）: {'該当' if sell_item3 else '非該当'}（85%ライン={zone_85:.0f}円、ゾーン内後場売り{format_number(large_sell_pm_zone85)}株、比率{zone85_sell_ratio:.1f}%）→ {'+2' if sell_item3 else '0'}点")
    output.append(f"項目4（終値<最大出来高価格帯）: {'該当' if sell_item4 else '非該当'}（終値{closing_price} vs {max_vol_price}）→ {'+1' if sell_item4 else '0'}点")

    output.append("")
    output.append(f"【売りスコア合計】{sell_score}/5点")
    output.append(f"【売り判定】{sell_judgment}")

    output.append("")
    output.append("【重点時間帯】")
    if heavy_bands:
        output.append(f"あり（{', '.join(heavy_bands)}）")
        for tb in sorted(time_bins.keys()):
            pct = time_bins[tb] / total_large_amount * 100 if total_large_amount > 0 else 0
            marker = " ★" if pct >= 25 else ""
            output.append(f"  {tb}: {format_number(time_bins[tb])}円（{pct:.1f}%）{marker}")
    else:
        output.append("なし")

    output.append("")
    output.append("【機関・個人の売買試算（金額基準・確度3段階）】")
    output.append(f"機関確度・高（500万円以上＋執行パターンあり）: 件数{format_number(ih_count)}件 ／ 株数{format_number(ih_vol)}株 ／ 金額{format_number(ih_amt)}円 ／ 比率{ih_ratio:.1f}% ／ 買い主導（全体比）{ih_buy_pct:.1f}%・売り主導（全体比）{ih_sell_pct:.1f}%")
    output.append(f"機関確度・中（500万円以上・単発）: 件数{format_number(im_count)}件 ／ 株数{format_number(im_vol)}株 ／ 金額{format_number(im_amt)}円 ／ 比率{im_ratio:.1f}% ／ 買い主導（全体比）{im_buy_pct:.1f}%・売り主導（全体比）{im_sell_pct:.1f}%")
    output.append(f"個人（500万円未満）: 件数{format_number(rt_count)}件 ／ 株数{format_number(rt_vol)}株 ／ 金額{format_number(rt_amt)}円 ／ 比率{rt_ratio:.1f}% ／ 買い主導（全体比）{rt_buy_pct:.1f}%・売り主導（全体比）{rt_sell_pct:.1f}%")
    output.append(f"機関500万円以上合算: 買い主導（全体比）{inst_buy_pct:.1f}%・売り主導（全体比）{inst_sell_pct:.1f}%")

    output.append("")
    output.append("【貼り付け用（タブ区切り）】")
    paste = "\t".join([
        str(ih_amt), f"{ih_ratio:.1f}", f"{ih_buy_pct:.1f}", f"{ih_sell_pct:.1f}",
        str(im_amt), f"{im_ratio:.1f}", f"{im_buy_pct:.1f}", f"{im_sell_pct:.1f}",
        str(rt_amt), f"{rt_ratio:.1f}", f"{rt_buy_pct:.1f}", f"{rt_sell_pct:.1f}",
        str(total_amount), f"{total_buy_pct:.1f}", f"{total_sell_pct:.1f}"
    ])
    output.append(paste)

    output.append("")
    output.append("【需給構造判定用の数値】")
    output.append(f"機関500万円以上・買い主導（全体比）: {inst_buy_pct:.1f}%")
    output.append(f"機関500万円以上・売り主導（全体比）: {inst_sell_pct:.1f}%")
    output.append(f"個人・買い主導（全体比）: {rt_buy_pct:.1f}%")
    output.append(f"個人・売り主導（全体比）: {rt_sell_pct:.1f}%")

    output.append("")
    output.append("【注意逆シグナル判定用】")
    output.append(f"機関500万円以上・買い主導 ≤ 2%: {'はい' if inst_buy_pct <= 2 else 'いいえ'}（{inst_buy_pct:.1f}%）")
    output.append(f"個人・買い主導 > 30%: {'はい' if rt_buy_pct > 30 else 'いいえ'}（{rt_buy_pct:.1f}%）")

    output.append("")
    output.append("【30分ブロック別VWAP・出来高（参考）】")
    for line in block_details:
        output.append(line)

    output.append("")
    output.append("【価格帯情報（参考）】")
    output.append(f"当日高値: {max(prices := [r['price'] for r in rows])}円")
    output.append(f"当日安値: {min(prices)}円")
    output.append(f"年初来高値: {format_number(YEAR_HIGH)}円")
    output.append(f"年初来高値の90%: {zone_90:.0f}円")
    output.append(f"年初来高値の85%: {zone_85:.0f}円")

    # ============================================================
    # 需給構造判定（分析プロンプトの回答形式にそろえる）
    # 　当日値・前日値ともに小数第1位で比較する
    # ============================================================
    cb, cs = r1(inst_buy_pct), r1(inst_sell_pct)
    ib, isl = r1(rt_buy_pct), r1(rt_sell_pct)

    output.append("")
    output.append("【需給構造判定】")

    if PREV is None:
        output.append("押し目買い判定：前日データなし：判定不能")
    else:
        d_inst_buy = r1(cb - PREV['inst_buy'])
        d_ind_sell = r1(isl - PREV['ind_sell'])
        dip1 = d_inst_buy >= TH_DIP_INST_BUY_PT
        dip2 = d_ind_sell > 0
        detail = ("機関買い主導 前日%.1f%%→当日%.1f%%：%+.1fpt、個人売り主導 前日%.1f%%→当日%.1f%%：%s"
                  % (PREV['inst_buy'], cb, d_inst_buy,
                     PREV['ind_sell'], isl, '増加' if dip2 else ('減少' if d_ind_sell < 0 else '横ばい')))
        if dip1 and dip2:
            output.append("押し目買い判定：押し目買いの構図あり（%s）" % detail)
        else:
            output.append("押し目買い判定：構図なし（%s）" % detail)

    output.append("")
    output.append("当日数値判定：")
    day1 = cb >= TH_DAY_INST_BUY
    diff_ind = r1(isl - ib)
    day2 = diff_ind >= TH_DAY_IND_DIFF_PT
    output.append("条件1（機関買い5%%以上）：%s（機関買い主導%.1f%%）"
                  % ('該当' if day1 else '非該当', cb))
    output.append("条件2（個人売り−買い≧8pt）：%s（個人売り主導%.1f%% − 個人買い主導%.1f%% ＝ %+.1fpt差）"
                  % ('該当' if day2 else '非該当', isl, ib, diff_ind))
    if PREV is None:
        day3 = None
        output.append("条件3（機関売り前日比低下・横ばい）：判定不能（前日データなし）")
    else:
        d_inst_sell = r1(cs - PREV['inst_sell'])
        day3 = d_inst_sell <= 0
        output.append("条件3（機関売り前日比低下・横ばい）：%s（機関売り主導 前日%.1f%%→当日%.1f%%：%+.1fpt）"
                      % ('該当' if day3 else '非該当', PREV['inst_sell'], cs, d_inst_sell))
    if day3 is None:
        output.append("総合：一部判定不能")
    elif day1 and day2 and day3:
        output.append("総合：押し目買い候補")
    else:
        output.append("総合：非該当")

    output.append("")
    rev1 = cb <= TH_REV_INST_BUY
    rev2 = ib > TH_REV_IND_BUY
    if rev1 and rev2:
        output.append("注意逆シグナル判定：注意逆シグナル（機関買い主導%.1f%%、個人買い主導%.1f%%）" % (cb, ib))
    else:
        output.append("注意逆シグナル判定：該当なし")

    output.append("")
    output.append("【前日データの出典】")
    if PREV is None:
        output.append("前日ファイルなし（前日比が必要な判定は判定不能）")
    else:
        output.append("%s（%s ／ %s）" % (PREV_NAME or '貼り付け', PREV['date'], INFO_CODE))

    output.append("")
    output.append("=" * 60)
    output.append("集計完了")
    output.append("=" * 60)

    # ============================================================
    # 出力（見出し部分を付け、ブラウザ側が扱える形式で返す）
    # ============================================================
    body = []
    body.append("---")
    body.append("date: %s" % date_iso(TODAY_DT))
    body.append("code: %s" % INFO_CODE)
    body.append("name: %s" % INFO_NAME)
    body.append("rule_version: %s" % RULE_VERSION)
    body.append("---")
    body.append("")
    body.extend(output)

    readback = []
    readback.append("日付: %s" % INFO_DATE)
    readback.append("銘柄: %s %s" % (INFO_CODE, INFO_NAME))
    readback.append("年初来高値: %s円（%s）" % (format_number(YEAR_HIGH), INFO_YEARHIGH_DATE or '日付なし'))
    readback.append("信用: 売れ残%s ／ 買い残%s ／ 倍率%s" % (INFO_MARGIN_SELL, INFO_MARGIN_BUY, INFO_MARGIN_RATIO))
    if META_IGNORED:
        readback.append("使わなかった項目: %s" % '、'.join(META_IGNORED))
    if META_DUP:
        readback.append("注意: 同じ項目名が重複していました（先に書かれた方を使用）: %s" % '、'.join(META_DUP))
    if PREV is None:
        readback.append("前日データ: なし（前日比の判定は判定不能）")
    else:
        readback.append("前日データ: %s（%s）" % (PREV_NAME or '貼り付け', PREV['date']))
        for w in PREV_WARN:
            readback.append("注意: %s" % w)

    if MARKERS:
        print("<<<FILENAME>>>")
        print("summary-%s-%s.md" % (INFO_CODE, date_compact(TODAY_DT)))
        print("<<<READBACK>>>")
        print("\n".join(readback))
        print("<<<BODY>>>")
        print("\n".join(body))
    else:
        print("\n".join(body))

if __name__ == '__main__':
    main()
