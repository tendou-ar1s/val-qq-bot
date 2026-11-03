import asyncio
import random
import re
import secrets
import time
from typing import Any, Dict, Optional

import aiohttp
from nonebot.log import logger

WEGAME_HOME = "https://www.wegame.com.cn/helper/valorant/"
QR_CONNECT = "https://open.weixin.qq.com/connect/qrconnect"
QR_IMAGE = "https://open.weixin.qq.com/connect/qrcode/{uuid}"
QR_POLL = "https://lp.open.weixin.qq.com/connect/l/qrconnect"
CALLBACK = "https://www.wegame.com.cn/login/callback.html"
LOGIN_BY_WECHAT = "https://www.wegame.com.cn/api/middle/clientapi/auth/login_by_wechat"
LOGIN_BY_QQ = "https://www.wegame.com.cn/api/middle/clientapi/auth/login_by_qq"
APP_ID = "wx911818d5d92affa8"
REDIRECT_URI = f"{CALLBACK}?t=wx&c=0&a=0"

# QQ 扫码通道（逆向自 weame-login.desktop.js：ptAppId=1600001063, ptDaId=733,
# clienttype=1000005, qq_info_type=6, 回调 /login/callback.html?t=qq）
XLOGIN_URL = "https://xui.ptlogin2.qq.com/cgi-bin/xlogin"
PTQR_SHOW_URL = "https://xui.ptlogin2.qq.com/ssl/ptqrshow"
PTQR_LOGIN_URL = "https://xui.ptlogin2.qq.com/ssl/ptqrlogin"
QQ_AID = "1600001063"
QQ_DAID = "733"
QQ_CALLBACK = f"{CALLBACK}?t=qq&c=0&a=0&ct=1000005"
QQ_CLIENTTYPE = "1000005"
QR_STYLE = "https://wegame.gtimg.com/g.55555-r.c4663/lib/login-sdk/qrcode.css"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)


def _cookie(session: aiohttp.ClientSession, name: str) -> str:
    for url in (WEGAME_HOME, CALLBACK, "https://www.wegame.com.cn"):
        item = session.cookie_jar.filter_cookies(url).get(name)
        if item and item.value:
            return item.value
    return ""


def _calc_ptqrtoken(qrsig: str) -> int:
    token = 0
    for ch in qrsig:
        token += (token << 5) + ord(ch)
    return token & 2147483647


# ---------------- QQ 扫码通道（WeGame 互联 appid=1600001063） ----------------


async def generate_wegame_qr_qq() -> Optional[Dict[str, Any]]:
    """生成 WeGame QQ 扫码二维码（QQ互联 xlogin 链路，appid=1600001063/daid=733）。"""
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30),
        cookie_jar=aiohttp.CookieJar(unsafe=True),
        headers={"user-agent": UA},
    )
    try:
        async with session.get(
            XLOGIN_URL,
            params={
                "appid": QQ_AID, "daid": QQ_DAID, "style": "20",
                "pt_no_auth": "1", "target": "self", "hide_close_icon": "1",
                "hide_border": "1", "s_url": QQ_CALLBACK, "lang": "zh-cn",
            },
        ) as response:
            await response.read()
            response.raise_for_status()

        async with session.get(
            PTQR_SHOW_URL,
            params={
                "s": "8", "e": "0", "appid": QQ_AID, "type": "0",
                "t": str(random.random()), "daid": QQ_DAID,
                "pt_3rd_aid": QQ_AID,
            },
            headers={"referer": XLOGIN_URL, "accept": "image/*,*/*"},
        ) as response:
            response.raise_for_status()
            qr_bytes = await response.read()
        if not qr_bytes:
            raise RuntimeError("二维码内容为空")

        qrsig = ""
        for url in (XLOGIN_URL, "https://ssl.ptlogin2.qq.com"):
            item = session.cookie_jar.filter_cookies(url).get("qrsig")
            if item and item.value:
                qrsig = item.value
                break
        if not qrsig:
            raise RuntimeError("未获取到 qrsig")
        return {
            "session": session,
            "qr_bytes": qr_bytes,
            "ptqrtoken": _calc_ptqrtoken(qrsig),
        }
    except Exception as e:
        logger.warning(f"wegame qq login: 生成二维码失败: {type(e).__name__}: {e}")
        await session.close()
        return None


