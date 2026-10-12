"""Finite Facebook searches and exact-post visible metadata, no network/API scraping."""

from __future__ import annotations

import time
from typing import Any, Callable
from urllib.parse import quote

from creator_loop.facebook_browser import (
    EXTRACT,
    LAZY_TIMESTAMPS,
    SessionRequired,
    session_problem,
)
from creator_loop.facebook_cdp import is_facebook_url
from creator_loop.facebook_research import candidate, criteria, hashtag_summary
from creator_loop.facebook_sources import post_link

RESEARCH_SECONDS = 600
DETAIL = r"""(id) => {
 const visible=e=>{if(!e?.getClientRects().length||e.closest('[aria-hidden="true"]'))return false;let r=e.getBoundingClientRect();if(r.width<=0||r.height<=0||r.bottom<=0||r.right<=0||r.top>=innerHeight||r.left>=innerWidth)return false;let hit=document.elementFromPoint(Math.min(innerWidth-1,Math.max(0,r.left+r.width/2)),Math.min(innerHeight-1,Math.max(0,r.top+r.height/2)));return hit===e||e.contains(hit);};
 const text=e=>(e?.innerText||'').trim();
 const postId=href=>{try{let u=new URL(href);if(!['facebook.com','www.facebook.com','m.facebook.com','web.facebook.com'].includes(u.hostname))return null;return u.searchParams.get('multi_permalinks')||u.searchParams.get('story_fbid')||(u.pathname==='/watch/'?u.searchParams.get('v'):null)||u.pathname.match(/\/(?:posts|permalink|videos|reel)\/([^/]+)/)?.[1]||null;}catch{return null;}};
 if(location.protocol!=='https:'||postId(location.href)!==id)return null;
 const articles=[...document.querySelectorAll('[role="article"]')].filter(visible);
 const matched=articles.filter(a=>[...a.querySelectorAll('a[href]')].some(x=>x.closest('[role="article"]')===a&&postId(x.href)===id));
 let root=matched.length===1?matched[0]:null;
 let message=root?.querySelector('[data-ad-preview="message"],[data-ad-comet-preview="message"]');
 // A directly opened reel/post viewer can use nested dialogs with no permalink anchor.
 // Require a single actually visible caption in those dialogs and the exact route ID.
 if(!root){const messages=[...document.querySelectorAll('[role="dialog"] [data-ad-preview="message"],[role="dialog"] [data-ad-comet-preview="message"]')].filter(visible);const unique=[...new Set(messages)];if(unique.length===1){message=unique[0];root=message.closest('[role="dialog"]');}}
 if(root?.getAttribute('role')==='dialog'){let parent=root.parentElement?.closest('[role="dialog"]');while(parent){const captions=[...new Set([...parent.querySelectorAll('[data-ad-preview="message"],[data-ad-comet-preview="message"]')].filter(visible))];const ids=[...parent.querySelectorAll('a[href]')].filter(visible).map(a=>postId(a.href)).filter(Boolean);if(captions.length!==1||captions[0]!==message||ids.some(x=>x!==id))break;root=parent;parent=root.parentElement?.closest('[role="dialog"]');}}
 // Use whole-page caption only when a unique message and the exact og:url identify this post.
 if(!root&&postId(document.querySelector('meta[property="og:url"]')?.content)===id){let main=document.querySelector('[role="main"]');let messages=main?.querySelectorAll('[data-ad-preview="message"],[data-ad-comet-preview="message"]');if(messages?.length===1){root=main;message=messages[0];}}
 if(!root)return {url:location.href,excerpt:null,metrics:{}};
 if(!message&&root.getAttribute('role')==='article'){const texts=[...root.querySelectorAll('[dir="auto"]')].filter(e=>visible(e)&&e.closest('[role="article"]')===root&&!e.closest('h2,h3,button,[role="button"],[role="textbox"],a')&&!e.querySelector('h2,h3,button,[role="button"],input,textarea')&&text(e).length>=20);const longest=texts.sort((a,b)=>text(b).length-text(a).length)[0];if(longest&&texts.every(e=>text(longest).includes(text(e))))message=longest;}
 const heading=root.querySelector('h2,h3');
 const publisherAnchor=[...heading?.querySelectorAll('a[href]')||[]].find(a=>!postId(a.href));
 const dates=[...root.querySelectorAll('time[datetime],[data-utime]')].filter(e=>visible(e)&&(root.getAttribute('role')!=='article'||e.closest('[role="article"]')===root));
 let published=null;
 if(dates.length===1){const d=dates[0];const iso=d.getAttribute('datetime');const seconds=d.getAttribute('data-utime');try{if(iso)published=new Date(iso).toISOString();else if(seconds&&/^\d+$/.test(seconds)&&Number(seconds)>0)published=new Date(Number(seconds)*1000).toISOString();}catch{}}
 const timestamp=[...root.querySelectorAll('a[href]')].find(a=>postId(a.href)===id);
 const labels=[...root.querySelectorAll('[role="button"],button,[aria-label]')].filter(visible).map(e=>e.getAttribute('aria-label')||text(e)).filter(v=>v.length<=160).slice(0,150);
 const metrics={};
 const patterns={likes:/(?:all reactions|tất cả cảm xúc|lượt thích|likes|reactions)\s*[:：]?\s*[\d.,]+(?:\s*[kKmM]|\s*(?:nghìn|triệu))?|[\d.,]+(?:\s*[kKmM]|\s*(?:nghìn|triệu))?\s+(?:likes|reactions|lượt thích)/i,comments:/[\d.,]+(?:\s*[kKmM]|\s*(?:nghìn|triệu))?\s+(?:comments|bình luận)/i,shares:/[\d.,]+(?:\s*[kKmM]|\s*(?:nghìn|triệu))?\s+(?:shares|lượt chia sẻ)/i,views:/[\d.,]+(?:\s*[kKmM]|\s*(?:nghìn|triệu))?\s+(?:views|lượt xem)/i};
 for(const [key,pattern] of Object.entries(patterns)){let found=labels.map(l=>l.match(pattern)?.[0]).filter(Boolean);let unique=[...new Set(found)];if(unique.length===1)metrics[key]=unique[0];}
 return {url:location.href,excerpt:(visible(message)?text(message):'').slice(0,8192)||null,scope:root.querySelector('video')?'CAPTION_ONLY':'VISIBLE_TEXT',publisher:text(heading).slice(0,200)||null,publisher_url:publisherAnchor?.href||null,published_at:published,published_label:(timestamp?.getAttribute('aria-label')||timestamp?.getAttribute('title')||text(timestamp)).slice(0,200)||null,metrics};
}"""


