import json
import time
import logging
from typing import Any, Dict, Tuple
from bs4 import BeautifulSoup
from util.selenium_utils import SeleniumUtils

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class LululemonCrawler:
    def __init__(self):
        self._init_driver()

    def _init_driver(self):
        try:
            logger.info("브라우저 드라이버 초기화 중...")
            self.selenium_driver = SeleniumUtils(headless=False, debug=True)
            self.driver = self.selenium_driver.start_driver(
                view_mode="browser",
                window_size=(1024, 768)
            )
            logger.info("드라이버 초기화 완료.")
        except Exception as e:
            logger.error(f"드라이버 시작 실패: {e}")

    def _restart_driver(self):
        logger.warning("드라이버를 재시작합니다.")
        try:
            if self.driver:
                self.driver.quit()
        except:
            pass
        self._init_driver()

    def fetch_product_soup(self, url: str) -> BeautifulSoup:
        self.driver.get(url)
        self.selenium_driver.wait_ready_state_complete(timeout_sec=10)
        time.sleep(2)
        return BeautifulSoup(self.driver.page_source, "html.parser")

    def extract_next_data(self, soup) -> Dict[str, Any]:
        sc = soup.find("script", id="__NEXT_DATA__")
        text = sc.string if sc else ""
        if not text: return {}
        try:
            return json.loads(text)
        except:
            return {}

    def product_api_data(self, url: str) -> Tuple[Dict[str, Any], str]:
        try:
            soup = self.fetch_product_soup(url)
            next_data = self.extract_next_data(soup)
            h1_tag = soup.find("h1")
            name = h1_tag.get_text(" ", strip=True) if h1_tag else "상품명 없음"
            return next_data, name
        except Exception as e:
            logger.error(f"크롤링 중 에러 발생: {e}")
            self._restart_driver()  # 에러 발생 시 드라이버 재시작
            return {}, "Error"

    def close(self):
        if self.selenium_driver:
            self.selenium_driver.quit()