async def wait_wegame_qq_login(
    ctx: Dict[str, Any], timeout: int = 60,
) -> Optional[Dict[str, Any]]:
    """轮询 QQ 扫码结果；成功后走 check_sig 拿 uin/p_skey 并换 WeGame 票据。"""
    session: aiohttp.ClientSession = ctx["session"]
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        params = {
            "u1": QQ_CALLBACK, "from_ui": "1", "type": "1", "ptlang": "2052",
            "ptqrtoken": str(ctx["ptqrtoken"]), "daid": QQ_DAID, "aid": QQ_AID,
            "pt_3rd_aid": QQ_AID, "device": "2", "ptopt": "1",
            "pt_uistyle": "40", "jsver": "28d22679",
            "r": str(random.random()),
        }
        try:
            async with session.get(
                PTQR_LOGIN_URL, params=params,
                headers={"accept": "*/*", "referer": XLOGIN_URL},
            ) as response:
                text = await response.text(errors="ignore")
            match = re.search(
                r"ptuiCB\('([^']*)','([^']*)','([^']*)','([^']*)','([^']*)'", text)
            if not match:
                await asyncio.sleep(2)
                continue
            code, _, redirect_url, _, message = match.groups()
            if code == "0" and redirect_url:
                return await _exchange_ticket_qq(session, redirect_url)
            if code in ("65", "10005"):  # 二维码失效
                logger.info(f"wegame qq login: 二维码失效: {message}")
                return None
            # 66=待扫 67=已扫待确认
            await asyncio.sleep(2)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(2)
        except Exception as e:
            logger.warning(f"wegame qq login: 轮询异常: {type(e).__name__}: {e}")
            await asyncio.sleep(2)
    return None


async def _exchange_ticket_qq(
    session: aiohttp.ClientSession, check_sig_url: str,
) -> Optional[Dict[str, Any]]:
    """访问 check_sig URL 换 p_uin/p_skey，再调 login_by_qq 换 tgp 票据。"""
    try:
        async with session.get(
            check_sig_url, headers={"referer": XLOGIN_URL}, allow_redirects=True,
        ) as response:
            await response.read()
            response.raise_for_status()
    except Exception as e:
        logger.warning(f"wegame qq login: check_sig 失败: {type(e).__name__}: {e}")
        return None

    uin = ""
    sig = ""
    for url in ("https://www.wegame.com.cn", CALLBACK, "https://ptlogin2.qq.com"):
        cookies = session.cookie_jar.filter_cookies(url)
        for name, pick in (("p_uin", "uin"), ("uin", "uin"),
                           ("p_skey", "sig"), ("pskey", "sig"),
                           ("skey", "sig")):
            item = cookies.get(name)
            if item and item.value:
                if pick == "uin" and not uin:
                    uin = item.value
                elif pick == "sig" and not sig:
                    sig = item.value
        if uin and sig:
            break
    uin = re.sub(r"^o0*", "", uin)
    if not uin or not sig:
        logger.warning(
            f"wegame qq login: check_sig 后缺少凭据 uin={bool(uin)} sig={bool(sig)}")
        return None

    payload = {
        "clienttype": QQ_CLIENTTYPE,
        "mappid": "10001",
        "mcode": "",
        "config_params": {"lang_type": 0},
        "login_info": {"qq_info_type": 6, "uin": uin, "sig": sig},
    }
    async with session.post(
        LOGIN_BY_QQ,
        json=payload,
        headers={
            "accept": "*/*",
            "content-type": "application/json",
            "origin": "https://www.wegame.com.cn",
            "referer": f"{CALLBACK}?t=qq",
        },
    ) as response:
        body = await response.json(content_type=None)
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    if body.get("code") not in (0, "0"):
        logger.warning(f"wegame qq login: login_by_qq 失败: {str(body)[:120]}")
        return None
    if (
        data.get("error_code") not in (None, 0, "0")
        or data.get("result") not in (None, 0, "0")
    ):
        logger.warning(f"wegame qq login: 业务错误: {str(data)[:120]}")
        return None

    fallbacks = {
        "tgp_id": data.get("user_id"),
        "tgp_ticket": data.get("wt"),
        "tgp_env": data.get("env"),
        "tgp_user_type": data.get("third_type"),
        "tgp_third_openid": data.get("third_openid"),
    }
    saved: Dict[str, Any] = {"login_type": "wegame_qq", "uin": uin,
                            "qq_uin": uin, "qq_sig": sig}
    for name in (
        "tgp_id", "tgp_ticket", "tgp_env", "tgp_user_type",
        "tgp_third_openid", "_qimei_uuid42", "_qimei_fingerprint",
        "_qimei_q36", "_qimei_h38",
    ):
        value = _cookie(session, name) or fallbacks.get(name) or ""
        if value != "":
            saved[name] = str(value)
    for name in ("ct", "refresh_wt_span", "refresh_ct_span", "expires", "server_time"):
        value = data.get(name)
        if value not in (None, ""):
            saved[name] = value
    now = int(time.time())
    span = int(saved.get("refresh_wt_span") or 1800)
    saved.update({
        "auth_refreshed_at": now,
        "next_refresh_at": now + max(300, int(span * 0.65)),
        "needs_relogin": False,
        "last_auth_error": "",
        "last_auth_error_at": 0,
        "last_verified_at": now,
    })
    return saved if saved.get("tgp_ticket") else None


