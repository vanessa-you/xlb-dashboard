#!/usr/bin/env python3
# 任务5：每日统计订单 —— 按司机/门店统计整箱、拆零、总计，写入「每日统计订单」
#
# 触发格式（2026-09-18 用户要求）：
#   python3 task5.py                                  ← 无参数：默认查「默认司机名单」+ 今天
#   python3 task5.py "9-17，姓名1，姓名2，姓名3"      ← 首段=日期(9-17→当年)，其余=司机姓名
#   python3 task5.py 9-17 姓名1 姓名2                 ← 空格分隔亦可
#   python3 task5.py 2026-09-17                       ← 完整日期，全部司机
#   python3 task5.py 张家志 王勇                       ← 首段不是日期时，全部段按司机名处理（日期=今天）
#   python3 task5.py 9-17 -n 姓名1                     ← 兼容旧 -n 风格
# 接口：delivery_date=["YYYY-MM-DD","YYYY-MM-DD"]（当日），统计该日全部运单
# 聚合：每 (车牌,司机,门店) 一行；当天没有配送任务的司机自然不出现在结果里（忽略）
# 写表：文档 QRvnCQXoQNBt / sheet BB08J2，表头 A车牌号 B司机 C门店 D整箱 E拆零 F总计；
#       写入前清 A2:Fxxx，数据从第2行起
import json, os, subprocess, sys, urllib.request, datetime, re

# 默认司机名单（2026-10-10 用户指定）：不带司机参数时只查这些人
# 优先级：命令行 > 环境变量 XLB_DRIVERS（逗号分隔）> 下面的默认名单
DEFAULT_DRIVERS = ['张家志', '王勇', '刘习', '黄镇', '梁奥琪', '许祥', '左肖', '王杭军']

# ---------- 0. token 自举 ----------
def _bootstrap_env():
    env = dict(os.environ)
    if env.get('TDOC_OAUTH_ACCESS_TOKEN'):
        return env
    try:
        cfg = json.loads(os.environ['CODEBUDDY_MCP_CONFIG'])
        server = cfg['mcpServers']['connector-proxy']
        headers = {k: v for k, v in (server.get('headers') or {}).items()
                   if isinstance(k, str) and isinstance(v, str) and v}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = server['url'] + '/internal/tencent-docs/tokens'
        data = json.loads(opener.open(urllib.request.Request(url, headers=headers, method='GET'), timeout=10).read().decode('utf-8'))
        tok = (data.get('personal') or {}).get('token', '')
        if tok:
            env['TDOC_OAUTH_ACCESS_TOKEN'] = tok
    except Exception as e:
        print('bootstrap warn:', e, file=sys.stderr)
    return env

ENV = _bootstrap_env()
PY = '/Users/youww/.workbuddy/binaries/python/versions/3.13.12/bin/python3'
def _resolve_tdoc():
    """动态定位 tencentdocs.py：腾讯文档插件升级后版本目录名会变（如 1.0.0 → 5.5.6），不能硬编码"""
    import glob
    cands = glob.glob('/Users/youww/.workbuddy/plugins/cache/workbuddy-builtin/'
                      'tencent-docs-plugin/*/skills/tencent-docs/tencentdocs.py')
    def _ver(p):
        import re as _re
        m = _re.search(r'tencent-docs-plugin/([0-9][0-9.]*)', p)
        try:
            return tuple(int(x) for x in m.group(1).strip('.').split('.'))
        except Exception:
            return (0,)
    cands.sort(key=_ver)
    return cands[-1] if cands else ''

TD = os.environ.get('XLB_TDOC') or _resolve_tdoc()
FILE_ID = 'QRvnCQXoQNBt'
SHEET = 'BB08J2'
NOWRITE = '--nowrite' in sys.argv or os.environ.get('XLB_NOWRITE') == '1'
SNAP_DIR = os.environ.get('XLB_WEB_DIR', '/tmp/xlb_web')

def tdoc(tool, args):
    r = subprocess.run([PY, TD, 'tdoc_call', 'sheet-mcp', tool, json.dumps(args)],
                       capture_output=True, text=True, env=ENV)
    if r.returncode != 0:
        print('ERR', tool, r.stderr[-300:], file=sys.stderr); sys.exit(1)
    return json.loads(r.stdout)

