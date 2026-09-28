"""管理主注册浏览器生命周期并实现注册页面自动化操作。"""
import gc
import json
import random
import re
import secrets
import struct
import time

from DrissionPage import Chromium
from DrissionPage.errors import ContextLostError, JavaScriptError, PageDisconnectedError
from curl_cffi import requests
from proxy_pool import ProxyTransportError, safe_proxy_error_text

browser = None
page = None
browser_proxy_bridge = None
browser_started_with_proxy = False
cf_clearance = ""
SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

TURNSTILE_ABSENT = "ABSENT"
TURNSTILE_LOADING = "LOADING"
TURNSTILE_WAITING = "WAITING"
TURNSTILE_SOLVED = "SOLVED"
TURNSTILE_FAILED = "FAILED"

# 自动点击 Turnstile 复选框：先给 managed 模式留出自动完成的时间，再按间隔有限次尝试。
TURNSTILE_AUTOCLICK_GRACE_SEC = 2.5
TURNSTILE_AUTOCLICK_INTERVAL_SEC = 5.0
TURNSTILE_AUTOCLICK_MAX_ATTEMPTS = 4


def _mark_registration_stage(stage):
    # Function-local import avoids an import-time cycle while keeping the browser module reusable.
    from registration_flow import _set_registration_stage
    return _set_registration_stage(stage)


def _managed_proxy_mode():
    try:
        return str(config.get("proxy_mode", "auto") or "auto").strip().lower() in ("single", "pool")
    except Exception:
        return False


def _is_pre_submit_js_transient(exc):
    if isinstance(exc, (TimeoutError, ContextLostError, PageDisconnectedError)):
        return True
    if isinstance(exc, RuntimeError):
        message = str(exc or "").lower()
        return "js结果解析错误" in message or "js result parsing error" in message
    return False


def _run_pre_submit_js(script, *args):
    try:
        return page.run_js(script, *args)
    except Exception as exc:
        if _is_pre_submit_js_transient(exc):
            raise AccountRetryNeeded(f"提交前浏览器 JS 暂时失败: {exc}") from exc
        raise
_OWN_NAMES = {'is_cloudflare_block_response', 'response_preview', 'start_browser', 'enable_nsfw_for_token', 'stop_browser_proxy_bridge', 'set_tos_accepted', 'fill_email_and_submit', 'getTurnstileToken', 'set_birth_date', 'generate_random_birthdate', 'fill_profile_and_submit', 'click_email_signup_button', 'wait_for_sso_cookie', 'fill_code_and_submit', 'build_profile', 'cleanup_runtime_memory', 'open_signup_page', 'stop_browser', 'encode_grpc_nsfw_settings', 'restart_browser', 'has_profile_form', 'update_nsfw_settings', 'refresh_active_page'}


def bind_runtime(namespace):
    for name, value in namespace.items():
        if name.startswith("__") or name in _OWN_NAMES or name in {
            "browser", "page", "browser_proxy_bridge", "browser_started_with_proxy", "cf_clearance",
        }:
            continue
        globals()[name] = value


def generate_random_birthdate():
    import datetime as dt

    today = dt.date.today()
    age = random.randint(20, 40)
    birth_year = today.year - age
    birth_month = random.randint(1, 12)
    birth_day = random.randint(1, 28)
    return f"{birth_year}-{birth_month:02d}-{birth_day:02d}T16:00:00.000Z"

def response_preview(res, limit=200):
    try:
        text = str(res.text or "")
    except Exception:
        text = ""
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]

def is_cloudflare_block_response(res):
    try:
        headers = {str(k).lower(): str(v).lower() for k, v in dict(res.headers).items()}
        text = str(res.text or "").lower()
        server = headers.get("server", "")
        content_type = headers.get("content-type", "")
        return (
            res.status_code in (403, 429, 503)
            and (
                "cloudflare" in server
                or "cloudflare" in text
                or "cf-error" in text
                or "__cf_chl" in text
                or "text/html" in content_type
            )
        )
    except Exception:
        return False

