#!/usr/bin/env python3
# 任务6：调度管理已调度(transportorder.page, DISPATCHED) -> 追加到推单顺序表
# 规则：B列门店已存在不记录；不存在往下追加；司机组之间空一行；司机名只写组首行A列
import json, os, subprocess, sys, urllib.request, datetime
from collections import OrderedDict

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
TD = '/Users/youww/.workbuddy/plugins/cache/workbuddy-builtin/tencent-docs-plugin/1.0.0/skills/tencent-docs/tencentdocs.py'
FILE_ID = 'DUXpmYVVQcmRneXFL'
SHEET = 'BB08J2'
# 看板模式（XLB_NOWRITE=1 或 --nowrite）：只取数展示，不读写腾讯表格（2026-09-12 21:20 用户确认）
NOWRITE = '--nowrite' in sys.argv or os.environ.get('XLB_NOWRITE') == '1'

def tdoc(tool, args):
    r = subprocess.run([PY, TD, 'tdoc_call', 'sheet-mcp', tool, json.dumps(args)],
                       capture_output=True, text=True, env=ENV)
    if r.returncode != 0:
        print('ERR', tool, r.stderr[-300:], file=sys.stderr); sys.exit(1)
    return json.loads(r.stdout)

# ---------- 1. 调接口：当日已调度 ----------
# 【2026-09-12 21:32 看板新增】支持按供应商(劳务)筛选：XLB_CARRIER=carrier_id 或 all（命令行第2参数亦可）
CARRIERS = {
    '6666600000863': '当涂云顺',
    '6666600000749': '当涂日日顺',
    '6666600000114': '当涂逍蜂',
    '6666600000115': '当涂原赫',
}
CARRIER_ARG = (sys.argv[2] if len(sys.argv) > 2 else '') or os.environ.get('XLB_CARRIER', 'all')
# 【2026-09-14 03:05 用户确认】只查当涂云顺(863)；如需其他供应商用 XLB_CARRIER=carrier_id 指定
ALL_CARRIER_IDS = [6666600000863]
if CARRIER_ARG in ('all', ''):
    CARRIER_IDS = ALL_CARRIER_IDS
    CARRIER_LABEL = '当涂云顺'
else:
    cid = int(CARRIER_ARG)
    CARRIER_IDS = [cid]
    CARRIER_LABEL = CARRIERS.get(str(cid), str(cid))

AUTH_FILE = os.environ.get('XLB_AUTH', '/tmp/xlb_auth.json')
SNAP_DIR = os.environ.get('XLB_WEB_DIR', '/tmp/xlb_web')
auth = json.load(open(AUTH_FILE))
QDATE = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().strftime('%Y-%m-%d')
URL = 'https://tms-web.tms.ali-prod.xlbsoft.com/tms/hxl.tms.transportorder.page'
def fetch(pn, ps=200):
    body = json.dumps({"page_size": ps, "page_number": pn,
        "Data_Compact_RangeType_delivery_date": "day", "delivery_date": [QDATE, QDATE],
        "date_type": 3, "store_id": 6666600000001, "storehouse_id": 6666600000134,
        "client_ids": [], "carrier_ids": CARRIER_IDS, "plate_numbers": [],
        "store_ids": [6666600000001], "storehouse_ids": [6666600000134],
        "arrange_state": "DISPATCHED"}).encode()
    req = urllib.request.Request(URL, data=body, method='POST')
    req.add_header('Content-Type', 'application/json')
    req.add_header('Cookie', auth['cookies']); req.add_header('Access-Token', auth['access_token'])
    if auth.get('authorization'):
        req.add_header('Authorization', auth['authorization'])  # 2026-09-12 起 TMS 需带 Bearer JWT
    req.add_header('Origin', 'https://gdp.xlbsoft.com'); req.add_header('Referer', 'https://gdp.xlbsoft.com/')
    req.add_header('User-Agent', 'Mozilla/5.0')
    return json.loads(urllib.request.urlopen(req, timeout=20).read().decode())

r = fetch(0)
if r.get('code') != 0:
    print('接口错误', r.get('code'), r.get('msg')); sys.exit(2)
rows = list(r['data']['content'])
for pn in range(1, r['data'].get('total_pages') or 1):
    rows += fetch(pn)['data']['content']
print(f'{QDATE} 已调度 {len(rows)} 单')

# ---------- 1.5 按 dispatch_time 升序排序（时间早的在前；缺失的排最后，保持原相对顺序） ----------
rows.sort(key=lambda o: (o.get('dispatch_time') or '9999-12-31 23:59:59'))

