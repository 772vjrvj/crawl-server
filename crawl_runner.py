import json
import logging
import math
import os
import queue
import random
import re
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from bs4 import BeautifulSoup
from selenium.common.exceptions import WebDriverException

from util.selenium_utils import SeleniumUtils


logger = logging.getLogger(__name__)


# =========================================================
# 사용자 정의 예외
# =========================================================

class CrawlerBlockedError(RuntimeError):
    """
    대상 사이트 차단 상태일 때 발생한다.
    """

    def __init__(
            self,
            retry_after: int,
            reason: str,
    ):
        self.retry_after = max(1, int(retry_after))
        self.reason = reason

        super().__init__(
            f"대상 사이트 접근 제한: "
            f"{self.retry_after}초 후 재시도 | {reason}"
        )


class CrawlerQueueFullError(RuntimeError):
    """
    크롤링 작업 큐가 가득 찼을 때 발생한다.
    """


class CrawlerClosedError(RuntimeError):
    """
    크롤러가 종료된 상태일 때 발생한다.
    """


# =========================================================
# 크롤링 작업
# =========================================================

@dataclass
class CrawlJob:
    url: str
    future: Future


# =========================================================
# 룰루레몬 크롤러
# =========================================================

class LululemonCrawler:
    """
    Selenium 브라우저 1개를 유지하면서
    작업 큐의 요청을 하나씩 순차 처리한다.

    Flask / Waitress 요청 스레드는 직접 Selenium을 조작하지 않는다.
    Selenium 전용 Worker 스레드만 브라우저를 조작한다.
    """

    def __init__(self):
        # -------------------------------------------------
        # Selenium 객체
        # -------------------------------------------------

        self.selenium_driver = None
        self.driver = None

        # -------------------------------------------------
        # 환경 설정
        # -------------------------------------------------

        self.request_delay_min_sec = float(
            os.getenv(
                "CRAWL_DELAY_MIN_SEC",
                "8",
            )
        )

        self.request_delay_max_sec = float(
            os.getenv(
                "CRAWL_DELAY_MAX_SEC",
                "15",
            )
        )

        self.page_settle_min_sec = float(
            os.getenv(
                "PAGE_SETTLE_MIN_SEC",
                "2",
            )
        )

        self.page_settle_max_sec = float(
            os.getenv(
                "PAGE_SETTLE_MAX_SEC",
                "4",
            )
        )

        self.cooldown_sec = int(
            os.getenv(
                "CRAWL_COOLDOWN_SEC",
                "1800",
            )
        )

        self.queue_maxsize = int(
            os.getenv(
                "CRAWL_QUEUE_MAXSIZE",
                "0",
            )
        )

        self.headless = (
                os.getenv(
                    "CRAWL_HEADLESS",
                    "false",
                ).strip().lower()
                in {"1", "true", "yes", "y"}
        )

        self.debug = (
                os.getenv(
                    "CRAWL_DEBUG",
                    "true",
                ).strip().lower()
                in {"1", "true", "yes", "y"}
        )

        # 최소값과 최대값이 반대로 입력된 경우 보정
        if (
                self.request_delay_min_sec
                > self.request_delay_max_sec
        ):
            (
                self.request_delay_min_sec,
                self.request_delay_max_sec,
            ) = (
                self.request_delay_max_sec,
                self.request_delay_min_sec,
            )

        if (
                self.page_settle_min_sec
                > self.page_settle_max_sec
        ):
            (
                self.page_settle_min_sec,
                self.page_settle_max_sec,
            ) = (
                self.page_settle_max_sec,
                self.page_settle_min_sec,
            )

        # -------------------------------------------------
        # 작업 큐
        # -------------------------------------------------

        # maxsize=0이면 무제한 큐
        self._job_queue = queue.Queue(
            maxsize=max(0, self.queue_maxsize)
        )

        self._stop_sentinel = object()
        self._stop_event = threading.Event()

        # -------------------------------------------------
        # 차단 상태
        # -------------------------------------------------

        self._state_lock = threading.Lock()
        self._blocked_until = 0.0
        self._blocked_reason = ""

        # 이전 실제 사이트 요청 완료 시각
        self._last_request_completed_at = 0.0

        # -------------------------------------------------
        # 단일 Worker 시작
        # -------------------------------------------------

        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="lululemon-crawl-worker",
            daemon=True,
        )

        self._worker_thread.start()

        logger.info(
            "크롤링 Worker 시작 "
            "| delay=%.1f~%.1f초 "
            "| settle=%.1f~%.1f초 "
            "| cooldown=%s초 "
            "| queue_max=%s",
            self.request_delay_min_sec,
            self.request_delay_max_sec,
            self.page_settle_min_sec,
            self.page_settle_max_sec,
            self.cooldown_sec,
            (
                "unlimited"
                if self.queue_maxsize == 0
                else self.queue_maxsize
            ),
        )

    # =====================================================
    # 외부 호출
    # =====================================================

    def product_api_data(self, url: str) -> str:
        """
        API 요청을 작업 큐에 넣고 완료될 때까지 기다린다.

        반환값은 BeautifulSoup 객체가 아니라
        JSON 응답에 넣을 수 있는 HTML 문자열이다.
        """

        if self._stop_event.is_set():
            raise CrawlerClosedError(
                "크롤러가 종료된 상태입니다."
            )

        # 차단 중에는 큐에 넣지 않고 즉시 반환
        self._raise_if_blocked()

        future = Future()

        job = CrawlJob(
            url=url,
            future=future,
        )

        try:
            # 큐가 무제한이면 항상 들어간다.
            # 제한이 설정되어 있으면 가득 찬 경우 즉시 예외 발생.
            self._job_queue.put_nowait(job)

        except queue.Full as error:
            raise CrawlerQueueFullError(
                "크롤링 작업 큐가 가득 찼습니다."
            ) from error

        logger.info(
            "크롤링 작업 큐 등록 "
            "| queue_size=%s "
            "| url=%s",
            self._job_queue.qsize(),
            url,
        )

        # Worker 처리 완료까지 현재 HTTP 요청 대기
        #
        # Worker에서 set_result() 또는 set_exception()을 호출하면
        # 대기가 해제된다.
        return future.result()

    def get_status(self) -> Dict[str, Any]:
        blocked, retry_after, reason = (
            self._get_block_status()
        )

        return {
            "blocked": blocked,
            "retryAfter": retry_after,
            "blockedReason": reason,
            "queueSize": self._job_queue.qsize(),
            "workerAlive": self._worker_thread.is_alive(),
            "browserReady": self.driver is not None,
        }

    # =====================================================
    # Worker
    # =====================================================

    def _worker_loop(self):
        """
        작업 큐에서 요청을 하나씩 꺼내 순차 처리한다.

        Selenium 관련 작업은 모두 이 스레드에서만 실행된다.
        """

        logger.info(
            "Selenium 전용 Worker 실행"
        )

        try:
            while True:
                job = self._job_queue.get()

                try:
                    if job is self._stop_sentinel:
                        logger.info(
                            "Worker 종료 신호 수신"
                        )
                        return

                    if not isinstance(job, CrawlJob):
                        logger.warning(
                            "알 수 없는 작업 객체 무시"
                        )
                        continue

                    if job.future.cancelled():
                        logger.info(
                            "취소된 작업 건너뜀 | url=%s",
                            job.url,
                        )
                        continue

                    # 큐에 들어올 때는 정상이어도
                    # 대기 중 차단이 시작될 수 있으므로 다시 검사
                    try:
                        self._raise_if_blocked()

                    except CrawlerBlockedError as error:
                        self._set_future_exception(
                            job.future,
                            error,
                        )
                        continue

                    logger.info(
                        "크롤링 작업 시작 "
                        "| remaining_queue=%s "
                        "| url=%s",
                        self._job_queue.qsize(),
                        job.url,
                    )

                    try:
                        soup_html = self._crawl_once(
                            job.url
                        )

                        self._set_future_result(
                            job.future,
                            soup_html,
                        )

                        logger.info(
                            "크롤링 작업 완료 | url=%s",
                            job.url,
                        )

                    except CrawlerBlockedError as error:
                        # 현재 작업 차단 응답
                        self._set_future_exception(
                            job.future,
                            error,
                        )

                        # 이미 큐에서 대기 중인 요청도
                        # 사이트에 접속하지 않고 전부 차단 응답
                        self._fail_all_pending_jobs_as_blocked()

                    except Exception as error:
                        logger.exception(
                            "크롤링 작업 실패 | url=%s",
                            job.url,
                        )

                        self._set_future_exception(
                            job.future,
                            error,
                        )

                finally:
                    self._job_queue.task_done()

        finally:
            # Worker 스레드에서 생성한 브라우저는
            # Worker 스레드에서 종료한다.
            self._quit_driver()

            logger.info(
                "Selenium 전용 Worker 종료"
            )

    # =====================================================
    # 실제 크롤링
    # =====================================================

    def _crawl_once(self, url: str) -> str:
        """
        실제 브라우저로 URL 하나를 처리한다.
        """

        # 이전 사이트 요청과의 간격 적용
        self._wait_before_next_request()

        try:
            self._ensure_driver()

            soup = self.fetch_product_soup(url)

            blocked_reason = self._detect_block_reason(
                soup
            )

            if blocked_reason:
                retry_after = self._activate_cooldown(
                    blocked_reason
                )

                raise CrawlerBlockedError(
                    retry_after=retry_after,
                    reason=blocked_reason,
                )

            return str(soup)

        except CrawlerBlockedError:
            # 사이트 차단은 브라우저 오류가 아니므로
            # 브라우저를 재시작하지 않는다.
            raise

        except Exception:
            logger.exception(
                "크롤링 중 오류 발생, 브라우저 재시작 시도 "
                "| url=%s",
                url,
            )

            self._restart_driver_safely()

            raise

        finally:
            # 성공, 실패, 차단 여부와 관계없이
            # 실제 사이트 요청이 끝난 시각을 기록한다.
            self._last_request_completed_at = (
                time.monotonic()
            )

    def fetch_product_soup(
            self,
            url: str,
    ) -> BeautifulSoup:
        """
        페이지에 접속하고 렌더링이 안정된 뒤 HTML을 가져온다.

        스크롤이나 마우스 이동은 하지 않는다.
        """

        if self.driver is None:
            raise RuntimeError(
                "Selenium 드라이버가 초기화되지 않았습니다."
            )

        self.driver.get(url)

        self.selenium_driver.wait_ready_state_complete(
            timeout_sec=15
        )

        # 동적 페이지 렌더링 안정화 대기
        settle_sec = random.uniform(
            self.page_settle_min_sec,
            self.page_settle_max_sec,
        )

        logger.info(
            "페이지 렌더링 대기 %.1f초 | url=%s",
            settle_sec,
            url,
        )

        time.sleep(settle_sec)

        html = self.driver.page_source

        if not html:
            raise RuntimeError(
                "페이지 HTML이 비어 있습니다."
            )

        return BeautifulSoup(
            html,
            "html.parser",
        )

    # =====================================================
    # 요청 간격
    # =====================================================

    def _wait_before_next_request(self):
        """
        이전 크롤링 완료 시점부터 다음 접속 전까지
        8~15초 범위의 간격을 적용한다.

        첫 번째 요청은 이전 요청이 없으므로 바로 실행한다.
        """

        if self._last_request_completed_at <= 0:
            return

        delay_sec = random.uniform(
            self.request_delay_min_sec,
            self.request_delay_max_sec,
        )

        elapsed_sec = (
                time.monotonic()
                - self._last_request_completed_at
        )

        remain_sec = delay_sec - elapsed_sec

        if remain_sec <= 0:
            return

        logger.info(
            "다음 사이트 요청까지 %.1f초 대기",
            remain_sec,
        )

        time.sleep(remain_sec)

    # =====================================================
    # 차단 판단
    # =====================================================

    def _detect_block_reason(
            self,
            soup: BeautifulSoup,
    ) -> Optional[str]:
        """
        브라우저에 표시된 실제 텍스트를 기준으로
        차단 또는 일시적 접근 제한 여부를 판단한다.

        JavaScript 내부 문자열 오탐을 줄이기 위해
        전체 HTML이 아니라 화면 텍스트와 title을 우선 사용한다.
        """

        title_text = ""

        if soup.title:
            title_text = soup.title.get_text(
                " ",
                strip=True,
            )

        visible_text = soup.get_text(
            " ",
            strip=True,
        )

        normalized_text = re.sub(
            r"\s+",
            " ",
            f"{title_text} {visible_text}",
        ).strip().lower()

        # -------------------------------------------------
        # JSON 오류 응답 검사
        # -------------------------------------------------

        compact_body_text = soup.get_text(
            "",
            strip=True,
        )

        if compact_body_text.startswith("{"):
            try:
                response_data = json.loads(
                    compact_body_text
                )

                message = str(
                    response_data.get(
                        "message",
                        "",
                    )
                ).strip()

                error_code = str(
                    response_data.get(
                        "errorCode",
                        "",
                    )
                ).strip()

                json_text = (
                    f"{message} {error_code}"
                ).lower()

                json_block_terms = (
                    "bad request",
                    "access denied",
                    "request blocked",
                    "too many requests",
                    "temporarily blocked",
                    "forbidden",
                    "unusual traffic",
                    "verify you are human",
                    "unable to process",
                    "service unavailable",
                )

                for term in json_block_terms:
                    if term in json_text:
                        reason = (
                            "JSON 오류 응답 감지 "
                            f"| message={message} "
                            f"| errorCode={error_code or '-'}"
                        )

                        self._log_block_page(
                            reason,
                            normalized_text,
                        )

                        return reason

            except (
                    json.JSONDecodeError,
                    TypeError,
                    ValueError,
            ):
                # 일반 HTML이면 JSON 파싱 실패가 정상
                pass

        # -------------------------------------------------
        # 강한 차단 문구
        #
        # 아래 문구는 화면에 표시되면
        # 단독으로도 차단 또는 접근 제한으로 판단한다.
        # -------------------------------------------------

        strong_block_terms = {
            "bad request": "Bad Request",
            "access denied": "Access Denied",
            "request blocked": "Request Blocked",
            "too many requests": "Too Many Requests",
            "temporarily blocked": "Temporarily Blocked",
            "you have been blocked": "You Have Been Blocked",
            "sorry, you have been blocked": (
                "Sorry, You Have Been Blocked"
            ),
            "unusual traffic": "Unusual Traffic",
            "verify you are human": "Verify You Are Human",
            "verification required": "Verification Required",
            "automated requests": "Automated Requests",
            "automated access": "Automated Access",
            "captcha": "CAPTCHA",
            "request could not be satisfied": (
                "Request Could Not Be Satisfied"
            ),
            "unable to process your request": (
                "Unable To Process Your Request"
            ),
            "temporarily unavailable": (
                "Temporarily Unavailable"
            ),
            "service unavailable": "Service Unavailable",
            "rate limit": "Rate Limit",
            "rate limit exceeded": "Rate Limit Exceeded",
            "forbidden": "Forbidden",
        }

        for term, display_name in (
                strong_block_terms.items()
        ):
            if term in normalized_text:
                reason = (
                    f"차단 문구 감지: {display_name}"
                )

                self._log_block_page(
                    reason,
                    normalized_text,
                )

                return reason

        # -------------------------------------------------
        # 약한 오류 문구
        #
        # 일반적인 사이트 오류에서도 나타날 수 있으므로
        # 정상 상품 데이터가 없거나 화면이 매우 짧을 때만
        # 접근 제한으로 판단한다.
        # -------------------------------------------------

        weak_error_terms = {
            "something went wrong": (
                "Something Went Wrong"
            ),
            "please try again later": (
                "Please Try Again Later"
            ),
            "an error occurred": "An Error Occurred",
            "error processing request": (
                "Error Processing Request"
            ),
            "we are having trouble": (
                "We Are Having Trouble"
            ),
            "cannot complete your request": (
                "Cannot Complete Your Request"
            ),
        }

        matched_weak_terms = [
            display_name
            for term, display_name
            in weak_error_terms.items()
            if term in normalized_text
        ]

        if matched_weak_terms:
            has_next_data = (
                    soup.find(
                        "script",
                        id="__NEXT_DATA__",
                    )
                    is not None
            )

            visible_text_length = len(
                visible_text
            )

            # 오류 문구가 있고,
            # 정상 상품 데이터가 없거나 화면이 짧으면 차단 판단
            if (
                    not has_next_data
                    or visible_text_length < 500
            ):
                reason = (
                    "비정상 오류 페이지 감지 "
                    f"| keywords={', '.join(matched_weak_terms)} "
                    f"| next_data={has_next_data} "
                    f"| text_length={visible_text_length}"
                )

                self._log_block_page(
                    reason,
                    normalized_text,
                )

                return reason

        return None

    def _log_block_page(
            self,
            reason: str,
            normalized_text: str,
    ):
        """
        차단 페이지의 일부 내용을 로그로 남긴다.

        이후 실제 차단 문구가 달라졌을 때
        판단 키워드를 추가할 수 있도록 한다.
        """

        text_preview = normalized_text[:1000]

        logger.warning(
            "차단 또는 접근 제한 페이지 감지 "
            "| reason=%s "
            "| page_text=%s",
            reason,
            text_preview,
        )

    # =====================================================
    # 차단 상태 관리
    # =====================================================

    def _activate_cooldown(
            self,
            reason: str,
    ) -> int:
        """
        현재 시각부터 30분 차단 상태를 활성화한다.
        """

        current_time = time.time()

        with self._state_lock:
            new_blocked_until = (
                    current_time
                    + self.cooldown_sec
            )

            # 기존 차단 종료 시각보다 짧아지지 않도록 한다.
            self._blocked_until = max(
                self._blocked_until,
                new_blocked_until,
            )

            self._blocked_reason = reason

            retry_after = max(
                1,
                math.ceil(
                    self._blocked_until
                    - current_time
                ),
            )

        blocked_until_text = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(
                self._blocked_until
            ),
        )

        logger.error(
            "크롤링 30분 중지 시작 "
            "| retry_after=%s초 "
            "| blocked_until=%s "
            "| reason=%s",
            retry_after,
            blocked_until_text,
            reason,
        )

        return retry_after

    def _get_block_status(
            self,
    ) -> Tuple[bool, int, str]:
        """
        현재 차단 상태와 남은 시간을 반환한다.
        """

        current_time = time.time()

        with self._state_lock:
            # 차단 시간이 만료된 경우 자동 해제
            if (
                    self._blocked_until > 0
                    and current_time >= self._blocked_until
            ):
                logger.info(
                    "크롤링 차단 시간 만료, 정상 요청 재개"
                )

                self._blocked_until = 0.0
                self._blocked_reason = ""

            if self._blocked_until <= current_time:
                return False, 0, ""

            retry_after = max(
                1,
                math.ceil(
                    self._blocked_until
                    - current_time
                ),
            )

            return (
                True,
                retry_after,
                self._blocked_reason,
            )

    def _raise_if_blocked(self):
        blocked, retry_after, reason = (
            self._get_block_status()
        )

        if blocked:
            raise CrawlerBlockedError(
                retry_after=retry_after,
                reason=(
                        reason
                        or "대상 사이트 접근 제한 상태"
                ),
            )

    def _fail_all_pending_jobs_as_blocked(self):
        """
        차단 감지 전에 이미 큐에 들어온 작업을 모두 꺼내
        사이트에 접속하지 않고 차단 응답으로 종료한다.
        """

        failed_count = 0

        while True:
            try:
                pending_job = (
                    self._job_queue.get_nowait()
                )

            except queue.Empty:
                break

            try:
                if (
                        pending_job
                        is self._stop_sentinel
                ):
                    # 종료 신호는 다시 큐에 넣는다.
                    self._job_queue.put_nowait(
                        self._stop_sentinel
                    )
                    break

                if not isinstance(
                        pending_job,
                        CrawlJob,
                ):
                    continue

                blocked, retry_after, reason = (
                    self._get_block_status()
                )

                if not blocked:
                    break

                self._set_future_exception(
                    pending_job.future,
                    CrawlerBlockedError(
                        retry_after=retry_after,
                        reason=reason,
                    ),
                )

                failed_count += 1

            finally:
                self._job_queue.task_done()

        if failed_count > 0:
            logger.warning(
                "차단으로 대기 작업 일괄 종료 "
                "| count=%s",
                failed_count,
            )

    # =====================================================
    # Selenium 드라이버
    # =====================================================

    def _ensure_driver(self):
        if self.driver is not None:
            return

        self._init_driver()

    def _init_driver(self):
        """
        Selenium Worker 스레드 내부에서 브라우저를 생성한다.
        """

        logger.info(
            "브라우저 드라이버 초기화 중..."
        )

        self.selenium_driver = SeleniumUtils(
            headless=self.headless,
            debug=self.debug,
        )

        self.driver = (
            self.selenium_driver.start_driver(
                view_mode="browser",
                window_size=(1024, 768),
            )
        )

        if self.driver is None:
            raise RuntimeError(
                "Selenium 드라이버 생성에 실패했습니다."
            )

        try:
            self.driver.set_page_load_timeout(30)

        except WebDriverException:
            logger.warning(
                "페이지 로드 타임아웃 설정 실패",
                exc_info=True,
            )

        logger.info(
            "브라우저 드라이버 초기화 완료"
        )

    def _restart_driver_safely(self):
        logger.warning(
            "브라우저 드라이버 재시작"
        )

        try:
            self._quit_driver()

        except Exception:
            logger.exception(
                "기존 브라우저 종료 실패"
            )

        try:
            self._init_driver()

        except Exception:
            logger.exception(
                "브라우저 드라이버 재시작 실패"
            )

    def _quit_driver(self):
        driver = self.driver

        self.driver = None
        self.selenium_driver = None

        if driver is None:
            return

        try:
            driver.quit()

        except Exception:
            logger.exception(
                "브라우저 종료 중 오류"
            )

    # =====================================================
    # Future 처리
    # =====================================================

    @staticmethod
    def _set_future_result(
            future: Future,
            result: str,
    ):
        if (
                future.done()
                or future.cancelled()
        ):
            return

        future.set_result(result)

    @staticmethod
    def _set_future_exception(
            future: Future,
            error: Exception,
    ):
        if (
                future.done()
                or future.cancelled()
        ):
            return

        future.set_exception(error)

    # =====================================================
    # 기존 데이터 추출 유틸
    # =====================================================

    @staticmethod
    def extract_next_data(
            soup: BeautifulSoup,
    ) -> Dict[str, Any]:
        script = soup.find(
            "script",
            id="__NEXT_DATA__",
        )

        text = (
            script.string
            if script
            else ""
        )

        if not text:
            return {}

        try:
            return json.loads(text)

        except (
                json.JSONDecodeError,
                TypeError,
                ValueError,
        ):
            logger.warning(
                "__NEXT_DATA__ JSON 파싱 실패"
            )

            return {}

    # =====================================================
    # 종료
    # =====================================================

    def close(self):
        """
        서버 종료 시 Worker를 정리한다.
        """

        if self._stop_event.is_set():
            return

        logger.info(
            "크롤러 종료 요청"
        )

        self._stop_event.set()

        # 대기 중인 요청은 종료 예외로 반환
        self._fail_all_pending_jobs_as_closed()

        try:
            self._job_queue.put_nowait(
                self._stop_sentinel
            )

        except queue.Full:
            # 큐가 제한되어 있고 가득 찼다면 잠시 기다려 입력
            try:
                self._job_queue.put(
                    self._stop_sentinel,
                    timeout=1,
                )
            except queue.Full:
                logger.warning(
                    "Worker 종료 신호 등록 실패"
                )

        if self._worker_thread.is_alive():
            self._worker_thread.join(
                timeout=5
            )

    def _fail_all_pending_jobs_as_closed(self):
        failed_count = 0

        while True:
            try:
                pending_job = (
                    self._job_queue.get_nowait()
                )

            except queue.Empty:
                break

            try:
                if (
                        pending_job
                        is self._stop_sentinel
                ):
                    continue

                if not isinstance(
                        pending_job,
                        CrawlJob,
                ):
                    continue

                self._set_future_exception(
                    pending_job.future,
                    CrawlerClosedError(
                        "크롤링 서버가 종료 중입니다."
                    ),
                )

                failed_count += 1

            finally:
                self._job_queue.task_done()

        if failed_count > 0:
            logger.info(
                "서버 종료로 대기 작업 종료 "
                "| count=%s",
                failed_count,
            )