# ---------- 1. 参数解析：时间 + 司机姓名 ----------
def parse_args(argv):
    argv = [a for a in argv if a != '--nowrite']
    drivers, date = [], None
    if '-n' in argv:  # 旧风格：日期 ... -n 名字...
        i = argv.index('-n')
        drivers = [x for x in argv[i+1:] if x]
        argv = argv[:i]
    # 合并后按 全角/半角逗号、分号、空白 切段
    parts = [p.strip() for p in re.split(r'[，,;；\s]+', ' '.join(argv)) if p.strip()]
    DATE_PATS = (r'(\d{4})-(\d{1,2})-(\d{1,2})', r'(\d{1,2})-(\d{1,2})', r'(\d{4})\.?(\d{1,2})\.?(\d{1,2})')
    if parts:
        d = parts[0]
        m = re.fullmatch(DATE_PATS[0], d)
        m2 = re.fullmatch(DATE_PATS[1], d)
        m3 = re.fullmatch(DATE_PATS[2], d)
        if m:
            date = '%s-%02d-%02d' % (m.group(1), int(m.group(2)), int(m.group(3)))
            drivers += [p for p in parts[1:] if p]
        elif m2:  # 9-17 → 当年年份
            today = datetime.date.today()
            date = '%04d-%02d-%02d' % (today.year, int(m2.group(1)), int(m2.group(2)))
            drivers += [p for p in parts[1:] if p]
        elif m3:
            date = '%s-%02d-%02d' % (m3.group(1), int(m3.group(2)), int(m3.group(3)))
            drivers += [p for p in parts[1:] if p]
        else:
            # 首段不是日期 → 全部按司机名处理（日期取今天）
            drivers += parts
    if not date:
        date = datetime.date.today().strftime('%Y-%m-%d')
    # 没给司机 → 用默认名单（可用 XLB_DRIVERS 覆盖）；给了就按给的来
    if not drivers:
        env_d = [x.strip() for x in re.split(r'[，,;；\s]+', os.environ.get('XLB_DRIVERS', '')) if x.strip()]
        drivers = env_d or list(DEFAULT_DRIVERS)
    return date, drivers

QDATE, DRIVERS = parse_args(sys.argv[1:])

# ---------- 2. 调接口：当日运单 ----------
AUTH_FILE = os.environ.get('XLB_AUTH', '/tmp/xlb_auth.json')
auth = json.load(open(AUTH_FILE))
URL = 'https://tms-web.tms.ali-prod.xlbsoft.com/tms/hxl.tms.transportorder.page'
# 统计默认含全部四家当涂劳务；可用 XLB_CARRIERS="863,749" 覆盖（短号即可）
DEF_CARRIERS = [6666600000863, 6666600000749, 6666600000114, 6666600000115]
_cs = os.environ.get('XLB_CARRIERS', '')
CARRIER_IDS = [6666600000000 + int(x) if len(x) < 13 else int(x)
               for x in _cs.split(',') if x.strip()] if _cs else DEF_CARRIERS
ARRANGE = os.environ.get('XLB_ARRANGE_STATE', 'DISPATCHED')

def fetch(pn, ps=200):
    body = json.dumps({"page_size": ps, "page_number": pn,
        "Data_Compact_RangeType_delivery_date": "day", "delivery_date": [QDATE, QDATE],
        "date_type": 3, "store_id": 6666600000001, "storehouse_id": 6666600000134,
        "client_ids": [], "carrier_ids": CARRIER_IDS, "plate_numbers": [],
        "store_ids": [6666600000001], "storehouse_ids": [6666600000134],
        "arrange_state": ARRANGE}).encode()
    req = urllib.request.Request(URL, data=body, method='POST')
    req.add_header('Content-Type', 'application/json')
    req.add_header('Cookie', auth['cookies']); req.add_header('Access-Token', auth['access_token'])
    if auth.get('authorization'):
        req.add_header('Authorization', auth['authorization'])
    req.add_header('Origin', 'https://gdp.xlbsoft.com'); req.add_header('Referer', 'https://gdp.xlbsoft.com/')
    req.add_header('User-Agent', 'Mozilla/5.0')
    return json.loads(urllib.request.urlopen(req, timeout=20).read().decode())

r = fetch(0)
if r.get('code') != 0:
    print('接口错误', r.get('code'), r.get('msg')); sys.exit(2)
rows = list(r['data']['content'])
for pn in range(1, r['data'].get('total_pages') or 1):
    rows += fetch(pn)['data']['content']
print(f'{QDATE} 运单共 {len(rows)} 单（arrange_state={ARRANGE}，劳务 {len(CARRIER_IDS)} 家）')

# ---------- 3. 司机筛选（精确匹配优先，退化为包含匹配） ----------
if DRIVERS:
    def hit(dn):
        return dn in DRIVERS or any((d in dn) or (dn in d) for d in DRIVERS if d)
    before = len(rows)
    rows = [o for o in rows if hit((o.get('driver_name') or '').strip())]
    print(f'司机筛选 {DRIVERS}: {before} → {len(rows)} 单')

# ---------- 4. 按（司机,门店）聚合：整箱/拆零/总计 ----------
def num(v):
    try: return float(v or 0)
    except (TypeError, ValueError): return 0.0

agg = {}   # (车牌,司机,门店) -> [总计,拆零,整箱,单数]
order = [] # 保持接口出现顺序
for o in rows:
    pl = (o.get('plate_number') or '').strip()
    dn = (o.get('driver_name') or '').strip()
    st = (o.get('client_name') or '').strip()
    if not dn and not st:
        continue
    k = (pl, dn, st)
    if k not in agg:
        agg[k] = [0.0, 0.0, 0.0, 0]
        order.append(k)
    a = agg[k]
    a[0] += num(o.get('delivery_total_quantity'))
    a[1] += num(o.get('delivery_pack_quantity'))
    a[2] += num(o.get('delivery_whole_quantity'))
    a[3] += 1