def discover(
    page: Any,
    settings: dict[str, Any],
    emit: Callable[[dict[str, Any]], None],
    cancelled: Callable[[], bool],
    deadline: float,
) -> dict[str, Any]:
    settings = criteria(settings)
    found: dict[str, dict[str, Any]] = {}
    queue = [
        {"text": q, "kind": "EDITABLE_SUGGESTION", "source_urls": []}
        for q in settings["queries"]
    ]
    executed: list[dict[str, Any]] = []
    opened: set[str] = set()
    seen_lazy: set[str] = set()
    details_used = 0

    def guard() -> None:
        if cancelled():
            raise InterruptedError()
        if time.monotonic() >= deadline:
            raise TimeoutError()
        if page.is_closed() or not is_facebook_url(page.url):
            raise SessionRequired("FACEBOOK_TAB_REQUIRED")
        if problem := session_problem(page):
            raise SessionRequired(problem)

    def remember(raw: Any, query: str) -> None:
        if not isinstance(raw, dict) or (link := post_link(raw.get("url"))) is None:
            return
        item = candidate(raw, query, settings)
        if link.external_id in found:
            if item["excerpt"] and not found[link.external_id]["excerpt"]:
                found[link.external_id] = item
        elif len(found) < settings["max_candidates"]:
            found[link.external_id] = item

    for query_record in queue:
        if (
            len(executed) >= settings["max_queries"]
            or len(found) >= settings["max_candidates"]
        ):
            break
        guard()
        query = query_record["text"]
        executed.append(query_record)
        page.goto(
            "https://www.facebook.com/search/posts/?q=" + quote(query, safe=""),
            wait_until="domcontentloaded",
            timeout=20000,
        )
        page.wait_for_timeout(1200)
        idle = 0
        for turn in range(4):
            guard()
            before = len(found)
            for raw in page.evaluate(EXTRACT):
                remember(raw, query)
            # Details use exact canonical posts already found, bounded globally.
            for item in list(found.values()):
                if (
                    item["external_id"] in opened
                    or details_used >= settings["max_details"]
                ):
                    continue
                guard()
                previous = page.url
                opened.add(item["external_id"])
                details_used += 1
                page.goto(item["url"], wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(700)
                guard()
                actual = post_link(page.url)
                if actual is not None and actual.external_id == item["external_id"]:
                    raw = page.evaluate(DETAIL, actual.external_id)
                    if isinstance(raw, dict):
                        remember(raw, item["query"])
                if page.url != previous:
                    page.go_back(wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(400)
                guard()
            # A lazy timestamp is a detail opening too, even if it yields no ID.
            if (
                details_used < settings["max_details"]
                and len(found) < settings["max_candidates"]
            ):
                for lazy in page.evaluate(LAZY_TIMESTAMPS):
                    if (
                        not isinstance(lazy, dict)
                        or type(lazy.get("index")) is not int
                        or not 0 <= lazy["index"] <= 10000
                        or not isinstance(lazy.get("href"), str)
                        or len(lazy["href"]) > 4096
                    ):
                        raise ValueError("Invalid timestamp")
                    if lazy["href"] in seen_lazy:
                        continue
                    element = page.locator("a[href]").nth(lazy["index"])
                    if element.evaluate("a=>a.href") != lazy["href"]:
                        break
                    guard()
                    previous = page.url
                    seen_lazy.add(lazy["href"])
                    details_used += 1
                    element.click(timeout=5000)
                    page.wait_for_timeout(700)
                    guard()
                    actual = post_link(page.url)
                    if actual is not None:
                        opened.add(actual.external_id)
                        raw = page.evaluate(DETAIL, actual.external_id)
                        remember(
                            raw if isinstance(raw, dict) else {"url": actual.url}, query
                        )
                    if page.url != previous:
                        page.go_back(wait_until="domcontentloaded", timeout=15000)
                        page.wait_for_timeout(400)
                    break
            emit(
                {
                    "status": "SCANNING",
                    "count": len(found),
                    "queries_used": len(executed),
                    "details_used": details_used,
                }
            )
            if len(found) >= settings["max_candidates"]:
                break
            idle = idle + 1 if len(found) == before else 0
            if idle >= 2:
                break
            page.mouse.wheel(0, 850)
            page.wait_for_timeout(600)
        tags = hashtag_summary(list(found.values()))
        if tags and len(queue) < settings["max_queries"]:
            tag = tags[0]
            expanded = (
                settings["topic"][: max(1, 199 - len(tag["tag"]))] + " " + tag["tag"]
            )[:200]
            if expanded not in {q["text"] for q in queue}:
                queue.append(
                    {
                        "text": expanded,
                        "kind": "OBSERVED_HASHTAG",
                        "source_urls": tag["source_urls"][:40],
                    }
                )
    return {
        "status": "SUCCEEDED",
        "count": len(found),
        "candidates": list(found.values()),
        "queries": executed,
        "details_used": details_used,
    }
