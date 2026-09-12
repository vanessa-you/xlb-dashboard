#!/usr/bin/env python3
"""云端批量拉数入口（GitHub Actions 用）：依次跑 task1/2/3 的 NOWRITE 模式。
凭据从 secret XLB_AUTH_B64（/tmp/xlb_auth.json 的 base64）注入。
快照 data_task{1,2,3}.json 写到仓库根目录，由 Pages 直接展示。
"""
import os, sys, json, base64, subprocess, time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ['XLB_NOWRITE'] = '1'
os.environ['XLB_WEB_DIR'] = HERE
os.environ.setdefault('XLB_AUTH', os.path.join(HERE, 'auth.json'))

# secret 注入（base64 规避换行/引号问题）
b64 = os.environ.get('XLB_AUTH_B64', '')
auth_path = os.environ['XLB_AUTH']
if b64:
    open(auth_path, 'wb').write(base64.b64decode(b64))
if not os.path.exists(auth_path):
    print('错误: 缺少 XLB_AUTH_B64 secret'); sys.exit(2)

fails = 0
for label, script in [('任务1调度', 'task1.py'), ('任务2待拣', 'task2.py'), ('任务3集货位', 'task3_direct.py')]:
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, script)],
                           capture_output=True, text=True, timeout=240, cwd=HERE)
        lines = [l for l in (r.stdout + r.stderr).strip().splitlines() if l.strip()]
        print(f'{label}: exit={r.returncode} | ' + (lines[-1] if lines else '(无输出)'))
        if r.returncode != 0:
            for l in lines[-5:]: print('   ', l)
            fails += 1
    except Exception as e:
        print(f'{label}: 异常 {e}'); fails += 1

print('--- 快照校验 ---')
for f in ('data_task1.json', 'data_task2.json', 'data_task3.json'):
    try:
        d = json.load(open(os.path.join(HERE, f)))
        print(f, 'ts=', time.strftime('%m-%d %H:%M:%S', time.localtime(d['ts'])),
              'total=', d.get('total'), 'groups=', len(d.get('groups') or d.get('stores') or {}))
    except Exception as e:
        print(f, '缺失:', e); fails += 1
sys.exit(1 if fails else 0)
