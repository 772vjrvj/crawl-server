import json
import time
from typing import Any, Dict, Tuple
from bs4 import BeautifulSoup
from util.selenium_utils import SeleniumUtils

class LululemonCrawler:
    def __init__(self):
        print("[DEBUG] 크롤러 초기화 중 (브라우저 실행)...")
        self.selenium_driver = SeleniumUtils(headless=False, debug=True)
        self.driver = self.selenium_driver.start_driver(
            view_mode="browser",
            window_size=(1024, 768)
        )
        print("[DEBUG] 크롤러 초기화 완료.")

    def fetch_product_soup(self, url: str) -> BeautifulSoup:
        self.driver.get(url)
        self.selenium_driver.wait_ready_state_complete(timeout_sec=10)
        time.sleep(2) # 페이지 로딩 대기
        return BeautifulSoup(self.driver.page_source, "html.parser")

    def extract_next_data(self, soup: BeautifulSoup) -> Dict[str, Any]:
        sc = soup.find("script", id="__NEXT_DATA__")
        text = sc.string if sc else ""
        if not text:
            return {}
        try:
            return json.loads(text)
        except:
            return {}

    def product_api_data(self, url: str) -> Tuple[Dict[str, Any], str]:
        try:
            soup = self.fetch_product_soup(url)

            # 1. h1 상품명
            h1_tag = soup.find("h1")
            h1_product_name = h1_tag.get_text(" ", strip=True) if h1_tag else "상품명 없음"

            # 2. next_data 원본 파싱
            next_data = self.extract_next_data(soup)

            return next_data, h1_product_name

        except Exception as e:
            print(f"[ERROR] 파싱 실패: {e}")
            return {}, "Error"

    def close(self):
        if self.selenium_driver:
            self.selenium_driver.quit()