def set_birth_date(session, log_callback=None):
    url = "https://grok.com/rest/auth/set-birth-date"
    new_headers = {
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    payload = {"birthDate": generate_random_birthdate()}
    try:
        res = session.post(url, json=payload, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(
                f"[Debug] set_birth_date status: {res.status_code}, body: {response_preview(res)}"
            )
        if 200 <= res.status_code < 300:
            return True, "ok"
        if is_cloudflare_block_response(res):
            return (
                False,
                "set_birth_date 被 grok.com 的 Cloudflare 防护拦截，HTTP "
                f"{res.status_code}",
            )
        return False, f"set_birth_date HTTP {res.status_code}: {response_preview(res)}"
    except Exception as e:
        if log_callback:
            log_callback(f"[set_birth_date] 异常: {e}")
        return False, f"set_birth_date 异常: {e}"

def set_tos_accepted(session, log_callback=None):
    url = "https://accounts.x.ai/auth_mgmt.AuthManagement/SetTosAcceptedVersion"
    payload = struct.pack("B", (2 << 3) | 0) + struct.pack("B", 1)
    data = b"\x00" + struct.pack(">I", len(payload)) + payload
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "x-user-agent": "connect-es/2.1.1",
        "origin": "https://accounts.x.ai",
        "referer": "https://accounts.x.ai/accept-tos",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(f"[Debug] set_tos_accepted status: {res.status_code}")
        if 200 <= res.status_code < 300:
            return True, "ok"
        if is_cloudflare_block_response(res):
            return (
                False,
                "set_tos_accepted 被 accounts.x.ai 的 Cloudflare 防护拦截，HTTP "
                f"{res.status_code}",
            )
        return False, f"set_tos_accepted HTTP {res.status_code}: {response_preview(res)}"
    except Exception as e:
        if log_callback:
            log_callback(f"[set_tos_accepted] 异常: {e}")
        return False, f"set_tos_accepted 异常: {e}"

def encode_grpc_nsfw_settings():
    field1_content = bytes([0x10, 0x01])
    field1 = bytes([0x0A, len(field1_content)]) + field1_content
    nsfw_string = b"always_show_nsfw_content"
    field2_inner = bytes([0x0A, len(nsfw_string)]) + nsfw_string
    field2 = bytes([0x12, len(field2_inner)]) + field2_inner
    payload = field1 + field2
    return b"\x00" + struct.pack(">I", len(payload)) + payload

def update_nsfw_settings(session, log_callback=None):
    url = "https://grok.com/auth_mgmt.AuthManagement/UpdateUserFeatureControls"
    data = encode_grpc_nsfw_settings()
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(
                f"[Debug] update_nsfw status: {res.status_code}, body: {response_preview(res)}"
            )
        if 200 <= res.status_code < 300:
            return True, "ok"
        if is_cloudflare_block_response(res):
            return (
                False,
                "update_nsfw_settings 被 grok.com 的 Cloudflare 防护拦截，HTTP "
                f"{res.status_code}",
            )
        return False, f"update_nsfw_settings HTTP {res.status_code}: {response_preview(res)}"
    except Exception as e:
        if log_callback:
            log_callback(f"[update_nsfw] 异常: {e}")
        return False, f"update_nsfw_settings 异常: {e}"

def enable_nsfw_for_token(token, cf_clearance="", log_callback=None):
    proxies = get_proxies()
    user_agent = get_user_agent()
    try:
        with requests.Session(impersonate="chrome120", proxies=proxies) as session:
            cookie_parts = [f"sso={token}", f"sso-rw={token}"]
            if cf_clearance:
                cookie_parts.append(f"cf_clearance={cf_clearance}")
            session.headers.update(
                {
                    "user-agent": user_agent,
                    "cookie": "; ".join(cookie_parts),
                }
            )
            ok, message = set_tos_accepted(session, log_callback)
            if not ok:
                return False, message
            ok, message = set_birth_date(session, log_callback)
            if not ok:
                return False, message
            ok, message = update_nsfw_settings(session, log_callback)
            if not ok:
                return False, message
            return True, "成功开启 NSFW"
    except Exception as e:
        return False, f"异常: {str(e)}"

def stop_browser_proxy_bridge():
    global browser_proxy_bridge
    if browser_proxy_bridge is not None:
        try:
            browser_proxy_bridge.stop()
        except Exception:
            pass
    browser_proxy_bridge = None

def start_browser(log_callback=None, use_proxy=True):
    global browser, page, browser_proxy_bridge, browser_started_with_proxy
    last_exc = None
    proxy_enabled = bool(use_proxy and get_configured_proxy())
    for attempt in range(1, 5):
        bridge = None
        try:
            browser_proxy, bridge = prepare_browser_proxy(use_proxy=use_proxy, log_callback=log_callback)
            browser = Chromium(create_browser_options(browser_proxy=browser_proxy))
            browser_proxy_bridge = bridge
            browser_started_with_proxy = bool(browser_proxy)
            tabs = browser.get_tabs()
            page = tabs[-1] if tabs else browser.new_tab()
            if log_callback and getattr(browser, "user_data_path", None):
                log_callback(f"[Debug] 当前浏览器资料目录: {browser.user_data_path}")
            if log_callback and get_configured_proxy():
                mode = "代理" if browser_started_with_proxy else "直连"
                log_callback(f"[*] 浏览器网络模式: {mode}")
            if log_callback and attempt > 1:
                log_callback(f"[*] 浏览器第 {attempt} 次启动成功")
            return browser, page
        except Exception as exc:
            last_exc = exc
            if bridge is not None:
                try:
                    bridge.stop()
                except Exception:
                    pass
            if log_callback:
                mode = "代理" if proxy_enabled else "直连"
                log_callback(f"[Debug] 浏览器{mode}启动失败(第{attempt}/4次): {exc}")
            try:
                if browser is not None:
                    browser.quit(del_data=True)
            except Exception:
                pass
            browser = None
            page = None
            browser_proxy_bridge = None
            browser_started_with_proxy = False
            time.sleep(min(1.5 * attempt, 4))
    if _managed_proxy_mode() and is_proxy_connection_error(last_exc):
        raise ProxyTransportError("Chromium 通过当前代理启动失败: %s" % safe_proxy_error_text(last_exc)) from last_exc
    raise Exception(f"浏览器启动失败，已重试4次: {last_exc}")

def stop_browser():
    global browser, page, browser_started_with_proxy
    if browser is not None:
        try:
            browser.quit(del_data=True)
        except Exception:
            pass
    stop_browser_proxy_bridge()
    browser = None
    page = None
    browser_started_with_proxy = False

def restart_browser(log_callback=None, use_proxy=True):
    stop_browser()
    return start_browser(log_callback=log_callback, use_proxy=use_proxy)

def cleanup_runtime_memory(log_callback=None, reason="定期清理"):
    if log_callback:
        log_callback(f"[*] {reason}: 关闭浏览器并清理内存")
    stop_browser()
    try:
        from cpa_xai.browser_confirm import shutdown_mint_browsers
        shutdown_mint_browsers()
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] CPA 浏览器清理失败: {exc}")
    collected = gc.collect()
    if log_callback:
        log_callback(f"[*] Python GC 已回收对象数: {collected}")

def refresh_active_page():
    global browser, page
    if browser is None:
        restart_browser()
    try:
        tabs = browser.get_tabs()
        if tabs:
            page = tabs[-1]
        else:
            page = browser.new_tab()
    except Exception:
        restart_browser()
    return page

def click_email_signup_button(timeout=10, log_callback=None, cancel_callback=None):
    global page
    deadline = time.time() + timeout
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if log_callback:
            log_callback("[Debug] 尝试查找“使用邮箱注册”按钮...")

        clicked = _run_pre_submit_js(r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function nodeText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('href'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function scoreEntry(node) {
    const compact = nodeText(node).replace(/\s+/g, '');
    const lower = compact.toLowerCase();
    if (compact.includes('使用邮箱注册')) return 100;
    if (lower.includes('signupwithemail')) return 95;
    if (lower.includes('continuewithemail')) return 90;
    if (lower.includes('email') && (lower.includes('sign') || lower.includes('continue') || lower.includes('use') || lower.includes('with'))) return 80;
    if (lower === 'email' || lower.includes('邮箱')) return 70;
    return 0;
}
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map((node) => ({ node, score: scoreEntry(node), text: nodeText(node) }))
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score);
const target = candidates[0]?.node || null;
if (!target) {
    return false;
}
target.click();
return candidates[0].text || true;
        """)

        if clicked:
            if log_callback:
                detail = f": {clicked}" if isinstance(clicked, str) else ""
                log_callback(f"[*] 已点击「使用邮箱注册」按钮{detail}")
            sleep_with_cancel(2, cancel_callback)
            return True

        if log_callback:
            current_url = page.url if page else "none"
            log_callback(f"[Debug] 当前URL: {current_url}")

        sleep_with_cancel(1, cancel_callback)

    if log_callback:
        page_html = page.html[:500] if page else "no page"
        log_callback(f"[Debug] 页面内容片段: {page_html}")

    raise Exception("未找到「使用邮箱注册」按钮")

def open_signup_page(log_callback=None, cancel_callback=None):
    global browser, page
    raise_if_cancelled(cancel_callback)
    if browser is None:
        start_browser(log_callback=log_callback)
        if log_callback:
            log_callback("[*] 浏览器已启动")

    def _open_with_current_browser():
        global page
        try:
            page = browser.get_tab(0)
            page.get(SIGNUP_URL)
        except Exception as e:
            if log_callback:
                log_callback(f"[Debug] 打开URL异常: {e}")
            page = browser.new_tab(SIGNUP_URL)
        page.wait.doc_loaded()

    try:
        _open_with_current_browser()
    except Exception as e:
        if browser_started_with_proxy and get_configured_proxy():
            if _managed_proxy_mode():
                raise ProxyTransportError("当前代理访问注册页失败: %s" % safe_proxy_error_text(e)) from e
            if log_callback:
                log_callback(f"[!] 浏览器代理访问注册页失败，自动回退直连: {e}")
            restart_browser(log_callback=log_callback, use_proxy=False)
            _open_with_current_browser()
        else:
            raise

    if browser_started_with_proxy and page_has_proxy_error(page):
        if _managed_proxy_mode():
            raise ProxyTransportError("Chromium 页面显示代理连接错误")
        if log_callback:
            log_callback("[!] 浏览器页面显示代理错误，自动回退直连")
        restart_browser(log_callback=log_callback, use_proxy=False)
        _open_with_current_browser()

    sleep_with_cancel(2, cancel_callback)
    if log_callback:
        log_callback(f"[*] 当前URL: {page.url}")
    click_email_signup_button(
        log_callback=log_callback, cancel_callback=cancel_callback
    )

def has_profile_form(log_callback=None):
    refresh_active_page()
    try:
        return bool(
            page.run_js(
                """
const givenInput = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
return !!(givenInput && familyInput && passwordInput);
            """
            )
        )
    except Exception:
        return False

def fill_email_and_submit(timeout=45, log_callback=None, cancel_callback=None, on_mail_created=None):
    raise_if_cancelled(cancel_callback)
    email, dev_token = get_email_and_token()
    if not email or not dev_token:
        raise Exception("获取邮箱失败")
    if log_callback:
        log_callback(f"[*] 已创建邮箱: {email}")
    if on_mail_created is not None:
        try:
            persisted = on_mail_created(email, dev_token)
            if persisted is False and log_callback:
                log_callback("[!] 邮箱凭据提前保存失败；注册继续，提交成功后还会再次尝试保存")
        except Exception as exc:
            if log_callback:
                log_callback(f"[!] 邮箱凭据提前保存异常；注册继续，提交成功后还会再次尝试保存: {exc}")
    deadline = time.time() + timeout
    last_diag_time = 0
    last_reclick_time = 0
    last_snapshot = None
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        filled_raw = _run_pre_submit_js(
            """
const email = arguments[0];
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function textOf(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('placeholder'),
        node.getAttribute('data-testid'),
        node.getAttribute('name'),
        node.getAttribute('id'),
        node.getAttribute('autocomplete'),
    ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
}
function describeInput(node) {
    return [
        `type=${node.getAttribute('type') || ''}`,
        `name=${node.getAttribute('name') || ''}`,
        `id=${node.getAttribute('id') || ''}`,
        `placeholder=${node.getAttribute('placeholder') || ''}`,
        `aria=${node.getAttribute('aria-label') || ''}`,
        `testid=${node.getAttribute('data-testid') || ''}`,
    ].join(' ').replace(/\\s+/g, ' ').trim().slice(0, 160);
}
function describeAction(node) {
    return textOf(node).slice(0, 120);
}
function emailCandidates() {
    const direct = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"], input[placeholder*="mail" i], input[aria-label*="mail" i]'));
    const all = Array.from(document.querySelectorAll('input, textarea'));
    for (const node of all) {
        const type = (node.getAttribute('type') || '').toLowerCase();
        if (['hidden', 'submit', 'button', 'checkbox', 'radio', 'file', 'search'].includes(type)) continue;
        const meta = textOf(node).toLowerCase();
        if (meta.includes('email') || meta.includes('e-mail') || meta.includes('mail') || meta.includes('邮箱') || meta.includes('电子邮件')) {
            direct.push(node);
        }
    }
    return Array.from(new Set(direct));
}
const visibleInputs = Array.from(document.querySelectorAll('input, textarea'))
    .filter((node) => isVisible(node) && !node.disabled && !node.readOnly)
    .map(describeInput)
    .slice(0, 8);
const visibleActions = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map(describeAction)
    .filter(Boolean)
    .slice(0, 10);
const input = emailCandidates().find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input) {
    return JSON.stringify({
        state: 'not-ready',
        url: location.href,
        title: document.title,
        inputs: visibleInputs,
        buttons: visibleActions,
    });
}
input.focus(); input.click();
const valueProto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
const valueSetter = Object.getOwnPropertyDescriptor(valueProto, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) tracker.setValue('');
if (valueSetter) valueSetter.call(input, email); else input.value = email;
input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new InputEvent('input', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new Event('change', { bubbles: true }));
const inputType = (input.getAttribute('type') || '').toLowerCase();
const isValid = inputType !== 'email' || input.checkValidity();
if ((input.value || '').trim() !== email || !isValid) {
    return JSON.stringify({
        state: 'fill-failed',
        value: input.value || '',
        valid: isValid,
        input: describeInput(input),
        url: location.href,
    });
}
input.blur();
return JSON.stringify({
    state: 'filled',
    input: describeInput(input),
    url: location.href,
});
            """,
            email,
        )
        filled = json.loads(filled_raw)
        state = filled.get("state")
        last_snapshot = filled
        if state == "not-ready":
            now = time.time()
            if now - last_reclick_time >= 3:
                reclicked = _run_pre_submit_js(r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function nodeText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('href'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function scoreEntry(node) {
    const compact = nodeText(node).replace(/\s+/g, '');
    const lower = compact.toLowerCase();
    if (compact.includes('使用邮箱注册')) return 100;
    if (lower.includes('signupwithemail')) return 95;
    if (lower.includes('continuewithemail')) return 90;
    if (lower.includes('email') && (lower.includes('sign') || lower.includes('continue') || lower.includes('use') || lower.includes('with'))) return 80;
    if (lower === 'email' || lower.includes('邮箱')) return 70;
    return 0;
}
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map((node) => ({ node, score: scoreEntry(node), text: nodeText(node) }))
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score);
if (!candidates.length) return false;
candidates[0].node.click();
return candidates[0].text || true;
                """)
                last_reclick_time = now
                if reclicked and log_callback:
                    detail = f": {reclicked}" if isinstance(reclicked, str) else ""
                    log_callback(f"[Debug] 邮箱输入框未出现，已再次触发邮箱注册入口{detail}")
            if log_callback and now - last_diag_time >= 5:
                last_diag_time = now
                inputs = " | ".join((filled or {}).get("inputs", [])[:6])
                buttons = " | ".join((filled or {}).get("buttons", [])[:8])
                url = (filled or {}).get("url", page.url if page else "")
                log_callback(f"[Debug] 等待邮箱输入框: url={url}; inputs={inputs or 'none'}; buttons={buttons or 'none'}")
            sleep_with_cancel(0.5, cancel_callback)
            continue
        if state != "filled":
            if log_callback:
                log_callback(f"[Debug] 邮箱输入框已出现，但写入失败: {filled}")
            sleep_with_cancel(0.5, cancel_callback)
            continue
        sleep_with_cancel(0.8, cancel_callback)
        ready_to_submit = _run_pre_submit_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function textOf(node) {
    return [node.getAttribute('placeholder'), node.getAttribute('data-testid'), node.getAttribute('name'),
            node.getAttribute('id'), node.getAttribute('autocomplete'), node.getAttribute('aria-label')]
        .filter(Boolean).join(' ').toLowerCase();
}
const direct = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"], input[placeholder*="mail" i], input[aria-label*="mail" i]'));
for (const node of Array.from(document.querySelectorAll('input, textarea'))) {
    const type = (node.getAttribute('type') || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'checkbox', 'radio', 'file', 'search'].includes(type)) continue;
    const meta = textOf(node);
    if (meta.includes('email') || meta.includes('e-mail') || meta.includes('mail') || meta.includes('邮箱') || meta.includes('电子邮件')) direct.push(node);
}
const input = Array.from(new Set(direct)).find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input || !(input.value || '').trim()) return false;
return (input.getAttribute('type') || '').toLowerCase() !== 'email' || input.checkValidity();
            """
        )
        if not ready_to_submit:
            sleep_with_cancel(0.5, cancel_callback)
            continue
        _mark_registration_stage("email_submit")
        clicked = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function textOf(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('placeholder'),
        node.getAttribute('data-testid'),
        node.getAttribute('name'),
        node.getAttribute('id'),
        node.getAttribute('autocomplete'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function emailCandidates() {
    const direct = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"], input[placeholder*="mail" i], input[aria-label*="mail" i]'));
    const all = Array.from(document.querySelectorAll('input, textarea'));
    for (const node of all) {
        const type = (node.getAttribute('type') || '').toLowerCase();
        if (['hidden', 'submit', 'button', 'checkbox', 'radio', 'file', 'search'].includes(type)) continue;
        const meta = textOf(node).toLowerCase();
        if (meta.includes('email') || meta.includes('e-mail') || meta.includes('mail') || meta.includes('邮箱') || meta.includes('电子邮件')) {
            direct.push(node);
        }
    }
    return Array.from(new Set(direct));
}
const input = emailCandidates().find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input || !(input.value || '').trim()) return false;
const inputType = (input.getAttribute('type') || '').toLowerCase();
if (inputType === 'email' && !input.checkValidity()) return false;
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true');
const submitButton = buttons.find((node) => {
    const text = textOf(node).replace(/\s+/g, '');
    const lower = text.toLowerCase();
    return (
        text === '注册' ||
        text.includes('注册') ||
        text.includes('继续') ||
        text.includes('下一步') ||
        text.includes('确认') ||
        lower.includes('signup') ||
        lower.includes('sign up') ||
        lower.includes('continue') ||
        lower.includes('next') ||
        lower.includes('createaccount') ||
        lower.includes('submit')
    );
});
if (submitButton) {
    submitButton.click();
    return textOf(submitButton) || true;
}
const form = input.closest('form');
if (form) {
    if (form.requestSubmit) form.requestSubmit();
    else form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    return 'form-submit';
}
input.focus();
input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
return 'enter';
            """
        )
        if clicked:
            if log_callback:
                detail = f" ({clicked})" if isinstance(clicked, str) else ""
                log_callback(f"[*] 已填写邮箱并提交: {email}{detail}")
            return email, dev_token
        sleep_with_cancel(0.5, cancel_callback)
    if last_snapshot:
        inputs = " | ".join(last_snapshot.get("inputs", [])[:6])
        buttons = " | ".join(last_snapshot.get("buttons", [])[:8])
        url = last_snapshot.get("url", page.url if page else "")
        raise Exception(
            f"未找到邮箱输入框或注册按钮，最后页面: url={url}; inputs={inputs or 'none'}; buttons={buttons or 'none'}"
        )
    raise Exception("未找到邮箱输入框或注册按钮")

