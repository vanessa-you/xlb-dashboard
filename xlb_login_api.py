#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新零帮 纯 API 密码登录 → 重建 /tmp/xlb_auth.json（32位AT + JWT + RT + cookie）

背景（2026-10-11 逆向 gdp umi.js 实测确认）：
  业务域(TMS/WMS)唯一有效凭据是 32 位 Access-Token，寿命约 1 天且无法用 refresh_token 续期
  （refresh_token 只换 JWT，jti/AT 与 JWT 生命周期解绑）。
  但可以通过「机构号 + 账号 + 密码」纯 API 重新登录，直接拿到新的 32 位 AT。

登录三步（全部实测可用）：
  A. POST {GDP}/erp-mdm/hxl.erp.account.pwd.user.find
       body {account, company_id, pwd}                 → data.login_token(JWT,10分钟)
  B. POST {GDP}/erp-mdm/hxl.erp.user.account.login
       body {account, company_id, source:"WEB"}  header Access-token: <login_token>
                                                       → data.access_token(32位AT)
                                                         data.oauth2.{access_token(JWT),refresh_token,token_type}
  C. 把新凭据写入 /tmp/xlb_auth.json（保留 url/body 等原字段）

登录参数来源（按优先级）：
  1) 环境变量 XLB_ACCOUNT / XLB_COMPANY_ID / XLB_PASSWORD
  2) 凭据文件（默认 ~/.xlb_login.json，可用 XLB_LOGIN_JSON 覆盖）
     {"account":"15555599699","company_id":66666,"password":"***"}
  注意：密码文件放在仓库外，禁止提交到 GitHub。

