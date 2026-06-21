import atexit
import logging
import os
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from crawl_runner import (
    CrawlerBlockedError,
    CrawlerClosedError,
    CrawlerQueueFullError,
    LululemonCrawler,
)


# =========================================================
# 기본 설정
# =========================================================

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


app = Flask(__name__)

API_TOKEN = os.getenv("API_TOKEN", "")


# 허용할 호스트
#
# .env 예시:
# ALLOWED_CRAWL_HOSTS=shop.lululemon.com
#
ALLOWED_CRAWL_HOSTS = {
    host.strip().lower()
    for host in os.getenv(
        "ALLOWED_CRAWL_HOSTS",
        "shop.lululemon.com",
    ).split(",")
    if host.strip()
}


# 크롤러는 서버 프로세스당 하나만 생성
crawler = LululemonCrawler()


# 서버 종료 시 Worker 및 브라우저 종료
atexit.register(crawler.close)


# =========================================================
# 인증 및 URL 검사
# =========================================================

def is_authorized(req) -> bool:
    token = req.headers.get("X-API-KEY", "")

    return (
            bool(API_TOKEN)
            and token == API_TOKEN
    )


def is_allowed_url(url: str) -> bool:
    """
    서버가 임의의 URL에 접속하는 것을 방지하고
    허용된 룰루레몬 호스트만 접속하도록 제한한다.
    """

    try:
        parsed = urlparse(url)

        if parsed.scheme.lower() != "https":
            return False

        hostname = (parsed.hostname or "").lower()

        return hostname in ALLOWED_CRAWL_HOSTS

    except Exception:
        return False


# =========================================================
# 공통 응답
# =========================================================

def create_blocked_response(
        message: str,
        retry_after: int,
):
    response = jsonify({
        "success": False,
        "message": message,
        "errorCode": "CRAWL_BLOCKED",
        "retryAfter": retry_after,
    })

    response.status_code = 429
    response.headers["Retry-After"] = str(retry_after)

    return response


# =========================================================
# API
# =========================================================

@app.post("/api/crawl")
def handle_crawl():
    # -----------------------------------------------------
    # API 인증
    # -----------------------------------------------------

    if not is_authorized(request):
        logger.warning(
            "권한 없는 접근 시도 | remote=%s",
            request.remote_addr,
        )

        return jsonify({
            "success": False,
            "message": "unauthorized",
        }), 401

    # -----------------------------------------------------
    # 요청 데이터 확인
    # -----------------------------------------------------

    req_data = request.get_json(silent=True) or {}
    url = str(req_data.get("url", "")).strip()

    if not url:
        return jsonify({
            "success": False,
            "message": "URL is required",
        }), 400

    if not is_allowed_url(url):
        logger.warning(
            "허용되지 않은 URL 요청 | url=%s",
            url,
        )

        return jsonify({
            "success": False,
            "message": "허용되지 않은 URL입니다.",
        }), 400

    logger.info(
        "크롤링 API 요청 | url=%s",
        url,
    )

    # -----------------------------------------------------
    # 크롤링 요청
    # -----------------------------------------------------

    try:
        # 내부적으로 큐에 등록된다.
        # 앞선 요청이 처리 중이면 현재 HTTP 요청은 대기한다.
        soup_html = crawler.product_api_data(url)

        return jsonify({
            "success": True,
            "soup": soup_html,
        })

    except CrawlerBlockedError as error:
        logger.warning(
            "크롤링 차단 응답 | retry_after=%s | reason=%s",
            error.retry_after,
            error.reason,
        )

        return create_blocked_response(
            message=(
                "대상 사이트 접근이 일시적으로 제한되었습니다. "
                f"약 {error.retry_after}초 후 다시 실행해 주세요."
            ),
            retry_after=error.retry_after,
        )

    except CrawlerQueueFullError as error:
        logger.warning(
            "크롤링 대기열 초과 | %s",
            error,
        )

        return jsonify({
            "success": False,
            "message": "크롤링 요청 대기열이 가득 찼습니다.",
            "errorCode": "CRAWL_QUEUE_FULL",
        }), 503

    except CrawlerClosedError as error:
        logger.warning(
            "크롤러 종료 상태 | %s",
            error,
        )

        return jsonify({
            "success": False,
            "message": "크롤링 서버가 종료 중입니다.",
            "errorCode": "CRAWLER_CLOSED",
        }), 503

    except Exception:
        logger.exception(
            "크롤링 API 처리 중 오류 | url=%s",
            url,
        )

        # 실제 내부 예외 내용을 고객에게 그대로 노출하지 않는다.
        return jsonify({
            "success": False,
            "message": "Crawler failed, retry later",
            "errorCode": "CRAWL_ERROR",
        }), 500


@app.get("/api/status")
def handle_status():
    """
    서버 상태 확인용 API.

    X-API-KEY 인증이 필요하다.
    """

    if not is_authorized(request):
        return jsonify({
            "success": False,
            "message": "unauthorized",
        }), 401

    status = crawler.get_status()

    return jsonify({
        "success": True,
        **status,
    })


if __name__ == "__main__":
    # 개발용 실행
    #
    # 운영에서는 run_server.py의 Waitress를 사용한다.
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=True,
        use_reloader=False,
    )