def fill_code_and_submit(email, dev_token, timeout=180, transition_timeout=15, log_callback=None, cancel_callback=None):
    def _resend_code():
        page.run_js(
            r"""
const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = nodes.find((node) => {
  const t = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
  return t.includes('重新发送') || t.includes('resend') || t.includes('再次发送');
});
if (target && !target.disabled) { target.click(); return true; }
return false;
            """
        )

    _mark_registration_stage("code_wait")
    code = get_oai_code(
        dev_token,
        email,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
        resend_callback=_resend_code,
    )
    if not code:
        raise Exception("获取验证码失败")
    clean_code = str(code).replace("-", "").strip()
    deadline = time.time() + timeout

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        ready = page.run_js(
            """
const code = String(arguments[0] || '').trim();
if (!code) return 'not-ready';
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const aggregate = Array.from(document.querySelectorAll(
  'input[data-input-otp=\"true\"], input[name=\"code\"], input[autocomplete=\"one-time-code\"], input[inputmode=\"numeric\"], input[inputmode=\"text\"]'
)).find((node) => isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 6) > 1);
if (aggregate) return 'aggregate';
const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) return false;
    const maxLength = Number(node.maxLength || 0);
    const ac = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || ac === 'one-time-code';
});
return otpBoxes.length >= code.length ? 'boxes' : 'not-ready';
            """,
            clean_code,
        )
        if ready == "not-ready":
            sleep_with_cancel(0.5, cancel_callback)
            continue
        _mark_registration_stage("code_submit")
        filled = page.run_js(
            """
const code = String(arguments[0] || '').trim();
if (!code) return 'empty-code';

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setInputValue(input, value) {
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const aggregate = Array.from(document.querySelectorAll(
  'input[data-input-otp=\"true\"], input[name=\"code\"], input[autocomplete=\"one-time-code\"], input[inputmode=\"numeric\"], input[inputmode=\"text\"]'
)).find((node) => isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 6) > 1);

if (aggregate) {
    aggregate.focus();
    aggregate.click();
    setInputValue(aggregate, code);
    return String(aggregate.value || '').replace(/\\s+/g, '') ? 'filled-aggregate' : 'aggregate-failed';
}

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) return false;
    const maxLength = Number(node.maxLength || 0);
    const ac = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || ac === 'one-time-code';
});

if (otpBoxes.length >= code.length) {
    for (let i = 0; i < code.length; i += 1) {
        const ch = code[i] || '';
        const box = otpBoxes[i];
        box.focus();
        box.click();
        setInputValue(box, ch);
        box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: ch }));
        box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: ch }));
    }
    const merged = otpBoxes.slice(0, code.length).map((x) => String(x.value || '').trim()).join('');
    return merged.length ? 'filled-boxes' : 'boxes-failed';
}

return 'not-ready';
            """,
            clean_code,
        )

        if filled == "not-ready":
            sleep_with_cancel(0.5, cancel_callback)
            continue
        if "failed" in str(filled):
            if log_callback:
                log_callback(f"[Debug] 验证码填写失败: {filled}")
            sleep_with_cancel(0.5, cancel_callback)
            continue

        clicked = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const buttons = Array.from(document.querySelectorAll('button[type=\"submit\"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});

const btn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\\s+/g, '').toLowerCase();
    return (
        t.includes('确认邮箱') ||
        t.includes('继续') ||
        t.includes('下一步') ||
        t.includes('confirm') ||
        t.includes('continue') ||
        t.includes('next')
    );
});