async def generate_wegame_qr() -> Optional[Dict[str, Any]]:
    """按 WeGame 网页链路生成微信二维码，并保留同一登录会话。"""
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30),
        cookie_jar=aiohttp.CookieJar(unsafe=True),
        headers={"user-agent": UA},
    )
    state = secrets.token_hex(4)
    params = {
        "appid": APP_ID,
        "scope": "snsapi_login",
        "redirect_uri": REDIRECT_URI,
        "login_type": "jssdk",
        "self_redirect": "true",
        "state": state,
        "style": "black",
        "href": QR_STYLE,
    }
    try:
        async with session.get(WEGAME_HOME) as response:
            await response.read()
            response.raise_for_status()
        async with session.get(
            QR_CONNECT,
            params=params,
            headers={
                "accept": "text/html,application/xhtml+xml",
                "referer": "https://www.wegame.com.cn/",
            },
        ) as response:
            page = await response.text(errors="ignore")
            response.raise_for_status()
            referer = str(response.url)
        match = re.search(r"/connect/qrcode/([A-Za-z0-9_-]+)", page)
        if not match:
            match = re.search(r"uuid\s*[:=]\s*['\"]([^'\"]+)", page)
        if not match:
            raise RuntimeError("微信二维码页面缺少 uuid")
        uuid = match.group(1)
        async with session.get(
            QR_IMAGE.format(uuid=uuid),
            headers={"accept": "image/*,*/*;q=0.8", "referer": referer},
        ) as response:
            qr_bytes = await response.read()
            response.raise_for_status()
        if not qr_bytes:
            raise RuntimeError("微信二维码内容为空")
        return {
            "session": session,
            "qr_bytes": qr_bytes,
            "uuid": uuid,
            "state": state,
        }
    except Exception:
        await session.close()
        return None


async def wait_wegame_login(
    ctx: Dict[str, Any], timeout: int = 60
) -> Optional[Dict[str, Any]]:
    """等待微信确认，并按抓包中的 WeGame 接口换取登录 Cookie。"""
    session: aiohttp.ClientSession = ctx["session"]
    started = time.monotonic()
    last: Optional[int] = None
    while time.monotonic() - started < timeout:
        params: Dict[str, Any] = {"uuid": ctx["uuid"]}
        if last is not None:
            params["last"] = last
        try:
            async with session.get(
                QR_POLL,
                params=params,
                headers={"accept": "*/*", "referer": "https://open.weixin.qq.com/"},
            ) as response:
                text = await response.text(errors="ignore")
            code_match = re.search(r"wx_errcode\s*=\s*(\d+)", text)
            wx_code_match = re.search(r"wx_code\s*=\s*['\"]([^'\"]*)", text)
            if not code_match:
                await asyncio.sleep(1)
                continue
            last = int(code_match.group(1))
            if last in (0, 405) and wx_code_match and wx_code_match.group(1):
                return await _exchange_ticket(
                    session, wx_code_match.group(1), str(ctx["state"])
                )
            if last in (402, 403):
                return None
            if last not in (404, 408):
                await asyncio.sleep(1)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(1)
    return None


