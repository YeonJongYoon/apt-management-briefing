
import os
import json
import base64
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

KAKAO_TOKEN_URL = "https://kauth.kakao.com/oauth/token"
KAKAO_SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"

SEARCH_TERMS = [
    "공동주택관리",
    "공동주택관리법",
    "아파트 관리규약",
    "장기수선계획",
    "공동주택 사업자 선정",
    "아파트 관리사무소 정책"
]

MAX_ARTICLES = 5


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def get_text(self):
        return " ".join(" ".join(self.parts).split())


def clean_html(value):
    parser = TextExtractor()
    parser.feed(value or "")
    return parser.get_text()


def get_url(url, data=None, headers=None):
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers or {},
        method="POST" if data is not None else "GET"
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def refresh_kakao_token():
    rest_key = os.environ["KAKAO_REST_API_KEY"]
    client_secret = os.environ["KAKAO_CLIENT_SECRET"]
    refresh_token = os.environ["KAKAO_REFRESH_TOKEN"]

    form = {
        "grant_type": "refresh_token",
        "client_id": rest_key,
        "client_secret": client_secret,
        "refresh_token": refresh_token
    }

    result = json.loads(get_url(
        KAKAO_TOKEN_URL,
        data=urllib.parse.urlencode(form).encode("utf-8"),
        headers={
            "Content-Type":
            "application/x-www-form-urlencoded;charset=utf-8"
        }
    ))

    access_token = result.get("access_token")
    if not access_token:
        raise RuntimeError("카카오 액세스 토큰 갱신에 실패했습니다.")

    # 리프레시 토큰이 갱신된 경우 GitHub 비밀 저장소도 자동 갱신
    new_refresh_token = result.get("refresh_token")
    if new_refresh_token and new_refresh_token != refresh_token:
        update_github_secret("KAKAO_REFRESH_TOKEN", new_refresh_token)
        print("카카오 리프레시 토큰을 갱신했습니다.")

    return access_token


def update_github_secret(secret_name, secret_value):
    repo = os.environ["GITHUB_REPOSITORY"]
    github_token = os.environ["GH_SECRETS_TOKEN"]
    api_base = f"https://api.github.com/repos/{repo}/actions/secrets"

    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    public_key_data = json.loads(get_url(
        f"{api_base}/public-key",
        headers=headers
    ))

    # GitHub 공개키로 암호화합니다. 비밀값 자체는 API에 평문으로 보내지 않습니다.
    from nacl.public import PublicKey, SealedBox

    public_key = PublicKey(
        base64.b64decode(public_key_data["key"])
    )
    encrypted = SealedBox(public_key).encrypt(
        secret_value.encode("utf-8")
    )

    payload = {
        "encrypted_value": base64.b64encode(encrypted).decode("ascii"),
        "key_id": public_key_data["key_id"]
    }

    get_url(
        f"{api_base}/{secret_name}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            **headers,
            "Content-Type": "application/json"
        }
    )


def fetch_articles():
    articles = []
    seen = set()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)

    for term in SEARCH_TERMS:
        query = urllib.parse.urlencode({
            "q": term,
            "hl": "ko",
            "gl": "KR",
            "ceid": "KR:ko"
        })
        url = "https://news.google.com/rss/search?" + query

        try:
            xml_text = get_url(url)
            root = ET.fromstring(xml_text)
        except Exception as exc:
            print(f"뉴스 검색 건너뜀 ({term}): {type(exc).__name__}")
            continue

        for item in root.findall(".//item"):
            title = clean_html(item.findtext("title", ""))
            link = item.findtext("link", "").strip()
            description = clean_html(item.findtext("description", ""))
            source_node = item.find("source")
            source = (
                clean_html(source_node.text or "")
                if source_node is not None else "출처 미상"
            )
            pubdate = item.findtext("pubDate", "")

            if not title or not link or title in seen:
                continue

            try:
                published = parsedate_to_datetime(pubdate)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
                if published < cutoff:
                    continue
            except (TypeError, ValueError, OverflowError):
                published = now

            seen.add(title)
            articles.append({
                "title": title,
                "link": link,
                "description": description,
                "source": source,
                "published": published
            })

    articles.sort(key=lambda item: item["published"], reverse=True)
    return articles[:MAX_ARTICLES]


def send_kakao_message(access_token, text, link=None):
    # 카카오 기본 텍스트 템플릿의 글자 수 제한을 고려합니다.
    text = text[:190]

    if link:
        template = {
            "object_type": "text",
            "text": text,
            "link": {
                "web_url": link,
                "mobile_web_url": link
            },
            "button_title": "기사 보기"
        }
    else:
        template = {
            "object_type": "text",
            "text": text,
            "link": {
                "web_url": "https://news.google.com/",
                "mobile_web_url": "https://news.google.com/"
            },
            "button_title": "뉴스 열기"
        }

    form = urllib.parse.urlencode({
        "template_object": json.dumps(template, ensure_ascii=False)
    }).encode("utf-8")

    response = get_url(
        KAKAO_SEND_URL,
        data=form,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type":
            "application/x-www-form-urlencoded;charset=utf-8"
        }
    )
    result = json.loads(response)

    if result.get("result_code") != 0:
        raise RuntimeError("카카오 메시지 전송 결과가 성공이 아닙니다.")


def main():
    print("공동주택 관리 뉴스 검색을 시작합니다.")
    access_token = refresh_kakao_token()
    articles = fetch_articles()

    if not articles:
        send_kakao_message(
            access_token,
            "🏢 공동주택 관리 뉴스 브리핑\n"
            "최근 7일 내 검색 결과가 없습니다. 링크를 눌러 확인해 주세요."
        )
        print("검색 결과가 없어 안내 메시지를 보냈습니다.")
        return

    for index, article in enumerate(articles, start=1):
        summary = article["description"] or "제목과 출처를 확인해 주세요."
        summary = summary[:65]

        message = (
            f"🏢 공동주택 관리 뉴스 {index}/{len(articles)}\n"
            f"{article['title']}\n"
            f"출처: {article['source']}\n"
            f"요약: {summary}"
        )

        send_kakao_message(access_token, message, article["link"])
        print(f"기사 {index} 전송 완료")

    print(f"완료: {len(articles)}개 기사를 전송했습니다.")


if __name__ == "__main__":
    main()