if (!btn) return 'no-button';
btn.focus();
btn.click();
return 'clicked';
            """
        )

        if clicked == "clicked" or clicked == "no-button":
            if log_callback:
                if clicked == "clicked":
                    log_callback(f"[*] 已填写验证码并触发提交，等待资料页确认: {code}")
                else:
                    log_callback(f"[*] 已填写验证码，页面未显示提交按钮，等待自动提交确认: {code}")
            transition_deadline = min(deadline, time.time() + max(1.0, float(transition_timeout)))
            while time.time() < transition_deadline:
                raise_if_cancelled(cancel_callback)
                if has_profile_form(log_callback=log_callback):
                    if log_callback:
                        log_callback("[*] 验证码提交已确认，资料页已出现")
                    return code
                try:
                    rejection = page.run_js(
                        r"""
const text = (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').toLowerCase();
const markers = [
  'invalid code', 'incorrect code', 'wrong code', 'code expired',
  '验证码错误', '验证码无效', '验证码已过期', '验证码不正确'
];
return markers.find((item) => text.includes(item)) || '';
                        """
                    )
                except (ContextLostError, JavaScriptError) as exc:
                    # A navigation can invalidate the old execution context
                    # just before the profile form becomes observable.
                    if log_callback:
                        log_callback(f"[Debug] 验证码提交后页面正在切换，继续等待: {exc}")
                    try:
                        refresh_active_page()
                    except Exception:
                        pass
                    rejection = ""
                if rejection:
                    raise RuntimeError(f"验证码被页面明确拒绝: {rejection}")
                sleep_with_cancel(0.5, cancel_callback)
            from registration_flow import VerificationSubmissionUnconfirmed
            raise VerificationSubmissionUnconfirmed(
                f"验证码已填写，但在 {transition_timeout}s 内未确认进入资料页"
            )

        sleep_with_cancel(0.5, cancel_callback)

    from registration_flow import VerificationSubmissionUnconfirmed
    raise VerificationSubmissionUnconfirmed("验证码已获取，但自动填写/提交结果无法确认")

def _read_turnstile_state():
    """只读 Turnstile 状态，不重置、不点击、不修改页面。"""
    if page is None:
        return {
            "state": TURNSTILE_ABSENT,
            "present": False,
            "token": "",
            "token_length": 0,
            "widget_present": False,
            "iframe_present": False,
            "script_present": False,
            "visible": False,
        }

    state_raw = page.run_js(
        """
try {
  function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  const input = document.querySelector('input[name="cf-turnstile-response"]');
  let token = String((input && input.value) || '').trim();
  if (!token && window.turnstile && typeof window.turnstile.getResponse === 'function') {
    try {
      token = String(window.turnstile.getResponse() || '').trim();
    } catch (e) {
      token = '';
    }
  }

  const iframe = document.querySelector('iframe[src*="turnstile"]');
  const widget = document.querySelector('div.cf-turnstile, [data-sitekey]');
  const scriptPresent = !!document.querySelector('script[src*="turnstile"]');
  const widgetPresent = !!input || !!iframe || !!widget;
  const visible = isVisible(iframe) || isVisible(widget);

  const nearbyText = [widget, iframe && iframe.parentElement, input && input.parentElement]
    .filter(Boolean)
    .map((node) => String(node.innerText || node.textContent || node.getAttribute?.('title') || ''))
    .join(' ')
    .toLowerCase();
  const failed = widgetPresent && [
    'verification failed',
    'challenge failed',
    'challenge expired',
    '验证失败',
    '验证已过期',
    '校验失败',
  ].some((marker) => nearbyText.includes(marker));

  let state = 'ABSENT';
  if (token) state = 'SOLVED';
  else if (failed) state = 'FAILED';
  else if (input || iframe) state = 'WAITING';
  else if (widget) state = 'LOADING';

  return JSON.stringify({
    state,
    token,
    widget_present: widgetPresent,
    iframe_present: !!iframe,
    script_present: scriptPresent,
    visible,
  });
} catch (e) {
  return JSON.stringify({
    state: 'ABSENT',
    token: '',
    widget_present: false,
    iframe_present: false,
    script_present: false,
    visible: false,
  });
}
        """
    )
    try:
        state = json.loads(state_raw)
    except (TypeError, ValueError):
        state = {}

    token = str(state.get("token") or "").strip()
    status = str(state.get("state") or TURNSTILE_ABSENT).strip().upper()
    if token:
        status = TURNSTILE_SOLVED
    if status not in {
        TURNSTILE_ABSENT,
        TURNSTILE_LOADING,
        TURNSTILE_WAITING,
        TURNSTILE_SOLVED,
        TURNSTILE_FAILED,
    }:
        status = TURNSTILE_ABSENT

    return {
        "state": status,
        "present": status != TURNSTILE_ABSENT,
        "token": token,
        "token_length": len(token),
        "widget_present": bool(state.get("widget_present")),
        "iframe_present": bool(state.get("iframe_present")),
        "script_present": bool(state.get("script_present")),
        "visible": bool(state.get("visible")),
    }


def _wait_for_turnstile(log_callback=None, cancel_callback=None, timeout=60):
    """等待 Turnstile 自动完成、自动点击成功或由用户在当前浏览器窗口完成。"""
    global page
    if page is None:
        raise Exception("页面未就绪，无法执行 Turnstile")

    timeout = max(float(timeout or 0), 0.0)
    deadline = time.time() + timeout
    started = time.time()
    last_state = None
    last_log_at = 0.0
    autoclick_enabled = _turnstile_autoclick_enabled()
    autoclick_attempts = 0
    last_autoclick_at = None
    autoclick_diagnostics_logged = False

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        state = _read_turnstile_state()
        status = state["state"]
        token = state["token"]

        if status == TURNSTILE_SOLVED and token:
            if log_callback:
                log_callback(f"[*] Cloudflare 人机验证已完成，token长度={len(token)}")
            return token

        if status == TURNSTILE_ABSENT:
            return ""

        if status == TURNSTILE_FAILED:
            raise Exception("Cloudflare 人机验证失败")

        now = time.time()
        state_changed = status != last_state
        if log_callback and (state_changed or now - last_log_at >= 5):
            if status == TURNSTILE_LOADING:
                log_callback("[*] Cloudflare 人机验证组件正在加载...")
            elif status == TURNSTILE_WAITING:
                if state_changed and state.get("visible"):
                    log_callback("[*] Cloudflare 人机验证等待完成，请在当前浏览器窗口完成验证")
                else:
                    log_callback(
                        f"[*] 仍在等待 Cloudflare 人机验证... {int(now - started)}s"
                    )
            last_log_at = now
            last_state = status

        if (
            autoclick_enabled
            and status == TURNSTILE_WAITING
            and autoclick_attempts < TURNSTILE_AUTOCLICK_MAX_ATTEMPTS
            and now - started >= TURNSTILE_AUTOCLICK_GRACE_SEC
            and (last_autoclick_at is None or now - last_autoclick_at >= TURNSTILE_AUTOCLICK_INTERVAL_SEC)
        ):
            rect, diagnostics = _turnstile_widget_probe()
            if rect is not None:
                last_autoclick_at = now
                if _click_turnstile_widget(
                    rect, log_callback=log_callback, cancel_callback=cancel_callback
                ):
                    autoclick_attempts += 1
            elif log_callback and not autoclick_diagnostics_logged:
                autoclick_diagnostics_logged = True
                log_callback(
                    "[Debug] 未找到可点击的 Cloudflare 组件: "
                    + _format_turnstile_candidates(diagnostics)
                )

        sleep_with_cancel(min(1.0, max(deadline - now, 0.0)), cancel_callback)

    raise Exception("Turnstile 验证超时")


def getTurnstileToken(log_callback=None, cancel_callback=None, timeout=60):
    """兼容入口：等待当前页面的 Turnstile 验证完成。"""
    return _wait_for_turnstile(
        log_callback=log_callback,
        cancel_callback=cancel_callback,
        timeout=timeout,
    )


def _turnstile_autoclick_enabled():
    """是否允许自动点击 Turnstile 复选框；缺少配置时保持开启。"""
    try:
        return bool(config.get("turnstile_autoclick_enabled", True))
    except Exception:
        pass
    try:
        import app_config

        return bool(app_config.config.get("turnstile_autoclick_enabled", True))
    except Exception:
        return True


_TURNSTILE_PROBE_JS = r"""
try {
  function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }
  function describe(node) {
    const rect = node.getBoundingClientRect();
    return {
      tag: String(node.tagName || '').toLowerCase(),
      src: String((node.getAttribute && node.getAttribute('src')) || '').slice(0, 100),
      w: Math.round(rect.width),
      h: Math.round(rect.height),
      visible: isVisible(node),
    };
  }
  function turnstileLike(node) {
    if (!node) return false;
    if (String(node.tagName || '').toUpperCase() !== 'IFRAME') return true;
    const src = String(node.getAttribute('src') || '');
    return /turnstile|challenges\.cloudflare\.com/i.test(src);
  }
  function lightCandidates(root, out) {
    let nodes = [];
    try { nodes = root.querySelectorAll('iframe, div.cf-turnstile, [data-sitekey]'); } catch (e) { nodes = []; }
    for (const node of nodes) if (turnstileLike(node)) out.push(node);
  }
  function deepCandidates(root, out, depth) {
    if (!root || depth > 3) return;
    lightCandidates(root, out);
    let hosts = [];
    try { hosts = root.querySelectorAll('*'); } catch (e) { hosts = []; }
    for (const host of hosts) if (host.shadowRoot) deepCandidates(host.shadowRoot, out, depth + 1);
    let frames = [];
    try { frames = root.querySelectorAll('iframe'); } catch (e) { frames = []; }
    for (const frame of frames) {
      let doc = null;
      try { doc = frame.contentDocument; } catch (e) { doc = null; }
      if (doc) deepCandidates(doc, out, depth + 1);
    }
  }
  function firstClickable(nodes) {
    for (const node of nodes) {
      const rect = node.getBoundingClientRect();
      if (isVisible(node) && rect.width >= 24 && rect.height >= 24) return {node: node, rect: rect};
    }
    return null;
  }

  const light = [];
  lightCandidates(document, light);
  let hit = firstClickable(light);
  if (!hit) {
    const deep = [];
    deepCandidates(document, deep, 0);
    hit = firstClickable(deep);
  }

  const candidates = [];
  for (const node of light.slice(0, 5)) candidates.push(describe(node));
  if (!hit) {
    let frames = [];
    try { frames = Array.from(document.querySelectorAll('iframe')).slice(0, 6); } catch (e) { frames = []; }
    for (const frame of frames) {
      const item = describe(frame);
      const seen = candidates.some((entry) => entry.src === item.src && entry.w === item.w && entry.h === item.h);
      if (!seen) candidates.push(item);
    }
    return JSON.stringify({rect: null, candidates: candidates});
  }

  const target = hit.node;
  let rect = hit.rect;
  const pointX = () => rect.left + Math.min(Math.max(rect.width * 0.08, 12), 32);
  const pointY = () => rect.top + rect.height / 2;
  const outside = () => pointX() < 0 || pointY() < 0 || pointX() > window.innerWidth || pointY() > window.innerHeight;
  if (outside() && typeof target.scrollIntoView === 'function') {
    target.scrollIntoView({block: 'center', inline: 'nearest'});
    rect = target.getBoundingClientRect();
  }
  if (rect.width < 24 || rect.height < 24 || outside()) {
    return JSON.stringify({rect: null, candidates: candidates});
  }
  return JSON.stringify({
    rect: {x: rect.left, y: rect.top, w: rect.width, h: rect.height},
    candidates: candidates,
  });
} catch (e) {
  return JSON.stringify({rect: null, candidates: [], error: String(e)});
}
"""


def _format_turnstile_candidates(candidates):
    if not candidates:
        return "页面上没有 turnstile / cloudflare iframe"
    parts = []
    for item in candidates[:5]:
        if isinstance(item, str):
            parts.append(item[:120])
            continue
        if not isinstance(item, dict):
            continue
        parts.append(
            "{tag} {w}x{h}{hidden} {src}".format(
                tag=str(item.get("tag") or "?"),
                w=item.get("w", "?"),
                h=item.get("h", "?"),
                hidden="" if item.get("visible") else "(hidden)",
                src=str(item.get("src") or "")[:80],
            )
        )
    return " | ".join(parts) if parts else "无可用诊断信息"


def _turnstile_js_probe():
    """页面 JS 探测：能看到的候选组件（不穿透 closed shadow root）。"""
    if page is None:
        return None, []
    try:
        raw = page.run_js(_TURNSTILE_PROBE_JS)
    except Exception:
        return None, []
    try:
        payload = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None, []
    if not isinstance(payload, dict):
        return None, []
    diagnostics = payload.get("candidates")
    if not isinstance(diagnostics, list):
        diagnostics = []
    rect = payload.get("rect")
    if not isinstance(rect, dict):
        return None, diagnostics
    try:
        x = float(rect["x"])
        y = float(rect["y"])
        width = float(rect["w"])
        height = float(rect["h"])
    except (KeyError, TypeError, ValueError):
        return None, diagnostics
    if width < 24 or height < 24:
        return None, diagnostics
    return {"x": x, "y": y, "w": width, "h": height}, diagnostics


_TURNSTILE_FRAME_RE = re.compile(r"turnstile|challenges\.cloudflare\.com", re.IGNORECASE)
_TURNSTILE_MIN_SIDE = 24.0
_TURNSTILE_PIERCED_NODE_LIMIT = 20000


def _iter_cdp_frames(node, out):
    if not isinstance(node, dict):
        return
    frame = node.get("frame")
    if isinstance(frame, dict):
        out.append(frame)
    for child in node.get("childFrames") or []:
        _iter_cdp_frames(child, out)


def _cdp_viewport_size():
    try:
        metrics = page.run_cdp("Page.getLayoutMetrics") or {}
    except Exception:
        return None
    viewport = metrics.get("cssVisualViewport") or metrics.get("visualViewport") or {}
    try:
        return float(viewport["clientWidth"]), float(viewport["clientHeight"])
    except (KeyError, TypeError, ValueError):
        return None


def _cdp_frame_owner_backend_id(frame_id):
    try:
        owner = page.run_cdp("DOM.getFrameOwner", frameId=frame_id) or {}
    except Exception:
        return None
    backend_id = owner.get("backendNodeId")
    return int(backend_id) if backend_id else None


def _cdp_backend_box(backend_id):
    try:
        payload = page.run_cdp("DOM.getBoxModel", backendNodeId=backend_id) or {}
    except Exception:
        return None
    model = payload.get("model") or {}
    quad = model.get("border") or model.get("content")
    if not quad or len(quad) < 8:
        return None
    try:
        xs = [float(quad[0]), float(quad[2]), float(quad[4]), float(quad[6])]
        ys = [float(quad[1]), float(quad[3]), float(quad[5]), float(quad[7])]
    except (TypeError, ValueError):
        return None
    left, top = min(xs), min(ys)
    return {"x": left, "y": top, "w": max(xs) - left, "h": max(ys) - top}


def _cdp_frame_rect(frame_id, viewport):
    """frame owner 的视口坐标矩形；必要时先滚动进视口。"""
    backend_id = _cdp_frame_owner_backend_id(frame_id)
    if backend_id is None:
        return None
    rect = _cdp_backend_box(backend_id)
    if rect is None:
        return None
    if viewport and not _click_point_inside(rect, viewport):
        try:
            page.run_cdp("DOM.scrollIntoViewIfNeeded", backendNodeId=backend_id)
        except Exception:
            return rect
        rect = _cdp_backend_box(backend_id) or rect
    return rect


def _click_point_inside(rect, viewport):
    if not viewport:
        return True
    width, height = viewport
    point_x = rect["x"] + min(max(rect["w"] * 0.08, 12.0), 32.0)
    point_y = rect["y"] + rect["h"] / 2
    return 0.0 <= point_x <= width and 0.0 <= point_y <= height


def _turnstile_frame_probe():
    """CDP frame tree 探测：可穿透 closed shadow root 与未设置 src 的 iframe。"""
    if page is None:
        return None, []
    try:
        tree = page.run_cdp("Page.getFrameTree") or {}
    except Exception:
        return None, []
    frames = []
    _iter_cdp_frames(tree.get("frameTree") or {}, frames)
    if not frames:
        return None, []
    main_id = str(frames[0].get("id") or "")
    viewport = _cdp_viewport_size()
    diagnostics = []
    matched = []
    for frame in frames:
        frame_id = str(frame.get("id") or "")
        url = str(frame.get("url") or "")
        if not frame_id or frame_id == main_id or not _TURNSTILE_FRAME_RE.search(url):
            continue
        matched.append((frame_id, url))
    matched.sort(key=lambda item: 0 if "turnstile" in item[1].lower() else 1)
    for frame_id, url in matched:
        rect = _cdp_frame_rect(frame_id, viewport)
        if rect is None:
            diagnostics.append(f"frame {url[:90]} (no box)")
            continue
        diagnostics.append(
            "frame {url} @{w}x{h}".format(
                url=url[:90], w=int(rect["w"]), h=int(rect["h"])
            )
        )
        if (
            rect["w"] >= _TURNSTILE_MIN_SIDE
            and rect["h"] >= _TURNSTILE_MIN_SIDE
            and _click_point_inside(rect, viewport)
        ):
            return rect, diagnostics
    return None, diagnostics


def _turnstile_widget_probe():
    """只读探测：返回 (rect, diagnostics)；先看页面 DOM，再看 CDP frame tree / pierced DOM。"""
    rect, diagnostics = _turnstile_js_probe()
    if rect is not None:
        return rect, diagnostics
    combined = list(diagnostics)
    frame_rect, frame_diagnostics = _turnstile_frame_probe()
    combined.extend(frame_diagnostics)
    if frame_rect is not None:
        return frame_rect, combined
    pierced_rect, pierced_diagnostics = _turnstile_pierced_probe()
    combined.extend(pierced_diagnostics)
    if pierced_rect is not None:
        return pierced_rect, combined
    return None, combined


def _turnstile_pierced_probe():
    """CDP pierced DOM 探测：可穿透 closed shadow root（当前 Cloudflare 组件的真实渲染方式）。"""
    if page is None:
        return None, []
    try:
        document = page.run_cdp("DOM.getDocument", depth=-1, pierce=True) or {}
    except Exception:
        return None, []
    nodes = []
    _walk_pierced_nodes(document.get("root") or {}, nodes)
    viewport = _cdp_viewport_size()
    diagnostics = []
    for node in nodes:
        if str(node.get("nodeName") or "").upper() != "IFRAME":
            continue
        src = _pierced_node_attr(node, "src")
        if not _TURNSTILE_FRAME_RE.search(src):
            continue
        backend_id = node.get("backendNodeId")
        rect = _cdp_backend_box(backend_id) if backend_id else None
        if rect is None:
            diagnostics.append(f"pierced iframe {src[:90]} (no box)")
            continue
        diagnostics.append(
            "pierced iframe {url} @{w}x{h}".format(url=src[:90], w=int(rect["w"]), h=int(rect["h"]))
        )
        if (
            rect["w"] >= _TURNSTILE_MIN_SIDE
            and rect["h"] >= _TURNSTILE_MIN_SIDE
            and _click_point_inside(rect, viewport)
        ):
            return rect, diagnostics
    return None, diagnostics


def _walk_pierced_nodes(node, out, depth=0):
    """遍历 CDP DOM 节点树，包含 closed shadow root 与子 frame 文档。"""
    if not isinstance(node, dict) or depth > 64 or len(out) >= _TURNSTILE_PIERCED_NODE_LIMIT:
        return
    out.append(node)
    for child in node.get("children") or []:
        _walk_pierced_nodes(child, out, depth + 1)
    for root in node.get("shadowRoots") or []:
        _walk_pierced_nodes(root, out, depth + 1)
    content = node.get("contentDocument")
    if content:
        _walk_pierced_nodes(content, out, depth + 1)


def _pierced_node_attr(node, name):
    attributes = node.get("attributes") or []
    for index in range(0, len(attributes) - 1, 2):
        if str(attributes[index]).lower() == name:
            return str(attributes[index + 1])
    return ""


def _turnstile_widget_rect():
    """只读探测 Turnstile 组件的视口矩形；没有可点击的组件时返回 None。"""
    rect, _diagnostics = _turnstile_widget_probe()
    return rect


def _click_turnstile_widget(rect, log_callback=None, cancel_callback=None):
    """用真实鼠标事件点击 Turnstile 复选框；坐标事件可穿透跨域 iframe。"""
    if page is None or not rect:
        return False
    target_x = rect["x"] + min(max(rect["w"] * 0.08, 12.0), 32.0) + random.uniform(-2.0, 2.0)
    target_y = rect["y"] + rect["h"] / 2 + random.uniform(-2.0, 2.0)
    start_x = target_x - 26.0
    start_y = target_y + 20.0
    try:
        for step in range(1, 6):
            page.run_cdp(
                "Input.dispatchMouseEvent",
                type="mouseMoved",
                x=start_x + (target_x - start_x) * step / 5.0,
                y=start_y + (target_y - start_y) * step / 5.0,
                buttons=0,
            )
            sleep_with_cancel(0.02, cancel_callback)
        page.run_cdp(
            "Input.dispatchMouseEvent",
            type="mousePressed",
            x=target_x,
            y=target_y,
            button="left",
            buttons=1,
            clickCount=1,
        )
        sleep_with_cancel(random.uniform(0.05, 0.12), cancel_callback)
        page.run_cdp(
            "Input.dispatchMouseEvent",
            type="mouseReleased",
            x=target_x,
            y=target_y,
            button="left",
            buttons=0,
            clickCount=1,
        )
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] Cloudflare 自动点击失败: {exc}")
        return False
    if log_callback:
        log_callback("[*] 已尝试自动点击 Cloudflare 人机验证复选框")
    return True


def build_profile():
    given_name_pool = [
        "Neo", "Ethan", "Liam", "Noah", "Lucas", "Mason", "Ryan", "Leo",
        "Owen", "Aiden", "Elio", "Aron", "Ivan", "Nolan", "Evan", "Kai",
        "Caleb", "Adam", "Ezra", "Miles", "Logan", "Carter", "Hunter", "Jason",
        "Brian", "Dylan", "Alex", "Colin", "Blake", "Gavin", "Henry", "Julian",
        "Kevin", "Louis", "Marcus", "Nathan", "Oscar", "Peter", "Quinn", "Robin",
        "Simon", "Tristan", "Victor", "Wesley", "Xavier", "Yuri", "Zane", "Felix",
        "Aaron", "Damian",
    ]
    family_name_pool = [
        "Lin", "Wang", "Zhao", "Liu", "Chen", "Zhang", "Xu", "Sun",
        "Guo", "He", "Yang", "Wu", "Zhou", "Tang", "Qin", "Shi",
        "Fang", "Peng", "Cao", "Deng", "Fan", "Fu", "Gao", "Han",
        "Hu", "Jiang", "Kong", "Lu", "Ma", "Nie", "Pan", "Qiao",
        "Ren", "Shao", "Tian", "Xie", "Yan", "Yao", "Yu", "Zeng",
        "Bai", "Duan", "Hou", "Jin", "Kang", "Luo", "Mao", "Song",
        "Wei", "Xiong",
    ]
    given_name = random.choice(given_name_pool)
    family_name = random.choice(family_name_pool)
    password = "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)
    return given_name, family_name, password

def fill_profile_and_submit(timeout=120, log_callback=None, cancel_callback=None):
    given_name, family_name, password = build_profile()
    deadline = time.time() + timeout
    form_filled_once = False

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if not form_filled_once:
            filled = page.run_js(
                """
