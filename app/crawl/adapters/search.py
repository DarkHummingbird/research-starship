import json
import os
from datetime import timedelta
from urllib.parse import quote

from ..errors import CrawlError, NotConfigured


def search(name, ctx, params):
    query = str(params.get("query", ""))
    if name == "search.searxng":
        base = os.getenv("SEARXNG_URL", "")
        if not base:
            raise NotConfigured("未配置 SearXNG（需要 SEARXNG_URL）")
        days = params.get("recency_days")
        period = None if days is None else "day" if days <= 1 else "week" if days <= 7 else "month" if days <= 31 else "year"
        rows = ctx.json(base.rstrip("/") + "/search", params={"q": query, "format": "json", "time_range": period}).get("results", [])
    else:
        if params.get("recency_days"):
            query += " after:" + (ctx.router.date().date() - timedelta(days=params["recency_days"])).isoformat()
        if name == "search.deepseek":
            if not os.getenv("DEEPSEEK_API_KEY"):
                raise NotConfigured("未配置 DeepSeek 搜索（需要 DEEPSEEK_API_KEY）")
            query = query.strip()
            if not 1 <= len(query) <= 128:
                raise CrawlError("DeepSeek search query must contain 1 to 128 characters")
            rows = ctx.json(
                "https://api.deepseek.com/v1/tools/search",
                headers={"Accept": "application/json", "Content-Type": "application/json",
                         "Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"]},
                method="POST", body=json.dumps({"query": query, "count": 20, "need_content": False}), check_robots=False,
            ).get("results", [])
            return [{"url": x.get("url"), "title": x.get("title"),
                     "snippet": x.get("snippet") or x.get("summary") or x.get("content") or "",
                     "published_at": x.get("date")} for x in rows[:20]]
        headers = {"Accept": "application/json"}
        if os.getenv("JINA_API_KEY"):
            headers["Authorization"] = "Bearer " + os.environ["JINA_API_KEY"]
        rows = ctx.json("https://s.jina.ai/" + quote(query, safe=""), headers=headers).get("data", [])
    return [{"url": x.get("url"), "title": x.get("title"),
             "snippet": x.get("content") or x.get("description", ""),
             "published_at": x.get("publishedDate")} for x in rows[:20]]
