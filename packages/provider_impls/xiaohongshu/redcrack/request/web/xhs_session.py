import urllib.parse
import json
import time
import uuid
import logging
from typing import Optional

import httpx

from request.web.encrypt.config import xhs_config
from request.web.encrypt.xhs_encrypt import xhs_encrypt
from request.web.exceptions import session_exceptions
from request.web.apis import APIModule
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# getuseragent may not be installed; fallback UA if missing
try:
    from getuseragent import UserAgent
    _has_ua = True
except ImportError:
    _has_ua = False


class XHS_Session:
    def __init__(self, web_session: str = None, proxy_url: str = "", did: str = None, sid: str = None, *args, **kwargs):
        self.apis = APIModule(self)
        self.__xhs_encrypt = xhs_encrypt
        self.__web_session = web_session
        self.__fp = {}

        self._proxy_url = proxy_url

        self._did = did if did else str(uuid.uuid4())
        self._sid = sid
        
        self._session: Optional[httpx.Client] = None
        
    @property  
    def headers(self):
        return dict(self._session.headers) if self._session else {}
        
    @property
    def cookies(self):
        if not self._session:
            return {}
        return dict(self._session.cookies)

    def get_fp(self) -> dict:
        return self.__fp


    def __init_headers(self) -> None:
        initial_headers = xhs_config.get('HEADER', 'DEFAULT_HEADER').copy()
        if _has_ua:
            ua = UserAgent("chrome+edge+firefox")
            initial_headers['User-Agent'] = ua.Random()
        if self._session:
            self._session.headers.update(initial_headers)

    def __init_cookies(self) -> None:
        a1, webId = self.__xhs_encrypt.encrypt_cookie_a1_and_webId()
        
        initial_cookies = {
            'a1': a1,
            'webId': webId,
            'webBuild': xhs_config.get('XHS_VERSION', 'ARTIFACT_VERSION'),
            'xsecappid': xhs_config.get('XHS_VERSION', 'APP_ID'),
            'loadts': str(int(time.time())),
            'abRequestId': str(uuid.uuid4())
        }

        for key, value in initial_cookies.items():
            self._session.cookies.set(key, str(value))

        self.__set_websectiga_and_secPoisonId()

        cookies_dict = self.cookies
        self.__fp = self.__xhs_encrypt.get_fingerprint(
            cookies_dict, 
            self._session.headers.get('User-Agent', '')
        )

        self.__set_gid_and_acw_tc()
        
        if self.__web_session is None:
            self.__set_web_session()
        else:
            self._session.cookies.set("web_session", self.__web_session)

    def initialize(self) -> None:
        """初始化会话 (同步版)"""

        # 代理设置
        proxy: Optional[dict] = None
        if self._proxy_url and not self._proxy_url.startswith('socks'):
            proxy = {"http://": self._proxy_url, "https://": self._proxy_url}

        self._session = httpx.Client(
            headers=xhs_config.get('HEADER', 'DEFAULT_HEADER').copy(),
            timeout=httpx.Timeout(30),
        )
        
        self.__init_headers()
        self.__init_cookies()

        # 如果填写了sid, did, 则尝试自动登录
        if self._sid and self._did:
            self.apis.auth.scan_login()

    def __set_websectiga_and_secPoisonId(self) -> None:
        url = "https://as.xiaohongshu.com/api/sec/v1/scripting"
        data = {"callFrom": "web", "callback": "seccallback"}

        res = self.request(method="post",
                           url=url,
                           data=data,
                           need_encrypt=False)

        res_json = res.json()

        self._session.cookies.set(
            "websectiga", self.__xhs_encrypt.gen_websectiga(res_json.get('data', {}).get('data', ''))
        )
        self._session.cookies.set(
            "sec_poison_id", res_json.get('data', {}).get('secPoisonId', '')
        )

    def __set_gid_and_acw_tc(self) -> None:
        """  注意:xhs两个acw_tc, 他们的域不一样，一个是www.xiaohongshu.com 一个是 edith.xiaohongshu.com
            但由于同名会引发异常，所以不要轻易的访问www.xiaohongshu.com
            API协议请求通常只需要edith.xiaohongshu.com的acw_tc即可 """

        url, data = self.__xhs_encrypt.gen_gid_webprofile_data(self.__fp)

        self.request(method="post",
                     url=url,
                     data=data)


    def __set_web_session(self) -> None:
        url = "https://edith.xiaohongshu.com/api/sns/web/v1/login/activate"
        data = {}

        self.request(method="post",
                     url=url,
                     data=data)


    def __request_encrypt(self, url, params, data) -> None:
        """加密请求"""
        cookies_dict = self.cookies
        a1 = cookies_dict.get('a1', '')
        if not a1:
            return
            
        loadts = str(int(time.time() * 1000))
        self._session.cookies.set("loadts", loadts)
        
        xray = self.__xhs_encrypt.encrypt_headers_xray()
        self._session.headers['x-xray-traceid'] = str(xray)
        
        xb3 = self.__xhs_encrypt.encrypt_header_xb3()
        self._session.headers['x-b3-traceid'] = str(xb3)
        
        url_path = urlparse(url).path
        xs = self.__xhs_encrypt.encrypt_headers_xs(
            a1, int(loadts), url_path, params, data
        )
        self._session.headers['x-s'] = str(xs)

        if not self.__fp:
            self.__fp = self.__xhs_encrypt.get_fingerprint(
                cookies_dict,
                self._session.headers.get('User-Agent', ''),
            )
        else:
            self.__xhs_encrypt.update_fingerprint(self.__fp, cookies_dict, url)
        xsc = self.__xhs_encrypt.encrypt_headers_xsc(a1, self.__fp)
        self._session.headers['x-s-common'] = str(xsc)

        if xrap := self.__xhs_encrypt.encrypt_headers_xrap_param(url, data):
            self._session.headers['x-rap-param'] = str(xrap)

        xt = int(time.time() * 1000)
        self._session.headers['x-t'] = str(xt)

    def __handle_xhs_response_exceptions(self, response: httpx.Response, request_info: str):
        """异常处理"""
        try:
            res_json = response.json()
            res_text = response.text
        except Exception:
            res_json = {}
            res_text = ""

        status_code = response.status_code
        res_msg = res_json.get('msg', '')
        
        cookies_dict = self.cookies
        web_session = cookies_dict.get('web_session')
                
        logger_info = f"{status_code} | {response.request.method} | {request_info}"

        if status_code != 200:
            if status_code in [461, 471]:
                verifyUuid = response.headers.get("verifyuuid", "")
                verifyType = response.headers.get("verifytype", "")
                VerifyCode = int(status_code)

                if str(verifyType) == "124":
                    logger.error(f"需要扫描二维码 | {logger_info}")
                    raise session_exceptions.NeedScanLogin({
                        "verifyUuid": verifyUuid, "verifyType": verifyType,
                        "VerifyCode": VerifyCode, "msg": res_msg,
                    })
                if str(verifyType) == "102":
                    logger.error(f"需要通过滑块旋转验证码 | {logger_info}")
                    raise session_exceptions.NeedSlideVerify({
                        "verifyUuid": verifyUuid, "verifyType": verifyType,
                        "VerifyCode": VerifyCode, "msg": res_msg,
                    })
                logger.error(f"其他安全相关异常 | {res_text} | {logger_info}")
                raise session_exceptions.OtherStatusError("461异常")

            elif status_code == 406:
                logger.error(f"406异常 | {logger_info}")
                raise session_exceptions.OtherStatusError("406异常")

            else:
                logger.error(f"其他异常状态码 | {logger_info}")
                raise session_exceptions.OtherStatusError(f"状态码: {status_code}")

        if res_msg in ["成功", 'ok']:
            logger.info(f"成功 | {logger_info}")
            return response

        # 其他异常匹配
        msg_lower = str(res_msg).lower()
        res_text_lower = str(res_text).lower()

        if "禁言" in msg_lower or "被禁言" in msg_lower:
            logger.warning(f"禁言 | {web_session} | {res_msg} | {logger_info}")
            raise session_exceptions.MutedError(res_msg)

        elif "登录已过期" in res_text_lower or "登录超时" in msg_lower:
            logger.warning(f"掉线 | {web_session} | web_session 登录超时 | {logger_info}")
            raise session_exceptions.LoginTimeOut(res_msg)

        elif "删除" in res_text_lower:
            logger.warning(f"笔记/评论被删除 | {logger_info}")
            raise session_exceptions.TaskDeleteError(res_msg)

        elif "无权限访问" in msg_lower:
            logger.warning(f"过期 | {web_session} | web_session 没有权限访问 | {logger_info}")
            raise session_exceptions.PermissionError(res_msg)

        elif "违规情形" in msg_lower or "被封号" in msg_lower or "封号" in msg_lower:
            logger.warning(f"封号 | {web_session} | {res_msg} | {logger_info}")
            raise session_exceptions.BannedError(res_msg)

        elif "用户已关闭评论艾特" in msg_lower:
            logger.warning(f"用户已关闭评论艾特 | {logger_info}")
            raise session_exceptions.UserCloseCommentAtError(res_msg)

        elif "对方设置" in msg_lower or "无法发布评论" in msg_lower:
            logger.warning(f"对方设置你无法评论 | {logger_info}")
            raise session_exceptions.CantCommentError(res_msg)

        elif "blockedps" in res_text_lower:
            logger.warning(f"封号 | {web_session} | blockedPs | {logger_info}")
            raise session_exceptions.BannedError(res_msg)

        else:
            logger.error(f"未捕获异常 | {logger_info} | {res_json}")
            raise session_exceptions.OtherMessagesError(res_text)

    def request(self,
                method: str,
                url: str,
                max_retries=5,
                retry_delay=0.1,
                is_catch_response_exception=True,
                need_encrypt=True,
                update_headers=None,
                **kwargs) -> httpx.Response:
        """统一请求方法 - 确保资源正确管理"""
        # 保存原始参数用于重试
        original_params = dict(
            method=method, url=url, max_retries=max_retries, retry_delay=retry_delay,
            is_catch_response_exception=is_catch_response_exception,
            need_encrypt=need_encrypt,
            update_headers=update_headers,
            kwargs=kwargs
        )

        params = kwargs.get('params')
        data = kwargs.get('data')
        
        # 处理数据
        request_data = None
        if data is not None and data != 'null':
            request_data = json.dumps(data, separators=(',', ':'))

        # 构建URL
        full_url = url
        if params:
            full_url += '?' + urllib.parse.urlencode(params).replace('%2C', ',')

        # 日志信息预处理（还原原始格式）
        data_string = f" | data:{json.dumps(request_data)[:200]}" if request_data else ""
        params_string = f" | params:{json.dumps(params)[:200]}" if params else ""
        request_info = f"{full_url}{data_string}{params_string}"

        # 处理加密相关
        if need_encrypt:
            self.__request_encrypt(url, params, data if data != 'null' else None)

        # 头信息
        headers = dict(self._session.headers)
        if update_headers:
            headers.update(update_headers)

        # 重试逻辑
        for attempt in range(max_retries):
            try:
                response = self._session.request(
                    method=method.upper(),
                    url=full_url,
                    content=request_data,
                    headers=headers,
                )

                if is_catch_response_exception:
                    return self.__handle_xhs_response_exceptions(response, request_info)
                return response
                    
            except httpx.HTTPError as e:
                logger.warning(f"尝试第 {attempt + 1}/{max_retries} 次时出错: {request_info} - {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    logger.error(f"重试 {max_retries} 次后仍然失败: {request_info}")
                    logger.exception(e)
                    raise session_exceptions.RequestMaxRetryError(str(e))

            except session_exceptions.NeedScanLogin:
                if self._sid is None:
                    logger.error(f"需要扫描二维码, 但由于没有设置app的sid, 无法自动通过: {request_info}")
                    raise

                self.apis.auth.pass_scan_124(
                    verifyType=None,
                    verifyBiz=None,
                    verifyUuid=None,
                    sourceSite=f"https://www.xiaohongshu.com/explore",
                    did=self._did,
                    sid=self._sid
                )
                logger.success(f"扫码过验证码成功, 重新提交该次请求 | {request_info}")

                return self.request(
                    original_params['method'],
                    original_params['url'],
                    original_params['max_retries'],
                    original_params['retry_delay'],
                    original_params['is_catch_response_exception'],
                    original_params['need_encrypt'],
                    original_params['update_headers'],
                    **original_params['kwargs']
                )

            except Exception as e:
                logger.error(f"请求异常: {request_info}")
                logger.exception(e)
                raise

    def close_session(self):
        """安全关闭会话"""
        if self._session:
            self._session.close()
            self._session = None


def create_xhs_session(
    web_session: str = None,
    proxy: str = None,
    did: str = None,
    sid: str = None,
) -> XHS_Session:
    """创建XHS_Session实例 (同步版)"""
    session = XHS_Session(web_session, proxy_url=proxy, did=did, sid=sid)
    session.initialize()
    return session