const givenName = arguments[0];
const familyName = arguments[1];
const password = arguments[2];

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

function setInputValue(input, value) {
    if (!input) return false;
    input.focus();
    input.click();
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.blur();
    return String(input.value || '').trim() === String(value || '').trim();
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"], input[aria-label*="名"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"], input[aria-label*="姓"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"], input[autocomplete="new-password"]');

if (!givenInput || !familyInput || !passwordInput) return 'not-ready';

const ok1 = setInputValue(givenInput, givenName);
const ok2 = setInputValue(familyInput, familyName);
const ok3 = setInputValue(passwordInput, password);

if (!ok1 || !ok2 || !ok3) return 'fill-failed';

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
});

if (submitBtn) {
    return 'ready-to-submit';
}
return 'filled-no-submit';
            """,
                given_name,
                family_name,
                password,
            )

            if filled in ("ready-to-submit", "filled-no-submit"):
                form_filled_once = True
            elif filled == "fill-failed" and log_callback:
                log_callback("[Debug] 资料输入失败，重试中...")
                sleep_with_cancel(0.5, cancel_callback)
                continue
            elif filled == "not-ready":
                sleep_with_cancel(0.5, cancel_callback)
                continue

        turnstile_state = _read_turnstile_state()
        if turnstile_state["state"] in {
            TURNSTILE_LOADING,
            TURNSTILE_WAITING,
            TURNSTILE_FAILED,
        }:
            remaining = max(deadline - time.time(), 0.0)
            getTurnstileToken(
                log_callback=log_callback,
                cancel_callback=cancel_callback,
                timeout=remaining,
            )
            continue

        submit_state = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function buttonText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('value'),
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = buttonText(node).replace(/\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
});
if (!submitBtn) {
    const visibleTexts = buttons.map(buttonText).filter(Boolean).slice(0, 8).join(' | ');
    return 'no-submit-button:' + visibleTexts;
}
return 'ready-to-submit';
            """
        )

        if submit_state == "ready-to-submit":
            _mark_registration_stage("profile_submit")
            submit_state = page.run_js(
                r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function buttonText(node) {
    return [node.innerText, node.textContent, node.getAttribute('value'), node.getAttribute('aria-label'), node.getAttribute('title')]
      .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
const submitBtn = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .find((node) => {
        const t = buttonText(node).replace(/\s+/g, '').toLowerCase();
        return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
    });
if (!submitBtn) return 'submit-disappeared';
submitBtn.focus();
submitBtn.click();
return 'submitted';
                """
            )
            if submit_state == "submitted":
                if log_callback:
                    log_callback(f"[*] 已填写注册资料并提交: {given_name} {family_name}")
                return {"given_name": given_name, "family_name": family_name, "password": password}
        if isinstance(submit_state, str) and submit_state.startswith("no-submit-button") and log_callback:
            visible_buttons = submit_state.split(":", 1)[1] if ":" in submit_state else ""
            suffix = f" 可见按钮: {visible_buttons}" if visible_buttons else ""
            log_callback(f"[Debug] 未找到提交按钮，继续等待页面稳定...{suffix}")

        sleep_with_cancel(0.5, cancel_callback)

    raise Exception("最终注册页资料填写失败")

def wait_for_sso_cookie(timeout=120, log_callback=None, cancel_callback=None):
    deadline = time.time() + timeout
    last_seen_names = set()
    last_submit_retry = 0.0
    final_no_submit_state = ""
    final_no_submit_since = None
    final_no_submit_timeout = 25
    last_wait_exception_message = ""
    last_wait_exception_at = 0.0
    consecutive_wait_errors = 0
    last_consecutive_error_message = ""
    last_wait_exception = None

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            refresh_active_page()
            if page is None:
                sleep_with_cancel(1, cancel_callback)
                continue

            # 仍停留在“完成注册”页时，若 Cloudflare 已通过，周期性重试点击提交
            now = time.time()
            if now - last_submit_retry >= 2.5:
                retried = page.run_js(
                    r"""
const titleHit = !!Array.from(document.querySelectorAll('h1,h2,div,span')).find((el) => {
    const t = (el.textContent || '').replace(/\s+/g, '');
    const lower = t.toLowerCase();
    return t.includes('完成注册') || lower.includes('completeyoursignup') || lower.includes('completesignup');
});
return titleHit ? 'final-page' : 'not-final-page';
                    """
                )

                if retried == "final-page":
                    turnstile_state = _read_turnstile_state()
                    if turnstile_state["state"] in {
                        TURNSTILE_LOADING,
                        TURNSTILE_WAITING,
                        TURNSTILE_FAILED,
                    }:
                        if log_callback:
                            log_callback(
                                f"[Debug] 最终页 Cloudflare 状态: {turnstile_state['state']}, "
                                f"token长度={turnstile_state['token_length']}"
                            )
                        remaining = max(deadline - time.time(), 0.0)
                        getTurnstileToken(
                            log_callback=log_callback,
                            cancel_callback=cancel_callback,
                            timeout=remaining,
                        )
                        last_submit_retry = now
                        continue

                    retried = page.run_js(
                        r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function buttonText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('value'),
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = buttonText(node).replace(/\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
});
if (!submitBtn) {
    const visibleTexts = buttons.map(buttonText).filter(Boolean).slice(0, 8).join(' | ');
    return 'final-page-no-submit:' + visibleTexts;
}
submitBtn.focus();
submitBtn.click();
return 'final-page-clicked-submit';
                        """
                    )
                last_submit_retry = now
                if log_callback and (retried == "final-page-clicked-submit" or (isinstance(retried, str) and retried.startswith("final-page-no-submit"))):
                    log_callback(f"[Debug] 最终页状态: {retried}")
                if isinstance(retried, str) and retried.startswith("final-page-no-submit"):
                    if retried != final_no_submit_state:
                        final_no_submit_state = retried
                        final_no_submit_since = now
                    elif final_no_submit_since and now - final_no_submit_since >= final_no_submit_timeout:
                        raise AccountRetryNeeded(
                            f"最终注册页状态 {final_no_submit_timeout}s 未变化且未找到提交按钮，重试当前账号: {retried}"
                        )
                else:
                    final_no_submit_state = ""
                    final_no_submit_since = None

            cookies = page.cookies(all_domains=True, all_info=True) or []
            for item in cookies:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                else:
                    name = str(getattr(item, "name", "")).strip()
                    value = str(getattr(item, "value", "")).strip()

                if name:
                    last_seen_names.add(name)

                if name == "sso" and value:
                    if log_callback:
                        log_callback("[*] 已获取到 sso cookie")
                    return value
            # A completed polling iteration breaks any previous exception streak.
            consecutive_wait_errors = 0
            last_consecutive_error_message = ""
        except PageDisconnectedError:
            consecutive_wait_errors = 0
            last_consecutive_error_message = ""
            refresh_active_page()
        except AccountRetryNeeded:
            raise
        except ProxyTransportError:
            raise
        except (ContextLostError, JavaScriptError) as exc:
            last_wait_exception = exc
            message = f"{exc.__class__.__name__}: {exc}"
            if message == last_consecutive_error_message:
                consecutive_wait_errors += 1
            else:
                consecutive_wait_errors = 1
                last_consecutive_error_message = message
            if log_callback:
                now = time.time()
                if message != last_wait_exception_message or now - last_wait_exception_at >= 10:
                    log_callback(f"[Debug] 等待 sso cookie 时出现瞬时异常 ({consecutive_wait_errors}/3): {message}")
                    last_wait_exception_message = message
                    last_wait_exception_at = now
            if consecutive_wait_errors >= 3:
                raise RuntimeError(f"等待 sso cookie 连续瞬时异常: {last_wait_exception.__class__.__name__}: {last_wait_exception}") from last_wait_exception
        except Exception:
            raise

        sleep_with_cancel(1, cancel_callback)

    cause = "" if last_wait_exception is None else f"; last_error={last_wait_exception.__class__.__name__}: {last_wait_exception}"
    raise Exception(
        f"等待超时：未获取到 sso cookie。已看到 cookies: {sorted(last_seen_names)}{cause}"
    )