def g(v):
    s = '%g' % round(v, 2)
    return s

data_rows = []
# 2026-10-08 用户要求：按命令行给的司机姓名顺序写表（未点名的司机排最后，组内保持接口出现顺序）
def _drv_rank(dn):
    for i, d in enumerate(DRIVERS):
        if dn == d or (d in dn) or (dn in d):
            return i
    return len(DRIVERS)
_stable = list(enumerate(order))          # 先固化原始顺序，再原地 sort
order.sort(key=lambda ik: (_drv_rank(ik[1][1]), ik[0]))
for pl, dn, st in order:
    a = agg[(pl, dn, st)]
    # 列序（2026-09-25 用户指定）：A车牌号 B司机 C门店 D整箱 E拆零 F总计
    data_rows.append([pl, dn, st, g(a[2]), g(a[1]), g(a[0])])
    print(f'  {pl} {dn} / {st}: 整箱{data_rows[-1][3]} 拆零{data_rows[-1][4]} 总计{data_rows[-1][5]}（{a[3]}单）')
print(f'聚合后 {len(data_rows)} 行（车牌×司机×门店）')

# ---------- 5. 写表：先探测残行 → 清理 → 从第2行写 ----------
def _last_nonempty_row():
    """读表探测最后一个有内容的行号(1-based)；失败返回 0。
    用途：防止上一次写的行数多于本次时留下残行（历史固定清 200 行不够）"""
    try:
        d = tdoc('get_cell_data', {'file_id': FILE_ID, 'sheet_id': SHEET, 'start_row': 1,
                                   'end_row': 400, 'start_col': 0, 'end_col': 5,
                                   'return_csv': True})
        csv = ((d.get('result') or {}).get('structuredContent') or {}).get('csv_data', '') or ''
        last = 0
        for i, ln in enumerate(csv.splitlines()):
            if ln.replace(',', '').strip():
                last = i + 2      # start_row=1(0-based) → 表格第 2 行起
        return last
    except Exception as e:
        print(f'[warn] 探测表格残行失败，按默认范围清理: {e}', file=sys.stderr)
        return 0

if NOWRITE:
    print('[看板模式] 跳过写表')
else:
    last = _last_nonempty_row()
    # 表头（2026-09-25 新列序：车牌号/司机/门店/整箱/拆零/总计）
    hdr = ['车牌号', '司机', '门店', '整箱', '拆零', '总计']
    tdoc('set_range_value', {'file_id': FILE_ID, 'sheet_id': SHEET,
        'values': [{'row': 0, 'col': c, 'value_type': 'STRING', 'string_value': v} for c, v in enumerate(hdr)]})
    # 清理范围：覆盖历史残留(last)、保证 >=200 行；越界时自动缩减重试
    clear_rows = max(200, len(data_rows) + 20, last + 20)
    for attempt in range(5):
        writes = []
        for r0 in range(1, clear_rows):   # 0-based 行1..clear_rows-1 = 表格第2行起
            for c in range(6):
                writes.append({'row': r0, 'col': c, 'value_type': 'STRING', 'string_value': ''})
        for i, row in enumerate(data_rows):
            for c, v in enumerate(row):
                writes.append({'row': 1 + i, 'col': c, 'value_type': 'STRING', 'string_value': v})
        res = tdoc('set_range_value', {'file_id': FILE_ID, 'sheet_id': SHEET, 'values': writes})
        err = res.get('error') if isinstance(res, dict) else None
        if not err:
            break
        msg = str(err.get('msg') or err)
        if 'boundary' in msg and clear_rows > last + 1:
            clear_rows = last + 1          # 缩到实际最后一行，仍能覆盖全部残行
        elif clear_rows > 200:
            clear_rows = max(200, clear_rows // 2)
        else:
            sys.exit(f'写入失败: {msg[:200]}')
        print(f'[retry{attempt + 1}] 清理范围调整为 {clear_rows}：{msg[:100]}', file=sys.stderr)
    else:
        sys.exit('写入失败：重试次数用尽')
    print(f'已写入 {len(data_rows)} 行到 每日统计订单（清理 A2:F{clear_rows}，原残行至 {last} 行），DONE')

# ---------- 6. 快照（看板/后续扩展用） ----------
import time as _t
try:
    os.makedirs(SNAP_DIR, exist_ok=True)
    json.dump({'ts': _t.time(), 'date': QDATE, 'total': len(rows),
               'drivers_filter': DRIVERS, 'rows': data_rows},
              open(os.path.join(SNAP_DIR, 'data_task5.json'), 'w'), ensure_ascii=False)
except Exception as _e:
    print('snapshot warn:', _e)
