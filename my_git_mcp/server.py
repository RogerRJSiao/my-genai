import httpx
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

ALLOWED_HOSTS = (
    "github.com",
    "raw.githubusercontent.com",
    "gitlab.com",
)
MAX_CHARS = 50_000

mcp = FastMCP("my-mcp-web")


def _host_allowed(url: str) -> bool:
    host = httpx.URL(url).host or ""
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


@mcp.tool()
async def fetch_page(url: str) -> str:
    """讀取 GitHub 或 GitLab 網頁內容並回傳純文字。

    Args:
        url: 要讀取的 GitHub / GitLab 網址
    """
    if not _host_allowed(url):
        return f"不支援的網址，只接受 github.com / gitlab.com / raw.githubusercontent.com：{url}"

    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    content_type = resp.headers.get("content-type", "")
    if "text/html" in content_type:
        text = BeautifulSoup(resp.text, "html.parser").get_text("\n", strip=True)
    else:
        text = resp.text

    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n...(內容過長，已截斷)"
    return text


if __name__ == "__main__":
    mcp.run()
