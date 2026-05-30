# -*- coding: utf-8 -*-
"""
SeleniumUtils (undetected-chromedriver 기반 유틸)

목적
- Windows 환경에서 Chrome 실행 파일 경로를 탐색하고(레지스트리/기본 경로),
  undetected_chromedriver(uc)로 안정적으로 드라이버를 기동한다.
- 필요 시 CDP(Network) + performance log를 이용해 특정 API 호출의
  request/response/body(json)까지 캡처한다.
- 임시 프로필을 생성/정리하여 실행 간 세션 충돌을 줄인다.

주의
- performance log 캡처는 Chrome/드라이버 조합에 따라 지원 여부가 달라질 수 있다.
- Network.getResponseBody는 로딩 완료(loadingFinished) 이후에만 정상 동작하는 편이다.

===============================================================================
[Performance Log + CDP 기반 네트워크 캡처 설명]

이 모듈은 Chrome DevTools Protocol(CDP)과 performance log를 함께 사용하여
브라우저 내부에서 발생하는 특정 API 요청/응답을 감지하고,
최종적으로 응답 body(JSON 등)까지 추출하기 위한 유틸리티이다.

--------------------------------------------------------------------------------
1. CDP (Chrome DevTools Protocol)

CDP는 Chrome 개발자도구(F12)가 내부적으로 사용하는 디버깅 프로토콜이다.
Selenium에서는 driver.execute_cdp_cmd()를 통해 직접 명령을 호출할 수 있다.

주요 사용 예:
- Network.enable
    → 네트워크 이벤트 도메인 활성화
- Network.getResponseBody
    → 특정 requestId의 응답 body 조회

CDP는 "명령 실행"과 "응답 body 직접 조회"에 강점이 있다.

--------------------------------------------------------------------------------
2. Performance Log

Chrome에서 발생하는 네트워크 이벤트를 로그 형태로 수집하는 기능이다.

driver.get_log("performance") 로 읽을 수 있으며,
다음과 같은 이벤트들이 포함된다:

- Network.requestWillBeSent   (요청 발생)
- Network.responseReceived    (응답 헤더 도착)
- Network.loadingFinished     (다운로드 완료)
- Network.loadingFailed       (다운로드 실패)

Performance log는 "이벤트 감지 및 requestId 추적"에 사용된다.

--------------------------------------------------------------------------------
3. 왜 둘을 같이 사용하는가?

Performance Log만 사용하면:
- 요청/응답 메타 정보(URL, status 등)는 확인 가능
- 하지만 응답 body는 직접 얻기 어렵다

CDP만 사용하면:
- 응답 body는 가져올 수 있음
- 하지만 어떤 requestId가 목표 요청인지 찾는 과정이 필요함

따라서 일반적인 실무 패턴은 다음과 같다:

1) Performance log에서 특정 URL을 가진 요청을 탐지한다.
2) 해당 요청의 requestId를 확보한다.
3) CDP(Network.getResponseBody)로 body를 가져온다.

본 유틸은 위 3단계를 자동화하여,
특정 API의 JSON 응답을 안정적으로 추출하는 것을 목적으로 한다.

--------------------------------------------------------------------------------
4. 사용 전제 조건

- capture_enabled=True 설정 필요
- ChromeOptions에 performance logging 활성화 필요
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
- CDP Network.enable 호출 필요

--------------------------------------------------------------------------------
5. 주요 사용 목적

- 화면에 렌더링되지 않는 내부 API(JSON) 응답 추출
- F12 네트워크 탭에 보이는 요청 자동 추적
- 백엔드 응답 기반 데이터 수집

===============================================================================
"""

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import winreg
from typing import Optional, Dict, Any, List, Set, Union, TypedDict, Callable, Tuple

import undetected_chromedriver as uc
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    ElementClickInterceptedException,
    ElementNotInteractableException,
    InvalidSelectorException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


class ApiRequestMeta(TypedDict, total=False):
    requestId: str
    url: str
    method: str
    headers: Any
    postData: Optional[str]


class ApiBodyMeta(TypedDict, total=False):
    requestId: str
    url: str
    status: int
    mimeType: Optional[str]
    bodyText: str