async def _exchange_ticket(
    session: aiohttp.ClientSession, wx_code: str, state: str
) -> Optional[Dict[str, Any]]:
    callback_params = {
        "t": "wx", "c": "0", "a": "0", "code": wx_code, "state": state,
    }
    async with session.get(
        CALLBACK,
        params=callback_params,
        headers={"referer": "https://open.weixin.qq.com/"},
    ) as response:
        await response.read()
        response.raise_for_status()
        callback_url = str(response.url)

    payload = {
        "clienttype": "1000005",
        "mappid": "10001",
        "mcode": "",
        "config_params": {"lang_type": 0},
        "login_info": {"wx_info_type": 1, "appid": APP_ID, "code": wx_code},
    }
    async with session.post(
        LOGIN_BY_WECHAT,
        json=payload,
        headers={
            "accept": "*/*",
            "content-type": "application/json",
            "origin": "https://www.wegame.com.cn",
            "referer": callback_url,
        },
    ) as response:
        body = await response.json(content_type=None)
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    if body.get("code") not in (0, "0"):
        return None
    if (
        data.get("error_code") not in (None, 0, "0")
        or data.get("result") not in (None, 0, "0")
    ):
        return None

    fallbacks = {
        "tgp_id": data.get("user_id"),
        "tgp_ticket": data.get("wt"),
        "tgp_env": data.get("env"),
        "tgp_user_type": data.get("third_type"),
        "tgp_third_openid": data.get("third_openid"),
    }
    saved: Dict[str, Any] = {}
    for name in (
        "tgp_id", "tgp_ticket", "tgp_env", "tgp_user_type",
        "tgp_third_openid", "_qimei_uuid42", "_qimei_fingerprint",
        "_qimei_q36", "_qimei_h38",
    ):
        value = _cookie(session, name) or fallbacks.get(name) or ""
        if value != "":
            saved[name] = str(value)
    for name in (
        "ct", "refresh_wt_span", "refresh_ct_span", "expires", "server_time",
    ):
        value = data.get(name)
        if value not in (None, ""):
            saved[name] = value
    now = int(time.time())
    span = int(saved.get("refresh_wt_span") or 1800)
    saved.update({
        "auth_refreshed_at": now,
        "next_refresh_at": now + max(300, int(span * 0.65)),
        "needs_relogin": False,
        "last_auth_error": "",
        "last_auth_error_at": 0,
        "last_verified_at": now,
    })
    return saved if saved.get("tgp_ticket") else None


async def relogin_by_saved_sig(account: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """用保存的 QQ uin+p_skey 重新走 login_by_qq 换全新票据（免扫码自愈）。

    p_skey 由 QQ 互联签发，有效期通常数周；期间 WeGame 会话再被作废也能自动恢复。
    """
    uin = str(account.get("qq_uin") or "")
    sig = str(account.get("qq_sig") or "")
    if not uin or not sig:
        logger.warning("relogin_by_sig: 账号缺少 qq_uin/qq_sig，无法自动重签")
        return None
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30),
        cookie_jar=aiohttp.CookieJar(unsafe=True),
        headers={"user-agent": UA},
    )
    try:
        payload = {
            "clienttype": QQ_CLIENTTYPE,
            "mappid": "10001",
            "mcode": "",
            "config_params": {"lang_type": 0},
            "login_info": {"qq_info_type": 6, "uin": uin, "sig": sig},
        }
        async with session.post(
            LOGIN_BY_QQ,
            json=payload,
            headers={
                "accept": "*/*",
                "content-type": "application/json",
                "origin": "https://www.wegame.com.cn",
                "referer": f"{CALLBACK}?t=qq",
            },
        ) as response:
            body = await response.json(content_type=None)
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        if body.get("code") not in (0, "0"):
            logger.warning(f"relogin_by_sig: login_by_qq 失败: {str(body)[:120]}")
            return None
        if data.get("error_code") not in (None, 0, "0") or data.get("result") not in (None, 0, "0"):
            logger.warning(f"relogin_by_sig: 业务错误: {str(data)[:120]}")
            return None
        fallbacks = {
            "tgp_id": data.get("user_id"),
            "tgp_ticket": data.get("wt"),
            "tgp_env": data.get("env"),
            "tgp_user_type": data.get("third_type"),
            "tgp_third_openid": data.get("third_openid"),
        }
        saved: Dict[str, Any] = {"login_type": "wegame_qq", "uin": uin,
                                 "qq_uin": uin, "qq_sig": sig}
        for name in ("tgp_id", "tgp_ticket", "tgp_env", "tgp_user_type",
                     "tgp_third_openid", "_qimei_uuid42", "_qimei_fingerprint",
                     "_qimei_q36", "_qimei_h38"):
            value = _cookie(session, name) or fallbacks.get(name) or ""
            if value != "":
                saved[name] = str(value)
        for name in ("ct", "refresh_wt_span", "refresh_ct_span", "expires", "server_time"):
            value = data.get(name)
            if value not in (None, ""):
                saved[name] = value
        now = int(time.time())
        span = int(saved.get("refresh_wt_span") or 1800)
        saved.update({
            "auth_refreshed_at": now,
            "next_refresh_at": now + max(300, int(span * 0.65)),
            "needs_relogin": False,
            "last_auth_error": "",
            "last_auth_error_at": 0,
            "last_verified_at": now,
        })
        if saved.get("tgp_ticket"):
            logger.info(f"relogin_by_sig: 自动重签成功 tgp_id={saved.get('tgp_id')}")
            return saved
        return None
    except Exception as e:
        logger.warning(f"relogin_by_sig: 异常 {type(e).__name__}: {e}")
        return None
    finally:
        await session.close()