# ---------- 2. 读表：existing + last_data_row（重读最新，用户可能中途手写） ----------
# v2 (2026-09-09 05:04)：end_row 原=89 会截断读不到更下方数据，导致已写入门店被误判新增重复追加；
# 扩大到 299 行（表数据远小于此）
# v3 (2026-09-10 08:55)：A列前新增「劳务」列（carrier_name），布局 A劳务 B司机 C门店 D待拣 E通道 F托盘
existing, last_data_row = set(), 0
if not NOWRITE:
    rr = tdoc('get_cell_data', {'file_id': FILE_ID, 'sheet_id': SHEET,
            'start_row': 0, 'end_row': 299, 'start_col': 0, 'end_col': 2, 'return_csv': True})
    csvd = rr.get('result', {}).get('structuredContent', {}).get('csv_data', '') or ''
    for i, line in enumerate(csvd.rstrip('\n').split('\n')):
        cells = line.split(',')
        a = cells[0].strip() if len(cells) > 0 else ''
        b = cells[1].strip() if len(cells) > 1 else ''
        c = cells[2].strip() if len(cells) > 2 else ''
        if a or b or c: last_data_row = i
        if c and i >= 1: existing.add(c)  # C列=门店（v3）
    print(f'表中已有门店 {len(existing)} 家, 最后数据行(0-based): {last_data_row}')
else:
    print('[看板模式] 跳过读表')

# ---------- 3. 按接口顺序分（劳务,司机）组：相邻同司机连续合并为一条（不同门店也算同一条） ----------
# 【2026-09-12 20:28 用户确认】调度管理列表中相邻数据同司机=一条记录；司机中间隔开再出现=新的一条(多趟)
seq = []  # [(carrier, driver, [stores])] 保序
cur_key, cur_sts = None, []
for o in rows:
    dn = (o.get('driver_name') or '').strip()
    lb = (o.get('carrier_name') or '').strip()  # 劳务=承运商名
    st = (o.get('client_name') or '').strip()
    if (lb, dn) != cur_key:
        if cur_key is not None and cur_sts:
            seq.append((cur_key[0], cur_key[1], list(dict.fromkeys(cur_sts))))  # 【2026-09-13 05:16】组内门店去重（同店多次加单只留一行）
        cur_key, cur_sts = (lb, dn), []
    if st:
        cur_sts.append(st)
if cur_key is not None and cur_sts:
    seq.append((cur_key[0], cur_key[1], list(dict.fromkeys(cur_sts))))

# ---------- 3.5 同司机多条记录含相同门店 → 合并为一条 ----------
# 【2026-09-12 21:41 用户确认】同一司机出现两单且包含同一家门店（门店后续加单），合并到一起
_merged = []
for lb, dn, sts in seq:
    target = None
    for m in _merged:
        if m[0] == lb and m[1] == dn and set(m[2]) & set(sts):
            target = m
            break
    if target:
        added = [s for s in sts if s not in target[2]]
        target[2].extend(added)
        print(f'  合并同司机记录 {dn}: +{added}（含共同门店）')
    else:
        _merged.append([lb, dn, list(sts)])
seq = [(m[0], m[1], m[2]) for m in _merged]

new_seq = []
for lb, dn, sts in seq:
    sts = list(dict.fromkeys(sts))  # 批次内去重：同一司机对同一门店多张运单只记一行
    new_sts = [s for s in sts if s not in existing]
    skip = [s for s in sts if s in existing]
    if skip: print(f'  跳过已存在 {dn}: {skip}')
    if new_sts: new_seq.append((lb, dn, new_sts))

# ---------- 4. 追加：新数据上方空一行 + 组间空一行，劳务/司机写组首行A/B列，门店写C列 ----------
writes = []
r0 = last_data_row + 1
if last_data_row >= 1:  # 表中已有数据（除表头），新追加内容上方先空一行
    r0 += 1
first = True
for lb, dn, sts in new_seq:
    if not first:
        r0 += 1
    for j, st in enumerate(sts):
        if j == 0:
            writes.append({'row': r0, 'col': 0, 'value_type': 'STRING', 'string_value': lb})  # A劳务
            writes.append({'row': r0, 'col': 1, 'value_type': 'STRING', 'string_value': dn})  # B司机
        writes.append({'row': r0, 'col': 2, 'value_type': 'STRING', 'string_value': st})      # C门店
        r0 += 1
    first = False

if writes:
    if NOWRITE:
        print(f'[看板模式] 跳过写表，共 {len(new_seq)} 组待展示')
    else:
        tdoc('set_range_value', {'file_id': FILE_ID, 'sheet_id': SHEET, 'values': writes})
        for lb, dn, sts in new_seq:
            print(f'  新增 {lb}/{dn}: {sts}')
        print(f'追加 {len(writes)} 格, DONE')
else:
    print('无新增门店')

# ---------- 5. 快照输出（本地看板用，不影响写表） ----------
import time as _t
try:
    os.makedirs(SNAP_DIR, exist_ok=True)
    json.dump({'ts': _t.time(), 'date': QDATE, 'total': len(rows),
               'carrier': CARRIER_LABEL,
               'groups': [{'carrier': lb, 'driver': dn, 'stores': sts}
                          for lb, dn, sts in seq],
               'new_count': sum(len(v) for *_, v in new_seq)},
              open(os.path.join(SNAP_DIR, 'data_task1.json'), 'w'), ensure_ascii=False)
except Exception as _e:
    print('snapshot warn:', _e)
