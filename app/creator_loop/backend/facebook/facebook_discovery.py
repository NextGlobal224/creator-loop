"""Finite Facebook searches and exact-post visible metadata, no network/API scraping."""

from __future__ import annotations

import time
from typing import Any, Callable
from urllib.parse import quote

from creator_loop.backend.facebook.facebook_browser import (
    EXTRACT,
    LAZY_TIMESTAMPS,
    SessionRequired,
    session_problem,
)
from creator_loop.backend.facebook.facebook_cdp import is_facebook_url
from creator_loop.backend.facebook.facebook_research import (
    VIRAL_TARGET,
    candidate,
    criteria,
    hashtag_summary,
    research_counts,
)
from creator_loop.backend.facebook.facebook_sources import post_link

RESEARCH_SECONDS = 600
DOM_HELPERS = r"""
 const visible=e=>{if(!e?.getClientRects().length||e.closest('[aria-hidden="true"]'))return false;let r=e.getBoundingClientRect(),x1=Math.max(0,r.left),x2=Math.min(innerWidth,r.right),y1=Math.max(0,r.top),y2=Math.min(innerHeight,r.bottom);if(x2<=x1||y2<=y1)return false;return [[(x1+x2)/2,(y1+y2)/2],[x1+1,y1+1],[x2-1,y2-1]].some(([x,y])=>{let h=document.elementFromPoint(x,y);return h===e||e.contains(h)});};
 const text=e=>(e?.innerText||'').trim();
 const postId=href=>{try{let u=new URL(href);if(!['facebook.com','www.facebook.com','m.facebook.com','web.facebook.com'].includes(u.hostname))return null;return u.searchParams.get('multi_permalinks')||u.searchParams.get('story_fbid')||(u.pathname==='/watch/'?u.searchParams.get('v'):null)||u.pathname.match(/\/(?:posts|permalink|videos|reel)\/([^/]+)/)?.[1]||null;}catch{return null;}};
 const storyHref=(href,id)=>{try{let u=new URL(href);return postId(href)===id&&!u.searchParams.has('comment_id')&&!u.searchParams.has('reply_comment_id');}catch{return false;}};
 const MESSAGE='[data-ad-preview="message"],[data-ad-comet-preview="message"]';
 const usableMessage=e=>!e.closest('[aria-hidden="true"]')&&(visible(e)||visible(e.closest('[role="dialog"]')));
 function boundPost(id){
  if(location.protocol!=='https:'||postId(location.href)!==id)return null;
  const matched=[...document.querySelectorAll('[role="article"]')].filter(visible).filter(a=>[...a.querySelectorAll('a[href]')].some(x=>x.closest('[role="article"]')===a&&storyHref(x.href,id)));
  let root=matched.length===1?matched[0]:null,message=root?.querySelector(MESSAGE);
  if(!root){let messages=[...new Set([...document.querySelectorAll('[role="dialog"] [data-ad-preview="message"],[role="dialog"] [data-ad-comet-preview="message"]')].filter(usableMessage))];if(messages.length===1){message=messages[0];root=message.closest('[role="dialog"]');}}
  if(root?.getAttribute('role')==='dialog'){let parent=root.parentElement?.closest('[role="dialog"]');while(parent){let captions=[...new Set([...parent.querySelectorAll(MESSAGE)].filter(usableMessage))],ids=[...parent.querySelectorAll('a[href]')].filter(visible).map(a=>postId(a.href)).filter(Boolean);if(captions.length!==1||captions[0]!==message||ids.some(x=>x!==id))break;root=parent;parent=root.parentElement?.closest('[role="dialog"]');}}
  if(!root&&postId(document.querySelector('meta[property="og:url"]')?.content)===id){let main=document.querySelector('[role="main"]'),messages=main?.querySelectorAll(MESSAGE);if(messages?.length===1){root=main;message=messages[0];}}
  return root?{root,message}:null;
 }
 // Facebook can reorder timestamp glyphs and add invisible decoys. Reconstruct
 // only glyphs painted inside this exact anchor; never parse scrambled innerText.
 function rendered(e){
  if(!visible(e))return '';let chars=[],walker=document.createTreeWalker(e,NodeFilter.SHOW_TEXT),node;
  while((node=walker.nextNode())&&chars.length<400){let hidden=false;for(let p=node.parentElement;p&&p!==e;p=p.parentElement){let s=getComputedStyle(p);if(s.visibility==='hidden'||Number(s.opacity)===0||parseFloat(s.fontSize)===0)hidden=true;}if(hidden)continue;
   for(let offset=0;offset<node.length&&chars.length<400;){let c=String.fromCodePoint(node.textContent.codePointAt(offset)),range=document.createRange();range.setStart(node,offset);range.setEnd(node,offset+c.length);offset+=c.length;if(/[\u034f\u200b-\u200f\u2060-\u206f\ufeff]/u.test(c))continue;let r=range.getBoundingClientRect();if(r.width<=0||r.height<=0)continue;let h=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);if(h&&e.contains(h))chars.push({c,x:r.left,y:r.top,right:r.right});}
  }
  chars.sort((a,b)=>Math.abs(a.y-b.y)>3?a.y-b.y:a.x-b.x);let value='',prev=null;for(let c of chars){if(prev&&(c.y-prev.y>3||c.x-prev.right>3))value+=' ';value+=c.c;prev=c;}return value.normalize('NFC').trim();
 }
 const number='([\\d.,]+(?:\\s*[kKmMbB]|\\s*(?:nghìn|ngàn|triệu|tỷ))?\\+?)';
 const patterns={likes:new RegExp('(?:all reactions|tất cả cảm xúc|lượt thích|likes|thích)\\s*[:：]?\\s*'+number+'(?:\\s+(?:người|people))?|'+number+'\\s+(?:likes|reactions|lượt thích)','i'),comments:new RegExp('(?:comments?|(?:viết\\s+)?bình luận)\\s*[:：]?\\s*'+number+'|'+number+'\\s+(?:comments|bình luận)','i'),shares:new RegExp('(?:shares?|chia sẻ)\\s*[:：]?\\s*'+number+'|'+number+'\\s+(?:shares|lượt chia sẻ)','i'),views:new RegExp('(?:views?|lượt xem)\\s*[:：]?\\s*'+number+'|'+number+'\\s+(?:views|lượt xem)','i')};
 function metricNodes(root,message){
  let story=message?.closest('[role="article"]');return [...root.querySelectorAll('[aria-label],[role="button"],[dir="auto"]')].filter(e=>!e.matches(MESSAGE)&&!e.closest(MESSAGE)&&!e.contains(message)&&(!e.closest('[role="article"]')||e.closest('[role="article"]')===story)).map(e=>{
   let aria=(e.getAttribute('aria-label')||'').trim(),body=text(e),compact=e.getAttribute('role')==='button'&&/^(?:like|thích|comment|bình luận|viết bình luận|share|chia sẻ|views?|lượt xem)$/i.test(aria)&&/^[\d.,]+(?:\s*[kKmMbB]|\s*(?:nghìn|ngàn|triệu|tỷ))?\+?$/.test(body);
   return {e,label:compact?aria+': '+body:aria||body,priority:compact||/^(?:all reactions|tất cả cảm xúc)/i.test(aria)?2:1};
  }).filter(x=>x.label.length<=160&&Object.values(patterns).some(p=>p.test(x.label)));
 }
"""