退出码：0 成功 / 2 缺少登录参数 / 3 登录失败 / 4 新 AT 校验失败（网络等）
"""
import base64
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

GDP = os.environ.get('XLB_GDP', 'https://gdp.xlbsoft.com')
AUTH = os.environ.get('XLB_AUTH', '/tmp/xlb_auth.json')
LOGIN_JSON = os.environ.get('XLB_LOGIN_JSON', os.path.expanduser('~/.xlb_login.json'))
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36')
WMS_PROBE = ('https://wms-web.wms.ali-prod.xlbsoft.com/wms/hxl.wms.pickorder.page')
PROBE_BODY = {
    'operator_store_id': 6666600000001, 'company_id': 66666, 'client_ids': [],
    'storehouse_id': 6666600000134, 'store_id': 6666600000001, 'region_ids': [],
    'pick_states': ['INIT'], 'page': 1, 'size': 1,
}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 禁代理


def _log(msg):
    print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)


def _post(path_or_url, body, extra_headers=None, timeout=25):
    url = path_or_url if path_or_url.startswith('http') else GDP + path_or_url
    h = {'Content-Type': 'application/json', 'Origin': GDP, 'Referer': GDP + '/',
         'User-Agent': UA, 'Accept': 'application/json, text/plain, */*'}
    h.update(extra_headers or {})
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method='POST')
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {'code': e.code, 'msg': f'HTTP {e.code}'}
    except Exception as e:
        return {'code': -1, 'msg': str(e)}


def _jwt_payload(t):
    p = t.split('.')[1]
    p += '=' * (-len(p) % 4)
    return json.loads(base64.urlsafe_b64decode(p))


def load_login_params():
    acc = os.environ.get('XLB_ACCOUNT')
    comp = os.environ.get('XLB_COMPANY_ID')
    pwd = os.environ.get('XLB_PASSWORD')
    if not (acc and comp and pwd) and os.path.exists(LOGIN_JSON):
        try:
            d = json.load(open(LOGIN_JSON))
            acc = acc or str(d.get('account') or '')
            comp = comp or str(d.get('company_id') or '')
            pwd = pwd or str(d.get('password') or '')
        except Exception as e:
            _log(f'读取登录参数文件失败: {e}')
    if not (acc and comp and pwd):
        return None
    return acc.strip(), int(comp), pwd


def login(acc, company_id, pwd):
    """执行三步登录，返回 dict(access_token, jwt, refresh_token, token_type, user)"""
    # A. 密码校验 + 取 login_token
    ja = _post('/erp-mdm/hxl.erp.account.pwd.user.find',
               {'account': acc, 'company_id': company_id, 'pwd': pwd})
    if ja.get('code') != 0:
        raise RuntimeError(f'密码校验失败: code={ja.get("code")} msg={ja.get("msg")}')
    d = ja.get('data') or {}
    lt = d.get('login_token')
    if not lt:
        raise RuntimeError('未返回 login_token（账号未开通密码登录？）')
    ulist = d.get('account_user_list') or []
    uid = (ulist[0] or {}).get('id') if ulist else None
    _log(f'A. 密码校验通过 login_token={lt[:18]}… 用户={((ulist[0] or {}).get("name") if ulist else "?")}')

    # B. 用 login_token 换正式凭据（关键：header 名是 Access-token）
    jb = _post('/erp-mdm/hxl.erp.user.account.login',
               {'account': acc, 'company_id': company_id, 'source': 'WEB',
                'user_id': uid, 'login_source': 'WEB'},
               {'Access-token': lt})
    if jb.get('code') != 0:
        raise RuntimeError(f'换取凭据失败: code={jb.get("code")} msg={jb.get("msg")}')
    ud = jb.get('data') or {}
    at = ud.get('access_token')
    if not at:
        raise RuntimeError('未返回 32 位 access_token')
    oa = ud.get('oauth2') or {}
    jwt = oa.get('access_token') or ''
    rt = oa.get('refresh_token') or ud.get('refresh_token') or ''
    tt = oa.get('token_type') or 'Bearer'
    _log(f'B. 换取凭据成功 AT={at} JWT={len(jwt)}字符 RT={(rt or "")[:16]}…')
    return {'access_token': at, 'jwt': jwt, 'refresh_token': rt, 'token_type': tt,
            'user': ud, 'user_id': uid, 'company_id': company_id, 'account': acc,
            'expires_in': oa.get('expires_in')}


def verify_at(at):
    j = _post(WMS_PROBE, PROBE_BODY, {'Access-Token': at})
    code = j.get('code')
    if code == 0:
        _log(f'C. 新 AT 业务域校验通过（WMS code=0）')
        return True
    _log(f'C. 新 AT 业务域校验失败: code={code} msg={j.get("msg")}')
    return False


def _cookie_map(cookies):
    out = {}
    for part in (cookies or '').split(';'):
        if '=' in part:
            k, v = part.strip().split('=', 1)
            out[k] = v
    return out


def write_auth(res):
    old = {}
    if os.path.exists(AUTH):
        try:
            old = json.load(open(AUTH))
        except Exception:
            old = {}
        try:
            bak = AUTH + '.bak_login'
            with open(bak, 'w') as f:
                json.dump(old, f, ensure_ascii=False)
        except Exception:
            pass

    ck = _cookie_map(old.get('cookies', ''))
    q = urllib.parse.quote
    jwt = res['jwt']
    if jwt:
        ck['xlb_token'] = q(f'{res["token_type"]} {jwt}')
    if res['refresh_token']:
        ck['xlb_refresh_token'] = res['refresh_token']
    ck['xlb_profile_access_token'] = res['access_token']
    ck['production_qiankun_token'] = q(json.dumps(
        {'account': res['account'], 'companyId': res['company_id'],
         'accessToken': res['access_token']}, ensure_ascii=False, separators=(',', ':')))
    if res.get('user_id'):
        ck['User-Id'] = str(res['user_id'])
    ck['account'] = str(res['account'])
    ck['company_id'] = str(res['company_id'])
    if 'xlb_client_secret' not in ck:
        ck['xlb_client_secret'] = '31869922-f120-2d61-e2fb-b5f5f668928f'
    cookies = '; '.join(f'{k}={v}' for k, v in ck.items())

    new = dict(old)
    # 【2026-10-16】auth.json 不存在（如 /tmp 被清后）时补默认 url/body，
    # 否则 task2/task3 会因 KeyError: 'body' 失败（业务域 WMS 网关与查询体是固定值）
    if not new.get('url'):
        new['url'] = 'https://wms-web.wms.ali-prod.xlbsoft.com/wms/hxl.wms.distribute.center.find'
    if not new.get('body'):
        new['body'] = {'operator_store_id': 6666600000001, 'company_id': 66666, 'client_ids': [],
                       'storehouse_id': 6666600000134, 'store_id': 6666600000001, 'region_ids': []}
    new['access_token'] = res['access_token']
    if jwt:
        new['authorization'] = f'{res["token_type"]} Bearer'.replace('Bearer Bearer', 'Bearer') \
            if False else f'{res["token_type"]} {jwt}'
    if res['refresh_token']:
        new['refresh_token'] = res['refresh_token']
    new['cookies'] = cookies
    new['saved_at'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    new['source'] = 'api_password_login'
    new['login_user'] = (res.get('user') or {}).get('name')
    json.dump(new, open(AUTH, 'w'), ensure_ascii=False, indent=1)
    _log(f'D. 已写入 {AUTH}（AT/JWT/RT/cookie 全部更新，原文件备份为 {AUTH}.bak_login）')


def main():
    prm = load_login_params()
    if not prm:
        _log(f'缺少登录参数：请设置 XLB_ACCOUNT/XLB_COMPANY_ID/XLB_PASSWORD，'
             f'或创建 {LOGIN_JSON}（{{"account":"","company_id":0,"password":""}}）')
        return 2
    acc, company_id, pwd = prm
    try:
        res = login(acc, company_id, pwd)
    except Exception as e:
        _log(f'登录失败: {e}')
        return 3
    try:
        if not verify_at(res['access_token']):
            # 校验失败也先落盘（可能是接口偶发/网络问题），由调用方按需判断
            write_auth(res)
            return 4
    except Exception as e:
        _log(f'校验异常: {e}')
    write_auth(res)
    jwt_info = ''
    if res['jwt']:
        try:
            pl = _jwt_payload(res['jwt'])
            jwt_info = f'（JWT 至 {datetime.datetime.fromtimestamp(pl["exp"]).strftime("%H:%M:%S")}）'
        except Exception:
            pass
    _log(f'完成：新 AT={res["access_token"]}{jwt_info}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