class SeleniumUtils:
    """
    Selenium/undetected-chromedriver 공용 유틸 클래스.

    주요 기능
    - Chrome 경로 탐색 / 버전 major 감지
    - uc.ChromeOptions 구성
    - uc 드라이버 기동/종료 및 임시 프로필 정리
    - CDP(Network) enable 및 performance log 기반 API 캡처
    """

    def __init__(
            self,
            headless: bool = False,
            debug: Optional[bool] = None,
            log_func: Optional[Callable[[str], None]] = None,  # === 신규 ===
    ):
        """
        Args:
            headless: headless 실행 여부 (Chrome '--headless=new' 사용)
            debug: 디버깅 로그 출력 여부
                   None이면 환경변수 SELENIUMUTILS_DEBUG로 결정(1/true/y/yes)
        """
        self.headless = bool(headless)

        # WebDriver 인스턴스 (start_driver 호출 후 유효)
        self.driver: Optional[WebDriver] = None

        # 가장 최근 발생한 예외(내부적으로 잡아두는 용도)
        self.last_error: Optional[Exception] = None

        # debug 옵션 자동 결정
        if debug is None:
            debug = os.environ.get("SELENIUMUTILS_DEBUG", "").strip().lower() in ("1", "true", "y", "yes")
        self.debug = bool(debug)

        self.log_func = log_func

        # user-data-dir로 사용할 프로필 디렉토리 (임시 생성)
        self._profile_dir: Optional[str] = None

        # Network/performance 캡처 기능 on/off
        self.capture_enabled: bool = False

        # 이미지 로딩 차단(속도/트래픽 절감용)
        self.block_images: bool = False

        # CDP Network.enable 호출 여부(세션 단위)
        self._net_enabled: bool = False

        # performance log 지원 여부 캐시(드라이버별 지원 다름)
        self._perf_supported: Optional[bool] = None

        self._quit_done = False  # === 신규 ===

        # === 신규 === 기본 화면 모드/크기 설정
        # 기존 호출부 호환을 위해 기본은 browser + 600x700 유지
        self.default_view_mode: str = "browser"
        self.default_browser_window_size: Tuple[int, int] = (600, 700)

        # 모바일 기본값
        self.default_mobile_window_size: Tuple[int, int] = (520, 980)
        self.default_mobile_metrics: Tuple[int, int] = (430, 932)
        self.default_mobile_user_agent: str = (
            "Mozilla/5.0 (Linux; Android 13; SM-S918N) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/139.0.0.0 Mobile Safari/537.36"
        )

    # ---------------------------------------------------------------------
    # Logging / Config
    # ---------------------------------------------------------------------
    def _log(self, *args: Any, force: bool = False) -> None:
        """
        debug 모드일 때만 로그 출력.
        - log_func가 있으면 UI로 전달
        - 없으면 콘솔 print fallback
        """
        if not (self.debug or force):
            return

        msg = "[SeleniumUtils] " + " ".join(str(x) for x in args)

        # === 신규 === UI 로그로 전달
        if self.log_func:
            try:
                self.log_func(msg)
                return
            except Exception:
                # log_func가 터져도 크롤링이 죽지 않도록 fallback
                pass

        # fallback
        print(msg)

    def set_capture_options(self, enabled: bool, block_images: Optional[bool] = None) -> None:
        """
        네트워크 캡처 옵션 설정.

        Args:
            enabled: performance log + CDP 캡처 활성화 여부
            block_images: 이미지 로딩 차단 여부(옵션). None이면 기존값 유지
        """
        self.capture_enabled = bool(enabled)
        if block_images is not None:
            self.block_images = bool(block_images)

    # ---------------------------------------------------------------------
    # View mode
    # ---------------------------------------------------------------------
    def set_default_view_config(
            self,
            view_mode: str = "browser",
            browser_window_size: Optional[Tuple[int, int]] = None,
            mobile_window_size: Optional[Tuple[int, int]] = None,
            mobile_metrics: Optional[Tuple[int, int]] = None,
            mobile_user_agent: Optional[str] = None,
    ) -> None:
        """
        기본 화면 모드/크기를 설정한다.
        - 기존 호출부는 start_driver()만 써도 default 값으로 동작
        - 새 호출부는 start_driver(...override...)로 덮어쓰기 가능
        """
        mode = str(view_mode or "browser").strip().lower()
        if mode not in ("browser", "mobile"):
            mode = "browser"

        self.default_view_mode = mode

        if browser_window_size:
            self.default_browser_window_size = (
                int(browser_window_size[0]),
                int(browser_window_size[1]),
            )

        if mobile_window_size:
            self.default_mobile_window_size = (
                int(mobile_window_size[0]),
                int(mobile_window_size[1]),
            )

        if mobile_metrics:
            self.default_mobile_metrics = (
                int(mobile_metrics[0]),
                int(mobile_metrics[1]),
            )

        if mobile_user_agent:
            self.default_mobile_user_agent = str(mobile_user_agent)

    def _normalize_view_mode(self, view_mode: Optional[str]) -> str:
        mode = str(view_mode or self.default_view_mode or "browser").strip().lower()
        if mode not in ("browser", "mobile"):
            mode = "browser"
        return mode

    def _resolve_view_config(
            self,
            view_mode: Optional[str] = None,
            window_size: Optional[Tuple[int, int]] = None,
            mobile_metrics: Optional[Tuple[int, int]] = None,
            mobile_user_agent: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        최종 화면 설정값을 계산한다.
        - 기존 호출: browser + (600,700)
        - 신규 호출: view_mode/window_size/mobile_metrics override 가능
        """
        final_view_mode = self._normalize_view_mode(view_mode)

        if final_view_mode == "mobile":
            final_window_size = window_size or self.default_mobile_window_size
            final_mobile_metrics = mobile_metrics or self.default_mobile_metrics
        else:
            final_window_size = window_size or self.default_browser_window_size
            final_mobile_metrics = mobile_metrics or self.default_mobile_metrics

        return {
            "view_mode": final_view_mode,
            "window_size": (
                int(final_window_size[0]),
                int(final_window_size[1]),
            ),
            "mobile_metrics": (
                int(final_mobile_metrics[0]),
                int(final_mobile_metrics[1]),
            ),
            "mobile_user_agent": str(mobile_user_agent or self.default_mobile_user_agent),
        }

    # ---------------------------------------------------------------------
    # Profile handling
    # ---------------------------------------------------------------------
    def _new_tmp_profile(self) -> str:
        """
        임시 user-data-dir 프로필 디렉토리를 생성한다.

        Returns:
            생성된 프로필 경로
        """
        base = os.path.join(tempfile.gettempdir(), "selenium_profiles")
        os.makedirs(base, exist_ok=True)

        # 실행마다 UUID로 고유 폴더 생성(충돌 방지)
        path = os.path.join(base, f"profile_{uuid.uuid4().hex}")
        os.makedirs(path, exist_ok=True)
        return path

    # ---------------------------------------------------------------------
    # Chrome discovery / version
    # ---------------------------------------------------------------------
    def _find_chrome_exe_windows(self) -> Optional[str]:
        """
        Windows에서 Chrome 실행 파일 경로를 최대한 탐색한다.
        우선순위:
        1) uc.find_chrome_executable()
        2) ProgramFiles/LocalAppData 기본 설치 경로
        3) 레지스트리 App Paths

        Returns:
            chrome.exe 절대 경로 또는 None
        """
        # 1) uc 내장 탐색
        try:
            p = uc.find_chrome_executable()
            if p and os.path.isfile(p):
                return p
        except Exception:
            pass

        # 2) 대표 설치 경로 후보
        pf = os.environ.get("ProgramFiles")
        pf86 = os.environ.get("ProgramFiles(x86)")
        local = os.environ.get("LOCALAPPDATA")

        candidates: List[str] = []
        if pf:
            candidates.append(os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"))
        if pf86:
            candidates.append(os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe"))
        if local:
            candidates.append(os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"))

        # 3) 레지스트리 App Paths
        reg_paths = [
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe", ""),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe", ""),
        ]
        for hive, subkey, value_name in reg_paths:
            try:
                with winreg.OpenKey(hive, subkey) as k:
                    v, _ = winreg.QueryValueEx(k, value_name)
                    if v and os.path.isfile(v):
                        return v
            except Exception:
                pass

        # 후보 경로 순회
        for p in candidates:
            if p and os.path.isfile(p):
                return p

        return None

    def _detect_chrome_major(self, chrome_exe: Optional[str]) -> Optional[int]:
        """
        chrome.exe의 실제 major 버전을 최대한 안정적으로 추출한다.
        - 1차: chrome.exe --version
        - 2차: PowerShell VersionInfo.ProductVersion
        """
        if not chrome_exe or not os.path.isfile(chrome_exe):
            return None

        # 1차: chrome.exe --version
        try:
            out = subprocess.check_output(
                [chrome_exe, "--version"],
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="ignore",
            )
            m = re.search(r"(\d+)\.", out or "")
            if m:
                return int(m.group(1))
        except Exception as e:
            self._log("chrome --version failed:", str(e))

        # 2차: PowerShell 파일 버전 조회
        try:
            safe_path = str(chrome_exe).replace("'", "''")
            ps_cmd = f"(Get-Item '{safe_path}').VersionInfo.ProductVersion"
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="ignore",
            )
            m = re.search(r"(\d+)\.", out or "")
            if m:
                return int(m.group(1))
        except Exception as e:
            self._log("chrome powershell version failed:", str(e))

        return None

    # ---------------------------------------------------------------------
    # UC cache handling
    # ---------------------------------------------------------------------
    def wipe_uc_driver_cache(self) -> None:
        """
        undetected_chromedriver가 내려받아 캐시해두는 드라이버/패치 파일을 삭제한다.
        - 드라이버 생성 실패 시 '깨진 캐시' 가능성을 줄이기 위한 재시도 전략으로 사용.
        """
        bases = [
            os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "undetected_chromedriver"),
            os.path.join(os.path.expanduser("~"), "AppData", "Local", "undetected_chromedriver"),
        ]
        for base in bases:
            try:
                if os.path.isdir(base):
                    shutil.rmtree(base, ignore_errors=True)
                    self._log("uc cache removed:", base)
            except Exception as e:
                self._log("uc cache remove failed:", base, str(e))

    # ---------------------------------------------------------------------
    # Options building
    # ---------------------------------------------------------------------
    def _build_options(
            self,
            chrome_exe: Optional[str],
            view_mode: Optional[str] = None,
            window_size: Optional[Tuple[int, int]] = None,
            mobile_metrics: Optional[Tuple[int, int]] = None,
            mobile_user_agent: Optional[str] = None,
    ) -> uc.ChromeOptions:
        """
        uc.ChromeOptions를 구성한다.
        - locale, 팝업/첫실행 비활성화, 로그 레벨, 최대화 등
        - block_images/capture_enabled/profile_dir 적용
        - browser/mobile 모드 및 창 크기 적용

        Args:
            chrome_exe: chrome.exe 경로(있으면 binary_location 지정)
            view_mode: browser | mobile
            window_size: 실제 브라우저 창 크기
            mobile_metrics: 모바일 내부 viewport 크기
            mobile_user_agent: 모바일 UA

        Returns:
            uc.ChromeOptions 객체
        """
        opts = uc.ChromeOptions()

        final_cfg = self._resolve_view_config(
            view_mode=view_mode,
            window_size=window_size,
            mobile_metrics=mobile_metrics,
            mobile_user_agent=mobile_user_agent,
        )

        final_view_mode = final_cfg["view_mode"]
        final_window_size = final_cfg["window_size"]
        final_mobile_metrics = final_cfg["mobile_metrics"]
        final_mobile_user_agent = final_cfg["mobile_user_agent"]

        # === 기본 실행 옵션(실무에서 흔히 세팅) ===
        opts.add_argument("--lang=ko-KR")
        # 브라우저 기본 언어를 한국어로 설정 (사이트 언어/로케일 영향 방지)

        opts.add_argument("--disable-popup-blocking")
        # Chrome 기본 팝업 차단 기능 비활성화 (로그인/인증 팝업 막힘 방지)

        opts.add_argument("--no-first-run")
        # Chrome 최초 실행 시 뜨는 환영/초기 설정 화면 방지

        opts.add_argument("--no-default-browser-check")
        # 기본 브라우저 설정 여부 확인 팝업 방지

        opts.add_argument("--disable-dev-shm-usage")
        # /dev/shm(shared memory) 대신 디스크 사용 (리눅스/도커 환경에서 크래시 방지용)
        # Windows에서는 영향 거의 없음

        opts.add_argument("--disable-quic")
        # QUIC 프로토콜 비활성화 (일부 네트워크/프록시 환경에서 불안정 방지)

        opts.add_argument("--log-level=3")
        # Chrome 내부 로그 레벨 최소화 (0=verbose ~ 3=error만 출력)

        # === 신규 === 화면 모드별 창 크기 적용
        opts.add_argument(f"--window-size={final_window_size[0]},{final_window_size[1]}")

        # Headless 모드
        if self.headless:
            opts.add_argument("--headless=new")

        # 이미지 로딩 차단(속도 향상/트래픽 절감)
        if self.block_images:
            opts.add_experimental_option(
                "prefs",
                {
                    "profile.managed_default_content_settings.images": 2,
                    "profile.default_content_setting_values.notifications": 2,
                },
            )

        # 모바일 모드일 때 mobile emulation 적용
        if final_view_mode == "mobile":
            opts.add_argument(
                f"--user-agent={final_mobile_user_agent}"
            )

        # Network/performance 캡처를 위해 performance log 활성화
        # (드라이버/브라우저 조합에 따라 지원 안 될 수 있음)
        if self.capture_enabled:
            opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})

        # 임시 프로필 디렉토리(user-data-dir)
        if self._profile_dir:
            opts.add_argument(f"--user-data-dir={self._profile_dir}")

        # chrome binary 지정(탐색 성공 시)
        if chrome_exe:
            try:
                opts.binary_location = chrome_exe
            except Exception:
                pass

        return opts

    # ---------------------------------------------------------------------
    # Mobile emulation
    # ---------------------------------------------------------------------
    def _apply_mobile_emulation(
            self,
            width: int = 430,
            height: int = 932,
            user_agent: Optional[str] = None,
    ) -> None:
        """
        드라이버 생성 직후 CDP로 모바일 UA / viewport / touch를 강제 적용한다.
        - mobileEmulation 옵션만으로 부족한 사이트 보정용
        """
        if not self.driver:
            return

        ua = str(user_agent or self.default_mobile_user_agent)

        try:
            self.driver.execute_cdp_cmd(
                "Emulation.setUserAgentOverride",
                {
                    "userAgent": ua,
                    "platform": "Android",
                    "acceptLanguage": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                },
            )

            self.driver.execute_cdp_cmd(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": int(width),
                    "height": int(height),
                    "deviceScaleFactor": 3,
                    "mobile": True,
                },
            )

            self.driver.execute_cdp_cmd(
                "Emulation.setTouchEmulationEnabled",
                {
                    "enabled": True,
                    "maxTouchPoints": 5,
                },
            )

            self._log("mobile emulation applied:", width, height)

        except Exception as e:
            self._log("mobile emulation apply failed:", str(e))

    # ---------------------------------------------------------------------
    # CDP / performance log
    # ---------------------------------------------------------------------
    def enable_capture_now(self) -> bool:
        """
        CDP Network domain을 enable 한다.
        - performance log만 켜도 request/response 이벤트는 찍히지만,
          getResponseBody 등 일부는 Network.enable이 필요할 때가 많아 같이 호출한다.

        Returns:
            성공 여부
        """
        if not self.driver:
            return False
        try:
            self.driver.execute_cdp_cmd("Network.enable", {})
            self._net_enabled = True
            return True
        except Exception as e:
            self._net_enabled = False
            self._log("Network.enable failed:", str(e))
            return False

    def _ensure_perf_supported(self) -> bool:
        """
        performance log를 driver.get_log('performance')로 읽을 수 있는지 확인한다.
        지원 여부는 드라이버/Chrome 조합에 따라 달라질 수 있으므로 캐시한다.

        Returns:
            지원 여부
        """
        if self._perf_supported is not None:
            return bool(self._perf_supported)

        if not self.driver:
            self._perf_supported = False
            return False

        try:
            _ = self.driver.get_log("performance")
            self._perf_supported = True
        except Exception as e:
            self._perf_supported = False
            self._log("performance log not supported:", str(e))

        return bool(self._perf_supported)

    def _get_response_body(self, request_id: str) -> Optional[str]:
        """
        CDP Network.getResponseBody로 특정 requestId의 response body를 가져온다.

        Args:
            request_id: CDP requestId

        Returns:
            response body(utf-8 문자열) 또는 None

        Note:
            base64Encoded가 true인 경우 디코딩이 필요하다.
        """
        if not request_id:
            return None
        if not self.driver:
            return None

        try:
            res = self.driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id})
            if not isinstance(res, dict):
                return None

            body = res.get("body")
            if body is None:
                return None

            # 일부 응답은 base64로 인코딩되어 온다.
            if res.get("base64Encoded"):
                return base64.b64decode(body).decode("utf-8", "replace")

            return str(body)
        except Exception:
            return None

    # ---------------------------------------------------------------------
    # Network capture helpers
    # ---------------------------------------------------------------------
    def wait_api_request(
            self,
            url_contains: str,
            query_contains: Optional[str] = None,
            timeout_sec: float = 15.0,
            poll: float = 0.2,
    ) -> Optional[ApiRequestMeta]:
        """
        performance log에서 특정 API 요청(requestWillBeSent)을 탐지한다.

        Args:
            url_contains: URL에 포함되어야 하는 문자열(필수)
            query_contains: URL/로그 메시지에 추가로 포함되어야 하는 문자열(옵션)
            timeout_sec: 최대 대기 시간(초)
            poll: 폴링 간격(초)

        Returns:
            request 메타(dict): requestId/url/method/headers/postData 또는 None

        전제 조건:
            - set_capture_options(enabled=True)로 capture_enabled가 true여야 한다.
            - Network.enable 및 performance log 지원이 필요하다.
        """
        if not self.capture_enabled:
            return None
        if not self.driver:
            return None
        if not self._net_enabled and not self.enable_capture_now():
            return None
        if not self._ensure_perf_supported():
            return None

        t0 = time.time()
        while time.time() - t0 < timeout_sec:
            logs = self.driver.get_log("performance")

            # performance log는 JSON 문자열을 담고 있는 'message' 필드를 가진 dict 리스트 형태
            for row in logs or []:
                msg = row.get("message") if isinstance(row, dict) else None
                if not msg:
                    continue

                # 빠른 필터링(문자열 포함 여부로 1차 거르기)
                if "Network.requestWillBeSent" not in msg:
                    continue
                if url_contains not in msg:
                    continue
                if query_contains and query_contains not in msg:
                    continue

                # 메시지 파싱
                j = json.loads(msg)
                m = (j or {}).get("message") or {}
                if m.get("method") != "Network.requestWillBeSent":
                    continue

                params = m.get("params") or {}
                req = params.get("request") or {}
                url = req.get("url") or ""

                # 최종적으로 URL 기준으로 재검증
                if url_contains not in url:
                    continue
                if query_contains and query_contains not in url:
                    continue

                return {
                    "requestId": params.get("requestId") or "",
                    "url": url,
                    "method": req.get("method") or "",
                    "headers": req.get("headers"),
                    "postData": req.get("postData"),
                }

            time.sleep(poll)

        return None

    def wait_api_body(
            self,
            url_contains: str,
            query_contains: Optional[str] = None,
            timeout_sec: float = 15.0,
            poll: float = 0.2,
            require_status_200: bool = True,
    ) -> Optional[ApiBodyMeta]:
        """
        performance log + CDP를 이용해 특정 API 응답 body를 가져온다.

        처리 흐름
        1) responseReceived에서 requestId/상태/URL을 candidates에 저장
        2) loadingFinished(또는 loadingFailed)로 완료 여부를 추적
        3) 완료된 requestId에 대해 Network.getResponseBody로 실제 body를 가져옴

        Args:
            url_contains: URL에 포함되어야 하는 문자열(필수)
            query_contains: URL/로그 메시지에 추가로 포함되어야 하는 문자열(옵션)
            timeout_sec: 최대 대기 시간(초)
            poll: 폴링 간격(초)
            require_status_200: True면 status=200만 허용

        Returns:
            dict: requestId/url/status/mimeType/bodyText 또는 None
        """
        if not self.capture_enabled:
            return None
        if not self.driver:
            return None
        if not self._net_enabled and not self.enable_capture_now():
            return None
        if not self._ensure_perf_supported():
            return None

        # responseReceived에서 잡은 후보들(requestId -> meta)
        candidates: Dict[str, ApiBodyMeta] = {}

        # 로딩 완료/실패 집합
        finished: Set[str] = set()
        failed: Set[str] = set()

        t0 = time.time()
        while time.time() - t0 < timeout_sec:
            logs = self.driver.get_log("performance")

            for row in logs or []:
                msg = row.get("message") if isinstance(row, dict) else None
                if not msg:
                    continue

                # === responseReceived 탐지: response 메타 확보 ===
                if "Network.responseReceived" in msg and (url_contains in msg) and (
                        query_contains is None or query_contains in msg
                ):
                    j = json.loads(msg)
                    m = (j or {}).get("message") or {}
                    if m.get("method") != "Network.responseReceived":
                        continue

                    params = m.get("params") or {}
                    resp = params.get("response") or {}
                    url = resp.get("url") or ""

                    # URL 기준 필터
                    if url_contains not in url:
                        continue
                    if query_contains and query_contains not in url:
                        continue

                    status = int(resp.get("status") or 0)
                    if require_status_200 and status != 200:
                        # 200만 받도록 설정되어 있으면 다른 상태는 스킵
                        continue

                    rid = params.get("requestId")
                    if not rid:
                        continue

                    candidates[str(rid)] = {
                        "requestId": str(rid),
                        "url": url,
                        "status": status,
                        "mimeType": resp.get("mimeType"),
                    }
                    continue

                # === 로딩 완료/실패 추적 ===
                if ("Network.loadingFinished" in msg) or ("Network.loadingFailed" in msg):
                    j = json.loads(msg)
                    m = (j or {}).get("message") or {}
                    method = m.get("method")
                    params = m.get("params") or {}
                    rid = params.get("requestId")
                    if not rid:
                        continue

                    rid_s = str(rid)
                    if method == "Network.loadingFinished":
                        finished.add(rid_s)
                    elif method == "Network.loadingFailed":
                        failed.add(rid_s)

            # === 완료된 후보에 대해 body 수집 ===
            for rid, meta in list(candidates.items()):
                if rid in failed:
                    candidates.pop(rid, None)
                    continue

                if rid not in finished:
                    continue

                body_text = self._get_response_body(rid)
                if body_text:
                    out: ApiBodyMeta = dict(meta)
                    out["bodyText"] = body_text
                    return out

            time.sleep(poll)

        return None

    def wait_api_json(
            self,
            url_contains: str,
            query_contains: Optional[str] = None,
            timeout_sec: float = 15.0,
            poll: float = 0.2,
            require_status_200: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """
        wait_api_body로 받은 response bodyText를 JSON으로 파싱해 반환한다.

        Args:
            url_contains: URL 포함 문자열
            query_contains: 추가 포함 문자열
            timeout_sec: 최대 대기 시간
            poll: 폴링 간격
            require_status_200: status 200만 허용 여부

        Returns:
            파싱된 JSON(dict) 또는 None
        """
        hit = self.wait_api_body(
            url_contains=url_contains,
            query_contains=query_contains,
            timeout_sec=timeout_sec,
            poll=poll,
            require_status_200=require_status_200,
        )
        if not hit:
            return None

        text = hit.get("bodyText") or ""
        if not text:
            return None

        try:
            return json.loads(text)
        except Exception:
            return None

    # ---------------------------------------------------------------------
    # Diagnostics
    # ---------------------------------------------------------------------
    def dump_env(self) -> Dict[str, Any]:
        """
        실행 환경/버전/설정 정보를 진단용으로 반환한다.

        Returns:
            chrome 경로/major, 프로필 경로, headless/capture/block_images,
            selenium/uc 버전 등을 담은 dict
        """
        chrome_exe = self._find_chrome_exe_windows()
        info: Dict[str, Any] = {
            "chrome_exe": chrome_exe,
            "chrome_major": self._detect_chrome_major(chrome_exe),
            "profile_dir": self._profile_dir,
            "headless": self.headless,
            "capture_enabled": self.capture_enabled,
            "block_images": self.block_images,
            # === 신규 ===
            "default_view_mode": self.default_view_mode,
            "default_browser_window_size": self.default_browser_window_size,
            "default_mobile_window_size": self.default_mobile_window_size,
            "default_mobile_metrics": self.default_mobile_metrics,
        }
        try:
            import selenium
            info["selenium_version"] = getattr(selenium, "__version__", "")
        except Exception:
            info["selenium_version"] = ""
        try:
            info["uc_version"] = getattr(uc, "__version__", "")
        except Exception:
            info["uc_version"] = ""
        return info

    # ---------------------------------------------------------------------
    # Driver lifecycle
    # ---------------------------------------------------------------------
    def _safe_quit_driver(self) -> None:
        """
        driver.quit()를 안전하게 수행한다(예외 무시).
        - quit 중 예외가 나더라도 이후 cleanup이 진행되도록 보호한다.
        """
        d = self.driver
        self.driver = None
        if not d:
            return
        try:
            d.quit()
        except Exception:
            pass

    def start_driver(
            self,
            timeout: int = 30,
            force_major: Optional[int] = None,
            view_mode: Optional[str] = None,  # === 신규 ===
            window_size: Optional[Tuple[int, int]] = None,  # === 신규 ===
            mobile_metrics: Optional[Tuple[int, int]] = None,  # === 신규 ===
            mobile_user_agent: Optional[str] = None,  # === 신규 ===
    ) -> WebDriver:
        """
        uc.Chrome 드라이버를 기동한다.
        - 임시 프로필 생성 후 user-data-dir로 지정
        - Chrome exe 탐색 후 options에 반영
        - 현재 설치된 Chrome major를 자동 감지해 version_main에 적용
        - 감지 실패/버전 불일치 시 에러 메시지에서 실제 Chrome major를 추출해 재시도
        - driver 생성 실패 시 uc 캐시 삭제 후 1회 재시도
        - 기존 호출부는 start_driver() 그대로 사용 가능
        - 신규 호출부는 browser/mobile 및 크기 override 가능

        Args:
            timeout: page_load_timeout (초)
            force_major: 강제 major 버전(옵션). None이면 현재 Chrome 버전 자동 감지
            view_mode: "browser" | "mobile"
            window_size: 실제 브라우저 창 크기 (w, h)
            mobile_metrics: 모바일 내부 viewport 크기 (w, h)
            mobile_user_agent: 모바일 UA

        Returns:
            생성된 WebDriver(uc.Chrome)

        Raises:
            드라이버 생성이 최종 실패할 경우 예외를 그대로 raise
        """

        self.cleanup_old_profiles(older_than_hours=24)

        # === 신규 === quit 후 재시작 가능하도록 초기화
        self._quit_done = False

        # 실행마다 새 프로필(세션/락 충돌 방지)
        self._profile_dir = self._new_tmp_profile()

        chrome_exe = self._find_chrome_exe_windows()
        detected_major = self._detect_chrome_major(chrome_exe)
        major = int(force_major) if force_major else detected_major

        self._log(
            "chrome_exe:",
            chrome_exe,
            "detected_major:",
            detected_major,
            "force_major:",
            force_major,
            "use_major:",
            major,
            force=True,
        )

        # === 신규 === 최종 화면 설정값 계산
        final_cfg = self._resolve_view_config(
            view_mode=view_mode,
            window_size=window_size,
            mobile_metrics=mobile_metrics,
            mobile_user_agent=mobile_user_agent,
        )

        final_view_mode = final_cfg["view_mode"]
        final_window_size = final_cfg["window_size"]
        final_mobile_metrics = final_cfg["mobile_metrics"]
        final_mobile_user_agent = final_cfg["mobile_user_agent"]

        def _extract_browser_major_from_error(e: Exception) -> Optional[int]:
            """
            Selenium 오류 메시지에서 실제 Chrome major를 추출한다.
            예: Current browser version is 147.0.7727.102
            """
            try:
                msg = str(e)
                m = re.search(r"Current browser version is\s+(\d+)", msg)
                if m:
                    return int(m.group(1))
            except Exception:
                pass
            return None

        def _create_driver() -> WebDriver:
            """
            uc.Chrome 생성 래퍼.

            순서:
            1) 현재 Chrome major 자동 감지값으로 실행
            2) 실패하면 에러 메시지에서 실제 Chrome major 추출 후 재시도
            3) 그래도 실패하면 version_main 없이 uc 자동 방식으로 재시도
            4) 자동 방식 실패 시 다시 에러 메시지 major로 마지막 재시도

            이렇게 하면 Chrome 147 -> 148 -> 149로 올라가도 코드 수정 없이 대응 가능하다.
            """
            last_err: Optional[Exception] = None

            def _new_options() -> uc.ChromeOptions:
                """
                uc.ChromeOptions는 uc.Chrome에 한 번 넘기면 재사용할 수 없다.
                따라서 드라이버 생성 재시도마다 새 옵션 객체를 만든다.
                """
                return self._build_options(
                    chrome_exe=chrome_exe,
                    view_mode=final_view_mode,
                    window_size=final_window_size,
                    mobile_metrics=final_mobile_metrics,
                    mobile_user_agent=final_mobile_user_agent,
                )

            # 1차: 감지된 Chrome major 또는 force_major로 실행
            if major:
                try:
                    self._log("start with chrome major:", major, force=True)
                    return uc.Chrome(
                        options=_new_options(),
                        version_main=int(major),
                    )
                except Exception as e:
                    last_err = e
                    self._log("uc major version failed:", str(e), force=True)

                    # 에러 메시지에서 실제 Chrome 버전 추출 후 재시도
                    extracted_major = _extract_browser_major_from_error(e)
                    if extracted_major and extracted_major != int(major):
                        try:
                            self._log("retry with extracted chrome major:", extracted_major, force=True)
                            return uc.Chrome(
                                options=_new_options(),
                                version_main=int(extracted_major),
                            )
                        except Exception as e2:
                            last_err = e2
                            self._log("uc extracted major failed:", str(e2), force=True)

            # 2차: version_main 없이 uc 자동 방식
            try:
                self._log("retry with uc auto version", force=True)
                return uc.Chrome(options=_new_options())
            except Exception as e3:
                last_err = e3
                self._log("uc auto version failed:", str(e3), force=True)

                # 자동 방식 실패 시에도 에러에서 실제 버전 추출해서 마지막 재시도
                extracted_major = _extract_browser_major_from_error(e3)
                if extracted_major:
                    try:
                        self._log("final retry with extracted chrome major:", extracted_major, force=True)
                        return uc.Chrome(
                            options=_new_options(),
                            version_main=int(extracted_major),
                        )
                    except Exception as e4:
                        last_err = e4
                        self._log("uc final extracted major failed:", str(e4), force=True)

            if last_err:
                raise last_err
            raise RuntimeError("uc.Chrome 생성 실패")

        try:
            # 1차 생성 시도
            t = time.time()
            self.driver = _create_driver()
            self._force_window(final_window_size[0], final_window_size[1])  # === 신규 ===
            if final_view_mode == "mobile":
                self._apply_mobile_emulation(
                    width=final_mobile_metrics[0],
                    height=final_mobile_metrics[1],
                    user_agent=final_mobile_user_agent,
                )
            self._log("driver create time:", time.time() - t, force=True)

            try:
                self.driver.set_page_load_timeout(timeout)
            except Exception:
                pass

            return self.driver

        except Exception as e:
            self._log("start failed:", str(e), force=True)
            self.last_error = e
            self._safe_quit_driver()

            try:
                self.wipe_uc_driver_cache()
            except Exception:
                pass

            # 캐시 삭제 후 한 번 더 현재 Chrome major 재확인
            chrome_exe = self._find_chrome_exe_windows()
            detected_major = self._detect_chrome_major(chrome_exe)
            major = int(force_major) if force_major else detected_major

            self._log(
                "retry chrome_exe:",
                chrome_exe,
                "retry detected_major:",
                detected_major,
                "retry use_major:",
                major,
                force=True,
            )

            try:
                t = time.time()
                self.driver = _create_driver()
                self._force_window(final_window_size[0], final_window_size[1])  # === 신규 ===
                if final_view_mode == "mobile":
                    self._apply_mobile_emulation(
                        width=final_mobile_metrics[0],
                        height=final_mobile_metrics[1],
                        user_agent=final_mobile_user_agent,
                    )
                self._log("driver create time:", time.time() - t, force=True)

                try:
                    self.driver.set_page_load_timeout(timeout)
                except Exception:
                    pass

                return self.driver

            except Exception as e2:
                self.last_error = e2
                self._safe_quit_driver()
                raise e2

    def quit(self) -> None:
        # === 신규 === 중복 quit 방지 (PySide/QThread에서 매우 중요)
        if self._quit_done:
            return
        self._quit_done = True

        self._safe_quit_driver()

        try:
            if self._profile_dir and os.path.isdir(self._profile_dir):
                shutil.rmtree(self._profile_dir, ignore_errors=True)
        except Exception:
            pass

        self._profile_dir = None
        self._net_enabled = False
        self._perf_supported = None

    def _force_window(self, w: int = 600, h: int = 700) -> None:
        # === 신규 === uc/Chrome 조합에서 --window-size가 무시되는 케이스가 있어 생성 직후 강제 적용
        if not self.driver:
            return
        try:
            self.driver.set_window_position(0, 0)
            self.driver.set_window_size(int(w), int(h))
            self._log("window forced:", self.driver.get_window_size(), self.driver.get_window_position())
        except Exception as e:
            self._log("window force failed:", str(e))

    def cleanup_old_profiles(self, older_than_hours: int = 24) -> int:
        base = os.path.join(tempfile.gettempdir(), "selenium_profiles")
        if not os.path.isdir(base):
            return 0

        now = time.time()
        removed = 0

        for name in os.listdir(base):
            if not name.startswith("profile_"):
                continue
            path = os.path.join(base, name)
            if not os.path.isdir(path):
                continue
            try:
                mtime = os.path.getmtime(path)
                if (now - mtime) >= (older_than_hours * 3600):
                    shutil.rmtree(path, ignore_errors=True)
                    removed += 1
            except Exception:
                # 청소 실패는 무시(권한/락 등)
                pass

        return removed

    # ---------------------------------------------------------------------
    # Element helpers
    # ---------------------------------------------------------------------
    def wait_element(self, by: Union[By, str], selector: str, timeout: int = 10) -> Optional[WebElement]:
        """
        지정 selector의 요소가 DOM에 나타날 때까지 대기 후 반환한다(presence 기준).

        Args:
            by: selenium By 타입 등 (예: By.CSS_SELECTOR)
            selector: 선택자 문자열
            timeout: 최대 대기 시간(초)

        Returns:
            WebElement 또는 None(예외 발생 시)
        """
        if not self.driver:
            return None
        try:
            return WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((by, selector))
            )
        except Exception as e:
            self.last_error = e
            return None


    def wait_ready_state_complete(self, timeout_sec: int = 7) -> bool:
        if not self.driver:
            return False

        try:
            WebDriverWait(self.driver, timeout_sec).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            return True
        except TimeoutException:
            self._log("readyState complete timeout", force=True)
            return False


    def wait_current_frame_ready(self, timeout_sec: int = 5) -> bool:
        """
        현재 driver가 바라보는 frame/page의 DOM 사용 가능 상태를 기다린다.
        - main page 상태면 main page 기준
        - iframe으로 switch 되어 있으면 현재 iframe 기준
        """
        if not self.driver:
            return False

        try:
            WebDriverWait(self.driver, timeout_sec).until(
                lambda d: d.execute_script("return document.readyState") in ("interactive", "complete")
            )

            WebDriverWait(self.driver, timeout_sec).until(
                lambda d: d.execute_script("return !!document.body")
            )

            return True

        except TimeoutException:
            self._log("current frame ready timeout", force=True)
            return False

        except Exception as e:
            self._log("current frame ready failed:", str(e), force=True)
            return False


    # ---------------------------------------------------------------------
    # Exception explain helper
    # ---------------------------------------------------------------------
    @staticmethod
    def explain_exception(context: str, e: Exception) -> str:
        """
        Selenium 예외를 사용자 친화적인 메시지로 매핑한다.

        Args:
            context: 오류 발생 맥락(예: "로그인 버튼 클릭")
            e: 발생 예외

        Returns:
            한국어 요약 메시지
        """
        if isinstance(e, NoSuchElementException):
            return f"{context}: 요소 없음"
        if isinstance(e, StaleElementReferenceException):
            return f"{context}: Stale 요소"
        if isinstance(e, TimeoutException):
            return f"{context}: 시간 초과"
        if isinstance(e, ElementClickInterceptedException):
            return f"{context}: 클릭 방해"
        if isinstance(e, ElementNotInteractableException):
            return f"{context}: 비활성 요소"
        if isinstance(e, InvalidSelectorException):
            return f"{context}: 선택자 오류"
        if isinstance(e, WebDriverException):
            return f"{context}: WebDriver 오류"
        return f"{context}: 알 수 없는 오류"