DETAIL = (
    "(id) => {"
    + DOM_HELPERS
    + r"""
 const bound=boundPost(id);if(!bound)return {url:location.href,excerpt:null,metrics:{}};
 let {root,message}=bound;
 if(!root)return {url:location.href,excerpt:null,metrics:{}};
 if(!message&&root.getAttribute('role')==='article'){const texts=[...root.querySelectorAll('[dir="auto"]')].filter(e=>visible(e)&&e.closest('[role="article"]')===root&&!e.closest('h2,h3,button,[role="button"],[role="textbox"],a')&&!e.querySelector('h2,h3,button,[role="button"],input,textarea')&&text(e).length>=20);const longest=texts.sort((a,b)=>text(b).length-text(a).length)[0];if(longest&&texts.every(e=>text(longest).includes(text(e))))message=longest;}
 const owner=location.pathname.match(/^\/([^/]+)\/posts\//)?.[1];
 const ownerId=location.pathname==='/permalink.php'?new URL(location.href).searchParams.get('id'):null;
 const authorLinks=[...root.querySelectorAll('a[href]')].filter(a=>visible(a)&&!postId(a.href)&&(!message||(message.compareDocumentPosition(a)&Node.DOCUMENT_POSITION_PRECEDING))).filter(a=>{try{let u=new URL(a.href);return ['facebook.com','www.facebook.com','web.facebook.com','m.facebook.com'].includes(u.hostname)&&(/^\/(?:[A-Za-z0-9._-]+|pages\/[^/]+\/\d+|groups\/\d+\/user\/\d+)\/?$/.test(u.pathname))&&!/^\/(?:groups|search|hashtag|watch|reel|photo|home.php)\/?$/.test(u.pathname)&&(u.pathname!=='/profile.php'||/^\d+$/.test(u.searchParams.get('id')||''));}catch{return false}});
 const preferred=owner?authorLinks.filter(a=>new URL(a.href).pathname.replace(/\/$/,'')==='/'+owner):/^\d+$/.test(ownerId||'')?authorLinks.filter(a=>{let u=new URL(a.href);return u.pathname==='/profile.php'&&u.searchParams.get('id')===ownerId;}):authorLinks;
 const authorPaths=[...new Set(preferred.map(a=>new URL(a.href).pathname+((new URL(a.href)).searchParams.get('id')?'?id='+new URL(a.href).searchParams.get('id'):'')))];
 const publisherAnchor=authorPaths.length===1?preferred.find(a=>text(a)&&!a.getAttribute('aria-label'))||preferred[0]:null;
 const publisher=publisherAnchor?(publisherAnchor.getAttribute('aria-label')||text(publisherAnchor)).slice(0,200):null;
 const dates=[...root.querySelectorAll('time[datetime],[data-utime]')].filter(e=>visible(e)&&(root.getAttribute('role')!=='article'||e.closest('[role="article"]')===root));
 let published=null;
 if(dates.length===1){const d=dates[0];const iso=d.getAttribute('datetime');const seconds=d.getAttribute('data-utime');try{if(iso)published=new Date(iso).toISOString();else if(seconds&&/^\d+$/.test(seconds)&&Number(seconds)>0)published=new Date(Number(seconds)*1000).toISOString();}catch{}}
 const header=[...root.querySelectorAll('a[href]')].filter(a=>visible(a)&&(!message||(message.compareDocumentPosition(a)&Node.DOCUMENT_POSITION_PRECEDING))&&(!a.closest('[role="article"]')||a.closest('[role="article"]')===message?.closest('[role="article"]')));
 const dateLabel=a=>(a?.getAttribute('aria-label')||a?.getAttribute('title')||rendered(a)).trim();
 const dated=label=>/^(?:(?:Thứ\s+(?:[2-7]|Hai|Ba|Tư|Năm|Sáu|Bảy)|Chủ\s+nhật),?\s+)?(?:\d{1,2}\s+tháng\s+\d{1,2}(?:\s*,?\s*\d{4})?(?:\s+lúc\s+\d{1,2}:\d{2})?|\d+\s+(?:phút|giờ|ngày|tuần|tháng|năm)(?:\s+trước)?|Vừa\s+xong|Hôm\s+qua(?:\s+lúc\s+\d{1,2}:\d{2})?|\d{4}-\d{2}-\d{2})$/iu.test(label);
 const directTimestamp=header.find(a=>storyHref(a.href,id)&&dated(dateLabel(a)));
 const placeholderDates=header.filter(a=>{try{let u=new URL(a.href);return !postId(a.href)&&u.origin===location.origin&&u.pathname==='/permalink.php'&&/^(?:\d{1,2}\s+tháng\s+\d{1,2}(?:\s*,?\s*\d{4})?|\d{4}-\d{2}-\d{2})$/u.test(rendered(a));}catch{return false}});
 const timestamp=directTimestamp||(placeholderDates.length===1?placeholderDates[0]:null);
 const metrics={};
 const labels=metricNodes(root,message).filter(x=>visible(x.e));
 for(const [key,pattern] of Object.entries(patterns)){let matches=labels.map(l=>({match:l.label.match(pattern),priority:l.priority})).filter(x=>x.match),priority=Math.max(0,...matches.map(x=>x.priority)),found=matches.filter(x=>x.priority===priority).map(x=>x.match),counts=[...new Set(found.map(m=>(m[1]||m[2]).replace(/\s+/g,'').toLowerCase()))];if(counts.length===1)metrics[key]=found[0][0];}
 return {url:location.href,excerpt:(visible(message)?text(message):'').slice(0,8192)||null,scope:root.querySelector('video')?'CAPTION_ONLY':'VISIBLE_TEXT',publisher,publisher_url:publisherAnchor?.href||null,published_at:published,published_label:dateLabel(timestamp).slice(0,200)||null,metrics};
}"""
)

METRIC_SCROLL = (
    "(id) => {"
    + DOM_HELPERS
    + r"""
 const bound=boundPost(id);if(!bound)return false;
 const target=metricNodes(bound.root,bound.message)[0]?.e;
 if(!target)return false;target.scrollIntoView({block:'center'});return true;
}"""
)


def read_detail(page: Any, url: str, guard: Callable[[], None]) -> dict[str, Any]:
    """Wait finitely for one post, preserving facts before a metrics-only scroll."""
    expected = post_link(url)
    if expected is None:
        raise ValueError("Exact post required")
    best: dict[str, Any] = {"url": expected.url, "metrics": {}}

    def read() -> None:
        guard()
        actual = post_link(page.url)
        if actual is None or actual.external_id != expected.external_id:
            return
        raw = page.evaluate(DETAIL, expected.external_id)
        if not isinstance(raw, dict):
            return
        observed = post_link(raw.get("url"))
        if observed is None or observed.external_id != expected.external_id:
            return
        for key in (
            "excerpt",
            "publisher",
            "publisher_url",
            "published_at",
            "published_label",
            "scope",
        ):
            if raw.get(key) is not None:
                best[key] = raw[key]
        for key, value in raw.get("metrics", {}).items():
            if value is not None:
                best["metrics"][key] = value

    for attempt in range(8):
        read()
        if best.get("excerpt"):
            break
        if attempt < 7:
            page.wait_for_timeout(500)
    if best.get("excerpt"):
        # Reaction counters can arrive after the caption. One successful scroll,
        # at most four short readiness checks; no additional detail opening.
        for attempt in range(4):
            page.wait_for_timeout(300)
            guard()
            actual = post_link(page.url)
            if actual is None or actual.external_id != expected.external_id:
                break
            scrolled = page.evaluate(METRIC_SCROLL, expected.external_id)
            if scrolled:
                page.wait_for_timeout(400)
            read()
            if scrolled:
                break
    return best


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

    def viral_target_met() -> bool:
        return (
            settings["mode"] == "VIRAL"
            and research_counts(list(found.values()))["viral"] >= VIRAL_TARGET
        )

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
            previous = found[link.external_id]
            merged = {
                key: item[key] if item[key] is not None else previous[key]
                for key in (
                    "url",
                    "excerpt",
                    "publisher",
                    "publisher_url",
                    "published_at",
                    "published_label",
                )
            }
            merged["scope"] = item["scope"] if item["excerpt"] else previous["scope"]
            merged["metrics"] = {
                key: (item["metrics"][key] or previous["metrics"][key])["raw"]
                if item["metrics"][key] is not None
                or previous["metrics"][key] is not None
                else None
                for key in item["metrics"]
            }
            found[link.external_id] = candidate(
                merged, previous["query"], settings, previous["observed_at"]
            )
        elif len(found) < settings["max_candidates"]:
            found[link.external_id] = item

    for query_record in queue:
        if (
            len(executed) >= settings["max_queries"]
            or len(found) >= settings["max_candidates"]
            or viral_target_met()
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
        for turn in range(12 if settings["mode"] == "VIRAL" else 4):
            guard()
            before = len(found)
            for raw in page.evaluate(EXTRACT):
                remember(raw, query)
                if viral_target_met():
                    break
            # Details use exact canonical posts already found, bounded globally.
            for item in list(found.values()):
                if (
                    item["external_id"] in opened
                    or details_used >= settings["max_details"]
                    or viral_target_met()
                ):
                    continue
                if settings["mode"] == "VIRAL":
                    assessment = item["assessment"]
                    # Reuse already sufficient metadata; skip known off-topic
                    # posts. Missing caption/counters still need exact details.
                    if (
                        assessment["topical"] is False
                        or assessment["time_match"] is False
                        or (
                            item["publisher"]
                            and item["excerpt"]
                            and assessment["viral"] is not None
                            and assessment["time_match"] is not None
                        )
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
                    raw = read_detail(page, actual.url, guard)
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
                and not viral_target_met()
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
                    try:
                        element.click(timeout=5000)
                    except Exception as error:
                        # Keep the optional browser SDK out of module imports,
                        # as in the existing worker. Only its timeout is skipped.
                        from playwright.sync_api import (
                            TimeoutError as BrowserTimeoutError,
                        )

                        if not isinstance(error, BrowserTimeoutError):
                            raise
                        # An unreadable placeholder is not a failed search. It
                        # consumed a detail opening and must not be retried or
                        # turned into a fabricated post/caption.
                        guard()
                        if page.url != previous:
                            page.go_back(wait_until="domcontentloaded", timeout=15000)
                            guard()
                        break
                    page.wait_for_timeout(700)
                    guard()
                    actual = post_link(page.url)
                    if actual is not None:
                        opened.add(actual.external_id)
                        raw = read_detail(page, actual.url, guard)
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
                    **research_counts(list(found.values())),
                }
            )
            if len(found) >= settings["max_candidates"] or viral_target_met():
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
